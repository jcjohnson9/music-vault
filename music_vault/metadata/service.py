from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .schema import (
    EDITABLE_METADATA_FIELDS,
    MATERIALIZED_COLUMNS,
    OBSERVATION_FIELDS,
    normalize_release_date,
    observation_key,
    release_year,
    utc_now,
)


PROVENANCE_PRIORITY = {
    "unknown": 0,
    "filename": 5,
    "youtube": 10,
    "youtube_thumbnail": 10,
    "embedded": 40,
    "musicbrainz": 60,
    "cover_art_archive": 60,
    "musicbrainz_high_confidence": 70,
    "cover_art_archive_high_confidence": 70,
    "provider_confirmed": 80,
    "musicbrainz_confirmed": 90,
    "manual": 100,
}


@dataclass(frozen=True)
class MetadataFieldState:
    field_name: str
    value: str | None
    provenance: str
    provider_reference: str | None
    confidence: float | None
    is_manual: bool
    is_locked: bool
    updated_at: str


@dataclass(frozen=True)
class MetadataObservation:
    id: int
    track_id: int
    provider: str
    field_name: str
    value: str | None
    provider_reference: str | None
    confidence: float | None
    observed_at: str


@dataclass(frozen=True)
class EffectiveMetadataSnapshot:
    track_id: int
    path: str
    source_kind: str | None
    source_video_id: str | None
    source_upload_date: str | None
    musicbrainz_recording_id: str | None
    musicbrainz_release_id: str | None
    metadata_updated_at: str | None
    fields: Mapping[str, MetadataFieldState]

    def value(self, field_name: str) -> str | None:
        state = self.fields.get(field_name)
        return state.value if state is not None else None


@dataclass(frozen=True)
class ApprovedMetadataSnapshot:
    track_id: int
    path: str
    title: str
    artist: str | None
    album: str | None
    album_artist: str | None
    release_date: str | None
    artwork: str | None
    provenance: Mapping[str, str]
    locked_fields: frozenset[str]


@dataclass(frozen=True)
class MetadataAction:
    action: str
    value: str | None = None

    @classmethod
    def set(cls, value: object) -> "MetadataAction":
        return cls("set", str(value).strip())

    @classmethod
    def clear(cls) -> "MetadataAction":
        return cls("clear", None)

    @classmethod
    def unlock(cls) -> "MetadataAction":
        return cls("unlock", None)

    @classmethod
    def lock(cls) -> "MetadataAction":
        return cls("lock", None)

    @classmethod
    def reset(cls) -> "MetadataAction":
        return cls("reset", None)


@dataclass(frozen=True)
class MetadataChangeResult:
    track_id: int
    change_group_id: str | None
    changed_fields: frozenset[str]
    before: EffectiveMetadataSnapshot
    after: EffectiveMetadataSnapshot

    @property
    def changed(self) -> bool:
        return bool(self.changed_fields)


@dataclass(frozen=True)
class MetadataHistoryEntry:
    field_name: str
    old_value: str | None
    new_value: str | None
    old_provenance: str | None
    new_provenance: str | None
    old_provider_reference: str | None
    new_provider_reference: str | None
    old_confidence: float | None
    new_confidence: float | None
    old_is_manual: bool
    new_is_manual: bool
    old_is_locked: bool
    new_is_locked: bool


@dataclass(frozen=True)
class MetadataHistoryGroup:
    change_group_id: str
    actor: str
    reason: str
    changed_at: str
    entries: tuple[MetadataHistoryEntry, ...]


def _clean_optional(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _provider_provenance(provider: str, field_name: str) -> str:
    normalized = str(provider or "unknown").strip().casefold()
    if normalized == "confirmed_provider":
        return "provider_confirmed"
    if field_name == "artwork" and normalized == "youtube":
        return "youtube_thumbnail"
    if normalized in PROVENANCE_PRIORITY:
        return normalized
    return "unknown"


def _priority(state: MetadataFieldState) -> int:
    if state.is_manual and not state.is_locked:
        return 0
    return PROVENANCE_PRIORITY.get(state.provenance, 0)


class MetadataService:
    """Transactional authority for effective metadata, observations, and history."""

    def __init__(self, database: Any) -> None:
        self.conn: sqlite3.Connection = getattr(database, "conn", database)
        if not isinstance(self.conn, sqlite3.Connection):
            raise TypeError("MetadataService requires a SQLite connection or MusicVaultDB.")
        self.conn.row_factory = sqlite3.Row

    @contextmanager
    def _transaction(self, *, commit: bool = True):
        """Own a transaction without committing an existing caller transaction."""

        if not commit:
            yield
            return
        if not self.conn.in_transaction:
            with self.conn:
                yield
            return

        savepoint = f"metadata_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            self.conn.execute(f"RELEASE SAVEPOINT {savepoint}")

    @staticmethod
    def _validate_field(field_name: str) -> str:
        name = str(field_name).strip()
        if name not in EDITABLE_METADATA_FIELDS:
            raise ValueError(f"Unsupported metadata field: {name}")
        return name

    @staticmethod
    def _normalized_value(field_name: str, value: object) -> str | None:
        if field_name == "release_date":
            return normalize_release_date(value)
        return _clean_optional(value)

    def _track(self, track_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM tracks WHERE id=?", (int(track_id),)).fetchone()
        if row is None:
            raise KeyError(f"Track {track_id} does not exist.")
        return row

    @staticmethod
    def _state_from_row(row: sqlite3.Row) -> MetadataFieldState:
        return MetadataFieldState(
            field_name=str(row["field_name"]),
            value=row["value"],
            provenance=str(row["provenance"] or "unknown"),
            provider_reference=row["provider_reference"],
            confidence=(float(row["confidence"]) if row["confidence"] is not None else None),
            is_manual=bool(row["is_manual"]),
            is_locked=bool(row["is_locked"]),
            updated_at=str(row["updated_at"]),
        )

    def _state(self, track_id: int, field_name: str, track: sqlite3.Row) -> MetadataFieldState:
        row = self.conn.execute(
            "SELECT * FROM track_metadata_fields WHERE track_id=? AND field_name=?",
            (int(track_id), field_name),
        ).fetchone()
        if row is not None:
            return self._state_from_row(row)
        column = MATERIALIZED_COLUMNS[field_name]
        return MetadataFieldState(
            field_name=field_name,
            value=track[column],
            provenance="unknown",
            provider_reference=None,
            confidence=None,
            is_manual=False,
            is_locked=False,
            updated_at=str(track["updated_at"] or utc_now()),
        )

    def snapshot(self, track_id: int) -> EffectiveMetadataSnapshot:
        track = self._track(track_id)
        rows = self.conn.execute(
            "SELECT * FROM track_metadata_fields WHERE track_id=?",
            (int(track_id),),
        ).fetchall()
        states = {str(row["field_name"]): self._state_from_row(row) for row in rows}
        for field_name in EDITABLE_METADATA_FIELDS:
            if field_name not in states:
                states[field_name] = self._state(track_id, field_name, track)
        return EffectiveMetadataSnapshot(
            track_id=int(track["id"]),
            path=str(track["path"]),
            source_kind=track["source_kind"],
            source_video_id=track["source_video_id"],
            source_upload_date=track["source_upload_date"],
            musicbrainz_recording_id=track["musicbrainz_recording_id"],
            musicbrainz_release_id=track["musicbrainz_release_id"],
            metadata_updated_at=track["metadata_updated_at"],
            fields=MappingProxyType(states),
        )

    @staticmethod
    def _matches_remediation_snapshot(
        current: EffectiveMetadataSnapshot,
        expected: Mapping[str, object],
    ) -> bool:
        """Compare the complete effective metadata state used by remediation."""

        required_keys = {
            "track_id",
            "path",
            "source_kind",
            "source_video_id",
            "source_upload_date",
            "musicbrainz_recording_id",
            "musicbrainz_release_id",
            "metadata_updated_at",
            "fields",
        }
        if not required_keys.issubset(expected):
            return False
        try:
            expected_track_id = int(expected["track_id"])
        except (TypeError, ValueError):
            return False
        if (
            current.track_id != expected_track_id
            or current.path != expected.get("path")
            or current.source_kind != expected.get("source_kind")
            or current.source_video_id != expected.get("source_video_id")
            or current.source_upload_date != expected.get("source_upload_date")
            or current.musicbrainz_recording_id
            != expected.get("musicbrainz_recording_id")
            or current.musicbrainz_release_id != expected.get("musicbrainz_release_id")
            or current.metadata_updated_at != expected.get("metadata_updated_at")
        ):
            return False

        raw_fields = expected.get("fields")
        if not isinstance(raw_fields, Mapping) or set(raw_fields) != set(current.fields):
            return False
        required_field_keys = {
            "value",
            "provenance",
            "provider_reference",
            "confidence",
            "is_manual",
            "is_locked",
        }
        for field_name, state in current.fields.items():
            raw_state = raw_fields.get(field_name)
            if (
                not isinstance(raw_state, Mapping)
                or not required_field_keys.issubset(raw_state)
                or state.value != raw_state.get("value")
                or state.provenance != raw_state.get("provenance")
                or state.provider_reference != raw_state.get("provider_reference")
                or state.confidence != raw_state.get("confidence")
                or state.is_manual != raw_state.get("is_manual")
                or state.is_locked != raw_state.get("is_locked")
            ):
                return False
        return True

    def ensure_field_states(self, track_id: int, *, commit: bool = True) -> None:
        """Persist one effective-state row for every editable metadata field."""

        track = self._track(track_id)
        timestamp = str(
            track["metadata_updated_at"] or track["updated_at"] or utc_now()
        )
        with self._transaction(commit=commit):
            for field_name in EDITABLE_METADATA_FIELDS:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO track_metadata_fields (
                        track_id, field_name, value, provenance, provider_reference,
                        confidence, is_manual, is_locked, updated_at
                    ) VALUES (?, ?, ?, 'unknown', NULL, NULL, 0, 0, ?)
                    """,
                    (
                        int(track_id),
                        field_name,
                        track[MATERIALIZED_COLUMNS[field_name]],
                        timestamp,
                    ),
                )

    def observations(
        self,
        track_id: int,
        field_name: str | None = None,
    ) -> tuple[MetadataObservation, ...]:
        parameters: list[object] = [int(track_id)]
        where = "track_id=?"
        if field_name is not None:
            if field_name not in OBSERVATION_FIELDS:
                raise ValueError(f"Unsupported observation field: {field_name}")
            where += " AND field_name=?"
            parameters.append(field_name)
        rows = self.conn.execute(
            f"""
            SELECT * FROM track_metadata_observations
            WHERE {where}
            ORDER BY observed_at DESC, id DESC
            """,
            parameters,
        ).fetchall()
        return tuple(
            MetadataObservation(
                id=int(row["id"]),
                track_id=int(row["track_id"]),
                provider=str(row["provider"]),
                field_name=str(row["field_name"]),
                value=row["value"],
                provider_reference=row["provider_reference"],
                confidence=(float(row["confidence"]) if row["confidence"] is not None else None),
                observed_at=str(row["observed_at"]),
            )
            for row in rows
        )

    def _best_automatic_row(self, track_id: int, field_name: str) -> sqlite3.Row | None:
        rows = self.conn.execute(
            """
            SELECT * FROM track_metadata_observations
            WHERE track_id=? AND field_name=? AND value IS NOT NULL AND TRIM(value) != ''
              AND provider NOT IN ('manual', 'musicbrainz_candidate')
            ORDER BY observed_at DESC, id DESC
            """,
            (int(track_id), field_name),
        ).fetchall()
        if field_name == "release_date":
            valid_rows: list[sqlite3.Row] = []
            for row in rows:
                try:
                    normalize_release_date(row["value"])
                except ValueError:
                    continue
                valid_rows.append(row)
            rows = valid_rows
        if not rows:
            return None
        return max(
            rows,
            key=lambda row: (
                PROVENANCE_PRIORITY.get(_provider_provenance(row["provider"], field_name), 0),
                str(row["observed_at"]),
                int(row["id"]),
            ),
        )

    def best_automatic_value(
        self,
        track_id: int,
        field_name: str,
    ) -> MetadataObservation | None:
        name = self._validate_field(field_name)
        row = self._best_automatic_row(track_id, name)
        if row is None:
            return None
        return MetadataObservation(
            id=int(row["id"]),
            track_id=int(row["track_id"]),
            provider=str(row["provider"]),
            field_name=str(row["field_name"]),
            value=row["value"],
            provider_reference=row["provider_reference"],
            confidence=(float(row["confidence"]) if row["confidence"] is not None else None),
            observed_at=str(row["observed_at"]),
        )

    @staticmethod
    def _same_state(left: MetadataFieldState, right: MetadataFieldState) -> bool:
        return (
            left.value,
            left.provenance,
            left.provider_reference,
            left.confidence,
            left.is_manual,
            left.is_locked,
        ) == (
            right.value,
            right.provenance,
            right.provider_reference,
            right.confidence,
            right.is_manual,
            right.is_locked,
        )

    def _write_observation(
        self,
        *,
        track_id: int,
        provider: str,
        field_name: str,
        value: str | None,
        provider_reference: str | None,
        confidence: float | None,
        observed_at: str,
    ) -> None:
        key = observation_key(track_id, provider, field_name, value, provider_reference)
        self.conn.execute(
            """
            INSERT INTO track_metadata_observations (
                observation_key, track_id, provider, field_name, value,
                provider_reference, confidence, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(observation_key) DO UPDATE SET
                confidence=COALESCE(excluded.confidence, track_metadata_observations.confidence),
                observed_at=excluded.observed_at
            """,
            (
                key,
                int(track_id),
                provider,
                field_name,
                value,
                provider_reference,
                confidence,
                observed_at,
            ),
        )

    def _write_state(self, track_id: int, state: MetadataFieldState) -> None:
        self.conn.execute(
            """
            INSERT INTO track_metadata_fields (
                track_id, field_name, value, provenance, provider_reference,
                confidence, is_manual, is_locked, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id, field_name) DO UPDATE SET
                value=excluded.value,
                provenance=excluded.provenance,
                provider_reference=excluded.provider_reference,
                confidence=excluded.confidence,
                is_manual=excluded.is_manual,
                is_locked=excluded.is_locked,
                updated_at=excluded.updated_at
            """,
            (
                int(track_id),
                state.field_name,
                state.value,
                state.provenance,
                state.provider_reference,
                state.confidence,
                int(state.is_manual),
                int(state.is_locked),
                state.updated_at,
            ),
        )

    def _write_history(
        self,
        *,
        track_id: int,
        group_id: str,
        old: MetadataFieldState,
        new: MetadataFieldState,
        actor: str,
        reason: str,
        changed_at: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO track_metadata_history (
                change_group_id, track_id, field_name, old_value, new_value,
                old_provenance, new_provenance,
                old_provider_reference, new_provider_reference,
                old_confidence, new_confidence,
                old_is_manual, new_is_manual, old_is_locked, new_is_locked,
                actor, reason, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                group_id,
                int(track_id),
                old.field_name,
                old.value,
                new.value,
                old.provenance,
                new.provenance,
                old.provider_reference,
                new.provider_reference,
                old.confidence,
                new.confidence,
                int(old.is_manual),
                int(new.is_manual),
                int(old.is_locked),
                int(new.is_locked),
                actor,
                reason,
                changed_at,
            ),
        )

    def _materialize(
        self,
        track_id: int,
        states: Mapping[str, MetadataFieldState],
        changed_at: str,
    ) -> None:
        assignments: dict[str, object] = {}
        for field_name, state in states.items():
            assignments[MATERIALIZED_COLUMNS[field_name]] = state.value
            if field_name == "release_date":
                assignments["year"] = release_year(state.value)
        assignments["metadata_updated_at"] = changed_at
        assignments["updated_at"] = changed_at
        set_clause = ", ".join(f"{column}=?" for column in assignments)
        self.conn.execute(
            f"UPDATE tracks SET {set_clause} WHERE id=?",
            [*assignments.values(), int(track_id)],
        )

    def _commit_changes(
        self,
        *,
        track_id: int,
        changes: Mapping[str, tuple[MetadataFieldState, MetadataFieldState]],
        actor: str,
        reason: str,
        group_id: str | None = None,
    ) -> str | None:
        if not changes:
            return None
        changed_at = utc_now()
        identifier = group_id or str(uuid.uuid4())
        new_states: dict[str, MetadataFieldState] = {}
        for field_name, (old, new) in changes.items():
            stamped = MetadataFieldState(
                field_name=new.field_name,
                value=new.value,
                provenance=new.provenance,
                provider_reference=new.provider_reference,
                confidence=new.confidence,
                is_manual=new.is_manual,
                is_locked=new.is_locked,
                updated_at=changed_at,
            )
            self._write_state(track_id, stamped)
            self._write_history(
                track_id=track_id,
                group_id=identifier,
                old=old,
                new=stamped,
                actor=actor,
                reason=reason,
                changed_at=changed_at,
            )
            new_states[field_name] = stamped
        self._materialize(track_id, new_states, changed_at)
        return identifier

    def record_source_observations(
        self,
        track_id: int,
        *,
        provider: str,
        values: Mapping[str, object],
        provider_reference: str | Mapping[str, str | None] | None = None,
        confidence: float | Mapping[str, float | None] | None = None,
        apply_effective: bool = True,
        actor: str = "importer",
        reason: str = "source_observation",
        change_group_id: str | None = None,
        commit: bool = True,
    ) -> MetadataChangeResult:
        before = self.snapshot(track_id)
        track = self._track(track_id)
        provider_name = str(provider or "unknown").strip().casefold() or "unknown"
        observed_at = utc_now()
        pending: dict[str, tuple[MetadataFieldState, MetadataFieldState]] = {}
        with self._transaction(commit=commit):
            for raw_name, raw_value in values.items():
                field_name = str(raw_name).strip()
                if field_name not in OBSERVATION_FIELDS:
                    raise ValueError(f"Unsupported observation field: {field_name}")
                if field_name == "release_date" and provider_name.startswith("youtube"):
                    continue

                observation_value = _clean_optional(raw_value)
                effective_value = observation_value
                if field_name == "release_date" and observation_value is not None:
                    try:
                        effective_value = normalize_release_date(observation_value)
                    except ValueError:
                        effective_value = None
                    else:
                        observation_value = effective_value
                elif field_name in EDITABLE_METADATA_FIELDS:
                    effective_value = self._normalized_value(field_name, raw_value)
                    observation_value = effective_value
                if observation_value is None:
                    continue

                reference = _clean_optional(
                    provider_reference.get(field_name)
                    if isinstance(provider_reference, Mapping)
                    else provider_reference
                )
                raw_score = (
                    confidence.get(field_name)
                    if isinstance(confidence, Mapping)
                    else confidence
                )
                score = float(raw_score) if raw_score is not None else None
                old = (
                    self._state(track_id, field_name, track)
                    if field_name in EDITABLE_METADATA_FIELDS
                    else None
                )
                provenance = _provider_provenance(provider_name, field_name)
                if (
                    old is not None
                    and old.value == effective_value
                    and old.provenance == provenance
                ):
                    if reference is None:
                        reference = old.provider_reference
                    if score is None:
                        score = old.confidence
                self._write_observation(
                    track_id=track_id,
                    provider=provider_name,
                    field_name=field_name,
                    value=observation_value,
                    provider_reference=reference,
                    confidence=score,
                    observed_at=observed_at,
                )
                if (
                    not apply_effective
                    or old is None
                    or effective_value is None
                ):
                    continue
                if old.is_locked:
                    continue
                incoming_priority = PROVENANCE_PRIORITY.get(provenance, 0)
                if old.value is not None and incoming_priority < _priority(old):
                    continue
                new = MetadataFieldState(
                    field_name=field_name,
                    value=effective_value,
                    provenance=provenance,
                    provider_reference=reference,
                    confidence=score,
                    is_manual=False,
                    is_locked=False,
                    updated_at=old.updated_at,
                )
                if not self._same_state(old, new):
                    pending[field_name] = (old, new)
            group_id = self._commit_changes(
                track_id=track_id,
                changes=pending,
                actor=actor,
                reason=reason,
                group_id=change_group_id,
            )
        after = self.snapshot(track_id)
        return MetadataChangeResult(
            track_id=int(track_id),
            change_group_id=group_id,
            changed_fields=frozenset(pending),
            before=before,
            after=after,
        )

    def apply_actions(
        self,
        track_id: int,
        actions: Mapping[str, MetadataAction],
        *,
        actor: str = "user",
        reason: str = "manual_edit",
        commit: bool = True,
    ) -> MetadataChangeResult:
        before = self.snapshot(track_id)
        track = self._track(track_id)
        pending: dict[str, tuple[MetadataFieldState, MetadataFieldState]] = {}
        with self._transaction(commit=commit):
            for raw_name, command in actions.items():
                field_name = self._validate_field(raw_name)
                if not isinstance(command, MetadataAction):
                    raise TypeError("Metadata actions must use MetadataAction values.")
                old = self._state(track_id, field_name, track)
                if command.action == "set":
                    value = self._normalized_value(field_name, command.value)
                    if field_name == "title" and value is None:
                        raise ValueError("Title cannot be empty.")
                    new = MetadataFieldState(
                        field_name, value, "manual", None, None, True, True, old.updated_at
                    )
                elif command.action == "clear":
                    if field_name == "title":
                        raise ValueError("Title cannot be cleared.")
                    new = MetadataFieldState(
                        field_name, None, "manual", None, None, True, True, old.updated_at
                    )
                elif command.action == "unlock":
                    new = MetadataFieldState(
                        field_name,
                        old.value,
                        old.provenance,
                        old.provider_reference,
                        old.confidence,
                        old.is_manual,
                        False,
                        old.updated_at,
                    )
                elif command.action == "lock":
                    new = MetadataFieldState(
                        field_name,
                        old.value,
                        old.provenance,
                        old.provider_reference,
                        old.confidence,
                        old.is_manual,
                        True,
                        old.updated_at,
                    )
                elif command.action == "reset":
                    row = self._best_automatic_row(track_id, field_name)
                    if row is None:
                        reset_value = old.value if field_name == "title" else None
                        new = MetadataFieldState(
                            field_name,
                            reset_value,
                            "unknown",
                            None,
                            None,
                            False,
                            False,
                            old.updated_at,
                        )
                    else:
                        new = MetadataFieldState(
                            field_name,
                            self._normalized_value(field_name, row["value"]),
                            _provider_provenance(row["provider"], field_name),
                            row["provider_reference"],
                            (float(row["confidence"]) if row["confidence"] is not None else None),
                            False,
                            False,
                            old.updated_at,
                        )
                else:
                    raise ValueError(f"Unsupported metadata action: {command.action}")
                if not self._same_state(old, new):
                    pending[field_name] = (old, new)
            group_id = self._commit_changes(
                track_id=track_id,
                changes=pending,
                actor=actor,
                reason=reason,
            )
        after = self.snapshot(track_id)
        return MetadataChangeResult(
            int(track_id), group_id, frozenset(pending), before, after
        )

    def apply_manual_patch(
        self,
        track_id: int,
        values: Mapping[str, object],
        *,
        actor: str = "user",
        reason: str = "manual_edit",
        commit: bool = True,
    ) -> MetadataChangeResult:
        actions = {
            name: MetadataAction.clear() if value is None else MetadataAction.set(value)
            for name, value in values.items()
        }
        return self.apply_actions(
            track_id,
            actions,
            actor=actor,
            reason=reason,
            commit=commit,
        )

    def apply_approved_metadata_patch(
        self,
        track_id: int,
        values: Mapping[str, object],
        *,
        provenance: str = "provider_confirmed",
        provider_reference: str | Mapping[str, str | None] | None = None,
        confidence: float | None = None,
        actor: str = "user",
        reason: str = "approved_provider",
        commit: bool = True,
    ) -> MetadataChangeResult:
        if provenance not in {"provider_confirmed", "musicbrainz_confirmed"}:
            raise ValueError("Approved metadata requires a confirmed provenance.")
        before = self.snapshot(track_id)
        track = self._track(track_id)
        observed_at = utc_now()
        pending: dict[str, tuple[MetadataFieldState, MetadataFieldState]] = {}
        with self._transaction(commit=commit):
            for raw_name, raw_value in values.items():
                field_name = self._validate_field(raw_name)
                value = self._normalized_value(field_name, raw_value)
                if value is None:
                    continue
                reference = (
                    provider_reference.get(field_name)
                    if isinstance(provider_reference, Mapping)
                    else provider_reference
                )
                provider = "musicbrainz" if provenance == "musicbrainz_confirmed" else "confirmed_provider"
                self._write_observation(
                    track_id=track_id,
                    provider=provider,
                    field_name=field_name,
                    value=value,
                    provider_reference=_clean_optional(reference),
                    confidence=confidence,
                    observed_at=observed_at,
                )
                old = self._state(track_id, field_name, track)
                new = MetadataFieldState(
                    field_name,
                    value,
                    provenance,
                    _clean_optional(reference),
                    confidence,
                    False,
                    True,
                    old.updated_at,
                )
                if not self._same_state(old, new):
                    pending[field_name] = (old, new)
            group_id = self._commit_changes(
                track_id=track_id,
                changes=pending,
                actor=actor,
                reason=reason,
            )
        after = self.snapshot(track_id)
        return MetadataChangeResult(
            int(track_id), group_id, frozenset(pending), before, after
        )

    def unlock_fields(
        self,
        track_id: int,
        fields: Iterable[str],
        *,
        commit: bool = True,
    ) -> MetadataChangeResult:
        return self.apply_actions(
            track_id,
            {field: MetadataAction.unlock() for field in fields},
            reason="unlock",
            commit=commit,
        )

    def lock_fields(
        self,
        track_id: int,
        fields: Iterable[str],
        *,
        commit: bool = True,
    ) -> MetadataChangeResult:
        return self.apply_actions(
            track_id,
            {field: MetadataAction.lock() for field in fields},
            reason="lock",
            commit=commit,
        )

    def reset_fields(
        self,
        track_id: int,
        fields: Iterable[str],
        *,
        commit: bool = True,
    ) -> MetadataChangeResult:
        return self.apply_actions(
            track_id,
            {field: MetadataAction.reset() for field in fields},
            reason="reset_to_automatic",
            commit=commit,
        )

    def apply_confirmed_candidate(
        self,
        track_id: int,
        values: Mapping[str, object],
        *,
        recording_id: str | None,
        release_id: str | None,
        confidence: float | None,
        artwork_path: str | None = None,
        commit: bool = True,
    ) -> MetadataChangeResult:
        before = self.snapshot(track_id)
        track = self._track(track_id)
        observed_at = utc_now()
        pending: dict[str, tuple[MetadataFieldState, MetadataFieldState]] = {}
        selected = dict(values)
        if artwork_path is not None:
            selected["artwork"] = artwork_path
        with self._transaction(commit=commit):
            for raw_name, raw_value in selected.items():
                field_name = self._validate_field(raw_name)
                value = self._normalized_value(field_name, raw_value)
                if value is None:
                    continue
                reference = (
                    release_id
                    if field_name in {"album", "album_artist", "release_date", "artwork"}
                    else recording_id
                )
                provider = "cover_art_archive" if field_name == "artwork" else "musicbrainz"
                self._write_observation(
                    track_id=track_id,
                    provider=provider,
                    field_name=field_name,
                    value=value,
                    provider_reference=_clean_optional(reference),
                    confidence=confidence,
                    observed_at=observed_at,
                )
                old = self._state(track_id, field_name, track)
                new = MetadataFieldState(
                    field_name=field_name,
                    value=value,
                    provenance=(
                        "cover_art_archive" if field_name == "artwork" else "musicbrainz_confirmed"
                    ),
                    provider_reference=_clean_optional(reference),
                    confidence=confidence,
                    is_manual=False,
                    is_locked=True,
                    updated_at=old.updated_at,
                )
                if not self._same_state(old, new):
                    pending[field_name] = (old, new)
            group_id = self._commit_changes(
                track_id=track_id,
                changes=pending,
                actor="user",
                reason="musicbrainz_confirmed",
            )
            if any(value not in (None, "") for value in selected.values()):
                self.conn.execute(
                    """
                    UPDATE tracks SET
                        musicbrainz_recording_id=COALESCE(?, musicbrainz_recording_id),
                        musicbrainz_release_id=COALESCE(?, musicbrainz_release_id)
                    WHERE id=?
                    """,
                    (_clean_optional(recording_id), _clean_optional(release_id), int(track_id)),
                )
        after = self.snapshot(track_id)
        return MetadataChangeResult(
            int(track_id), group_id, frozenset(pending), before, after
        )

    def apply_high_confidence_candidate(
        self,
        track_id: int,
        values: Mapping[str, object],
        *,
        recording_id: str | None,
        release_id: str | None,
        confidence: float | None,
        artwork_path: str | None = None,
        commit: bool = True,
    ) -> MetadataChangeResult:
        """Apply only unlocked fields from a strict remediation assessment.

        Unlike explicit user confirmation, these changes remain editable and
        unlocked. Manual and confirmed states are never replaced here.
        """

        before = self.snapshot(track_id)
        track = self._track(track_id)
        observed_at = utc_now()
        pending: dict[str, tuple[MetadataFieldState, MetadataFieldState]] = {}
        selected = dict(values)
        if artwork_path is not None:
            selected["artwork"] = artwork_path
        protected_provenance = {
            "manual",
            "musicbrainz_confirmed",
            "provider_confirmed",
        }
        with self._transaction(commit=commit):
            for raw_name, raw_value in selected.items():
                field_name = self._validate_field(raw_name)
                value = self._normalized_value(field_name, raw_value)
                if value is None:
                    continue
                old = self._state(track_id, field_name, track)
                if old.is_locked or old.provenance in protected_provenance:
                    continue
                reference = (
                    release_id
                    if field_name in {"album", "album_artist", "release_date", "artwork"}
                    else recording_id
                )
                provenance = (
                    "cover_art_archive_high_confidence"
                    if field_name == "artwork"
                    else "musicbrainz_high_confidence"
                )
                provider = provenance
                self._write_observation(
                    track_id=track_id,
                    provider=provider,
                    field_name=field_name,
                    value=value,
                    provider_reference=_clean_optional(reference),
                    confidence=confidence,
                    observed_at=observed_at,
                )
                new = MetadataFieldState(
                    field_name=field_name,
                    value=value,
                    provenance=provenance,
                    provider_reference=_clean_optional(reference),
                    confidence=confidence,
                    is_manual=False,
                    is_locked=False,
                    updated_at=old.updated_at,
                )
                if not self._same_state(old, new):
                    pending[field_name] = (old, new)
            group_id = self._commit_changes(
                track_id=track_id,
                changes=pending,
                actor="remediation",
                reason="musicbrainz_high_confidence",
            )
            if pending:
                self.conn.execute(
                    """
                    UPDATE tracks SET
                        musicbrainz_recording_id=COALESCE(?, musicbrainz_recording_id),
                        musicbrainz_release_id=COALESCE(?, musicbrainz_release_id)
                    WHERE id=?
                    """,
                    (_clean_optional(recording_id), _clean_optional(release_id), int(track_id)),
                )
        after = self.snapshot(track_id)
        return MetadataChangeResult(
            int(track_id), group_id, frozenset(pending), before, after
        )

    def restore_remediation_snapshot(
        self,
        track_id: int,
        snapshot: Mapping[str, object],
        *,
        expected_current_snapshot: Mapping[str, object] | None = None,
        actor: str = "remediation_rollback",
        reason: str = "remediation_rollback",
        commit: bool = True,
    ) -> MetadataChangeResult:
        """Restore a private pre-remediation snapshot while preserving history."""

        raw_fields = snapshot.get("fields")
        if not isinstance(raw_fields, Mapping):
            raise ValueError("A remediation snapshot requires field state.")
        pending: dict[str, tuple[MetadataFieldState, MetadataFieldState]] = {}
        with self._transaction(commit=commit):
            before = self.snapshot(track_id)
            if expected_current_snapshot is not None and not self._matches_remediation_snapshot(
                before,
                expected_current_snapshot,
            ):
                raise RuntimeError("metadata_changed_after_remediation")
            track = self._track(track_id)
            for raw_name, raw_state in raw_fields.items():
                field_name = self._validate_field(str(raw_name))
                if not isinstance(raw_state, Mapping):
                    raise ValueError("A remediation field snapshot is invalid.")
                old = self._state(track_id, field_name, track)
                new = MetadataFieldState(
                    field_name=field_name,
                    value=self._normalized_value(field_name, raw_state.get("value")),
                    provenance=_clean_optional(raw_state.get("provenance")) or "unknown",
                    provider_reference=_clean_optional(raw_state.get("provider_reference")),
                    confidence=(
                        float(raw_state["confidence"])
                        if raw_state.get("confidence") is not None
                        else None
                    ),
                    is_manual=bool(raw_state.get("is_manual")),
                    is_locked=bool(raw_state.get("is_locked")),
                    updated_at=old.updated_at,
                )
                if not self._same_state(old, new):
                    pending[field_name] = (old, new)
            group_id = self._commit_changes(
                track_id=track_id,
                changes=pending,
                actor=actor,
                reason=reason,
            )
            self.conn.execute(
                """
                UPDATE tracks SET
                    musicbrainz_recording_id=?,
                    musicbrainz_release_id=?
                WHERE id=?
                """,
                (
                    _clean_optional(snapshot.get("musicbrainz_recording_id")),
                    _clean_optional(snapshot.get("musicbrainz_release_id")),
                    int(track_id),
                ),
            )
            after = self.snapshot(track_id)
        return MetadataChangeResult(
            int(track_id), group_id, frozenset(pending), before, after
        )

    def history_groups(self, track_id: int) -> tuple[MetadataHistoryGroup, ...]:
        rows = self.conn.execute(
            """
            SELECT * FROM track_metadata_history
            WHERE track_id=?
            ORDER BY id DESC
            """,
            (int(track_id),),
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        order: list[str] = []
        for row in rows:
            group_id = str(row["change_group_id"])
            if group_id not in grouped:
                grouped[group_id] = []
                order.append(group_id)
            grouped[group_id].append(row)
        groups: list[MetadataHistoryGroup] = []
        for group_id in order:
            entries = grouped[group_id]
            first = entries[0]
            groups.append(
                MetadataHistoryGroup(
                    change_group_id=group_id,
                    actor=str(first["actor"]),
                    reason=str(first["reason"]),
                    changed_at=str(first["changed_at"]),
                    entries=tuple(
                        MetadataHistoryEntry(
                            field_name=str(row["field_name"]),
                            old_value=row["old_value"],
                            new_value=row["new_value"],
                            old_provenance=row["old_provenance"],
                            new_provenance=row["new_provenance"],
                            old_provider_reference=row["old_provider_reference"],
                            new_provider_reference=row["new_provider_reference"],
                            old_confidence=row["old_confidence"],
                            new_confidence=row["new_confidence"],
                            old_is_manual=bool(row["old_is_manual"]),
                            new_is_manual=bool(row["new_is_manual"]),
                            old_is_locked=bool(row["old_is_locked"]),
                            new_is_locked=bool(row["new_is_locked"]),
                        )
                        for row in reversed(entries)
                    ),
                )
            )
        return tuple(groups)

    def preview_undo(self, track_id: int) -> MetadataHistoryGroup | None:
        groups = self.history_groups(track_id)
        for index, group in enumerate(groups):
            is_oldest_group = index == len(groups) - 1
            is_initial_import = (
                is_oldest_group
                and group.actor == "importer"
                and group.reason
                in {
                    "initial_track_upsert",
                    "initial_embedded_import",
                    "initial_youtube_import",
                }
                and all(
                    entry.old_value is None
                    and entry.old_provenance in {None, "unknown"}
                    and not entry.old_is_manual
                    and not entry.old_is_locked
                    for entry in group.entries
                )
            )
            if not is_initial_import:
                return group
        return None

    def undo_last_change(
        self,
        track_id: int,
        *,
        commit: bool = True,
    ) -> MetadataChangeResult:
        before = self.snapshot(track_id)
        group = self.preview_undo(track_id)
        if group is None:
            return MetadataChangeResult(int(track_id), None, frozenset(), before, before)
        track = self._track(track_id)
        pending: dict[str, tuple[MetadataFieldState, MetadataFieldState]] = {}
        with self._transaction(commit=commit):
            for entry in group.entries:
                old = self._state(track_id, entry.field_name, track)
                restored = MetadataFieldState(
                    field_name=entry.field_name,
                    value=entry.old_value,
                    provenance=entry.old_provenance or "unknown",
                    provider_reference=entry.old_provider_reference,
                    confidence=entry.old_confidence,
                    is_manual=entry.old_is_manual,
                    is_locked=entry.old_is_locked,
                    updated_at=old.updated_at,
                )
                if not self._same_state(old, restored):
                    pending[entry.field_name] = (old, restored)
            group_id = self._commit_changes(
                track_id=track_id,
                changes=pending,
                actor="user",
                reason=f"undo:{group.change_group_id}",
            )
        after = self.snapshot(track_id)
        return MetadataChangeResult(
            int(track_id), group_id, frozenset(pending), before, after
        )

    def approved_snapshot(self, track_id: int) -> ApprovedMetadataSnapshot:
        snapshot = self.snapshot(track_id)
        title = snapshot.value("title")
        if not title:
            title = Path(snapshot.path).stem
        return ApprovedMetadataSnapshot(
            track_id=snapshot.track_id,
            path=snapshot.path,
            title=title,
            artist=snapshot.value("artist"),
            album=snapshot.value("album"),
            album_artist=snapshot.value("album_artist"),
            release_date=snapshot.value("release_date"),
            artwork=snapshot.value("artwork"),
            provenance=MappingProxyType(
                {name: state.provenance for name, state in snapshot.fields.items()}
            ),
            locked_fields=frozenset(
                name for name, state in snapshot.fields.items() if state.is_locked
            ),
        )

    def field_needs_review(self, track_id: int, field_name: str) -> bool:
        name = self._validate_field(field_name)
        state = self.snapshot(track_id).fields[name]
        if name == "title" and not state.value:
            return True
        return state.provenance in {"unknown", "filename", "youtube", "youtube_thumbnail"}

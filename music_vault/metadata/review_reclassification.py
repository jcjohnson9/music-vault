"""Resumable, network-free reclassification of persisted review evidence."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Mapping

from .artist_credits import ArtistCreditInput, ArtistCreditService
from .intelligence_schema import MetadataIntelligenceJobStore
from .matching import normalize_for_comparison
from .review_policy import (
    ReviewDecision,
    ReviewOutcome,
    terminalize_stored_review_evidence,
)
from .schema import EDITABLE_METADATA_FIELDS, MATERIALIZED_COLUMNS
from .service import AutomaticMetadataField, MetadataService
from .soundtrack import is_various_artists
from .title_parser import STRONG_TITLE_PATTERNS
from .uploader_classifier import classify_uploader


@dataclass(frozen=True)
class ReviewReclassificationReport:
    scanned: int
    changed: int
    applied: int
    applied_with_gaps: int
    source_fallback: int
    needs_review: int
    safe_fields_applied: int
    last_item_id: int
    remaining: int
    dry_run: bool
    outcome_counts: Mapping[str, int]
    proposed_fields: int = 0
    reversed_orientation_repairs: int = 0
    album_fields_applied: int = 0
    terminalized_review_items: int = 0
    operational_failures: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "scanned": self.scanned,
            "changed": self.changed,
            "applied": self.applied,
            "applied_with_gaps": self.applied_with_gaps,
            "source_fallback": self.source_fallback,
            "needs_review": self.needs_review,
            "safe_fields_applied": self.safe_fields_applied,
            "last_item_id": self.last_item_id,
            "remaining": self.remaining,
            "dry_run": self.dry_run,
            "outcome_counts": dict(self.outcome_counts),
            "proposed_fields": self.proposed_fields,
            "reversed_orientation_repairs": self.reversed_orientation_repairs,
            "album_fields_applied": self.album_fields_applied,
            "terminalized_review_items": self.terminalized_review_items,
            "operational_failures": self.operational_failures,
        }


def _mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


class MetadataReviewReclassifier:
    """Re-evaluate saved review rows without constructing any provider client."""

    def __init__(self, database: object) -> None:
        self.database = database
        self.conn: sqlite3.Connection = getattr(database, "conn", database)
        if not isinstance(self.conn, sqlite3.Connection):
            raise TypeError("MetadataReviewReclassifier requires a SQLite database.")
        self.conn.row_factory = sqlite3.Row

    @contextmanager
    def _transaction(self):
        if not self.conn.in_transaction:
            with self.conn:
                yield
            return
        name = f"review_reclassification_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self.conn.execute(f"RELEASE SAVEPOINT {name}")

    @staticmethod
    def decision_for_row(row: sqlite3.Row) -> ReviewDecision:
        return terminalize_stored_review_evidence(
            parsed_hints=row["parsed_hints"],
            field_proposal=row["field_proposal"],
            field_confidence=row["field_confidence"],
            provider_agreement=row["provider_agreement"],
            review_reason=row["review_reason"],
        )

    def _safe_automatic_fields(
        self,
        row: sqlite3.Row,
        decision: ReviewDecision,
        effective_states: Mapping[str, tuple[object, bool, bool]],
    ) -> dict[str, AutomaticMetadataField]:
        if decision.outcome in {ReviewOutcome.FAILED, ReviewOutcome.SKIPPED}:
            return {}
        proposal = _mapping(row["field_proposal"])
        confidence = _mapping(row["field_confidence"])
        discogs = _mapping(proposal.get("_discogs"))
        musicbrainz = _mapping(proposal.get("_musicbrainz"))
        hints = _mapping(row["parsed_hints"])
        current_evidence = _mapping(proposal.get("_current"))
        resolved_sources = _mapping(proposal.get("_sources"))
        raw_reasons = _mapping(proposal.get("_reasons"))

        def numeric_duration(value: object) -> float | None:
            try:
                duration = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return duration if duration > 0 else None

        local_duration = numeric_duration(current_evidence.get("duration_seconds"))
        if local_duration is None:
            track_duration = self.conn.execute(
                "SELECT duration_seconds FROM tracks WHERE id=?",
                (int(row["track_id"]),),
            ).fetchone()
            if track_duration is not None:
                local_duration = numeric_duration(track_duration[0])

        decision_conflicts = {
            str(value).strip().casefold() for value in decision.critical_conflicts
        }
        reason_markers = {
            str(marker).strip().casefold()
            for markers in raw_reasons.values()
            for marker in (
                (markers,)
                if isinstance(markers, str)
                else markers
                if isinstance(markers, (list, tuple, set, frozenset))
                else ()
            )
        }
        hard_duration_reason = bool(
            decision_conflicts
            & {"duration", "duration_conflict", "incompatible_duration"}
            or "duration_mismatch" in reason_markers
        )
        hard_identity_reason = bool(
            decision_conflicts
            & {"wrong_song", "possible_wrong_song", "identity_conflict"}
        )
        version_conflict = bool(
            decision_conflicts & {"version_conflict", "version_type"}
            or "version_identity_conflict" in reason_markers
        )

        def provider_duration_conflict(candidate: Mapping[str, Any]) -> bool:
            candidate_duration = numeric_duration(candidate.get("duration_seconds"))
            if local_duration is None or candidate_duration is None:
                return False
            return abs(local_duration - candidate_duration) > max(
                30.0, local_duration * 0.2
            )

        provider_blocked = {
            "discogs": hard_identity_reason
            or hard_duration_reason
            or provider_duration_conflict(discogs),
            "musicbrainz": hard_identity_reason
            or hard_duration_reason
            or provider_duration_conflict(musicbrainz),
        }
        result: dict[str, AutomaticMetadataField] = {}
        for field_name in EDITABLE_METADATA_FIELDS:
            if field_name == "artwork":
                continue
            effective_value, is_manual, is_locked = effective_states.get(
                field_name, (None, False, False)
            )
            if is_manual or is_locked:
                continue

            candidates: list[tuple[str, object, float, object]] = []

            def add(
                source: str,
                value: object,
                score: object,
                reference: object = None,
            ) -> None:
                if provider_blocked.get(source, False):
                    return
                if source in {"discogs", "musicbrainz"} and version_conflict:
                    if field_name in {
                        "version_type",
                        "version_label",
                        "album",
                        "release_date",
                        "original_release_date",
                    }:
                        return
                if value in (None, "", "unknown"):
                    return
                try:
                    numeric_score = float(score)
                except (TypeError, ValueError, OverflowError):
                    numeric_score = 0.0
                if numeric_score < 60.0:
                    return
                candidates.append((source, value, numeric_score, reference))

            field_score = confidence.get(field_name, 0)
            resolved_source = str(
                resolved_sources.get(field_name) or ""
            ).strip().casefold()
            resolved_value = proposal.get(field_name)

            def provider_field_score(
                source: str, payload: Mapping[str, Any]
            ) -> object:
                provider_field_scores = _mapping(payload.get("field_scores"))
                if field_name in provider_field_scores:
                    return provider_field_scores[field_name]
                if resolved_source == source and field_name in confidence:
                    return field_score
                # Candidate-wide scores cannot be borrowed by a field the
                # original resolver attributed to another source.  Older rows
                # with no source map retain their only available score.
                return payload.get("score") if not resolved_sources else 0

            add(
                "discogs",
                discogs.get(field_name),
                provider_field_score("discogs", discogs),
                discogs.get("provider_reference"),
            )
            if resolved_source == "discogs":
                add(
                    "discogs",
                    resolved_value,
                    field_score,
                    discogs.get("provider_reference"),
                )
            add(
                "musicbrainz",
                musicbrainz.get(field_name),
                provider_field_score("musicbrainz", musicbrainz),
                musicbrainz.get("provider_reference"),
            )
            if resolved_source == "musicbrainz":
                add(
                    "musicbrainz",
                    resolved_value,
                    field_score,
                    musicbrainz.get("provider_reference"),
                )

            # A source performance qualifier is authoritative over a generic
            # studio/unknown provider version.  Other fields retain the strict
            # provider-first order.
            if field_name in {"version_type", "version_label"}:
                source_version = hints.get(field_name)
                if field_name == "version_type" and source_version not in (
                    None,
                    "",
                    "unknown",
                    "studio",
                ):
                    candidates.insert(
                        0,
                        ("adjudicated_source_title", source_version, 72.0, None),
                    )
                elif field_name == "version_label" and source_version:
                    candidates.insert(
                        0,
                        ("adjudicated_source_title", source_version, 72.0, None),
                    )

            if resolved_source not in {"discogs", "musicbrainz"}:
                add(
                    resolved_source or "stored_best_available",
                    resolved_value,
                    field_score,
                )

            # Existing effective metadata is the embedded/local tier. The
            # parsed source title follows it and the uploader is the final
            # artist fallback only. Raw provider clues never borrow confidence
            # from any of these independently resolved values.
            add("embedded_or_existing", effective_value, 70.0)
            hint_name = "title" if field_name == "title" else "artist" if field_name == "artist" else field_name
            strong_source_pattern = str(hints.get("pattern") or "").strip() in (
                STRONG_TITLE_PATTERNS
            )
            if strong_source_pattern:
                add("adjudicated_source_title", hints.get(hint_name), 68.0)
            if field_name == "artist":
                provider_artists = tuple(
                    value
                    for value in (discogs.get("artist"), musicbrainz.get("artist"))
                    if value not in (None, "")
                )
                uploader = classify_uploader(
                    hints.get("uploader"),
                    provider_artists=provider_artists,
                    parsed_artist=(hints.get("artist") if strong_source_pattern else None),
                )
                if uploader.may_be_primary_artist:
                    add(
                        "youtube_uploader_official_hint",
                        uploader.matched_artist or uploader.uploader,
                        62.0,
                    )

            if not candidates:
                continue
            source, value, score, reference = candidates[0]
            if str(value).strip() == str(effective_value or "").strip():
                continue
            score = max(60.0, min(100.0, score))
            try:
                # Invalid date evidence must not turn a missing field into an
                # operational transaction failure.
                if field_name in {"release_date", "original_release_date"}:
                    from .schema import normalize_release_date

                    value = normalize_release_date(value)
            except ValueError:
                continue
            result[field_name] = AutomaticMetadataField(
                value=value,
                confidence=score,
                provider=source,
                provider_reference=str(reference) if reference else None,
                conflict=False,
            )
        return result

    @staticmethod
    def _saved_field_score(
        payload: Mapping[str, Any],
        field_name: str,
        *,
        resolved_sources: Mapping[str, Any],
        confidence: Mapping[str, Any],
        provider: str,
    ) -> float:
        field_scores = _mapping(payload.get("field_scores"))
        raw: object
        if field_name in field_scores:
            raw = field_scores[field_name]
        elif str(resolved_sources.get(field_name) or "").casefold() == provider:
            raw = confidence.get(field_name, 0)
        elif not resolved_sources:
            raw = payload.get("score", 0)
        else:
            raw = 0
        try:
            return max(0.0, min(100.0, float(raw)))
        except (TypeError, ValueError, OverflowError):
            return 0.0

    def _apply_saved_catalogue_evidence(
        self,
        row: sqlite3.Row,
        decision: ReviewDecision,
        effective_states: Mapping[str, tuple[object, bool, bool]],
    ) -> None:
        """Persist accepted stored IDs/context/credits without provider I/O."""

        if decision.outcome in {ReviewOutcome.FAILED, ReviewOutcome.SKIPPED}:
            return
        proposal = _mapping(row["field_proposal"])
        confidence = _mapping(row["field_confidence"])
        resolved_sources = _mapping(proposal.get("_sources"))
        discogs = _mapping(proposal.get("_discogs"))
        musicbrainz = _mapping(proposal.get("_musicbrainz"))
        hints = _mapping(row["parsed_hints"])
        critical = {str(value).casefold() for value in decision.critical_conflicts}
        hard_identity_blocked = bool(
            critical
            & {
                "duration",
                "duration_conflict",
                "incompatible_duration",
                "wrong_song",
                "possible_wrong_song",
                "identity_conflict",
                "version_conflict",
                "version_type",
            }
        )
        track_id = int(row["track_id"])

        def numeric_duration(value: object) -> float | None:
            try:
                duration = float(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return duration if duration > 0 else None

        duration_row = self.conn.execute(
            "SELECT duration_seconds FROM tracks WHERE id=?",
            (track_id,),
        ).fetchone()
        local_duration = numeric_duration(duration_row[0]) if duration_row else None

        def provider_duration_conflict(candidate: Mapping[str, Any]) -> bool:
            candidate_duration = numeric_duration(candidate.get("duration_seconds"))
            if local_duration is None or candidate_duration is None:
                return False
            return abs(local_duration - candidate_duration) > max(
                30.0, local_duration * 0.2
            )

        provider_identity_blocked = {
            "discogs": hard_identity_blocked
            or provider_duration_conflict(discogs),
            "musicbrainz": hard_identity_blocked
            or provider_duration_conflict(musicbrainz),
        }
        row_keys = set(row.keys())
        album_state = effective_states.get("album", (None, False, False))
        album_authoritative = bool(album_state[1] or album_state[2])

        release_score = max(
            self._saved_field_score(
                discogs,
                name,
                resolved_sources=resolved_sources,
                confidence=confidence,
                provider="discogs",
            )
            for name in (
                "discogs_release_id",
                "discogs_master_id",
                "album",
                "release_date",
                "original_release_date",
            )
        )
        release_id = (
            row["discogs_release_id"] or discogs.get("release_id")
            if "discogs_release_id" in row_keys
            else discogs.get("release_id")
        )
        master_id = (
            row["discogs_master_id"] or discogs.get("master_id")
            if "discogs_master_id" in row_keys
            else discogs.get("master_id")
        )
        if (
            not provider_identity_blocked["discogs"]
            and not album_authoritative
            and release_score >= 60.0
            and (release_id or master_id or discogs.get("album"))
        ):
            self.conn.execute(
                """
                UPDATE tracks SET
                    discogs_release_id=COALESCE(discogs_release_id,?),
                    discogs_master_id=COALESCE(discogs_master_id,?),
                    discogs_track_position=COALESCE(discogs_track_position,?),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (
                    release_id,
                    master_id,
                    discogs.get("track_position"),
                    track_id,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO track_release_context (
                    track_id,discogs_release_id,discogs_master_id,
                    provider_release_family_id,release_title,release_country,
                    release_format,label_name,release_date,original_release_date,
                    provider_reference,confidence,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(track_id) DO UPDATE SET
                    discogs_release_id=COALESCE(
                        track_release_context.discogs_release_id,
                        excluded.discogs_release_id
                    ),
                    discogs_master_id=COALESCE(
                        track_release_context.discogs_master_id,
                        excluded.discogs_master_id
                    ),
                    provider_release_family_id=COALESCE(
                        track_release_context.provider_release_family_id,
                        excluded.provider_release_family_id
                    ),
                    release_title=COALESCE(
                        track_release_context.release_title,excluded.release_title
                    ),
                    release_country=COALESCE(
                        track_release_context.release_country,excluded.release_country
                    ),
                    release_format=COALESCE(
                        track_release_context.release_format,excluded.release_format
                    ),
                    label_name=COALESCE(
                        track_release_context.label_name,excluded.label_name
                    ),
                    release_date=COALESCE(
                        track_release_context.release_date,excluded.release_date
                    ),
                    original_release_date=COALESCE(
                        track_release_context.original_release_date,
                        excluded.original_release_date
                    ),
                    provider_reference=COALESCE(
                        track_release_context.provider_reference,
                        excluded.provider_reference
                    ),
                    confidence=MAX(
                        COALESCE(track_release_context.confidence,0),
                        COALESCE(excluded.confidence,0)
                    ),
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    track_id,
                    release_id,
                    master_id,
                    discogs.get("release_family_id"),
                    discogs.get("album"),
                    discogs.get("country"),
                    discogs.get("release_format"),
                    discogs.get("label"),
                    discogs.get("release_date"),
                    discogs.get("original_release_date"),
                    discogs.get("provider_reference"),
                    release_score,
                ),
            )

        mb_score = max(
            self._saved_field_score(
                musicbrainz,
                name,
                resolved_sources=resolved_sources,
                confidence=confidence,
                provider="musicbrainz",
            )
            for name in (
                "musicbrainz_recording_id",
                "musicbrainz_release_id",
                "musicbrainz_release_group_id",
            )
        )
        if not provider_identity_blocked["musicbrainz"] and mb_score >= 60.0:
            recording_id = (
                row["musicbrainz_recording_id"]
                or musicbrainz.get("recording_id")
                if "musicbrainz_recording_id" in row_keys
                else musicbrainz.get("recording_id")
            )
            mb_release_id = (
                row["musicbrainz_release_id"] or musicbrainz.get("release_id")
                if "musicbrainz_release_id" in row_keys
                else musicbrainz.get("release_id")
            )
            self.conn.execute(
                """
                UPDATE tracks SET
                    musicbrainz_recording_id=COALESCE(
                        musicbrainz_recording_id,?
                    ),
                    musicbrainz_release_id=COALESCE(musicbrainz_release_id,?),
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (recording_id, mb_release_id, track_id),
            )

        artist_state = effective_states.get("artist", (None, False, False))
        if not (artist_state[1] or artist_state[2]):
            credit_values: list[ArtistCreditInput] = []
            credit_score = self._saved_field_score(
                discogs,
                "artist_credits",
                resolved_sources=resolved_sources,
                confidence=confidence,
                provider="discogs",
            )
            raw_credits = discogs.get("artist_credits")
            if (
                not provider_identity_blocked["discogs"]
                and credit_score >= 60.0
                and isinstance(raw_credits, list)
            ):
                for raw in raw_credits:
                    credit = _mapping(raw)
                    name = str(
                        credit.get("name") or credit.get("display_name") or ""
                    ).strip()
                    if not name or is_various_artists(name):
                        continue
                    credit_values.append(
                        ArtistCreditInput(
                            display_name=name,
                            role=str(credit.get("role") or "primary"),
                            join_phrase=str(credit.get("join_phrase") or ""),
                            entity_type=str(credit.get("entity_type") or "unknown"),
                            discogs_artist_id=(
                                str(
                                    credit.get("artist_id")
                                    or credit.get("discogs_artist_id")
                                )
                                if credit.get("artist_id")
                                or credit.get("discogs_artist_id")
                                else None
                            ),
                        )
                    )
            strong_source = str(hints.get("pattern") or "") in STRONG_TITLE_PATTERNS
            if not credit_values and strong_source and hints.get("artist"):
                credit_values.append(
                    ArtistCreditInput(
                        display_name=str(hints["artist"]),
                        role="primary",
                        join_phrase=(" feat. " if hints.get("featured_artist") else ""),
                    )
                )
                if hints.get("featured_artist"):
                    credit_values.append(
                        ArtistCreditInput(
                            display_name=str(hints["featured_artist"]),
                            role="featured",
                        )
                    )
                credit_score = 68.0
            if credit_values and any(
                value.role.strip().casefold() == "primary" for value in credit_values
            ):
                ArtistCreditService(self.database).replace_track_credits(
                    track_id,
                    credit_values,
                    provenance=(
                        "discogs_best_available"
                        if raw_credits and credit_score >= 60.0
                        else "youtube_title_parsed"
                    ),
                    provider_reference=discogs.get("provider_reference"),
                    confidence=credit_score,
                    commit=False,
                )

    def reclassify(
        self,
        *,
        job_id: str | None = None,
        after_item_id: int = 0,
        limit: int = 250,
        apply: bool = False,
    ) -> ReviewReclassificationReport:
        if limit < 1 or limit > 5000:
            raise ValueError("Reclassification limit must be between 1 and 5000.")
        clauses = ["state IN ('review','ready','no_match')", "id > ?"]
        parameters: list[object] = [int(after_item_id)]
        if job_id is not None:
            clauses.append("job_id=?")
            parameters.append(str(job_id))
        rows = list(
            self.conn.execute(
                "SELECT * FROM metadata_intelligence_items WHERE "
                + " AND ".join(clauses)
                + " ORDER BY id LIMIT ?",
                (*parameters, int(limit)),
            ).fetchall()
        )
        outcomes = {outcome.value: 0 for outcome in ReviewOutcome}
        decisions: list[tuple[sqlite3.Row, ReviewDecision]] = []
        for row in rows:
            decision = self.decision_for_row(row)
            decisions.append((row, decision))
            outcomes[decision.outcome.value] += 1

        effective_by_track: dict[int, dict[str, tuple[object, bool, bool]]] = {
            int(row["track_id"]): {} for row in rows
        }
        if rows:
            track_ids = tuple(effective_by_track)
            placeholders = ",".join("?" for _ in track_ids)
            columns = ",".join(
                f"{column} AS {field_name}"
                for field_name, column in MATERIALIZED_COLUMNS.items()
                if field_name != "artwork"
            )
            materialized = {
                int(row["id"]): {
                    field_name: (row[field_name], False, False)
                    for field_name in EDITABLE_METADATA_FIELDS
                    if field_name != "artwork"
                }
                for row in self.conn.execute(
                    f"SELECT id,{columns} FROM tracks WHERE id IN ({placeholders})",
                    track_ids,
                ).fetchall()
            }
            effective_by_track.update(materialized)
            for field_row in self.conn.execute(
                "SELECT track_id,field_name,value,is_manual,is_locked "
                "FROM track_metadata_fields WHERE track_id IN ("
                + placeholders
                + ") AND field_name != 'artwork'",
                track_ids,
            ).fetchall():
                effective_by_track.setdefault(int(field_row["track_id"]), {})[
                    str(field_row["field_name"])
                ] = (
                    field_row["value"],
                    bool(field_row["is_manual"]),
                    bool(field_row["is_locked"]),
                )

        changed = 0
        safe_fields_applied = 0
        proposed_fields = 0
        reversed_orientation_repairs = 0
        album_fields_applied = 0
        touched_jobs: set[str] = set()
        automatic_by_item: dict[int, dict[str, AutomaticMetadataField]] = {}
        for row, decision in decisions:
            effective = effective_by_track.get(int(row["track_id"]), {})
            automatic = self._safe_automatic_fields(row, decision, effective)
            automatic_by_item[int(row["id"])] = automatic
            proposed_fields += len(automatic)
            album_fields_applied += int("album" in automatic)
            proposal = _mapping(row["field_proposal"])
            discogs = _mapping(proposal.get("_discogs"))
            current_title = effective.get("title", (None, False, False))[0]
            current_artist = effective.get("artist", (None, False, False))[0]
            if (
                "title" in automatic
                and "artist" in automatic
                and discogs.get("title")
                and discogs.get("artist")
                and normalize_for_comparison(current_title)
                == normalize_for_comparison(discogs.get("artist"))
                and normalize_for_comparison(current_artist)
                == normalize_for_comparison(discogs.get("title"))
            ):
                reversed_orientation_repairs += 1
        if apply and decisions:
            if not hasattr(self.database, "get_track"):
                raise TypeError("Applying reclassification requires MusicVaultDB.")
            metadata = MetadataService(self.database)
            with self._transaction():
                for row, decision in decisions:
                    automatic = automatic_by_item[int(row["id"])]
                    history_group = None
                    if automatic:
                        result = metadata.apply_automatic_fields(
                            int(row["track_id"]),
                            automatic,
                            actor="metadata_review_reclassification",
                            reason="stored_best_available_metadata_evidence",
                            minimum_confidence=60.0,
                            commit=False,
                        )
                        history_group = result.change_group_id
                        safe_fields_applied += len(result.changed_fields)
                    self._apply_saved_catalogue_evidence(row, decision, effective)
                    new_reason = decision.reason
                    state_or_reason_changed = (
                        str(row["state"]) != decision.outcome.value
                        or str(row["review_reason"] or "") != str(new_reason or "")
                    )
                    if not state_or_reason_changed and history_group is None:
                        continue
                    update = self.conn.execute(
                        """
                        UPDATE metadata_intelligence_items
                        SET state=?, review_reason=?,
                            applied_history_group=COALESCE(?, applied_history_group),
                            completed_at=COALESCE(completed_at, CURRENT_TIMESTAMP),
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=? AND state IN ('review','ready','no_match')
                        """,
                        (
                            decision.outcome.value,
                            new_reason,
                            history_group,
                            int(row["id"]),
                        ),
                    )
                    changed += int(update.rowcount == 1 and state_or_reason_changed)
                    touched_jobs.add(str(row["job_id"]))
                store = MetadataIntelligenceJobStore(self.database)
                for touched_job_id in sorted(touched_jobs):
                    store._refresh_job(touched_job_id)

        last_item_id = int(rows[-1]["id"]) if rows else int(after_item_id)
        remaining_clauses = ["state IN ('review','ready','no_match')", "id > ?"]
        remaining_parameters: list[object] = [last_item_id]
        if job_id is not None:
            remaining_clauses.append("job_id=?")
            remaining_parameters.append(str(job_id))
        remaining = int(
            self.conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items WHERE "
                + " AND ".join(remaining_clauses),
                tuple(remaining_parameters),
            ).fetchone()[0]
        )
        return ReviewReclassificationReport(
            scanned=len(rows),
            changed=changed,
            applied=outcomes[ReviewOutcome.APPLIED.value],
            applied_with_gaps=outcomes[ReviewOutcome.APPLIED_WITH_GAPS.value],
            source_fallback=outcomes[ReviewOutcome.SOURCE_FALLBACK.value],
            needs_review=outcomes[ReviewOutcome.NEEDS_REVIEW.value],
            safe_fields_applied=safe_fields_applied,
            last_item_id=last_item_id,
            remaining=remaining,
            dry_run=not apply,
            outcome_counts=outcomes,
            proposed_fields=proposed_fields,
            reversed_orientation_repairs=reversed_orientation_repairs,
            album_fields_applied=album_fields_applied,
            terminalized_review_items=sum(
                count
                for state, count in outcomes.items()
                if state != ReviewOutcome.NEEDS_REVIEW.value
            ),
            operational_failures=outcomes[ReviewOutcome.FAILED.value],
        )


def reclassify_stored_review_items(
    database: object,
    *,
    job_id: str | None = None,
    batch_size: int = 250,
    apply: bool = True,
) -> ReviewReclassificationReport:
    """Run bounded batches over all saved review rows, returning aggregates only.

    The function is safe to call again: terminal outcomes are not selected and
    unchanged critical-review rows do not receive timestamp-only writes.  No
    provider client is imported or constructed by this module.
    """

    reclassifier = MetadataReviewReclassifier(database)
    cursor = 0
    reports: list[ReviewReclassificationReport] = []
    while True:
        report = reclassifier.reclassify(
            job_id=job_id,
            after_item_id=cursor,
            limit=batch_size,
            apply=apply,
        )
        reports.append(report)
        if report.scanned == 0 or report.remaining == 0:
            break
        cursor = report.last_item_id
    outcome_counts = {outcome.value: 0 for outcome in ReviewOutcome}
    for report in reports:
        for state, count in report.outcome_counts.items():
            outcome_counts[state] = outcome_counts.get(state, 0) + int(count)
    return ReviewReclassificationReport(
        scanned=sum(report.scanned for report in reports),
        changed=sum(report.changed for report in reports),
        applied=outcome_counts[ReviewOutcome.APPLIED.value],
        applied_with_gaps=outcome_counts[ReviewOutcome.APPLIED_WITH_GAPS.value],
        source_fallback=outcome_counts[ReviewOutcome.SOURCE_FALLBACK.value],
        needs_review=outcome_counts[ReviewOutcome.NEEDS_REVIEW.value],
        safe_fields_applied=sum(report.safe_fields_applied for report in reports),
        last_item_id=reports[-1].last_item_id,
        remaining=0,
        dry_run=not apply,
        outcome_counts=outcome_counts,
        proposed_fields=sum(report.proposed_fields for report in reports),
        reversed_orientation_repairs=sum(
            report.reversed_orientation_repairs for report in reports
        ),
        album_fields_applied=sum(report.album_fields_applied for report in reports),
        terminalized_review_items=sum(
            report.terminalized_review_items for report in reports
        ),
        operational_failures=sum(report.operational_failures for report in reports),
    )


def best_available_reclassify(
    database: object,
    *,
    apply: bool = False,
    job_id: str | None = None,
    batch_size: int = 250,
) -> ReviewReclassificationReport:
    """Plan/apply the offline Batch 10.5 stored-evidence acceptance pass.

    When called inside a surrounding transaction every write uses savepoints
    and remains owned by the caller; this function never commits that outer
    transaction.  It constructs no provider client and invokes no tag writer.
    """

    return reclassify_stored_review_items(
        database,
        job_id=job_id,
        batch_size=batch_size,
        apply=apply,
    )


__all__ = [
    "MetadataReviewReclassifier",
    "ReviewReclassificationReport",
    "best_available_reclassify",
    "reclassify_stored_review_items",
]

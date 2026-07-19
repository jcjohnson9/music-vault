"""One-target Batch 10.6 orientation repair boundary.

This module deliberately has no provider client, filesystem writer, or UI
dependency.  It discovers one structurally identifiable terminal source-
fallback item and applies an already-normalized, coherent provider resolution
in one SQLite transaction.  Acceptance tooling owns backup creation and the
bounded provider lookup.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .artist_credits import ArtistCreditInput, ArtistCreditService
from .canonical_albums import upsert_track_canonical_album
from .ensemble import recording_group_key
from .intelligence_schema import MetadataIntelligenceJobStore
from .matching import normalize_for_comparison
from .schema import EDITABLE_METADATA_FIELDS, utc_now
from .service import AutomaticMetadataField, MetadataService
from .title_parser import (
    STRONG_TITLE_PATTERNS,
    TitleOrientationHypothesis,
    parse_youtube_title,
    title_orientation_hypotheses,
)


ORIENTATION_REPAIR_MARKER = "batch10_6_orientation_repair_v1"
REQUIRED_SCHEMA_VERSION = 7
_SUPPORTED_PROVIDERS = frozenset({"discogs", "musicbrainz"})
_RESOLUTION_CONFIDENCE_FIELDS = frozenset(
    {
        "title",
        "artist",
        "artist_credits",
        "album",
        "album_artist",
        "release_date",
        "original_release_date",
        "version_type",
        "version_label",
        "discogs_release_id",
        "discogs_master_id",
        "discogs_track_position",
        "musicbrainz_recording_id",
        "musicbrainz_release_id",
        "musicbrainz_release_group_id",
        "provider_release_family_id",
        "release_country",
        "release_format",
        "label_name",
    }
)
_PROTECTED_TABLES = (
    "playlists",
    "playlist_tracks",
    "playlist_origins",
    "sync_sources",
    "sync_source_items",
    "track_source_memberships",
    "source_track_identities",
    "source_identity_conflicts",
    "metadata_remediation_jobs",
    "metadata_remediation_items",
    "metadata_remediation_backups",
)


class OrientationRepairError(RuntimeError):
    """A stable failure that contains no private metadata values."""


@dataclass(frozen=True, slots=True, repr=False)
class OrientationRepairTarget:
    """Private target facts that must never be serialized into a report."""

    track_id: int
    item_id: int
    job_id: str
    raw_title: str
    current_title: str
    current_artist: str
    duration_seconds: float | None
    hypotheses: tuple[TitleOrientationHypothesis, ...]
    baseline_digest: str


@dataclass(frozen=True, slots=True)
class OrientationDiscoveryReport:
    marker_present: bool
    candidate_items_inspected: int = 0
    strong_dash_items: int = 0
    crosswise_current_value_items: int = 0
    empty_provider_proposal_items: int = 0
    year_hint_items: int = 0
    version_qualified_items: int = 0
    acceptance_qualified_items: int = 0
    exact_target_count: int = 0
    expected_metadata_field_changes: int = 0
    expected_credit_changes: int = 0
    expected_artist_cluster_changes: int = 0
    expected_album_membership_changes: int = 0
    expected_history_additions: int = 0
    expected_media_changes: int = 0
    expected_cover_path_changes: int = 0
    expected_source_membership_changes: int = 0
    expected_playlist_changes: int = 0

    def aggregate_dict(self) -> dict[str, int | bool]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


@dataclass(frozen=True, slots=True)
class RepairArtistCredit:
    display_name: str
    role: str = "primary"
    join_phrase: str = ""
    entity_type: str = "unknown"
    provider_artist_id: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class OrientationResolution:
    """Normalized provider facts only; never a raw response or query."""

    provider: str
    selected_orientation: str
    title: str
    artist: str
    coherent: bool
    confidence: float
    field_confidences: tuple[tuple[str, float], ...] = ()
    orientation_evaluated_count: int = 0
    orientation_reasons: tuple[str, ...] = ()
    provider_confirmed: bool = False
    requires_provider_adjudication: bool = True
    discogs_queries: int = 0
    musicbrainz_queries: int = 0
    provider_reference: str | None = None
    artist_credits: tuple[RepairArtistCredit, ...] = ()
    album: str | None = None
    album_artist: str | None = None
    release_date: str | None = None
    original_release_date: str | None = None
    version_type: str | None = None
    version_label: str | None = None
    discogs_release_id: str | None = None
    discogs_master_id: str | None = None
    discogs_track_position: str | None = None
    musicbrainz_recording_id: str | None = None
    musicbrainz_release_id: str | None = None
    musicbrainz_release_group_id: str | None = None
    provider_release_family_id: str | None = None
    release_country: str | None = None
    release_format: str | None = None
    label_name: str | None = None

    def field_confidence_map(self) -> dict[str, float]:
        return {str(name): float(score) for name, score in self.field_confidences}

    def accepts(self, field_name: str, *, threshold: float = 85.0) -> bool:
        score = self.field_confidence_map().get(str(field_name))
        return score is not None and score >= float(threshold)


@dataclass(frozen=True, slots=True)
class OrientationRepairResult:
    no_op: bool
    marker_written: bool
    targets_repaired: int = 0
    metadata_fields_changed: int = 0
    structured_credit_rows: int = 0
    canonical_album_memberships: int = 0
    history_rows_added: int = 0
    review_count: int = 0
    media_changes: int = 0
    cover_path_changes: int = 0
    source_membership_changes: int = 0
    playlist_changes: int = 0

    def aggregate_dict(self) -> dict[str, int | bool]:
        return {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }


def _connection(database: object) -> sqlite3.Connection:
    conn = getattr(database, "conn", database)
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("Orientation repair requires a SQLite connection.")
    conn.row_factory = sqlite3.Row
    return conn


def _json_mapping(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _clean(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _stable_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _rows_digest(
    conn: sqlite3.Connection,
    table: str,
    *,
    where: str = "",
    parameters: Sequence[object] = (),
) -> tuple[int, str]:
    if not _table_exists(conn, table):
        return 0, _stable_digest([])
    quoted = '"' + table.replace('"', '""') + '"'
    rows = conn.execute(
        f"SELECT * FROM {quoted} {where}", tuple(parameters)
    ).fetchall()
    payload = [tuple(row) for row in rows]
    return len(payload), _stable_digest(payload)


def orientation_repair_applied(database: object) -> bool:
    conn = _connection(database)
    return conn.execute(
        "SELECT 1 FROM app_meta WHERE key=?", (ORIENTATION_REPAIR_MARKER,)
    ).fetchone() is not None


def _provider_proposals_empty(row: sqlite3.Row) -> bool:
    proposal = _json_mapping(row["field_proposal"])
    discogs = proposal.get("_discogs")
    musicbrainz = proposal.get("_musicbrainz")
    return (
        isinstance(discogs, Mapping)
        and not discogs
        and isinstance(musicbrainz, Mapping)
        and not musicbrainz
        and str(row["provider_agreement"] or "").strip().casefold() == "none"
        and all(
        _clean(row[name]) is None
        for name in (
            "discogs_release_id",
            "discogs_master_id",
            "musicbrainz_recording_id",
            "musicbrainz_release_id",
        )
        )
    )


def _crosswise_current(
    title: object,
    artist: object,
    hypotheses: Sequence[TitleOrientationHypothesis],
) -> bool:
    """Recognize the parser's conventional assignment of the two raw sides."""

    conventional = next(
        (item for item in hypotheses if item.orientation == "left_is_artist"),
        None,
    )
    if conventional is None:
        return False
    return (
        normalize_for_comparison(title, title=True)
        == normalize_for_comparison(conventional.title, title=True)
        and normalize_for_comparison(artist)
        == normalize_for_comparison(conventional.artist)
    )


def discover_orientation_repair_targets(
    database: object,
) -> tuple[OrientationDiscoveryReport, tuple[OrientationRepairTarget, ...]]:
    """Read terminal evidence and return only structurally exact candidates."""

    conn = _connection(database)
    if orientation_repair_applied(conn):
        return OrientationDiscoveryReport(marker_present=True), ()
    rows = conn.execute(
        """
        SELECT item.*, job.job_kind, track.title AS current_title,
               track.artist AS current_artist,
               track.duration_seconds, track.metadata_updated_at,
               track.updated_at AS track_updated_at,
               (
                   SELECT observation.value
                   FROM track_metadata_observations AS observation
                   WHERE observation.track_id=track.id
                     AND observation.provider='youtube'
                     AND observation.field_name='title'
                   ORDER BY observation.observed_at DESC, observation.id DESC
                   LIMIT 1
               ) AS observed_raw_title
        FROM metadata_intelligence_items AS item
        JOIN metadata_intelligence_jobs AS job ON job.id=item.job_id
        JOIN tracks AS track ON track.id=item.track_id
        WHERE job.job_kind='existing_library'
          AND item.state='source_fallback'
        ORDER BY item.id
        """
    ).fetchall()
    strong = crosswise = empty = 0
    year_hints = version_qualified = acceptance_qualified = 0
    targets: list[OrientationRepairTarget] = []
    for row in rows:
        hints = _json_mapping(row["parsed_hints"])
        raw_title = _clean(hints.get("raw_title"))
        if raw_title is None:
            continue
        observed_raw_title = _clean(row["observed_raw_title"])
        if (
            observed_raw_title is None
            or normalize_for_comparison(observed_raw_title, title=True)
            != normalize_for_comparison(raw_title, title=True)
        ):
            continue
        parsed = parse_youtube_title(raw_title)
        hypotheses = title_orientation_hypotheses(parsed)
        if parsed.pattern not in STRONG_TITLE_PATTERNS or len(hypotheses) != 2:
            continue
        strong += 1
        if not _crosswise_current(
            row["current_title"], row["current_artist"], hypotheses
        ):
            continue
        crosswise += 1
        if not _provider_proposals_empty(row):
            continue
        empty += 1
        has_year_hint = parsed.year_hint is not None
        has_version_qualifier = bool(
            (
                str(parsed.version_type or "").strip().casefold()
                not in {"", "unknown"}
            )
            or _clean(parsed.version_label)
        )
        year_hints += int(has_year_hint)
        version_qualified += int(has_version_qualifier)
        if not has_year_hint:
            continue
        acceptance_qualified += 1
        baseline = {
            "track_id": int(row["track_id"]),
            "item_id": int(row["id"]),
            "job_id": str(row["job_id"]),
            "raw_title": raw_title,
            "observed_raw_title": observed_raw_title,
            "current_title": row["current_title"],
            "current_artist": row["current_artist"],
            "parsed_hints": row["parsed_hints"],
            "field_proposal": row["field_proposal"],
            "item_updated_at": row["updated_at"],
            "metadata_updated_at": row["metadata_updated_at"],
            "track_updated_at": row["track_updated_at"],
        }
        duration = row["duration_seconds"]
        targets.append(
            OrientationRepairTarget(
                track_id=int(row["track_id"]),
                item_id=int(row["id"]),
                job_id=str(row["job_id"]),
                raw_title=raw_title,
                current_title=str(row["current_title"] or ""),
                current_artist=str(row["current_artist"] or ""),
                duration_seconds=float(duration) if duration is not None else None,
                hypotheses=tuple(hypotheses),
                baseline_digest=_stable_digest(baseline),
            )
        )
    target_count = len(targets)
    report = OrientationDiscoveryReport(
        marker_present=False,
        candidate_items_inspected=len(rows),
        strong_dash_items=strong,
        crosswise_current_value_items=crosswise,
        empty_provider_proposal_items=empty,
        year_hint_items=year_hints,
        version_qualified_items=version_qualified,
        acceptance_qualified_items=acceptance_qualified,
        exact_target_count=target_count,
        expected_metadata_field_changes=2 if target_count == 1 else 0,
        expected_credit_changes=1 if target_count == 1 else 0,
        expected_artist_cluster_changes=1 if target_count == 1 else 0,
        expected_album_membership_changes=1 if target_count == 1 else 0,
        expected_history_additions=2 if target_count == 1 else 0,
    )
    return report, tuple(targets)


def require_exact_orientation_repair_target(database: object) -> OrientationRepairTarget:
    report, targets = discover_orientation_repair_targets(database)
    if report.marker_present:
        raise OrientationRepairError("orientation_repair_already_applied")
    if len(targets) != 1:
        raise OrientationRepairError("orientation_repair_target_count_not_one")
    return targets[0]


def _validate_resolution(
    target: OrientationRepairTarget,
    resolution: OrientationResolution,
) -> TitleOrientationHypothesis:
    provider = str(resolution.provider or "").strip().casefold()
    if provider not in _SUPPORTED_PROVIDERS:
        raise OrientationRepairError("orientation_provider_not_allowed")
    if not resolution.coherent or not 85.0 <= float(resolution.confidence) <= 100.0:
        raise OrientationRepairError("orientation_resolution_not_coherent")
    confidences = resolution.field_confidence_map()
    confidence_names = tuple(name for name, _score in resolution.field_confidences)
    if (
        len(confidence_names) != len(set(confidence_names))
        or not set(confidence_names).issubset(_RESOLUTION_CONFIDENCE_FIELDS)
        or any(not 0.0 <= value <= 100.0 for value in confidences.values())
    ):
        raise OrientationRepairError("orientation_field_confidence_invalid")
    if not all(resolution.accepts(name) for name in ("title", "artist", "artist_credits")):
        raise OrientationRepairError("orientation_identity_field_confidence_too_low")
    if (
        not resolution.provider_confirmed
        or resolution.requires_provider_adjudication
        or not 1 <= int(resolution.orientation_evaluated_count) <= 2
        or not 0 <= int(resolution.discogs_queries) <= 2
        or not 0 <= int(resolution.musicbrainz_queries) <= 1
        or not resolution.orientation_reasons
        or any(
            re.fullmatch(r"[a-z0-9_]{1,80}", str(reason)) is None
            for reason in resolution.orientation_reasons
        )
    ):
        raise OrientationRepairError("orientation_resolution_evidence_incomplete")
    selected = next(
        (
            item
            for item in target.hypotheses
            if item.orientation == resolution.selected_orientation
        ),
        None,
    )
    if selected is None:
        raise OrientationRepairError("orientation_resolution_not_a_hypothesis")
    if (
        normalize_for_comparison(resolution.title, title=True)
        != normalize_for_comparison(selected.title, title=True)
        or normalize_for_comparison(resolution.artist)
        != normalize_for_comparison(selected.artist)
    ):
        raise OrientationRepairError("orientation_resolution_identity_mismatch")
    if selected.orientation != "right_is_artist":
        raise OrientationRepairError("orientation_resolution_does_not_repair_target")
    return selected


def _assert_schema_and_locks(conn: sqlite3.Connection, target_id: int) -> None:
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) != REQUIRED_SCHEMA_VERSION:
        raise OrientationRepairError("orientation_repair_schema_not_seven")
    if int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise OrientationRepairError("orientation_repair_foreign_keys_disabled")
    locked = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM track_metadata_fields
            WHERE track_id=? AND field_name IN ('title','artist')
              AND (is_manual=1 OR is_locked=1)
            """,
            (target_id,),
        ).fetchone()[0]
    )
    locked += int(
        conn.execute(
            """
            SELECT COUNT(*) FROM track_artist_credits
            WHERE track_id=? AND (is_manual=1 OR is_locked=1)
            """,
            (target_id,),
        ).fetchone()[0]
    )
    if locked:
        raise OrientationRepairError("orientation_repair_blocked_by_lock")


def _assert_provider_id_compatibility(
    conn: sqlite3.Connection,
    target_id: int,
    resolution: OrientationResolution,
) -> None:
    """Never replace a different already-accepted provider identity."""

    track = conn.execute("SELECT * FROM tracks WHERE id=?", (target_id,)).fetchone()
    if track is None:
        raise OrientationRepairError("orientation_repair_target_missing")
    track_keys = set(track.keys())
    for name, incoming in (
        ("discogs_release_id", resolution.discogs_release_id),
        ("discogs_master_id", resolution.discogs_master_id),
        ("musicbrainz_recording_id", resolution.musicbrainz_recording_id),
        ("musicbrainz_release_id", resolution.musicbrainz_release_id),
    ):
        if (
            name not in track_keys
            or incoming in (None, "")
            or not resolution.accepts(name)
        ):
            continue
        existing = _clean(track[name])
        if existing is not None and existing != _clean(incoming):
            raise OrientationRepairError("orientation_repair_provider_identity_conflict")
    context = conn.execute(
        "SELECT * FROM track_release_context WHERE track_id=?", (target_id,)
    ).fetchone()
    if context is None:
        return
    for name, incoming in (
        ("discogs_release_id", resolution.discogs_release_id),
        ("discogs_master_id", resolution.discogs_master_id),
        ("musicbrainz_release_group_id", resolution.musicbrainz_release_group_id),
        ("provider_release_family_id", resolution.provider_release_family_id),
    ):
        if incoming in (None, "") or not resolution.accepts(name):
            continue
        existing = _clean(context[name])
        if existing is not None and existing != _clean(incoming):
            raise OrientationRepairError("orientation_repair_provider_identity_conflict")


def _guard_state(conn: sqlite3.Connection, target_id: int) -> dict[str, object]:
    target = conn.execute(
        """
        SELECT path,cover_path,source_url,source_kind,source_video_id,
               source_upload_date,created_at
        FROM tracks WHERE id=?
        """,
        (target_id,),
    ).fetchone()
    if target is None:
        raise OrientationRepairError("orientation_repair_target_missing")
    state: dict[str, object] = {
        "target_protected": tuple(target),
        "non_target_tracks": _rows_digest(
            conn, "tracks", where="WHERE id<>? ORDER BY id", parameters=(target_id,)
        ),
        "non_target_credits": _rows_digest(
            conn,
            "track_artist_credits",
            where="WHERE track_id<>? ORDER BY id",
            parameters=(target_id,),
        ),
    }
    for table in _PROTECTED_TABLES:
        state[table] = _rows_digest(conn, table, where="ORDER BY 1")
    return state


def _release_context_values(
    resolution: OrientationResolution,
) -> dict[str, object | None]:
    candidates = (
        ("discogs_release_id", "discogs_release_id", resolution.discogs_release_id),
        ("discogs_master_id", "discogs_master_id", resolution.discogs_master_id),
        (
            "musicbrainz_release_group_id",
            "musicbrainz_release_group_id",
            resolution.musicbrainz_release_group_id,
        ),
        (
            "provider_release_family_id",
            "provider_release_family_id",
            resolution.provider_release_family_id,
        ),
        ("release_title", "album", resolution.album),
        ("release_country", "release_country", resolution.release_country),
        ("release_format", "release_format", resolution.release_format),
        ("label_name", "label_name", resolution.label_name),
        ("release_date", "release_date", resolution.release_date),
        (
            "original_release_date",
            "original_release_date",
            resolution.original_release_date,
        ),
    )
    accepted = {
        column: value
        for column, field_name, value in candidates
        if value not in (None, "") and resolution.accepts(field_name)
    }
    if accepted:
        if resolution.provider_reference not in (None, ""):
            accepted["provider_reference"] = resolution.provider_reference
        accepted["confidence"] = max(
            resolution.field_confidence_map()[field_name]
            for _column, field_name, value in candidates
            if value not in (None, "") and resolution.accepts(field_name)
        )
    return accepted


def _upsert_release_context(
    conn: sqlite3.Connection,
    target_id: int,
    resolution: OrientationResolution,
) -> None:
    values = _release_context_values(resolution)
    if not values:
        return
    columns = tuple(values)
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{name}=excluded.{name}" for name in columns)
    conn.execute(
        f"""
        INSERT INTO track_release_context(track_id,{','.join(columns)},updated_at)
        VALUES(?,{placeholders},?)
        ON CONFLICT(track_id) DO UPDATE SET {updates},updated_at=excluded.updated_at
        """,
        (target_id, *(values[name] for name in columns), utc_now()),
    )


def _credit_inputs(
    resolution: OrientationResolution,
) -> tuple[ArtistCreditInput, ...]:
    credits = resolution.artist_credits or (
        RepairArtistCredit(resolution.artist, role="primary"),
    )
    provider = resolution.provider.casefold()
    return tuple(
        ArtistCreditInput(
            display_name=value.display_name,
            role=value.role,
            join_phrase=value.join_phrase,
            entity_type=value.entity_type,
            discogs_artist_id=(
                value.provider_artist_id if provider == "discogs" else None
            ),
            musicbrainz_artist_id=(
                value.provider_artist_id if provider == "musicbrainz" else None
            ),
        )
        for value in credits
    )


def _current_target_digest(conn: sqlite3.Connection, target_id: int, item_id: int) -> str:
    row = conn.execute(
        """
        SELECT track.id AS track_id,item.id AS item_id,item.job_id,
               TRIM(COALESCE(json_extract(item.parsed_hints,'$.raw_title'),''))
                   AS raw_title,
               (
                   SELECT TRIM(observation.value)
                   FROM track_metadata_observations AS observation
                   WHERE observation.track_id=track.id
                     AND observation.provider='youtube'
                     AND observation.field_name='title'
                   ORDER BY observation.observed_at DESC, observation.id DESC
                   LIMIT 1
               ) AS observed_raw_title,
               track.title AS current_title,track.artist AS current_artist,
               item.parsed_hints,item.field_proposal,item.updated_at AS item_updated_at,
               track.metadata_updated_at,track.updated_at AS track_updated_at
        FROM tracks AS track
        JOIN metadata_intelligence_items AS item ON item.track_id=track.id
        WHERE track.id=? AND item.id=?
        """,
        (target_id, item_id),
    ).fetchone()
    if row is None:
        raise OrientationRepairError("orientation_repair_target_missing")
    return _stable_digest(dict(row))


def apply_orientation_repair(
    database: object,
    *,
    target: OrientationRepairTarget | None = None,
    resolution: OrientationResolution | None = None,
) -> OrientationRepairResult:
    """Apply exactly one coherent resolution, or return a marker-first no-op."""

    conn = _connection(database)
    if orientation_repair_applied(conn):
        return OrientationRepairResult(no_op=True, marker_written=False)
    if target is None or resolution is None:
        raise OrientationRepairError("orientation_repair_resolution_required")
    selected = _validate_resolution(target, resolution)
    discovered = require_exact_orientation_repair_target(conn)
    if (
        discovered.track_id != target.track_id
        or discovered.item_id != target.item_id
        or discovered.baseline_digest != target.baseline_digest
    ):
        raise OrientationRepairError("orientation_repair_target_stale")
    _assert_schema_and_locks(conn, target.track_id)
    _assert_provider_id_compatibility(conn, target.track_id, resolution)
    before_guard = _guard_state(conn, target.track_id)
    before_history = int(
        conn.execute(
            "SELECT COUNT(*) FROM track_metadata_history WHERE track_id=?",
            (target.track_id,),
        ).fetchone()[0]
    )
    if conn.in_transaction:
        raise OrientationRepairError("orientation_repair_requires_owned_transaction")

    conn.execute("BEGIN IMMEDIATE")
    try:
        if orientation_repair_applied(conn):
            conn.rollback()
            return OrientationRepairResult(no_op=True, marker_written=False)
        if _current_target_digest(conn, target.track_id, target.item_id) != target.baseline_digest:
            raise OrientationRepairError("orientation_repair_target_stale")
        _assert_schema_and_locks(conn, target.track_id)
        _assert_provider_id_compatibility(conn, target.track_id, resolution)

        provider = resolution.provider.casefold()
        fields: dict[str, AutomaticMetadataField] = {}
        for name in EDITABLE_METADATA_FIELDS:
            if name == "artwork":
                continue
            value = getattr(resolution, name)
            confidence = resolution.field_confidence_map().get(name)
            if value in (None, "") or confidence is None or confidence < 85.0:
                continue
            fields[name] = AutomaticMetadataField(
                value=value,
                confidence=confidence,
                provider=provider,
                provider_reference=resolution.provider_reference,
            )
        result = MetadataService(conn).apply_automatic_fields(
            target.track_id,
            fields,
            provider=provider,
            minimum_confidence=85.0,
            actor="batch10_6_orientation_repair",
            reason="provider_confirmed_dual_orientation_repair",
            commit=False,
        )
        if not {"title", "artist"}.issubset(result.changed_fields):
            raise OrientationRepairError("orientation_repair_identity_not_applied")

        track_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(tracks)")
        }
        direct = {
            "discogs_release_id": resolution.discogs_release_id,
            "discogs_master_id": resolution.discogs_master_id,
            "discogs_track_position": resolution.discogs_track_position,
            "musicbrainz_recording_id": resolution.musicbrainz_recording_id,
            "musicbrainz_release_id": resolution.musicbrainz_release_id,
            "recording_group_key": recording_group_key(
                resolution.title,
                resolution.artist,
                master_id=(
                    resolution.discogs_master_id
                    if resolution.accepts("discogs_master_id")
                    else None
                ),
            ),
        }
        direct = {
            name: value
            for name, value in direct.items()
            if name in track_columns
            and value not in (None, "")
            and (
                name == "recording_group_key"
                or resolution.accepts(name)
            )
        }
        if direct:
            assignments = ",".join(f"{name}=?" for name in direct)
            conn.execute(
                f"UPDATE tracks SET {assignments} WHERE id=?",
                (*direct.values(), target.track_id),
            )

        credits = ArtistCreditService(conn).replace_track_credits(
            target.track_id,
            _credit_inputs(resolution),
            provenance=provider,
            provider_reference=resolution.provider_reference,
            confidence=resolution.field_confidence_map()["artist_credits"],
            actor="batch10_6_orientation_repair",
            reason="provider_confirmed_dual_orientation_repair",
            commit=False,
        )
        _upsert_release_context(conn, target.track_id, resolution)
        album_id = upsert_track_canonical_album(conn, target.track_id)

        parsed = parse_youtube_title(target.raw_title)
        parsed_summary = {
            "raw_title": target.raw_title,
            "title": selected.title,
            "artist": selected.artist,
            "year": selected.year_hint,
            "version_type": selected.version_type,
            "version_label": selected.version_label,
            "pattern": parsed.pattern,
            "orientation": {
                "evaluated_count": int(resolution.orientation_evaluated_count),
                "selected": selected.orientation,
                "confidence": float(resolution.confidence),
                "reasons": list(resolution.orientation_reasons),
                "provider_confirmed": bool(resolution.provider_confirmed),
                "requires_provider_adjudication": bool(
                    resolution.requires_provider_adjudication
                ),
                "discogs_queries": int(resolution.discogs_queries),
                "musicbrainz_queries": int(resolution.musicbrainz_queries),
            },
        }
        accepted_provider_fields = {
            name: getattr(resolution, name)
            for name in (
                "title",
                "artist",
                "album",
                "album_artist",
                "release_date",
                "original_release_date",
                "version_type",
                "version_label",
                "discogs_release_id",
                "discogs_master_id",
                "discogs_track_position",
                "musicbrainz_recording_id",
                "musicbrainz_release_id",
                "musicbrainz_release_group_id",
                "provider_release_family_id",
                "release_country",
                "release_format",
                "label_name",
            )
            if getattr(resolution, name) not in (None, "")
            and resolution.accepts(name)
        }
        proposal = {
            "_current": {
                "title": resolution.title,
                "artist": resolution.artist,
                "album": accepted_provider_fields.get("album"),
            },
            f"_{provider}": {
                **accepted_provider_fields,
                "score": float(resolution.confidence),
                "provider_reference": resolution.provider_reference,
            },
            "_sources": {name: provider for name in fields},
            "_orientation": parsed_summary["orientation"],
        }
        state = (
            "applied"
            if accepted_provider_fields.get("album")
            else "applied_with_gaps"
        )
        MetadataIntelligenceJobStore(conn).mark_item(
            target.item_id,
            state,
            parsed_hints=parsed_summary,
            discogs_release_id=(
                resolution.discogs_release_id
                if resolution.accepts("discogs_release_id")
                else None
            ),
            discogs_master_id=(
                resolution.discogs_master_id
                if resolution.accepts("discogs_master_id")
                else None
            ),
            musicbrainz_recording_id=(
                resolution.musicbrainz_recording_id
                if resolution.accepts("musicbrainz_recording_id")
                else None
            ),
            musicbrainz_release_id=(
                resolution.musicbrainz_release_id
                if resolution.accepts("musicbrainz_release_id")
                else None
            ),
            field_proposal=proposal,
            field_confidence={
                name: float(score)
                for name, score in resolution.field_confidences
            },
            provider_agreement=(
                "discogs_only" if provider == "discogs" else "musicbrainz_only"
            ),
            review_reason=None,
            applied_history_group=result.change_group_id,
            file_write_result="not_requested",
            artwork_result="not_requested",
        )

        review_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items "
                "WHERE state IN ('review','ready')"
            ).fetchone()[0]
        )
        if review_count:
            raise OrientationRepairError("orientation_repair_review_count_nonzero")
        if _guard_state(conn, target.track_id) != before_guard:
            raise OrientationRepairError("orientation_repair_protected_state_changed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise OrientationRepairError("orientation_repair_foreign_key_failure")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).casefold() != "ok":
            raise OrientationRepairError("orientation_repair_integrity_failure")
        conn.execute(
            "INSERT INTO app_meta(key,value) VALUES(?,?)",
            (ORIENTATION_REPAIR_MARKER, "1"),
        )
        after_history = int(
            conn.execute(
                "SELECT COUNT(*) FROM track_metadata_history WHERE track_id=?",
                (target.track_id,),
            ).fetchone()[0]
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return OrientationRepairResult(
        no_op=False,
        marker_written=True,
        targets_repaired=1,
        metadata_fields_changed=len(result.changed_fields),
        structured_credit_rows=len(credits),
        canonical_album_memberships=int(album_id is not None),
        history_rows_added=after_history - before_history,
        review_count=0,
    )


__all__ = [
    "ORIENTATION_REPAIR_MARKER",
    "REQUIRED_SCHEMA_VERSION",
    "OrientationDiscoveryReport",
    "OrientationRepairError",
    "OrientationRepairResult",
    "OrientationRepairTarget",
    "OrientationResolution",
    "RepairArtistCredit",
    "apply_orientation_repair",
    "discover_orientation_repair_targets",
    "orientation_repair_applied",
    "require_exact_orientation_repair_target",
]

"""Aggregate-only one-target Batch 10.6 live acceptance gate.

Dry-run mode is immutable and provider-free.  Apply mode requires an exact
acknowledgement, creates a verified schema-7 backup before provider work,
rehearses the normalized resolution on a disposable clone, then applies the
same resolution to the live database.  Reports intentionally contain counts,
booleans, hashes, and a backup file name only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.core.runtime_policy import (  # noqa: E402
    NO_NETWORK_ENVIRONMENT,
    NO_SECRETS_ENVIRONMENT,
)
from music_vault.metadata.intelligence_settings import DiscogsTokenStore  # noqa: E402
from music_vault.metadata.musicbrainz_enricher import MusicBrainzProvider  # noqa: E402
from music_vault.metadata.orientation_repair import (  # noqa: E402
    ORIENTATION_REPAIR_MARKER,
    OrientationRepairError,
    OrientationRepairTarget,
    OrientationResolution,
    RepairArtistCredit,
    apply_orientation_repair,
    discover_orientation_repair_targets,
    orientation_repair_applied,
)
from music_vault.metadata.providers import ProviderQuery  # noqa: E402
from music_vault.metadata.providers.discogs import (  # noqa: E402
    DiscogsProvider,
    DiscogsRateLimiter,
)
from music_vault.metadata.title_orientation import choose_orientation  # noqa: E402
from tools.dev import batch10_3_acceptance as acceptance  # noqa: E402
from tools.dev import batch10_5_acceptance as gate105  # noqa: E402


REPORT_FORMAT_VERSION = 1
SCHEMA_VERSION = 7
LIVE_ACKNOWLEDGEMENT = "batch10.6-live-one-track-orientation-repair"
DATABASE_BACKUP_PREFIX = "music_vault_batch10_6_pre_orientation_repair_"
MAX_HTTP_REQUESTS = 12
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


class Batch106Failure(acceptance.AcceptanceFailure):
    """A stable non-identifying acceptance failure."""


@dataclass(frozen=True, slots=True)
class TargetedLookupResult:
    resolution: OrientationResolution
    request_count: int
    discogs_orientation_searches: int
    musicbrainz_searches: int


ProviderLookup = Callable[[OrientationRepairTarget], TargetedLookupResult | OrientationResolution]


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _covers_inventory(project_root: Path) -> dict[str, Any]:
    covers = project_root / "data" / "covers"
    files = (
        sorted(
            (path for path in covers.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(covers).as_posix(),
        )
        if covers.is_dir()
        else ()
    )
    return gate105._inventory_from_paths(files)


def _capture(
    *, project_root: Path, database: Path, cache_root: Path
) -> dict[str, Any]:
    state = gate105.capture_preservation_state(
        project_root=project_root,
        database=database,
        cache_root=cache_root,
    )
    state["covers"] = _covers_inventory(project_root)
    return state


def _assert_runtime_preserved(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> None:
    before_db = before["database"]
    after_db = after["database"]
    for key in (
        "protected_tables",
        "track_ids",
        "track_identity",
        "manual_locked_fields",
        "manual_locked_credits",
    ):
        if before_db[key] != after_db[key]:
            raise Batch106Failure(f"batch10_6_preservation_failed_{key}")
    for table in (
        "tracks",
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
    ):
        if before_db["counts"].get(table) != after_db["counts"].get(table):
            raise Batch106Failure(f"batch10_6_preservation_count_failed_{table}")
    for table, prior in before_db["evidence_subsets"].items():
        if table == "metadata_intelligence_items":
            # The selected terminal item is intentionally replaced by its
            # normalized accepted outcome inside the repair transaction.
            continue
        current = after_db["evidence_subsets"].get(table)
        if current is None or not set(prior["row_hashes"]).issubset(
            current["row_hashes"]
        ):
            raise Batch106Failure(f"batch10_6_evidence_failed_{table}")
    gate105._assert_health(after_db["health"])
    for key in (
        "media",
        "portraits",
        "cache_index",
        "credential_metadata",
        "runtime_metadata",
        "covers",
    ):
        if before[key] != after[key]:
            raise Batch106Failure(f"batch10_6_preservation_failed_{key}")


def _readonly_target_analysis(
    database: Path,
) -> tuple[dict[str, Any], tuple[OrientationRepairTarget, ...]]:
    with contextlib.closing(acceptance.readonly(database, immutable=False)) as conn:
        if int(conn.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
            raise Batch106Failure("batch10_6_schema_not_seven")
        integrity = conn.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or str(integrity[0]).casefold() != "ok":
            raise Batch106Failure("batch10_6_integrity_failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise Batch106Failure("batch10_6_foreign_key_failed")
        report, targets = discover_orientation_repair_targets(conn)
    return report.aggregate_dict(), targets


def _marker_present_readonly(database: Path) -> bool:
    with contextlib.closing(acceptance.readonly(database, immutable=False)) as conn:
        return orientation_repair_applied(conn)


def _dry_run_unguarded(
    *, project_root: Path, database: Path, cache_root: Path
) -> dict[str, Any]:
    root = Path(project_root).expanduser().resolve()
    db_path = Path(database).expanduser().resolve()
    image_root = Path(cache_root).expanduser().resolve()
    before = _capture(project_root=root, database=db_path, cache_root=image_root)
    report, targets = _readonly_target_analysis(db_path)
    after = _capture(project_root=root, database=db_path, cache_root=image_root)
    if before != after:
        raise Batch106Failure("batch10_6_dry_run_changed_runtime")
    if not bool(report["marker_present"]) and len(targets) != 1:
        raise Batch106Failure("batch10_6_target_count_not_one")
    return {
        "report_format_version": REPORT_FORMAT_VERSION,
        "dry_run": True,
        "source_runtime_unchanged": True,
        "proposal": report,
        "provider_requests": 0,
        "media_writes": 0,
        "tag_writes": 0,
        "artwork_requests": 0,
        "artist_photo_requests": 0,
        "youtube_requests": 0,
        "lyrics_requests": 0,
        "credential_contents_read": False,
        "private_values_emitted": False,
    }


def run_dry_run(
    *, project_root: Path, database: Path, cache_root: Path
) -> dict[str, Any]:
    """Run an immutable aggregate-only target discovery with a network guard."""

    gate105.ensure_execution_policy()
    with gate105._offline_guard() as evidence:
        result = _dry_run_unguarded(
            project_root=project_root,
            database=database,
            cache_root=cache_root,
        )
    result["network_evidence"] = evidence
    result["provider_requests"] = int(evidence["attempt_count"])
    return result


class _BoundedSession(requests.Session):
    """Count and allowlist HTTP calls without retaining URLs or query values."""

    def __init__(self, counter: dict[str, int]) -> None:
        super().__init__()
        self.trust_env = False
        self._counter = counter

    @staticmethod
    def _validate_url(url: object) -> None:
        parsed = urlparse(str(url))
        host = (parsed.hostname or "").casefold()
        path = parsed.path
        allowed = bool(
            host == "api.discogs.com"
            and (
                path == "/database/search"
                or re.fullmatch(r"/(?:releases|masters)/[1-9]\d*", path)
            )
            or host == "musicbrainz.org" and path == "/ws/2/recording/"
        )
        if parsed.scheme != "https" or not allowed:
            raise Batch106Failure("batch10_6_endpoint_not_allowed")

    def send(self, request: requests.PreparedRequest, **kwargs: Any):  # type: ignore[override]
        # Requests follows redirects by calling send() directly.  Enforce the
        # host/path policy and bound on every actual outbound request, not just
        # the caller's initial get().
        self._validate_url(request.url)
        self._counter["http"] = self._counter.get("http", 0) + 1
        if self._counter["http"] > MAX_HTTP_REQUESTS:
            raise Batch106Failure("batch10_6_request_bound_exceeded")
        return super().send(request, **kwargs)


def _query(target: OrientationRepairTarget, orientation: str) -> ProviderQuery:
    hypothesis = next(
        item for item in target.hypotheses if item.orientation == orientation
    )
    return ProviderQuery(
        title=hypothesis.title,
        artist=hypothesis.artist,
        duration_seconds=target.duration_seconds,
        version_type=hypothesis.version_type,
        version_label=hypothesis.version_label,
        year_hint=hypothesis.year_hint,
    )


def _resolution_from_decision(decision: object) -> OrientationResolution:
    selected = getattr(decision, "selected", None)
    candidate = getattr(decision, "selected_candidate", None)
    confidence = float(getattr(decision, "confidence", 0.0))
    if (
        selected is None
        or candidate is None
        or not bool(getattr(decision, "provider_confirmed", False))
        or confidence < 85.0
    ):
        raise Batch106Failure("batch10_6_no_coherent_provider_resolution")
    provider = str(getattr(candidate, "provider", "") or "").strip().casefold()
    if provider == "musicbrainz":
        provider = "musicbrainz"
    elif provider != "discogs":
        provider = "discogs" if hasattr(candidate, "master_id") else "musicbrainz"
    raw_field_scores = getattr(candidate, "field_scores", {})
    field_scores: dict[str, float] = {}
    if isinstance(raw_field_scores, Mapping):
        for name, raw_score in raw_field_scores.items():
            if str(name) not in _RESOLUTION_CONFIDENCE_FIELDS:
                continue
            try:
                score = float(raw_score)
            except (TypeError, ValueError, OverflowError):
                continue
            if 0.0 <= score <= 100.0:
                field_scores[str(name)] = score
    if provider == "musicbrainz":
        candidate_score = float(getattr(candidate, "score", 0.0) or 0.0)
        for name in ("title", "artist", "artist_credits"):
            field_scores.setdefault(name, candidate_score)
    if provider == "discogs" and getattr(candidate, "release_family_id", None):
        field_scores.setdefault(
            "provider_release_family_id",
            field_scores.get("discogs_master_id", 0.0),
        )
    credits = tuple(
        RepairArtistCredit(
            display_name=str(getattr(value, "name", "") or ""),
            role=str(getattr(value, "role", "primary") or "primary"),
            join_phrase=str(getattr(value, "join_phrase", "") or ""),
            entity_type=str(getattr(value, "entity_type", "unknown") or "unknown"),
            provider_artist_id=(
                str(getattr(value, "artist_id", "") or "").strip() or None
            ),
        )
        for value in (getattr(candidate, "artist_credits", ()) or ())
        if str(getattr(value, "name", "") or "").strip()
    )
    title = str(getattr(candidate, "title", "") or "").strip()
    artist = str(getattr(candidate, "artist", "") or "").strip()
    return OrientationResolution(
        provider=provider,
        selected_orientation=str(getattr(selected, "orientation", "")),
        title=title,
        artist=artist,
        coherent=True,
        confidence=confidence,
        field_confidences=tuple(sorted(field_scores.items())),
        orientation_evaluated_count=int(
            getattr(decision, "evaluated_count", 0)
        ),
        orientation_reasons=tuple(getattr(decision, "reasons", ()) or ()),
        provider_confirmed=bool(getattr(decision, "provider_confirmed", False)),
        requires_provider_adjudication=bool(
            getattr(decision, "requires_provider_adjudication", True)
        ),
        discogs_queries=int(getattr(decision, "discogs_queries", 0)),
        musicbrainz_queries=int(getattr(decision, "musicbrainz_queries", 0)),
        provider_reference=(
            str(getattr(candidate, "provider_reference", "") or "").strip()
            or None
        ),
        artist_credits=credits,
        album=getattr(candidate, "album", None),
        album_artist=getattr(candidate, "album_artist", None),
        release_date=getattr(candidate, "release_date", None),
        original_release_date=(
            getattr(candidate, "original_release_date", None)
            or getattr(candidate, "release_date", None)
        ),
        version_type=(
            getattr(candidate, "version_type", None)
            or getattr(selected, "version_type", None)
        ),
        version_label=(
            getattr(candidate, "version_label", None)
            or getattr(selected, "version_label", None)
        ),
        discogs_release_id=(
            getattr(candidate, "release_id", None) if provider == "discogs" else None
        ),
        discogs_master_id=(
            getattr(candidate, "master_id", None) if provider == "discogs" else None
        ),
        discogs_track_position=(
            getattr(candidate, "track_position", None)
            if provider == "discogs"
            else None
        ),
        musicbrainz_recording_id=(
            getattr(candidate, "recording_id", None)
            if provider == "musicbrainz"
            else None
        ),
        musicbrainz_release_id=(
            getattr(candidate, "release_id", None)
            if provider == "musicbrainz"
            else None
        ),
        musicbrainz_release_group_id=(
            getattr(candidate, "release_group_id", None)
            if provider == "musicbrainz"
            else None
        ),
        provider_release_family_id=getattr(candidate, "release_family_id", None),
        release_country=getattr(candidate, "country", None),
        release_format=getattr(candidate, "release_format", None),
        label_name=getattr(candidate, "label", None),
    )


def _real_provider_lookup(
    target: OrientationRepairTarget,
    *,
    project_root: Path,
) -> TargetedLookupResult:
    """Perform at most two Discogs orientation searches and one MB fallback."""

    if os.environ.get(NO_NETWORK_ENVIRONMENT) == "1" or os.environ.get(
        NO_SECRETS_ENVIRONMENT
    ) == "1":
        raise Batch106Failure("batch10_6_live_provider_policy_blocked")
    token = DiscogsTokenStore(project_root / "data" / "discogs_token.txt").read()
    if not token:
        raise Batch106Failure("batch10_6_discogs_token_unavailable")
    counter: dict[str, int] = {"http": 0}
    session = _BoundedSession(counter)
    limiter = DiscogsRateLimiter()
    discogs = DiscogsProvider(token, session=session, rate_limiter=limiter)
    hypotheses = tuple(target.hypotheses)
    left = "left_is_artist"
    right = "right_is_artist"
    results: dict[str, object] = {}
    results[left] = discogs.search_releases(
        _query(target, left), max_pages=1, max_candidates=3
    )
    decision = choose_orientation(
        hypotheses,
        results,
        current_artist=target.current_artist,
        current_title=target.current_title,
        local_duration=target.duration_seconds,
    )
    first_conclusive = bool(
        getattr(decision, "provider_confirmed", False)
        and "conclusive_first_discogs_orientation" in getattr(decision, "reasons", ())
    )
    if not first_conclusive:
        results[right] = discogs.search_releases(
            _query(target, right), max_pages=1, max_candidates=3
        )
        decision = choose_orientation(
            hypotheses,
            results,
            current_artist=target.current_artist,
            current_title=target.current_title,
            local_duration=target.duration_seconds,
        )

    musicbrainz_searches = 0
    if not bool(getattr(decision, "provider_confirmed", False)):
        assessments = tuple(getattr(decision, "assessments", ()) or ())
        orientation = (
            max(assessments, key=lambda item: float(item.score)).orientation
            if assessments
            else right
        )
        query = _query(target, orientation)
        mb = MusicBrainzProvider(session=session)
        candidates = mb.search(query.title, query.artist)
        musicbrainz_searches = 1
        decision = choose_orientation(
            hypotheses,
            results,
            musicbrainz_candidate=candidates,
            musicbrainz_orientation=orientation,
            current_artist=target.current_artist,
            current_title=target.current_title,
            local_duration=target.duration_seconds,
        )
    resolution = _resolution_from_decision(decision)
    return TargetedLookupResult(
        resolution=resolution,
        request_count=int(counter["http"]),
        discogs_orientation_searches=len(results),
        musicbrainz_searches=musicbrainz_searches,
    )


def _verified_backup(
    *, database: Path, backup: Path, baseline: Mapping[str, Any]
) -> dict[str, Any]:
    report = gate105._verified_database_backup(
        database=database,
        backup=backup,
        baseline=baseline,
    )
    if int(report["schema_version"]) != SCHEMA_VERSION:
        raise Batch106Failure("batch10_6_backup_schema_failed")
    return report


def _rehearse_on_clone(
    database: Path,
    target: OrientationRepairTarget,
    resolution: OrientationResolution,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="MusicVault_Batch10_6_Rehearsal_") as temp:
        clone = Path(temp) / "music_vault.sqlite3"
        source = acceptance.readonly(database, immutable=False)
        destination = sqlite3.connect(clone)
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
            source.close()
        conn = sqlite3.connect(clone)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            result = apply_orientation_repair(
                conn, target=target, resolution=resolution
            )
            second = apply_orientation_repair(conn)
            if not second.no_op:
                raise Batch106Failure("batch10_6_rehearsal_not_idempotent")
            if conn.execute("PRAGMA foreign_key_check").fetchall():
                raise Batch106Failure("batch10_6_rehearsal_foreign_key_failed")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]).casefold() != "ok":
                raise Batch106Failure("batch10_6_rehearsal_integrity_failed")
        finally:
            conn.close()
    return result.aggregate_dict()


def apply_live_repair(
    *,
    project_root: Path,
    database: Path,
    cache_root: Path,
    acknowledgement: str,
    provider_lookup: ProviderLookup | None = None,
) -> dict[str, Any]:
    """Perform the explicitly acknowledged one-target lookup and repair."""

    if acknowledgement != LIVE_ACKNOWLEDGEMENT:
        raise Batch106Failure("batch10_6_live_acknowledgement_missing")
    root = Path(project_root).expanduser().resolve()
    db_path = Path(database).expanduser().resolve()
    image_root = Path(cache_root).expanduser().resolve()
    # Marker-first idempotence: do not inventory the library, discover a
    # target, create a backup, read a token, or construct a provider again.
    if _marker_present_readonly(db_path):
        return {
            "report_format_version": REPORT_FORMAT_VERSION,
            "dry_run": False,
            "no_op": True,
            "marker_present": True,
            "backups_created": 0,
            "provider_requests": 0,
            "targets_repaired": 0,
        }
    baseline = _capture(project_root=root, database=db_path, cache_root=image_root)
    proposal, targets = _readonly_target_analysis(db_path)
    if bool(proposal["marker_present"]):
        raise Batch106Failure("batch10_6_marker_changed_during_preflight")
    if len(targets) != 1:
        raise Batch106Failure("batch10_6_target_count_not_one")
    target = targets[0]

    backup = (
        root
        / "data"
        / "backups"
        / f"{DATABASE_BACKUP_PREFIX}{_utc_stamp()}.sqlite3"
    )
    backup_report = _verified_backup(
        database=db_path, backup=backup, baseline=baseline
    )

    lookup = provider_lookup or (
        lambda selected: _real_provider_lookup(selected, project_root=root)
    )
    raw_lookup = lookup(target)
    if isinstance(raw_lookup, OrientationResolution):
        lookup_result = TargetedLookupResult(
            raw_lookup,
            0,
            int(raw_lookup.discogs_queries),
            int(raw_lookup.musicbrainz_queries),
        )
    elif isinstance(raw_lookup, TargetedLookupResult):
        lookup_result = raw_lookup
    else:
        raise Batch106Failure("batch10_6_provider_result_invalid")
    if not 0 <= int(lookup_result.request_count) <= MAX_HTTP_REQUESTS:
        raise Batch106Failure("batch10_6_request_bound_exceeded")
    if not 0 <= int(lookup_result.discogs_orientation_searches) <= 2:
        raise Batch106Failure("batch10_6_discogs_search_bound_exceeded")
    if not 0 <= int(lookup_result.musicbrainz_searches) <= 1:
        raise Batch106Failure("batch10_6_musicbrainz_search_bound_exceeded")
    if (
        int(lookup_result.discogs_orientation_searches)
        != int(lookup_result.resolution.discogs_queries)
        or int(lookup_result.musicbrainz_searches)
        != int(lookup_result.resolution.musicbrainz_queries)
    ):
        raise Batch106Failure("batch10_6_lookup_evidence_mismatch")

    after_lookup = _capture(
        project_root=root, database=db_path, cache_root=image_root
    )
    if baseline != after_lookup:
        raise Batch106Failure("batch10_6_provider_lookup_changed_runtime")
    rehearsal = _rehearse_on_clone(
        db_path, target, lookup_result.resolution
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        applied = apply_orientation_repair(
            conn, target=target, resolution=lookup_result.resolution
        )
        second = apply_orientation_repair(conn)
    finally:
        conn.close()
    if not second.no_op:
        raise Batch106Failure("batch10_6_second_run_not_noop")

    current = _capture(project_root=root, database=db_path, cache_root=image_root)
    _assert_runtime_preserved(baseline, current)
    with contextlib.closing(acceptance.readonly(db_path, immutable=False)) as check:
        if not orientation_repair_applied(check):
            raise Batch106Failure("batch10_6_marker_missing")
        if int(check.execute("PRAGMA user_version").fetchone()[0]) != SCHEMA_VERSION:
            raise Batch106Failure("batch10_6_schema_changed")
        integrity = str(check.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_key_issues = len(check.execute("PRAGMA foreign_key_check").fetchall())
        review_count = int(
            check.execute(
                "SELECT COUNT(*) FROM metadata_intelligence_items "
                "WHERE state IN ('review','ready')"
            ).fetchone()[0]
        )
    if integrity.casefold() != "ok" or foreign_key_issues:
        raise Batch106Failure("batch10_6_post_apply_health_failed")
    if review_count:
        raise Batch106Failure("batch10_6_review_count_nonzero")
    return {
        "report_format_version": REPORT_FORMAT_VERSION,
        "dry_run": False,
        "no_op": False,
        "proposal": proposal,
        "database_backup": {
            **backup_report,
            "name": backup.name,
        },
        "rehearsal": rehearsal,
        "repair": applied.aggregate_dict(),
        "schema_version": SCHEMA_VERSION,
        "integrity_ok": True,
        "foreign_key_issue_count": 0,
        "review_count": review_count,
        "marker_present": True,
        "second_run_no_op": True,
        "provider_requests": int(lookup_result.request_count),
        "discogs_orientation_searches": int(
            lookup_result.discogs_orientation_searches
        ),
        "musicbrainz_searches": int(lookup_result.musicbrainz_searches),
        "tracks_looked_up": 1,
        "media_unchanged": baseline["media"] == current["media"],
        "cover_files_unchanged": baseline["covers"] == current["covers"],
        "portrait_cache_unchanged": (
            baseline["portraits"] == current["portraits"]
            and baseline["cache_index"] == current["cache_index"]
        ),
        "credentials_unchanged": (
            baseline["credential_metadata"] == current["credential_metadata"]
        ),
        "credential_contents_emitted": False,
        "private_values_emitted": False,
        "media_writes": 0,
        "tag_writes": 0,
        "artwork_requests": 0,
        "artist_photo_requests": 0,
        "youtube_requests": 0,
        "lyrics_requests": 0,
    }


def ensure_music_vault_closed() -> None:
    gate105.ensure_music_vault_closed()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("dry-run", "apply-live"):
        item = subparsers.add_parser(command)
        item.add_argument("--project-root", type=Path, required=True)
        item.add_argument("--database", type=Path, required=True)
        item.add_argument("--cache-root", type=Path, required=True)
        if command == "apply-live":
            item.add_argument("--acknowledge-targeted-lookup", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        ensure_music_vault_closed()
        if args.command == "dry-run":
            result = run_dry_run(
                project_root=args.project_root,
                database=args.database,
                cache_root=args.cache_root,
            )
        else:
            result = apply_live_repair(
                project_root=args.project_root,
                database=args.database,
                cache_root=args.cache_root,
                acknowledgement=args.acknowledge_targeted_lookup,
            )
        print(json.dumps({"ok": True, **result}, sort_keys=True))
        return 0
    except Exception:
        print(json.dumps({"ok": False, "error_code": "batch10_6_acceptance_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

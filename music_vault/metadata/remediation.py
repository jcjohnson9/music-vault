from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import unicodedata
import uuid
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from music_vault.core.paths import (
    cover_art_archive_dir,
    metadata_job_backups_dir,
    metadata_reports_dir,
)
from music_vault.core.safety import sanitize_error_text

from .artwork import (
    CoverArtArchiveProvider,
    normalize_artwork_for_embedding,
    store_prepared_artwork,
)
from .musicbrainz_enricher import MetadataProviderError, MusicBrainzProvider
from .remediation_schema import (
    PROVIDER_CACHE_TABLE,
    REMEDIATION_ITEMS_TABLE,
    REMEDIATION_JOBS_TABLE,
)
from .service import EffectiveMetadataSnapshot, MetadataService
from .tag_writer import (
    MediaBackup,
    SafeTagWriter,
    TagWriteResult,
    full_file_sha256,
)


_CACHE_TTL = timedelta(days=30)
_FAILED_CACHE_TTL = timedelta(minutes=15)
_PROVIDER_RETRY_ATTEMPTS = 3
_ANALYSIS_FINAL_STATUSES = frozenset(
    {"high_confidence", "needs_review", "ambiguous", "no_match", "skipped", "failed"}
)
_PROTECTED_PROVENANCE = frozenset(
    {"manual", "musicbrainz_confirmed", "provider_confirmed"}
)
_REVIEW_FIELDS = frozenset(
    {"title", "artist", "album", "album_artist", "release_date", "artwork"}
)


class RemediationError(RuntimeError):
    """A deliberately sanitized remediation lifecycle failure."""


@dataclass(frozen=True)
class JobSummary:
    id: str
    status: str
    mode: str
    provider: str
    library_revision: str
    total: int
    analyzed: int
    high_confidence: int
    needs_review: int
    ambiguous: int
    no_match: int
    skipped: int
    failed: int
    applied: int
    file_written: int
    rolled_back: int
    last_error: str | None

    def aggregate_dict(self) -> dict[str, object]:
        return {
            "job_id": self.id,
            "status": self.status,
            "total": self.total,
            "analyzed": self.analyzed,
            "high_confidence": self.high_confidence,
            "needs_review": self.needs_review,
            "ambiguous": self.ambiguous,
            "no_match": self.no_match,
            "skipped": self.skipped,
            "failed": self.failed,
            "applied": self.applied,
            "file_written": self.file_written,
            "rolled_back": self.rolled_back,
        }


@dataclass(frozen=True)
class ApplyEstimate:
    database_updates: int
    file_writes: int
    artwork_replacements: int
    backup_bytes: int
    temporary_bytes: int
    required_with_headroom: int
    review_items: int
    unchanged_items: int

    def aggregate_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderMetrics:
    provider_requests: int = 0
    cache_hits: int = 0
    elapsed_provider_seconds: float = 0.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        decoded = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _json_list(value: object) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        decoded = json.loads(str(value or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return decoded if isinstance(decoded, list) else []


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise RemediationError("private_report_write_failed") from exc


def _enum_text(value: object) -> str:
    candidate = getattr(value, "value", value)
    return str(candidate or "").strip().casefold()


def _candidate_value(candidate: object, name: str, default: object = None) -> object:
    if isinstance(candidate, Mapping):
        return candidate.get(name, default)
    return getattr(candidate, name, default)


def _candidate_dict(candidate: object) -> dict[str, object]:
    allowed = (
        "title",
        "artist",
        "album",
        "album_artist",
        "release_date",
        "recording_id",
        "release_id",
        "score",
        "duration_seconds",
        "duration_ms",
        "country",
        "release_status",
        "artwork_available",
        "provider",
        "provider_order",
    )
    return {
        name: _candidate_value(candidate, name)
        for name in allowed
        if _candidate_value(candidate, name) is not None
    }


def candidate_review_token(candidate: object) -> str:
    """Return a private, stable token binding approval to reviewed evidence."""

    payload = dict(candidate) if isinstance(candidate, Mapping) else {}
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _snapshot_dict(snapshot: EffectiveMetadataSnapshot, track: Mapping[str, object]) -> dict:
    fields = {
        name: {
            "value": state.value,
            "provenance": state.provenance,
            "provider_reference": state.provider_reference,
            "confidence": state.confidence,
            "is_manual": state.is_manual,
            "is_locked": state.is_locked,
        }
        for name, state in snapshot.fields.items()
    }
    path = Path(snapshot.path)
    try:
        stat = path.stat()
        file_size = int(stat.st_size)
        file_mtime_ns = int(stat.st_mtime_ns)
    except OSError:
        file_size = None
        file_mtime_ns = None
    return {
        "track_id": snapshot.track_id,
        "path": snapshot.path,
        "source_kind": snapshot.source_kind,
        "source_video_id": snapshot.source_video_id,
        "source_upload_date": snapshot.source_upload_date,
        "musicbrainz_recording_id": snapshot.musicbrainz_recording_id,
        "musicbrainz_release_id": snapshot.musicbrainz_release_id,
        "metadata_updated_at": snapshot.metadata_updated_at,
        "duration_seconds": track.get("duration_seconds"),
        "file_size": file_size,
        "file_mtime_ns": file_mtime_ns,
        "fields": fields,
    }


class RemediationService:
    """Resumable analysis/apply/rollback coordinator shared by UI and tooling."""

    def __init__(
        self,
        database: Any,
        *,
        metadata_service: MetadataService | None = None,
        provider: object | None = None,
        cover_provider: object | None = None,
        tag_writer: SafeTagWriter | None = None,
        reports_root: str | Path | None = None,
        backups_root: str | Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.db = database
        self.conn: sqlite3.Connection = database.conn
        self.metadata = metadata_service or MetadataService(database)
        self.provider = provider or MusicBrainzProvider()
        self.cover_provider = cover_provider or CoverArtArchiveProvider()
        self.tag_writer = tag_writer or SafeTagWriter()
        self.reports_root = Path(reports_root) if reports_root else metadata_reports_dir()
        self.backups_root = Path(backups_root) if backups_root else metadata_job_backups_dir()
        self.sleep = sleep

    def _report_dir(self, job_id: str) -> Path:
        return self.reports_root / str(job_id)

    def _job_row(self, job_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            f"SELECT * FROM {REMEDIATION_JOBS_TABLE} WHERE id=?", (str(job_id),)
        ).fetchone()
        if row is None:
            raise RemediationError("remediation_job_not_found")
        return row

    def latest_job(self) -> JobSummary | None:
        row = self.conn.execute(
            f"SELECT * FROM {REMEDIATION_JOBS_TABLE} ORDER BY updated_at DESC, created_at DESC LIMIT 1"
        ).fetchone()
        return self._summary(row) if row is not None else None

    @staticmethod
    def _summary(row: Mapping[str, object]) -> JobSummary:
        return JobSummary(
            id=str(row["id"]),
            status=str(row["status"]),
            mode=str(row["mode"]),
            provider=str(row["provider"]),
            library_revision=str(row["library_revision"]),
            total=int(row["total_items"] or 0),
            analyzed=int(row["analyzed_items"] or 0),
            high_confidence=int(row["high_confidence_items"] or 0),
            needs_review=int(row["review_items"] or 0),
            ambiguous=int(row["ambiguous_items"] or 0),
            no_match=int(row["no_match_items"] or 0),
            skipped=int(row["skipped_items"] or 0),
            failed=int(row["failed_items"] or 0),
            applied=int(row["applied_items"] or 0),
            file_written=int(row["file_written_items"] or 0),
            rolled_back=int(row["rolled_back_items"] or 0),
            last_error=(str(row["last_error"]) if row["last_error"] else None),
        )

    def status(self, job_id: str | None = None) -> JobSummary | None:
        return self._summary(self._job_row(job_id)) if job_id else self.latest_job()

    def list_items(
        self,
        job_id: str,
        *,
        confidence_class: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, object]]:
        self._job_row(job_id)
        query = f"SELECT * FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=?"
        values: list[object] = [str(job_id)]
        if confidence_class:
            query += " AND confidence_class=?"
            values.append(str(confidence_class))
        query += " ORDER BY confidence_score DESC, id LIMIT ?"
        values.append(max(1, min(int(limit), 5000)))
        rows = self.conn.execute(query, values).fetchall()
        decoded: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            for name in (
                "current_snapshot",
                "proposed_patch",
                "candidate_snapshot",
                "match_reasons",
                "artwork_candidate",
                "approved_fields",
            ):
                if item.get(name):
                    try:
                        item[name] = json.loads(str(item[name]))
                    except (ValueError, json.JSONDecodeError):
                        item[name] = None
            decoded.append(item)
        return decoded

    def library_revision(self) -> str:
        digest = hashlib.sha256()
        rows = self.conn.execute(
            """
            SELECT id, path, duration_seconds, metadata_updated_at, source_kind,
                   source_video_id, musicbrainz_recording_id, musicbrainz_release_id
            FROM tracks ORDER BY id
            """
        ).fetchall()
        for row in rows:
            path = Path(str(row["path"]))
            try:
                stat = path.stat()
                file_state = (int(stat.st_size), int(stat.st_mtime_ns))
            except OSError:
                file_state = (None, None)
            values = (
                int(row["id"]),
                str(path),
                row["duration_seconds"],
                row["metadata_updated_at"],
                row["source_kind"],
                row["source_video_id"],
                row["musicbrainz_recording_id"],
                row["musicbrainz_release_id"],
                *file_state,
            )
            digest.update(_json(values).encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def create_job(self, *, mode: str = "dry_run", reuse: bool = True) -> JobSummary:
        revision = self.library_revision()
        if reuse:
            row = self.conn.execute(
                f"""
                SELECT * FROM {REMEDIATION_JOBS_TABLE}
                WHERE library_revision=?
                  AND status IN ('created','analyzing','paused','ready')
                ORDER BY created_at DESC LIMIT 1
                """,
                (revision,),
            ).fetchone()
            if row is not None:
                return self._summary(row)
        job_id = uuid.uuid4().hex
        now = _utc_now()
        total = int(self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0])
        report_dir = self._report_dir(job_id)
        report_dir.mkdir(parents=True, exist_ok=True)
        with self.conn:
            self.conn.execute(
                f"""
                INSERT INTO {REMEDIATION_JOBS_TABLE} (
                    id, created_at, updated_at, status, mode, provider,
                    library_revision, total_items, private_report_path
                ) VALUES (?, ?, ?, 'created', ?, 'musicbrainz', ?, ?, ?)
                """,
                (job_id, now, now, str(mode), revision, total, str(report_dir.resolve())),
            )
        self._write_reports(job_id, ProviderMetrics())
        return self._summary(self._job_row(job_id))

    def _set_job_status(
        self,
        job_id: str,
        status: str,
        *,
        error: str | None = None,
        start: bool = False,
        finish: bool = False,
    ) -> None:
        now = _utc_now()
        assignments = ["status=?", "updated_at=?", "last_error=?"]
        values: list[object] = [status, now, sanitize_error_text(error, 300) if error else None]
        if start:
            assignments.append("started_at=COALESCE(started_at, ?)")
            values.append(now)
        if finish:
            assignments.append("finished_at=?")
            values.append(now)
        values.append(str(job_id))
        with self.conn:
            self.conn.execute(
                f"UPDATE {REMEDIATION_JOBS_TABLE} SET {', '.join(assignments)} WHERE id=?",
                values,
            )

    def pause(self, job_id: str) -> JobSummary:
        row = self._job_row(job_id)
        if str(row["status"]) not in {"created", "analyzing"}:
            raise RemediationError("remediation_job_cannot_pause")
        self._set_job_status(job_id, "paused")
        return self._summary(self._job_row(job_id))

    def cancel(self, job_id: str) -> JobSummary:
        row = self._job_row(job_id)
        if str(row["status"]) in {"complete", "rolled_back"}:
            raise RemediationError("remediation_job_cannot_cancel")
        self._set_job_status(job_id, "cancelled", finish=True)
        return self._summary(self._job_row(job_id))

    def _refresh_counts(self, job_id: str) -> JobSummary:
        row = self.conn.execute(
            f"""
            SELECT
                COUNT(*) AS item_count,
                SUM(CASE WHEN confidence_class IS NOT NULL THEN 1 ELSE 0 END) AS analyzed,
                SUM(CASE WHEN confidence_class='high_confidence' THEN 1 ELSE 0 END) AS high_count,
                SUM(CASE WHEN confidence_class='needs_review' THEN 1 ELSE 0 END) AS review_count,
                SUM(CASE WHEN confidence_class='ambiguous' THEN 1 ELSE 0 END) AS ambiguous_count,
                SUM(CASE WHEN confidence_class='no_match' THEN 1 ELSE 0 END) AS no_match_count,
                SUM(CASE WHEN confidence_class='skipped' THEN 1 ELSE 0 END) AS skipped_count,
                SUM(CASE WHEN confidence_class='failed' OR status IN ('failed','apply_failed') THEN 1 ELSE 0 END) AS failed_count,
                SUM(CASE WHEN status='applied' THEN 1 ELSE 0 END) AS applied_count,
                SUM(CASE WHEN file_write_status='verified' THEN 1 ELSE 0 END) AS file_count,
                SUM(CASE WHEN status='rolled_back' THEN 1 ELSE 0 END) AS rollback_count
            FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=?
            """,
            (str(job_id),),
        ).fetchone()
        now = _utc_now()
        with self.conn:
            self.conn.execute(
                f"""
                UPDATE {REMEDIATION_JOBS_TABLE} SET
                    updated_at=?, analyzed_items=?, high_confidence_items=?,
                    review_items=?, ambiguous_items=?, no_match_items=?,
                    skipped_items=?, failed_items=?, applied_items=?,
                    file_written_items=?, rolled_back_items=?
                WHERE id=?
                """,
                (
                    now,
                    int(row["analyzed"] or 0),
                    int(row["high_count"] or 0),
                    int(row["review_count"] or 0),
                    int(row["ambiguous_count"] or 0),
                    int(row["no_match_count"] or 0),
                    int(row["skipped_count"] or 0),
                    int(row["failed_count"] or 0),
                    int(row["applied_count"] or 0),
                    int(row["file_count"] or 0),
                    int(row["rollback_count"] or 0),
                    str(job_id),
                ),
            )
        return self._summary(self._job_row(job_id))

    @staticmethod
    def _query_key(title: str, artist: str | None, duration: float | None) -> str:
        from .matching import clean_presentation_suffixes, normalize_for_comparison

        cleaned_title = clean_presentation_suffixes(title)
        strict_title = " ".join(
            unicodedata.normalize("NFKC", cleaned_title).casefold().split()
        )
        strict_artist = " ".join(
            unicodedata.normalize("NFKC", artist or "").casefold().split()
        )
        normalized = (
            normalize_for_comparison(cleaned_title, title=True),
            normalize_for_comparison(artist or ""),
            strict_title,
            strict_artist,
            round(float(duration), 1) if duration is not None else None,
        )
        return hashlib.sha256(_json(normalized).encode("utf-8")).hexdigest()

    def _cache_get(self, query_key: str) -> tuple[list[dict[str, object]], str] | None:
        row = self.conn.execute(
            f"SELECT * FROM {PROVIDER_CACHE_TABLE} WHERE provider='musicbrainz' AND normalized_query_key=?",
            (query_key,),
        ).fetchone()
        if row is None:
            return None
        expiry = _parse_utc(row["expires_at"])
        if expiry is None or expiry <= datetime.now(timezone.utc):
            return None
        data = _json_list(row["candidate_data"])
        candidates = [dict(value) for value in data if isinstance(value, Mapping)]
        return candidates, str(row["response_status"])

    def _cache_put(
        self,
        query_key: str,
        candidates: Sequence[object],
        *,
        status: str,
    ) -> None:
        now = datetime.now(timezone.utc)
        ttl = _FAILED_CACHE_TTL if status == "failed" else _CACHE_TTL
        payload = [_candidate_dict(candidate) for candidate in candidates]
        with self.conn:
            self.conn.execute(
                f"""
                INSERT INTO {PROVIDER_CACHE_TABLE} (
                    provider, normalized_query_key, response_status,
                    candidate_data, fetched_at, expires_at
                ) VALUES ('musicbrainz', ?, ?, ?, ?, ?)
                ON CONFLICT(provider, normalized_query_key) DO UPDATE SET
                    response_status=excluded.response_status,
                    candidate_data=excluded.candidate_data,
                    fetched_at=excluded.fetched_at,
                    expires_at=excluded.expires_at
                """,
                (
                    query_key,
                    status,
                    _json(payload),
                    now.isoformat().replace("+00:00", "Z"),
                    (now + ttl).isoformat().replace("+00:00", "Z"),
                ),
            )

    def _provider_candidates(
        self,
        title: str,
        artist: str | None,
        duration: float | None,
        metrics: ProviderMetrics,
    ) -> tuple[list[object], ProviderMetrics, str | None]:
        from .matching import normalize_query

        key = self._query_key(title, artist, duration)
        cached = self._cache_get(key)
        if cached is not None:
            candidates, status = cached
            return (
                candidates,
                ProviderMetrics(
                    metrics.provider_requests,
                    metrics.cache_hits + 1,
                    metrics.elapsed_provider_seconds,
                ),
                ("provider_cached_failure" if status == "failed" else None),
            )
        started = time.monotonic()
        last_error: str | None = None
        requests_made = 0
        query = normalize_query(title, artist)
        query_title = query.title
        query_artist = query.artist or None
        for attempt in range(_PROVIDER_RETRY_ATTEMPTS):
            try:
                requests_made += 1
                found = self.provider.search(query_title, query_artist)
                candidates = list(found or [])
                status = "success" if candidates else "no_match"
                self._cache_put(key, candidates, status=status)
                elapsed = time.monotonic() - started
                return (
                    candidates,
                    ProviderMetrics(
                        metrics.provider_requests + requests_made,
                        metrics.cache_hits,
                        metrics.elapsed_provider_seconds + elapsed,
                    ),
                    None,
                )
            except MetadataProviderError as exc:
                last_error = sanitize_error_text(exc, 120)
                if attempt + 1 < _PROVIDER_RETRY_ATTEMPTS:
                    self.sleep(min(2.0, 0.5 * (2**attempt)))
            except Exception:
                last_error = "provider_request_failed"
                break
        self._cache_put(key, (), status="failed")
        elapsed = time.monotonic() - started
        return (
            [],
            ProviderMetrics(
                metrics.provider_requests + requests_made,
                metrics.cache_hits,
                metrics.elapsed_provider_seconds + elapsed,
            ),
            last_error or "provider_unavailable",
        )

    @staticmethod
    def _locked_complete(snapshot: EffectiveMetadataSnapshot) -> bool:
        required = ("title", "artist")
        if not all(
            snapshot.fields[name].value and snapshot.fields[name].is_locked
            for name in required
        ):
            return False
        populated = [state for state in snapshot.fields.values() if state.value]
        return len(populated) >= 3 and all(
            state.is_locked for state in populated if state.field_name != "artwork"
        )

    @staticmethod
    def _field_allowed(result: object, field_name: str, default: bool = False) -> bool:
        selected = getattr(result, "selected", None)
        decisions = getattr(
            selected, "field_decisions", getattr(result, "field_decisions", None)
        )
        if isinstance(decisions, Mapping):
            decision = decisions.get(field_name)
            if isinstance(decision, Mapping):
                return bool(decision.get("apply") or decision.get("auto_apply"))
            if decision is not None:
                return bool(
                    getattr(decision, "apply", getattr(decision, "auto_apply", default))
                )
        if isinstance(decisions, Sequence):
            for decision in decisions:
                if str(getattr(decision, "field_name", "")) != field_name:
                    continue
                safe = bool(
                    getattr(
                        decision,
                        "safe_to_apply",
                        getattr(decision, "recommended", default),
                    )
                )
                confidence = _enum_text(getattr(decision, "confidence", ""))
                return safe or confidence == "exact"
        return default

    @staticmethod
    def _best_candidate(result: object, candidates: Sequence[object]) -> object | None:
        selected = getattr(result, "selected", None)
        if selected is not None:
            index = getattr(selected, "candidate_index", None)
            if isinstance(index, int) and 0 <= index < len(candidates):
                return candidates[index]
        for name in ("best_candidate", "candidate", "best"):
            candidate = getattr(result, name, None)
            if candidate is not None:
                return candidate
        return candidates[0] if candidates else None

    def _assessment(
        self,
        snapshot: EffectiveMetadataSnapshot,
        duration: float | None,
        candidates: Sequence[object],
        *,
        query_title: str | None = None,
        query_artist: str | None = None,
    ) -> tuple[str, float | None, list[str], dict, dict, dict | None]:
        from .matching import TrackQuery, classify_candidates

        locked_fields = frozenset(
            name for name, state in snapshot.fields.items() if state.is_locked
        )
        query = TrackQuery(
            title=query_title if query_title is not None else snapshot.value("title") or "",
            artist=query_artist if query_artist is not None else snapshot.value("artist"),
            album=snapshot.value("album"),
            album_artist=snapshot.value("album_artist"),
            release_date=snapshot.value("release_date"),
            duration_seconds=float(duration) if duration is not None else None,
            recording_id=snapshot.musicbrainz_recording_id,
            release_id=snapshot.musicbrainz_release_id,
        )
        result = classify_candidates(query, candidates, locked_fields=locked_fields)
        classification = _enum_text(getattr(result, "classification", "failed"))
        if classification == "review":
            classification = "needs_review"
        selected_assessment = getattr(result, "selected", None)
        score_value = getattr(
            selected_assessment,
            "match_score",
            getattr(result, "confidence_score", getattr(result, "score", None)),
        )
        score = float(score_value) if score_value is not None else None
        raw_reasons = getattr(result, "reasons", ()) or ()
        reasons = [sanitize_error_text(value, 120) for value in raw_reasons][:20]
        best = self._best_candidate(result, candidates)
        candidate = _candidate_dict(best) if best is not None else {}
        if candidate:
            candidate["alternatives"] = [_candidate_dict(value) for value in candidates[:10]]
            serialized_assessments = []
            for assessment in (getattr(result, "assessments", ()) or ())[:10]:
                serializer = getattr(assessment, "to_dict", None)
                if callable(serializer):
                    serialized_assessments.append(serializer(include_values=True))
            candidate["assessments"] = serialized_assessments
        proposed: dict[str, object] = {}
        artwork_candidate: dict[str, object] | None = None
        if classification == "high_confidence" and best is not None:
            selected_decisions = getattr(selected_assessment, "field_decisions", ()) or ()
            release_unambiguous = not any(
                getattr(decision, "reason", "") == "release_identity_ambiguous"
                for decision in selected_decisions
                if getattr(decision, "field_name", "")
                in {"album", "album_artist", "release_date", "artwork"}
            )
            release_official = (
                str(_candidate_value(best, "release_status") or "").strip().casefold()
                == "official"
            )
            release_confident = release_unambiguous and release_official
            candidate["release_confident"] = release_confident
            for field in ("title", "artist", "album", "album_artist", "release_date"):
                value = _candidate_value(best, field)
                default = field in {"title", "artist"}
                if field in {"album", "album_artist", "release_date"} and not release_confident:
                    continue
                allowed = default or self._field_allowed(result, field, False)
                if value not in (None, "") and allowed:
                    proposed[field] = value
            release_id = _candidate_value(best, "release_id")
            artwork_available = bool(_candidate_value(best, "artwork_available", False))
            if (
                release_id
                and artwork_available
                and "artwork" not in locked_fields
                and release_confident
            ):
                artwork_candidate = {
                    "provider": "cover_art_archive",
                    "release_id": release_id,
                }
        serialized_decisions: dict[str, object] = {}
        decisions = getattr(selected_assessment, "field_decisions", None)
        if isinstance(decisions, Mapping):
            for name, decision in decisions.items():
                if is_dataclass(decision):
                    serialized_decisions[str(name)] = asdict(decision)
                elif isinstance(decision, Mapping):
                    serialized_decisions[str(name)] = dict(decision)
        elif isinstance(decisions, Sequence):
            for decision in decisions:
                name = str(getattr(decision, "field_name", ""))
                if not name:
                    continue
                if hasattr(decision, "to_dict"):
                    serialized_decisions[name] = decision.to_dict(include_values=True)
                elif is_dataclass(decision):
                    serialized_decisions[name] = asdict(decision)
        candidate["field_decisions"] = serialized_decisions
        return classification, score, reasons, proposed, candidate, artwork_candidate

    def _upsert_analysis_item(
        self,
        job_id: str,
        track_id: int,
        *,
        status: str,
        snapshot: Mapping[str, object],
        proposed_patch: Mapping[str, object] | None = None,
        candidate_snapshot: Mapping[str, object] | None = None,
        confidence_score: float | None = None,
        reasons: Sequence[str] = (),
        recording_id: object = None,
        release_id: object = None,
        artwork_candidate: Mapping[str, object] | None = None,
        review_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        now = _utc_now()
        confidence_class = status if status in _ANALYSIS_FINAL_STATUSES else None
        with self.conn:
            self.conn.execute(
                f"""
                INSERT INTO {REMEDIATION_ITEMS_TABLE} (
                    job_id, track_id, status, current_snapshot, proposed_patch,
                    candidate_snapshot, confidence_score, confidence_class,
                    match_reasons, provider_recording_id, provider_release_id,
                    artwork_candidate, review_reason, apply_error,
                    file_write_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'not_requested', ?, ?)
                ON CONFLICT(job_id, track_id) DO UPDATE SET
                    status=excluded.status,
                    current_snapshot=excluded.current_snapshot,
                    proposed_patch=excluded.proposed_patch,
                    candidate_snapshot=excluded.candidate_snapshot,
                    confidence_score=excluded.confidence_score,
                    confidence_class=excluded.confidence_class,
                    match_reasons=excluded.match_reasons,
                    provider_recording_id=excluded.provider_recording_id,
                    provider_release_id=excluded.provider_release_id,
                    artwork_candidate=excluded.artwork_candidate,
                    review_reason=excluded.review_reason,
                    apply_error=excluded.apply_error,
                    updated_at=excluded.updated_at
                """,
                (
                    str(job_id),
                    int(track_id),
                    status,
                    _json(snapshot),
                    _json(proposed_patch or {}),
                    _json(candidate_snapshot or {}),
                    confidence_score,
                    confidence_class,
                    _json(list(reasons)),
                    str(recording_id).strip() if recording_id else None,
                    str(release_id).strip() if release_id else None,
                    _json(artwork_candidate) if artwork_candidate else None,
                    sanitize_error_text(review_reason, 300) if review_reason else None,
                    sanitize_error_text(error, 300) if error else None,
                    now,
                    now,
                ),
            )

    def analyze(
        self,
        job_id: str | None = None,
        *,
        progress: Callable[[JobSummary], None] | None = None,
    ) -> tuple[JobSummary, ProviderMetrics]:
        job = self.create_job() if job_id is None else self._summary(self._job_row(job_id))
        if job.status == "ready" and job.analyzed >= job.total:
            return job, self._load_metrics(job.id)
        if job.status in {"cancelled", "complete", "rolled_back"}:
            raise RemediationError("remediation_job_not_resumable")
        self._set_job_status(job.id, "analyzing", start=True)
        metrics = self._load_metrics(job.id)
        tracks = self.conn.execute("SELECT * FROM tracks ORDER BY id").fetchall()
        try:
            for track in tracks:
                live_status = str(self._job_row(job.id)["status"])
                if live_status == "paused":
                    summary = self._refresh_counts(job.id)
                    self._write_reports(job.id, metrics)
                    return summary, metrics
                if live_status == "cancelled":
                    summary = self._refresh_counts(job.id)
                    self._write_reports(job.id, metrics)
                    return summary, metrics
                existing = self.conn.execute(
                    f"SELECT status FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=? AND track_id=?",
                    (job.id, int(track["id"])),
                ).fetchone()
                if existing is not None and str(existing["status"]) in (
                    _ANALYSIS_FINAL_STATUSES
                    | {"applying", "applied", "apply_failed", "rolled_back", "conflict"}
                ):
                    continue
                snapshot = self.metadata.snapshot(int(track["id"]))
                private_snapshot = _snapshot_dict(snapshot, dict(track))
                if self._locked_complete(snapshot):
                    self._upsert_analysis_item(
                        job.id,
                        int(track["id"]),
                        status="skipped",
                        snapshot=private_snapshot,
                        reasons=("complete_locked_metadata",),
                        review_reason="skipped_by_lock_policy",
                    )
                else:
                    title = snapshot.value("title") or ""
                    artist = snapshot.value("artist")
                    duration = (
                        float(track["duration_seconds"])
                        if track["duration_seconds"] is not None
                        else None
                    )
                    if not title.strip() or not str(artist or "").strip():
                        self._upsert_analysis_item(
                            job.id,
                            int(track["id"]),
                            status="skipped",
                            snapshot=private_snapshot,
                            reasons=("missing_required_query_metadata",),
                            review_reason="skipped_by_query_policy",
                        )
                        summary = self._refresh_counts(job.id)
                        if progress is not None:
                            progress(summary)
                        continue
                    candidates, metrics, provider_error = self._provider_candidates(
                        title, artist, duration, metrics
                    )
                    if provider_error:
                        self._upsert_analysis_item(
                            job.id,
                            int(track["id"]),
                            status="failed",
                            snapshot=private_snapshot,
                            reasons=("provider_failure",),
                            review_reason="provider_failure",
                            error=provider_error,
                        )
                    else:
                        (
                            classification,
                            score,
                            reasons,
                            proposed,
                            candidate,
                            artwork_candidate,
                        ) = self._assessment(snapshot, duration, candidates)
                        best = self._best_candidate(
                            type("Assessment", (), {"best_candidate": candidate})(),
                            [candidate] if candidate else [],
                        )
                        recording_id = _candidate_value(best, "recording_id") if best else None
                        release_fields_selected = bool(
                            set(proposed) & {"album", "album_artist", "release_date"}
                        ) or artwork_candidate is not None
                        release_id = (
                            _candidate_value(best, "release_id")
                            if best
                            and bool(candidate.get("release_confident"))
                            and release_fields_selected
                            else None
                        )
                        review_reason = (
                            None if classification == "high_confidence" else classification
                        )
                        self._upsert_analysis_item(
                            job.id,
                            int(track["id"]),
                            status=classification,
                            snapshot=private_snapshot,
                            proposed_patch=proposed,
                            candidate_snapshot=candidate,
                            confidence_score=score,
                            reasons=reasons,
                            recording_id=recording_id,
                            release_id=release_id,
                            artwork_candidate=artwork_candidate,
                            review_reason=review_reason,
                        )
                summary = self._refresh_counts(job.id)
                _atomic_json(self._report_dir(job.id) / "metrics.json", asdict(metrics))
                if progress is not None:
                    progress(summary)
            summary = self._refresh_counts(job.id)
            final_status = "failed" if summary.total and summary.failed == summary.total else "ready"
            self._set_job_status(job.id, final_status, finish=final_status == "failed")
            summary = self._refresh_counts(job.id)
            self._write_reports(job.id, metrics)
            return summary, metrics
        except Exception as exc:
            error = sanitize_error_text(exc, 300)
            self._set_job_status(job.id, "failed", error=error, finish=True)
            self._refresh_counts(job.id)
            self._write_reports(job.id, metrics)
            raise

    def resume(
        self,
        job_id: str,
        *,
        progress: Callable[[JobSummary], None] | None = None,
    ) -> tuple[JobSummary, ProviderMetrics]:
        row = self._job_row(job_id)
        status = str(row["status"])
        current_summary = self._summary(row)
        if status == "applying":
            mode = str(row["mode"])
            if mode in {"review_apply_files", "review_apply_database"}:
                pending = self.conn.execute(
                    f"""
                    SELECT id, approved_fields FROM {REMEDIATION_ITEMS_TABLE}
                    WHERE job_id=? AND status='applying'
                      AND approved_fields IS NOT NULL
                    ORDER BY id LIMIT 1
                    """,
                    (str(job_id),),
                ).fetchone()
                if pending is None:
                    raise RemediationError("review_apply_journal_missing")
                summary = self.approve_review_item(
                    job_id,
                    int(pending["id"]),
                    _json_list(pending["approved_fields"]),
                    confirmed=True,
                    write_files=mode == "review_apply_files",
                )
                return summary, self._load_metrics(job_id)
            summary, _estimate = self.apply_high_confidence(
                job_id,
                confirmed=True,
                write_files=mode == "apply_files",
                progress=progress,
            )
            return summary, self._load_metrics(job_id)
        if status == "rolling_back":
            summary = self.rollback(job_id, confirmed=True, progress=progress)
            return summary, self._load_metrics(job_id)
        if status in {"ready", "failed", "complete_with_issues"} and current_summary.failed:
            summary, metrics = self.retry_failed(job_id, progress=progress)
            pending_apply = int(
                self.conn.execute(
                    f"""
                    SELECT COUNT(*) FROM {REMEDIATION_ITEMS_TABLE}
                    WHERE job_id=? AND status='high_confidence'
                    """,
                    (str(job_id),),
                ).fetchone()[0]
            )
            mode = str(row["mode"])
            if pending_apply and mode in {"apply_files", "apply_database"}:
                summary, _estimate = self.apply_high_confidence(
                    job_id,
                    confirmed=True,
                    write_files=mode == "apply_files",
                    progress=progress,
                )
            return summary, metrics
        if status not in {"created", "analyzing", "paused"}:
            raise RemediationError("remediation_job_not_resumable")
        self._set_job_status(job_id, "analyzing")
        return self.analyze(job_id, progress=progress)

    def retry_failed(
        self,
        job_id: str,
        *,
        progress: Callable[[JobSummary], None] | None = None,
    ) -> tuple[JobSummary, ProviderMetrics]:
        self._job_row(job_id)
        now = _utc_now()
        with self.conn:
            self.conn.execute(
                f"""
                UPDATE {REMEDIATION_ITEMS_TABLE}
                SET status='pending', confidence_class=NULL, apply_error=NULL, updated_at=?
                WHERE job_id=? AND (status='failed' OR confidence_class='failed')
                """,
                (now, str(job_id)),
            )
            self.conn.execute(
                f"""
                UPDATE {REMEDIATION_ITEMS_TABLE}
                SET status='high_confidence', file_write_status='not_requested',
                    apply_error=NULL, prepared_file=NULL,
                    original_file_hash=NULL, original_audio_payload_hash=NULL,
                    backup_file=NULL, updated_file_hash=NULL,
                    updated_audio_payload_hash=NULL,
                    applied_change_group_id=NULL, applied_snapshot=NULL, updated_at=?
                WHERE job_id=? AND status='apply_failed'
                  AND confidence_class='high_confidence'
                  AND COALESCE(file_write_status, '') NOT IN ('conflict','prepared','written')
                  AND NOT (
                      COALESCE(file_write_status, '')='failed'
                      AND (
                          backup_file IS NOT NULL
                          OR prepared_file IS NOT NULL
                          OR updated_file_hash IS NOT NULL
                      )
                  )
                """,
                (now, str(job_id)),
            )
            self.conn.execute(
                f"DELETE FROM {PROVIDER_CACHE_TABLE} WHERE response_status='failed'"
            )
        self._set_job_status(job_id, "analyzing")
        return self.analyze(job_id, progress=progress)

    def _load_metrics(self, job_id: str) -> ProviderMetrics:
        path = self._report_dir(job_id) / "metrics.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return ProviderMetrics()
        if not isinstance(payload, Mapping):
            return ProviderMetrics()
        return ProviderMetrics(
            max(0, int(payload.get("provider_requests", 0) or 0)),
            max(0, int(payload.get("cache_hits", 0) or 0)),
            max(0.0, float(payload.get("elapsed_provider_seconds", 0.0) or 0.0)),
        )

    def _write_reports(self, job_id: str, metrics: ProviderMetrics | None = None) -> None:
        summary = self._refresh_counts(job_id)
        metrics = metrics or self._load_metrics(job_id)
        report_dir = self._report_dir(job_id)
        rows = self.conn.execute(
            f"SELECT * FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=? ORDER BY id",
            (str(job_id),),
        ).fetchall()
        private_items = []
        for row in rows:
            item = dict(row)
            for name in (
                "current_snapshot",
                "proposed_patch",
                "candidate_snapshot",
                "match_reasons",
                "artwork_candidate",
                "approved_fields",
            ):
                if item.get(name):
                    try:
                        item[name] = json.loads(str(item[name]))
                    except (ValueError, json.JSONDecodeError):
                        item[name] = None
            private_items.append(item)
        _atomic_json(report_dir / "summary.json", summary.aggregate_dict())
        _atomic_json(report_dir / "items.json", {"job_id": job_id, "items": private_items})
        _atomic_json(report_dir / "metrics.json", asdict(metrics))

    def estimate_apply(self, job_id: str) -> ApplyEstimate:
        job = self._summary(self._job_row(job_id))
        rows = self.conn.execute(
            f"""
            SELECT current_snapshot, artwork_candidate
            FROM {REMEDIATION_ITEMS_TABLE}
            WHERE job_id=? AND confidence_class='high_confidence'
              AND status='high_confidence'
            """,
            (str(job_id),),
        ).fetchall()
        file_writes = 0
        artwork = 0
        backup_bytes = 0
        for row in rows:
            snapshot = _json_object(row["current_snapshot"])
            path = Path(str(snapshot.get("path") or ""))
            if self.tag_writer.supports(path) and path.is_file():
                file_writes += 1
                try:
                    backup_bytes += int(path.stat().st_size)
                except OSError:
                    pass
            if row["artwork_candidate"]:
                artwork += 1
        temporary_bytes = backup_bytes
        try:
            database_backup_bytes = int(Path(self.db.db_path).stat().st_size)
        except OSError:
            database_backup_bytes = 0
        artwork_bytes = artwork * 1_500_000
        required = int(
            (database_backup_bytes + backup_bytes + temporary_bytes + artwork_bytes) * 1.2
        )
        unchanged = max(0, job.total - len(rows))
        return ApplyEstimate(
            database_updates=len(rows),
            file_writes=file_writes,
            artwork_replacements=artwork,
            backup_bytes=backup_bytes,
            temporary_bytes=temporary_bytes,
            required_with_headroom=required,
            review_items=job.needs_review + job.ambiguous,
            unchanged_items=unchanged,
        )

    def _verify_disk_space(
        self, job_id: str, estimate: ApplyEstimate, *, write_files: bool
    ) -> None:
        try:
            self.backups_root.mkdir(parents=True, exist_ok=True)
            requirements: dict[str, tuple[Path, int]] = {}

            def add_requirement(path: Path, amount: int) -> None:
                key = path.anchor.casefold() or str(path.resolve().parent).casefold()
                root, current = requirements.get(key, (path, 0))
                requirements[key] = (root, current + max(0, int(amount)))

            try:
                database_bytes = int(Path(self.db.db_path).stat().st_size)
            except OSError:
                database_bytes = 0
            database_backup_root = Path(
                getattr(self.db, "backup_dir", self.backups_root.parent)
            )
            database_backup_root.mkdir(parents=True, exist_ok=True)
            add_requirement(
                database_backup_root,
                database_bytes,
            )
            if write_files:
                add_requirement(
                    self.backups_root,
                    estimate.backup_bytes,
                )
            if estimate.artwork_replacements:
                artwork_root = cover_art_archive_dir()
                artwork_root.mkdir(parents=True, exist_ok=True)
                add_requirement(
                    artwork_root, estimate.artwork_replacements * 1_500_000
                )
            if not write_files:
                rows = ()
            else:
                rows = self.conn.execute(
                    f"""
                    SELECT current_snapshot, artwork_candidate
                    FROM {REMEDIATION_ITEMS_TABLE}
                    WHERE job_id=? AND confidence_class='high_confidence'
                      AND status IN ('high_confidence','applying')
                    """,
                    (str(job_id),),
                ).fetchall()
            for row in rows:
                snapshot = _json_object(row["current_snapshot"])
                path = Path(str(snapshot.get("path") or ""))
                if not self.tag_writer.supports(path) or not path.is_file():
                    continue
                temporary_bytes = int(path.stat().st_size)
                if row["artwork_candidate"]:
                    temporary_bytes += 1_500_000
                add_requirement(path, temporary_bytes)
            for root, required in requirements.values():
                free = int(shutil.disk_usage(root).free)
                if free < int(required * 1.2):
                    raise RemediationError("insufficient_disk_space")
        except OSError as exc:
            raise RemediationError("disk_space_unavailable") from exc

    def _create_database_backup(self, job_id: str) -> Path:
        folder = Path(getattr(self.db, "backup_dir", self.backups_root.parent)).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        destination = folder / f"music_vault_pre_remediation_{job_id}_{timestamp}.sqlite3"
        counter = 1
        while destination.exists():
            destination = folder / (
                f"music_vault_pre_remediation_{job_id}_{timestamp}_{counter}.sqlite3"
            )
            counter += 1
        expected_counts = self.db._aggregate_counts(self.conn)
        backup_connection = sqlite3.connect(destination)
        try:
            self.conn.backup(backup_connection)
        finally:
            backup_connection.close()
        self.db._verify_backup(destination, expected_counts=expected_counts)
        return destination

    def _existing_database_backup(self, job_id: str) -> Path:
        manifest_path = self._report_dir(job_id) / "backup_manifest.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            backup = Path(str(payload["database_backup"])).resolve()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RemediationError("remediation_database_backup_missing") from exc
        try:
            self.db._verify_backup(backup)
        except Exception as exc:
            raise RemediationError("remediation_database_backup_invalid") from exc
        return backup

    def _ensure_database_backup(self, job_id: str) -> Path:
        manifest_path = self._report_dir(job_id) / "backup_manifest.json"
        if manifest_path.is_file():
            return self._existing_database_backup(job_id)
        backup = self._create_database_backup(job_id)
        _atomic_json(
            manifest_path,
            {"job_id": str(job_id), "database_backup": str(backup), "items": []},
        )
        return backup

    def _verify_review_item_disk_space(
        self,
        snapshot: Mapping[str, object],
        *,
        write_files: bool,
        artwork_requested: bool,
    ) -> None:
        """Verify the separate backup and same-volume temporary-write budgets."""

        try:
            requirements: dict[str, tuple[Path, int]] = {}

            def add(path: Path, amount: int) -> None:
                path.mkdir(parents=True, exist_ok=True) if not path.suffix else None
                key = path.anchor.casefold() or str(path.resolve().parent).casefold()
                root, current = requirements.get(key, (path, 0))
                requirements[key] = (root, current + max(0, int(amount)))

            database_backup_root = Path(
                getattr(self.db, "backup_dir", self.backups_root.parent)
            )
            database_backup_root.mkdir(parents=True, exist_ok=True)
            try:
                database_bytes = int(Path(self.db.db_path).stat().st_size)
            except OSError:
                database_bytes = 0
            add(database_backup_root, database_bytes)

            path = Path(str(snapshot.get("path") or ""))
            media_bytes = 0
            if write_files and self.tag_writer.supports(path) and path.is_file():
                media_bytes = int(path.stat().st_size)
                add(path, media_bytes + (1_500_000 if artwork_requested else 0))
                self.backups_root.mkdir(parents=True, exist_ok=True)
                add(self.backups_root, media_bytes)
            if artwork_requested:
                artwork_root = cover_art_archive_dir()
                artwork_root.mkdir(parents=True, exist_ok=True)
                add(artwork_root, 1_500_000)
            for root, required in requirements.values():
                if int(shutil.disk_usage(root).free) < int(required * 1.2):
                    raise RemediationError("insufficient_disk_space")
        except OSError as exc:
            raise RemediationError("disk_space_unavailable") from exc

    def _snapshot_still_current(
        self,
        private_snapshot: Mapping[str, object],
        *,
        require_update_marker: bool = True,
    ) -> bool:
        try:
            track_id = int(private_snapshot["track_id"])
            current = self.metadata.snapshot(track_id)
            track = self.db.get_track(track_id)
        except (KeyError, TypeError, ValueError, LookupError):
            return False
        if track is None:
            return False
        if (
            current.path != str(private_snapshot.get("path") or "")
            or current.source_kind != private_snapshot.get("source_kind")
            or current.source_video_id != private_snapshot.get("source_video_id")
            or current.source_upload_date != private_snapshot.get("source_upload_date")
            or current.musicbrainz_recording_id
            != private_snapshot.get("musicbrainz_recording_id")
            or current.musicbrainz_release_id
            != private_snapshot.get("musicbrainz_release_id")
            or (
                require_update_marker
                and current.metadata_updated_at
                != private_snapshot.get("metadata_updated_at")
            )
        ):
            return False
        expected_duration = private_snapshot.get("duration_seconds")
        current_duration = track["duration_seconds"]
        if (expected_duration is None) != (current_duration is None):
            return False
        if expected_duration is not None and abs(
            float(expected_duration) - float(current_duration)
        ) > 0.001:
            return False
        raw_fields = private_snapshot.get("fields")
        if not isinstance(raw_fields, Mapping):
            return False
        for name, original in raw_fields.items():
            if not isinstance(original, Mapping) or name not in current.fields:
                return False
            state = current.fields[name]
            if (
                state.value != original.get("value")
                or state.provenance != original.get("provenance")
                or state.provider_reference != original.get("provider_reference")
                or state.confidence != original.get("confidence")
                or state.is_manual != bool(original.get("is_manual"))
                or state.is_locked != bool(original.get("is_locked"))
            ):
                return False
        return True

    @staticmethod
    def _media_snapshot_still_current(private_snapshot: Mapping[str, object]) -> bool:
        path = Path(str(private_snapshot.get("path") or ""))
        expected_size = private_snapshot.get("file_size")
        expected_mtime = private_snapshot.get("file_mtime_ns")
        try:
            stat = path.stat()
        except OSError:
            return expected_size is None and expected_mtime is None
        return (
            expected_size is not None
            and expected_mtime is not None
            and int(stat.st_size) == int(expected_size)
            and int(stat.st_mtime_ns) == int(expected_mtime)
        )

    def _mark_item_issue(
        self,
        item_id: int,
        *,
        status: str,
        file_status: str | None = None,
        error: str,
        confidence_class: str | None = None,
    ) -> None:
        assignments = ["status=?", "apply_error=?", "updated_at=?"]
        values: list[object] = [status, sanitize_error_text(error, 300), _utc_now()]
        if file_status is not None:
            assignments.append("file_write_status=?")
            values.append(file_status)
        if confidence_class is not None:
            assignments.append("confidence_class=?")
            values.append(confidence_class)
        values.append(int(item_id))
        with self.conn:
            self.conn.execute(
                f"UPDATE {REMEDIATION_ITEMS_TABLE} SET {', '.join(assignments)} WHERE id=?",
                values,
            )

    def _prepare_candidate_artwork(
        self,
        item: Mapping[str, object],
        private_snapshot: Mapping[str, object],
    ) -> tuple[str | None, str | None]:
        raw = item.get("artwork_candidate")
        if not raw:
            return None, None
        candidate = _json_object(raw)
        release_id = str(candidate.get("release_id") or "").strip()
        preview_path = Path(str(candidate.get("preview_path") or ""))
        reviewed_candidate = _json_object(item.get("candidate_snapshot"))
        expected_token = candidate_review_token(reviewed_candidate)
        preview_token = str(candidate.get("candidate_token") or "").strip()
        if preview_token == expected_token and preview_path.is_file():
            try:
                preview_path.resolve().relative_to(cover_art_archive_dir().resolve())
                return str(preview_path.resolve()), None
            except (OSError, ValueError):
                pass
        fields = private_snapshot.get("fields")
        artwork_state = fields.get("artwork") if isinstance(fields, Mapping) else None
        if isinstance(artwork_state, Mapping) and (
            bool(artwork_state.get("is_locked"))
            or str(artwork_state.get("provenance") or "") in _PROTECTED_PROVENANCE
        ):
            return None, "artwork_locked"
        if not release_id:
            return None, "artwork_release_missing"
        try:
            prepared = self.cover_provider.fetch(release_id)
            if prepared is None:
                return None, "artwork_not_available"
            normalized = normalize_artwork_for_embedding(prepared)
            stored = store_prepared_artwork(normalized, provider="cover_art_archive")
            return str(stored), None
        except Exception as exc:
            return None, sanitize_error_text(exc, 120)

    def _capture_apply_hashes(self, job_id: str, *, write_files: bool) -> None:
        if not write_files:
            return
        rows = self.conn.execute(
            f"""
            SELECT id, current_snapshot FROM {REMEDIATION_ITEMS_TABLE}
            WHERE job_id=? AND confidence_class='high_confidence'
              AND status='high_confidence'
            ORDER BY id
            """,
            (str(job_id),),
        ).fetchall()
        for row in rows:
            snapshot = _json_object(row["current_snapshot"])
            path = Path(str(snapshot.get("path") or ""))
            if not self.tag_writer.supports(path):
                continue
            if not self._media_snapshot_still_current(snapshot):
                self._mark_item_issue(
                    int(row["id"]),
                    status="needs_review",
                    error="media_changed_after_analysis",
                    confidence_class="needs_review",
                )
                continue
            try:
                fingerprint = self.tag_writer.fingerprint(path)
            except Exception as exc:
                self._mark_item_issue(
                    int(row["id"]),
                    status="apply_failed",
                    file_status="failed",
                    error=sanitize_error_text(exc, 160),
                )
                continue
            with self.conn:
                self.conn.execute(
                    f"""
                    UPDATE {REMEDIATION_ITEMS_TABLE} SET
                        original_file_hash=?, original_audio_payload_hash=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        fingerprint.full_sha256,
                        fingerprint.audio_payload_sha256,
                        _utc_now(),
                        int(row["id"]),
                    ),
                )

    def _recover_prepared_write(
        self,
        item: Mapping[str, object],
        path: Path,
    ) -> tuple[MediaBackup, TagWriteResult] | None:
        if str(item.get("file_write_status") or "") != "prepared":
            return None
        original_hash = str(item.get("original_file_hash") or "")
        updated_hash = str(item.get("updated_file_hash") or "")
        backup_path = Path(str(item.get("backup_file") or ""))
        if not original_hash or not updated_hash or not backup_path.is_file():
            raise RemediationError("prepared_write_journal_incomplete")
        original = self.tag_writer.fingerprint(backup_path)
        if original.full_sha256 != original_hash:
            raise RemediationError("prepared_write_backup_invalid")
        current = self.tag_writer.fingerprint(path)
        if current.full_sha256 == updated_hash:
            if current.audio_payload_sha256 != original.audio_payload_sha256:
                raise RemediationError("prepared_write_audio_mismatch")
            backup = MediaBackup(path.resolve(), backup_path.resolve(), original)
            return backup, TagWriteResult(path.resolve(), original, current)
        if current.full_sha256 != original_hash:
            raise RemediationError("prepared_write_file_conflict")
        prepared_path = Path(str(item.get("prepared_file") or ""))
        try:
            if (
                prepared_path.is_file()
                and prepared_path.resolve().parent == path.resolve().parent
                and prepared_path.name.startswith(f".{path.stem}.music-vault-")
            ):
                prepared_path.unlink()
        except OSError:
            pass
        return None

    def _reconcile_failed_file_write(
        self,
        *,
        item: Mapping[str, object],
        path: Path,
        backup: MediaBackup | None,
        prepared: object | None = None,
        file_result: TagWriteResult | None = None,
        commit_attempted: bool = False,
    ) -> str:
        """Prove a failed write is restored or retain it as a conflict.

        A tag-writer exception can occur after the temporary file has replaced
        the source and after the writer's own restore attempt has failed.  The
        caller must therefore inspect the journal and the current full-file
        fingerprint instead of assuming that a raised ``commit`` left the
        source untouched.
        """

        journal_status = str(item.get("file_write_status") or "")
        replacement_possible = bool(
            commit_attempted
            or file_result is not None
            or journal_status in {"prepared", "written", "verified"}
        )
        if not replacement_possible:
            return "failed"

        original_hash = str(item.get("original_file_hash") or "")
        original_audio_hash = str(item.get("original_audio_payload_hash") or "")
        backup_path = Path(str(item.get("backup_file") or ""))
        updated_hash = str(item.get("updated_file_hash") or "")
        if file_result is not None:
            updated_hash = file_result.updated.full_sha256
        elif prepared is not None:
            prepared_updated = getattr(prepared, "updated", None)
            updated_hash = str(
                getattr(prepared_updated, "full_sha256", "") or updated_hash
            )

        if backup is None and backup_path.is_file() and original_hash:
            try:
                fingerprint = self.tag_writer.fingerprint(backup_path)
            except Exception:
                return "conflict"
            if (
                fingerprint.full_sha256 != original_hash
                or (
                    original_audio_hash
                    and fingerprint.audio_payload_sha256 != original_audio_hash
                )
            ):
                return "conflict"
            backup = MediaBackup(path.resolve(), backup_path.resolve(), fingerprint)

        # A replacement was possible, so a missing or invalid backup is an
        # unresolved conflict rather than a retryable pre-write failure.
        if backup is None:
            return "conflict"

        original = backup.fingerprint
        if (
            (original_hash and original.full_sha256 != original_hash)
            or (
                original_audio_hash
                and original.audio_payload_sha256 != original_audio_hash
            )
        ):
            return "conflict"
        try:
            current = self.tag_writer.fingerprint(path)
        except Exception:
            return "conflict"
        if (
            current.full_sha256 == original.full_sha256
            and current.audio_payload_sha256 == original.audio_payload_sha256
        ):
            return "restored"
        if not updated_hash or current.full_sha256 != updated_hash:
            return "conflict"

        try:
            self.tag_writer.restore(
                path,
                backup.backup_path,
                expected_backup_sha256=original.full_sha256,
                expected_current_sha256=updated_hash,
            )
        except Exception:
            # A restore implementation can raise after compensating itself.
            # Re-fingerprint before declaring the state unresolved.
            try:
                current = self.tag_writer.fingerprint(path)
            except Exception:
                return "conflict"
            if (
                current.full_sha256 == original.full_sha256
                and current.audio_payload_sha256 == original.audio_payload_sha256
            ):
                return "restored"
            return "conflict"

        try:
            current = self.tag_writer.fingerprint(path)
        except Exception:
            return "conflict"
        return (
            "restored"
            if current.full_sha256 == original.full_sha256
            and current.audio_payload_sha256 == original.audio_payload_sha256
            else "conflict"
        )

    def apply_high_confidence(
        self,
        job_id: str,
        *,
        confirmed: bool = False,
        write_files: bool = False,
        progress: Callable[[JobSummary], None] | None = None,
    ) -> tuple[JobSummary, ApplyEstimate]:
        if not confirmed:
            raise RemediationError("explicit_apply_confirmation_required")
        job_row = self._job_row(job_id)
        job = self._summary(job_row)
        resuming_apply = job.status == "applying"
        if job.status not in {"ready", "complete_with_issues", "applying"}:
            raise RemediationError("remediation_job_not_ready")
        if resuming_apply:
            expected_mode = "apply_files" if write_files else "apply_database"
            if str(job_row["mode"]) != expected_mode:
                raise RemediationError("remediation_apply_mode_mismatch")
        if not resuming_apply and self.library_revision() != job.library_revision:
            raise RemediationError("remediation_job_stale")
        created = _parse_utc(job_row["created_at"])
        candidates_expired = (
            created is None or datetime.now(timezone.utc) - created > _CACHE_TTL
        )
        if candidates_expired and not resuming_apply:
            raise RemediationError("remediation_candidates_stale")
        if not resuming_apply:
            self._capture_apply_hashes(job_id, write_files=write_files)
        estimate = self.estimate_apply(job_id)
        self._verify_disk_space(job_id, estimate, write_files=write_files)
        database_backup = (
            self._existing_database_backup(job_id)
            if resuming_apply
            else self._ensure_database_backup(job_id)
        )
        originals_dir = self.backups_root / str(job_id) / "originals"
        originals_dir.mkdir(parents=True, exist_ok=True)
        if not resuming_apply:
            with self.conn:
                self.conn.execute(
                    f"UPDATE {REMEDIATION_JOBS_TABLE} SET mode=?, status='applying', updated_at=? WHERE id=?",
                    ("apply_files" if write_files else "apply_database", _utc_now(), str(job_id)),
                )
        rows = self.conn.execute(
            f"""
            SELECT * FROM {REMEDIATION_ITEMS_TABLE}
            WHERE job_id=? AND confidence_class='high_confidence'
              AND status IN ('high_confidence','applying')
            ORDER BY id
            """,
            (str(job_id),),
        ).fetchall()
        for raw_row in rows:
            item = dict(raw_row)
            item_id = int(item["id"])
            if candidates_expired and str(item.get("status")) != "applying":
                self._mark_item_issue(
                    item_id,
                    status="needs_review",
                    error="remediation_candidates_stale",
                    confidence_class="needs_review",
                )
                continue
            snapshot = _json_object(item["current_snapshot"])
            patch = _json_object(item["proposed_patch"])
            if not patch or not self._snapshot_still_current(snapshot):
                self._mark_item_issue(
                    item_id,
                    status="needs_review",
                    error="item_stale_or_locked",
                    confidence_class="needs_review",
                )
                continue
            path = Path(str(snapshot.get("path") or ""))
            recording_id = str(item.get("provider_recording_id") or "").strip() or None
            release_id = str(item.get("provider_release_id") or "").strip() or None
            confidence = (
                float(item["confidence_score"])
                if item.get("confidence_score") is not None
                else None
            )
            existing_artwork = str(patch.get("artwork") or "").strip()
            if existing_artwork and Path(existing_artwork).is_file():
                artwork_path, artwork_issue = existing_artwork, None
            else:
                artwork_path, artwork_issue = self._prepare_candidate_artwork(item, snapshot)
            if artwork_path is not None:
                patch["artwork"] = artwork_path
                with self.conn:
                    self.conn.execute(
                        f"UPDATE {REMEDIATION_ITEMS_TABLE} SET proposed_patch=?, updated_at=? WHERE id=?",
                        (_json(patch), _utc_now(), item_id),
                    )
            backup = None
            file_result = None
            prepared = None
            commit_attempted = False
            file_status = "not_requested"
            try:
                if write_files and self.tag_writer.supports(path):
                    tag_patch = dict(patch)
                    if recording_id:
                        tag_patch["musicbrainz_recording_id"] = recording_id
                    if release_id:
                        tag_patch["musicbrainz_release_id"] = release_id
                    recovered = self._recover_prepared_write(item, path)
                    if recovered is not None:
                        backup, file_result = recovered
                    else:
                        backup = self.tag_writer.create_backup(
                            path,
                            originals_dir,
                            identity=f"track-{int(item['track_id'])}",
                            expected_full_sha256=(
                                str(item["original_file_hash"])
                                if item.get("original_file_hash")
                                else None
                            ),
                        )
                        prepared = self.tag_writer.prepare(
                            path,
                            tag_patch,
                            expected_full_sha256=backup.fingerprint.full_sha256,
                            artwork_path=artwork_path,
                        )
                        with self.conn:
                            self.conn.execute(
                                f"""
                                UPDATE {REMEDIATION_ITEMS_TABLE} SET
                                    status='applying', file_write_status='prepared',
                                    original_file_hash=?, original_audio_payload_hash=?,
                                    backup_file=?, prepared_file=?, updated_file_hash=?,
                                    updated_audio_payload_hash=?, updated_at=?
                                WHERE id=?
                                """,
                                (
                                    backup.fingerprint.full_sha256,
                                    backup.fingerprint.audio_payload_sha256,
                                    str(backup.backup_path),
                                    str(prepared.temporary_path),
                                    prepared.updated.full_sha256,
                                    prepared.updated.audio_payload_sha256,
                                    _utc_now(),
                                    item_id,
                                ),
                            )
                        commit_attempted = True
                        file_result = self.tag_writer.commit(prepared, backup=backup)
                    file_status = "verified"
                elif write_files:
                    file_status = "unsupported"
                with self.conn:
                    if not self.conn.in_transaction:
                        self.conn.execute("BEGIN IMMEDIATE")
                    if not self._snapshot_still_current(snapshot):
                        raise RemediationError("metadata_precondition_changed")
                    result = self.metadata.apply_high_confidence_candidate(
                        int(item["track_id"]),
                        patch,
                        recording_id=recording_id,
                        release_id=release_id,
                        confidence=confidence,
                        artwork_path=artwork_path,
                        commit=False,
                    )
                    if set(result.changed_fields) != set(patch):
                        raise RemediationError("metadata_precondition_changed")
                    applied_track = self.db.get_track(int(item["track_id"]))
                    if applied_track is None:
                        raise RemediationError("applied_track_missing")
                    applied_snapshot = _snapshot_dict(result.after, dict(applied_track))
                    self.conn.execute(
                        f"""
                        UPDATE {REMEDIATION_ITEMS_TABLE} SET
                            status='applied', file_write_status=?,
                            prepared_file=NULL,
                            original_file_hash=COALESCE(original_file_hash, ?),
                            original_audio_payload_hash=COALESCE(original_audio_payload_hash, ?),
                            backup_file=COALESCE(backup_file, ?),
                            updated_file_hash=COALESCE(updated_file_hash, ?),
                            updated_audio_payload_hash=COALESCE(updated_audio_payload_hash, ?),
                            applied_change_group_id=?, applied_snapshot=?,
                            apply_error=?, updated_at=?
                        WHERE id=?
                        """,
                        (
                            file_status,
                            backup.fingerprint.full_sha256 if backup else None,
                            backup.fingerprint.audio_payload_sha256 if backup else None,
                            str(backup.backup_path) if backup else None,
                            file_result.updated.full_sha256 if file_result else None,
                            file_result.updated.audio_payload_sha256 if file_result else None,
                            result.change_group_id,
                            _json(applied_snapshot),
                            artwork_issue,
                            _utc_now(),
                            item_id,
                        ),
                    )
            except Exception as exc:
                restore_status = self._reconcile_failed_file_write(
                    item=item,
                    path=path,
                    backup=backup,
                    prepared=prepared,
                    file_result=file_result,
                    commit_attempted=commit_attempted,
                )
                self._mark_item_issue(
                    item_id,
                    status="apply_failed",
                    file_status=restore_status,
                    error=sanitize_error_text(exc, 200),
                )
            summary = self._refresh_counts(job_id)
            if progress is not None:
                progress(summary)
        summary = self._refresh_counts(job_id)
        applied_issues = int(
            self.conn.execute(
                f"""
                SELECT COUNT(*) FROM {REMEDIATION_ITEMS_TABLE}
                WHERE job_id=? AND status='applied'
                  AND apply_error IS NOT NULL AND TRIM(apply_error) <> ''
                """,
                (str(job_id),),
            ).fetchone()[0]
        )
        final_status = "complete_with_issues" if summary.failed or applied_issues else "complete"
        self._set_job_status(job_id, final_status, finish=True)
        with self.conn:
            self.conn.execute(
                f"UPDATE {REMEDIATION_JOBS_TABLE} SET library_revision=?, updated_at=? WHERE id=?",
                (self.library_revision(), _utc_now(), str(job_id)),
            )
        summary = self._refresh_counts(job_id)
        self._write_apply_manifests(job_id, database_backup)
        self._write_reports(job_id)
        return summary, estimate

    def _write_apply_manifests(self, job_id: str, database_backup: Path) -> None:
        rows = self.conn.execute(
            f"""
            SELECT id, track_id, status, file_write_status, original_file_hash,
                   original_audio_payload_hash, backup_file, updated_file_hash,
                   updated_audio_payload_hash, applied_change_group_id,
                   rollback_change_group_id
            FROM {REMEDIATION_ITEMS_TABLE}
            WHERE job_id=? AND status IN ('applied','apply_failed','rolled_back','conflict')
            ORDER BY id
            """,
            (str(job_id),),
        ).fetchall()
        manifest = {"job_id": job_id, "database_backup": str(database_backup), "items": []}
        for row in rows:
            manifest["items"].append(dict(row))
        _atomic_json(self._report_dir(job_id) / "backup_manifest.json", manifest)

    def _metadata_matches_applied_item(self, item: Mapping[str, object]) -> bool:
        applied_snapshot = _json_object(item.get("applied_snapshot"))
        if applied_snapshot:
            return self._snapshot_still_current(applied_snapshot)
        patch = _json_object(item.get("proposed_patch"))
        try:
            current = self.metadata.snapshot(int(item["track_id"]))
        except Exception:
            return False
        allowed_provenance = {
            "musicbrainz_high_confidence",
            "cover_art_archive_high_confidence",
            "musicbrainz_confirmed",
            "cover_art_archive",
        }
        for name, value in patch.items():
            state = current.fields.get(name)
            if state is None or state.value != value or state.provenance not in allowed_provenance:
                return False
        recording_id = str(item.get("provider_recording_id") or "").strip() or None
        release_id = str(item.get("provider_release_id") or "").strip() or None
        if recording_id and current.musicbrainz_recording_id != recording_id:
            return False
        if release_id and current.musicbrainz_release_id != release_id:
            return False
        return True

    def rollback(
        self,
        job_id: str,
        *,
        confirmed: bool = False,
        progress: Callable[[JobSummary], None] | None = None,
    ) -> JobSummary:
        if not confirmed:
            raise RemediationError("explicit_rollback_confirmation_required")
        job = self._summary(self._job_row(job_id))
        rollback_ready = job.status in {"complete", "complete_with_issues", "rolling_back"}
        rollback_ready = rollback_ready or (job.status == "ready" and job.applied > 0)
        if not rollback_ready:
            raise RemediationError("remediation_job_not_rollback_ready")
        database_backup = self._existing_database_backup(job_id)
        self._set_job_status(job_id, "rolling_back")
        rows = self.conn.execute(
            f"""
            SELECT * FROM {REMEDIATION_ITEMS_TABLE}
            WHERE job_id=? AND status IN ('applied','rollback_pending')
            ORDER BY id DESC
            """,
            (str(job_id),),
        ).fetchall()
        rollback_entries: list[dict[str, object]] = []
        for raw_row in rows:
            item = dict(raw_row)
            item_id = int(item["id"])
            snapshot = _json_object(item["current_snapshot"])
            applied_snapshot = _json_object(item.get("applied_snapshot"))
            path = Path(str(snapshot.get("path") or ""))
            if not applied_snapshot or not self._metadata_matches_applied_item(item):
                self._mark_item_issue(
                    item_id,
                    status="conflict",
                    file_status=(
                        str(item.get("file_write_status") or "not_requested")
                    ),
                    error="metadata_changed_after_remediation",
                )
                continue
            with self.conn:
                self.conn.execute(
                    f"UPDATE {REMEDIATION_ITEMS_TABLE} SET status='rollback_pending', updated_at=? WHERE id=?",
                    (_utc_now(), item_id),
                )
            file_restored = False
            rollback_safety: Path | None = None
            media_conflict = False
            try:
                with self.conn:
                    # Acquire SQLite's write lock before the final metadata
                    # precondition and hold it through the file+DB restore so
                    # neither side can race a competing metadata writer.
                    if not self.conn.in_transaction:
                        self.conn.execute("BEGIN IMMEDIATE")
                    if not self._snapshot_still_current(applied_snapshot):
                        raise RemediationError("metadata_changed_after_remediation")
                    if str(item.get("file_write_status")) == "verified":
                        original_hash = str(item.get("original_file_hash") or "")
                        updated_hash = str(item.get("updated_file_hash") or "")
                        backup_path = Path(str(item.get("backup_file") or ""))
                        current_hash = full_file_sha256(path)
                        if current_hash == updated_hash:
                            rollback_safety = path.parent / (
                                f".{path.stem}.music-vault-rollback-"
                                f"{uuid.uuid4().hex}.tmp.mp3"
                            )
                            shutil.copy2(path, rollback_safety)
                            if full_file_sha256(rollback_safety) != updated_hash:
                                raise RemediationError("rollback_safety_copy_failed")
                            self.tag_writer.restore(
                                path,
                                backup_path,
                                expected_backup_sha256=original_hash,
                                expected_current_sha256=updated_hash,
                            )
                            file_restored = True
                        elif current_hash == original_hash:
                            file_restored = True
                        else:
                            media_conflict = True
                            raise RemediationError("media_changed_after_remediation")
                    result = self.metadata.restore_remediation_snapshot(
                        int(item["track_id"]),
                        snapshot,
                        expected_current_snapshot=applied_snapshot,
                        commit=False,
                    )
                    self.conn.execute(
                        f"""
                        UPDATE {REMEDIATION_ITEMS_TABLE} SET
                            status='rolled_back',
                            file_write_status=CASE
                                WHEN file_write_status='verified' THEN 'restored'
                                ELSE file_write_status
                            END,
                            rollback_change_group_id=?,
                            apply_error=NULL, updated_at=?
                        WHERE id=?
                        """,
                        (result.change_group_id, _utc_now(), item_id),
                    )
                if rollback_safety is not None:
                    rollback_safety.unlink(missing_ok=True)
                rollback_entries.append(
                    {
                        "item_id": item_id,
                        "track_id": int(item["track_id"]),
                        "file_restored": file_restored,
                        "original_file_hash": item.get("original_file_hash"),
                    }
                )
            except Exception as exc:
                compensation_failed = False
                if file_restored and rollback_safety is not None and rollback_safety.is_file():
                    try:
                        self.tag_writer.restore(
                            path,
                            rollback_safety,
                            expected_backup_sha256=str(item.get("updated_file_hash") or ""),
                            expected_current_sha256=str(item.get("original_file_hash") or ""),
                        )
                        file_restored = False
                    except Exception:
                        compensation_failed = True
                if rollback_safety is not None:
                    rollback_safety.unlink(missing_ok=True)
                previous_file_status = str(
                    item.get("file_write_status") or "not_requested"
                )
                self._mark_item_issue(
                    item_id,
                    status="conflict",
                    file_status=(
                        "conflict"
                        if media_conflict or compensation_failed or file_restored
                        else previous_file_status
                    ),
                    error=sanitize_error_text(exc, 200),
                )
            summary = self._refresh_counts(job_id)
            if progress is not None:
                progress(summary)
        conflicts = int(
            self.conn.execute(
                f"SELECT COUNT(*) FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=? AND status='conflict'",
                (str(job_id),),
            ).fetchone()[0]
        )
        final_status = "complete_with_issues" if conflicts else "rolled_back"
        self._set_job_status(job_id, final_status, finish=True)
        summary = self._refresh_counts(job_id)
        _atomic_json(
            self._report_dir(job_id) / "rollback_manifest.json",
            {"job_id": job_id, "status": final_status, "items": rollback_entries},
        )
        self._write_apply_manifests(job_id, database_backup)
        self._write_reports(job_id)
        return summary

    def verify_job(self, job_id: str) -> dict[str, object]:
        summary = self._refresh_counts(job_id)
        rows = self.conn.execute(
            f"SELECT * FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=? ORDER BY id",
            (str(job_id),),
        ).fetchall()
        items = [dict(row) for row in rows]
        analyzed_count = sum(
            1 for item in items if item.get("confidence_class") is not None
        )
        class_counts = {
            name: sum(1 for item in items if item.get("confidence_class") == name)
            for name in (
                "high_confidence",
                "needs_review",
                "ambiguous",
                "no_match",
                "skipped",
            )
        }
        failed_count = sum(
            1
            for item in items
            if item.get("confidence_class") == "failed"
            or item.get("status") in {"failed", "apply_failed"}
        )
        applied_count = sum(1 for item in items if item.get("status") == "applied")
        written_count = sum(
            1 for item in items if item.get("file_write_status") == "verified"
        )
        rolled_back_count = sum(
            1 for item in items if item.get("status") == "rolled_back"
        )
        aggregate_reconciles = (
            len(items) <= summary.total
            and analyzed_count == summary.analyzed
            and sum(class_counts.values())
            + sum(1 for item in items if item.get("confidence_class") == "failed")
            == analyzed_count
            and class_counts["high_confidence"] == summary.high_confidence
            and class_counts["needs_review"] == summary.needs_review
            and class_counts["ambiguous"] == summary.ambiguous
            and class_counts["no_match"] == summary.no_match
            and class_counts["skipped"] == summary.skipped
            and failed_count == summary.failed
            and applied_count == summary.applied
            and written_count == summary.file_written
            and rolled_back_count == summary.rolled_back
        )
        checks = {
            "aggregate_counts_reconcile": aggregate_reconciles,
            "no_pending_applied_overlap": True,
            "no_unresolved_final_items": True,
            "database_backup_verified": True,
            "backup_manifest_reconciles": True,
            "rollback_manifest_reconciles": True,
            "reports_reconcile": True,
            "file_backups_verified": True,
            "applied_history_present": True,
            "rollback_history_present": True,
            "audio_payloads_preserved": True,
            "current_files_match_journal": True,
            "database_matches_applied_patch": True,
            "database_only_status_truthful": True,
            "unsupported_status_truthful": True,
            "no_ambiguous_auto_apply": True,
            "no_no_match_auto_apply": True,
            "no_low_confidence_auto_apply": True,
            "locked_fields_preserved": True,
            "source_identity_preserved": True,
            "release_dates_provider_derived": True,
            "no_unresolved_file_write_states": True,
            "reports_readable": True,
            "sqlite_integrity_ok": False,
            "foreign_keys_ok": False,
        }
        final_statuses = {"ready", "complete", "complete_with_issues", "rolled_back"}
        active_item_statuses = {
            "pending",
            "analyzing",
            "approved",
            "applying",
            "rollback_pending",
        }
        if summary.status in final_statuses:
            if len(items) != summary.total or any(
                str(item.get("status")) in active_item_statuses for item in items
            ):
                checks["no_unresolved_final_items"] = False
        for item in items:
            status = str(item["status"])
            confidence_class = str(item.get("confidence_class") or "")
            user_confirmed = str(item.get("review_reason") or "") == "user_confirmed"
            has_apply_group = bool(item.get("applied_change_group_id"))
            if status == "applied" and not has_apply_group:
                checks["no_pending_applied_overlap"] = False
            if has_apply_group and status not in {
                "applied",
                "rollback_pending",
                "rolled_back",
                "conflict",
            }:
                checks["no_pending_applied_overlap"] = False
            if status in active_item_statuses and has_apply_group:
                checks["no_pending_applied_overlap"] = False
            if status == "applied" and confidence_class != "high_confidence" and not user_confirmed:
                if confidence_class == "ambiguous":
                    checks["no_ambiguous_auto_apply"] = False
                elif confidence_class == "no_match":
                    checks["no_no_match_auto_apply"] = False
                else:
                    checks["no_low_confidence_auto_apply"] = False
            if status == "applied" and not item.get("applied_change_group_id"):
                checks["applied_history_present"] = False
            if status == "applied" and item.get("applied_change_group_id"):
                history = self.conn.execute(
                    "SELECT 1 FROM track_metadata_history WHERE change_group_id=? AND track_id=? LIMIT 1",
                    (item["applied_change_group_id"], int(item["track_id"])),
                ).fetchone()
                if history is None:
                    checks["applied_history_present"] = False
            if status == "rolled_back" and not item.get("rollback_change_group_id"):
                checks["rollback_history_present"] = False
            if status == "rolled_back" and item.get("rollback_change_group_id"):
                history = self.conn.execute(
                    "SELECT 1 FROM track_metadata_history WHERE change_group_id=? AND track_id=? LIMIT 1",
                    (item["rollback_change_group_id"], int(item["track_id"])),
                ).fetchone()
                if history is None:
                    checks["rollback_history_present"] = False
            file_status = str(item.get("file_write_status") or "")
            if status == "conflict" or file_status in {
                "pending",
                "prepared",
                "written",
                "conflict",
            }:
                checks["no_unresolved_file_write_states"] = False
            if file_status == "failed" and any(
                item.get(name)
                for name in ("backup_file", "prepared_file", "updated_file_hash")
            ):
                original_hash = str(item.get("original_file_hash") or "")
                original_audio_hash = str(
                    item.get("original_audio_payload_hash") or ""
                )
                try:
                    snapshot_path = Path(
                        str(
                            _json_object(item.get("current_snapshot")).get("path")
                            or ""
                        )
                    )
                    current = self.tag_writer.fingerprint(
                        snapshot_path
                    )
                    if (
                        not original_hash
                        or current.full_sha256 != original_hash
                        or (
                            original_audio_hash
                            and current.audio_payload_sha256 != original_audio_hash
                        )
                    ):
                        checks["no_unresolved_file_write_states"] = False
                except Exception:
                    checks["no_unresolved_file_write_states"] = False
            if file_status in {"verified", "restored"}:
                backup_file = Path(str(item.get("backup_file") or ""))
                original_hash = str(item.get("original_file_hash") or "")
                if not backup_file.is_file():
                    checks["file_backups_verified"] = False
                else:
                    try:
                        backup_fingerprint = self.tag_writer.fingerprint(backup_file)
                        if (
                            backup_fingerprint.full_sha256 != original_hash
                            or backup_fingerprint.audio_payload_sha256
                            != item.get("original_audio_payload_hash")
                        ):
                            checks["file_backups_verified"] = False
                    except Exception:
                        checks["file_backups_verified"] = False
                if (
                    item.get("updated_audio_payload_hash")
                    and item.get("original_audio_payload_hash")
                    != item.get("updated_audio_payload_hash")
                ):
                    checks["audio_payloads_preserved"] = False
            if file_status == "not_requested" and item.get("updated_file_hash"):
                checks["database_only_status_truthful"] = False
            snapshot = _json_object(item.get("current_snapshot"))
            fields = snapshot.get("fields")
            path = Path(str(snapshot.get("path") or ""))
            if file_status == "unsupported" and (
                item.get("updated_file_hash") or self.tag_writer.supports(path)
            ):
                checks["unsupported_status_truthful"] = False
            current_track = self.db.get_track(int(item["track_id"]))
            if current_track is None or any(
                current_track[name] != snapshot.get(name)
                for name in (
                    "path",
                    "source_kind",
                    "source_video_id",
                    "source_upload_date",
                )
            ):
                checks["source_identity_preserved"] = False
            if file_status in {"verified", "restored"}:
                expected_hash = (
                    str(item.get("updated_file_hash") or "")
                    if file_status == "verified"
                    else str(item.get("original_file_hash") or "")
                )
                try:
                    current_fingerprint = self.tag_writer.fingerprint(path)
                    expected_audio = (
                        item.get("updated_audio_payload_hash")
                        if file_status == "verified"
                        else item.get("original_audio_payload_hash")
                    )
                    if (
                        current_fingerprint.full_sha256 != expected_hash
                        or current_fingerprint.audio_payload_sha256 != expected_audio
                    ):
                        checks["current_files_match_journal"] = False
                except Exception:
                    checks["current_files_match_journal"] = False
            if status == "applied" and isinstance(fields, Mapping):
                current = self.metadata.snapshot(int(item["track_id"]))
                proposed = _json_object(item.get("proposed_patch"))
                for name, value in proposed.items():
                    state = current.fields.get(name)
                    if state is None or state.value != value:
                        checks["database_matches_applied_patch"] = False
                for name, original in fields.items():
                    if not isinstance(original, Mapping) or not original.get("is_locked"):
                        continue
                    state = current.fields.get(name)
                    if state is None or (
                        state.value != original.get("value")
                        or state.provenance != original.get("provenance")
                        or not state.is_locked
                    ):
                        checks["locked_fields_preserved"] = False
            if status == "rolled_back" and not self._snapshot_still_current(
                snapshot, require_update_marker=False
            ):
                checks["database_matches_applied_patch"] = False
            patch = _json_object(item.get("proposed_patch"))
            candidate = _json_object(item.get("candidate_snapshot"))
            if patch.get("release_date") and patch.get("release_date") != candidate.get(
                "release_date"
            ):
                checks["release_dates_provider_derived"] = False
        apply_journal_present = bool(
            summary.applied
            or summary.rolled_back
            or any(
                item.get("applied_change_group_id")
                or item.get("backup_file")
                or item.get("updated_file_hash")
                or item.get("status")
                in {"applied", "apply_failed", "rolled_back", "conflict"}
                for item in items
            )
        )
        if apply_journal_present:
            try:
                self._existing_database_backup(job_id)
            except RemediationError:
                checks["database_backup_verified"] = False
        report_names = ["summary.json", "items.json", "metrics.json"]
        backup_manifest_required = apply_journal_present
        if backup_manifest_required:
            report_names.append("backup_manifest.json")
        rollback_manifest_path = self._report_dir(job_id) / "rollback_manifest.json"
        if (
            summary.rolled_back
            or summary.status == "rolled_back"
            or rollback_manifest_path.is_file()
        ):
            report_names.append("rollback_manifest.json")
        report_payloads: dict[str, object] = {}
        for name in report_names:
            path = self._report_dir(job_id) / name
            try:
                report_payloads[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                checks["reports_readable"] = False
        summary_payload = report_payloads.get("summary.json")
        items_payload = report_payloads.get("items.json")
        if summary_payload != summary.aggregate_dict():
            checks["reports_reconcile"] = False
        if not isinstance(items_payload, Mapping):
            checks["reports_reconcile"] = False
        else:
            reported_items = items_payload.get("items")
            if (
                items_payload.get("job_id") != str(job_id)
                or not isinstance(reported_items, list)
                or {
                    (int(value.get("id", -1)), str(value.get("status") or ""))
                    for value in reported_items
                    if isinstance(value, Mapping)
                }
                != {(int(item["id"]), str(item["status"])) for item in items}
            ):
                checks["reports_reconcile"] = False
        if backup_manifest_required:
            manifest = report_payloads.get("backup_manifest.json")
            expected_items = {
                int(item["id"]): item
                for item in items
                if item.get("status")
                in {"applied", "apply_failed", "rolled_back", "conflict"}
            }
            if not isinstance(manifest, Mapping) or manifest.get("job_id") != str(job_id):
                checks["backup_manifest_reconciles"] = False
            else:
                manifest_rows = manifest.get("items")
                actual_items = {
                    int(value.get("id", -1)): value
                    for value in manifest_rows
                    if isinstance(value, Mapping)
                } if isinstance(manifest_rows, list) else {}
                compared_fields = (
                    "track_id",
                    "status",
                    "file_write_status",
                    "original_file_hash",
                    "original_audio_payload_hash",
                    "backup_file",
                    "updated_file_hash",
                    "updated_audio_payload_hash",
                    "applied_change_group_id",
                    "rollback_change_group_id",
                )
                if set(actual_items) != set(expected_items) or any(
                    any(actual_items[item_id].get(name) != expected.get(name) for name in compared_fields)
                    for item_id, expected in expected_items.items()
                ):
                    checks["backup_manifest_reconciles"] = False
        rollback_manifest = report_payloads.get("rollback_manifest.json")
        if rollback_manifest is not None:
            expected_rolled_ids = {
                int(item["id"]) for item in items if item.get("status") == "rolled_back"
            }
            if not isinstance(rollback_manifest, Mapping):
                checks["rollback_manifest_reconciles"] = False
            else:
                rollback_rows = rollback_manifest.get("items")
                reported_rolled_ids = {
                    int(value.get("item_id", -1))
                    for value in rollback_rows
                    if isinstance(value, Mapping)
                } if isinstance(rollback_rows, list) else set()
                if (
                    rollback_manifest.get("job_id") != str(job_id)
                    or rollback_manifest.get("status") != summary.status
                    or reported_rolled_ids != expected_rolled_ids
                ):
                    checks["rollback_manifest_reconciles"] = False
        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()
        checks["sqlite_integrity_ok"] = bool(
            integrity is not None and str(integrity[0]).casefold() == "ok"
        )
        checks["foreign_keys_ok"] = not bool(
            self.conn.execute("PRAGMA foreign_key_check").fetchone()
        )
        return {
            "job_id": job_id,
            "status": summary.status,
            "ok": all(checks.values()),
            "checks": checks,
            "counts": summary.aggregate_dict(),
        }

    def retry_item_with_query(
        self,
        job_id: str,
        item_id: int,
        title: str,
        artist: str | None,
    ) -> tuple[JobSummary, ProviderMetrics]:
        """Retry one review search without writing the edited query to metadata."""

        self._job_row(job_id)
        query_title = " ".join(str(title or "").split())
        query_artist = " ".join(str(artist or "").split()) or None
        if not query_title or not query_artist:
            raise RemediationError("edited_query_requires_title_artist")
        row = self.conn.execute(
            f"SELECT * FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=? AND id=?",
            (str(job_id), int(item_id)),
        ).fetchone()
        if row is None:
            raise RemediationError("remediation_item_not_found")
        item = dict(row)
        if str(item.get("status")) in {"applied", "applying", "rolled_back"}:
            raise RemediationError("remediation_item_not_reviewable")
        private_snapshot = _json_object(item.get("current_snapshot"))
        if not self._snapshot_still_current(private_snapshot):
            raise RemediationError("remediation_item_stale")
        track_id = int(item["track_id"])
        track = self.db.get_track(track_id)
        if track is None:
            raise RemediationError("remediation_item_not_found")
        snapshot = self.metadata.snapshot(track_id)
        duration = track["duration_seconds"]
        metrics = self._load_metrics(job_id)
        candidates, metrics, provider_error = self._provider_candidates(
            query_title,
            query_artist,
            float(duration) if duration is not None else None,
            metrics,
        )
        refreshed_snapshot = _snapshot_dict(snapshot, dict(track))
        if provider_error:
            self._upsert_analysis_item(
                job_id,
                track_id,
                status="failed",
                snapshot=refreshed_snapshot,
                reasons=("provider_failure",),
                review_reason="edited_query_provider_failure",
                error=provider_error,
            )
        else:
            classification, score, reasons, _proposed, candidate, artwork = self._assessment(
                snapshot,
                float(duration) if duration is not None else None,
                candidates,
                query_title=query_title,
                query_artist=query_artist,
            )
            if classification == "high_confidence":
                classification = "needs_review"
                reasons = [*reasons, "edited_query_requires_confirmation"]
            self._upsert_analysis_item(
                job_id,
                track_id,
                status=classification,
                snapshot=refreshed_snapshot,
                proposed_patch={},
                candidate_snapshot=candidate,
                confidence_score=score,
                reasons=reasons,
                recording_id=candidate.get("recording_id"),
                release_id=None,
                artwork_candidate=artwork,
                review_reason="edited_query_review",
            )
        self._set_job_status(job_id, "ready")
        summary = self._refresh_counts(job_id)
        self._write_reports(job_id, metrics)
        return summary, metrics

    def prepare_review_artwork(self, job_id: str, item_id: int) -> dict[str, object]:
        """Fetch a private, validated candidate-art preview after explicit review."""

        self._job_row(job_id)
        row = self.conn.execute(
            f"SELECT * FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=? AND id=?",
            (str(job_id), int(item_id)),
        ).fetchone()
        if row is None:
            raise RemediationError("remediation_item_not_found")
        item = dict(row)
        if str(item.get("status")) not in {"needs_review", "ambiguous", "approved"}:
            raise RemediationError("remediation_item_not_reviewable")
        candidate = _json_object(item.get("candidate_snapshot"))
        release_id = str(candidate.get("release_id") or "").strip()
        candidate_token = candidate_review_token(candidate)
        if not release_id or not bool(candidate.get("artwork_available")):
            raise RemediationError("artwork_not_available")
        artwork_candidate = _json_object(item.get("artwork_candidate"))
        if str(artwork_candidate.get("candidate_token") or "") != candidate_token:
            artwork_candidate.pop("preview_path", None)
        artwork_candidate.update(
            {
                "provider": "cover_art_archive",
                "release_id": release_id,
                "candidate_token": candidate_token,
            }
        )
        artwork_item = dict(item)
        artwork_item["artwork_candidate"] = _json(artwork_candidate)
        snapshot = _json_object(item.get("current_snapshot"))
        artwork_path, issue = self._prepare_candidate_artwork(artwork_item, snapshot)
        if artwork_path is None:
            raise RemediationError(issue or "artwork_not_available")
        artwork_candidate["preview_path"] = artwork_path
        with self.conn:
            self.conn.execute(
                f"UPDATE {REMEDIATION_ITEMS_TABLE} SET artwork_candidate=?, updated_at=? WHERE id=?",
                (_json(artwork_candidate), _utc_now(), int(item_id)),
            )
        self._write_reports(job_id)
        return {
            "item_id": int(item_id),
            "artwork_path": artwork_path,
            "candidate_token": candidate_token,
        }

    def skip_items(self, job_id: str, item_ids: Iterable[int]) -> JobSummary:
        return self._record_review_decision(job_id, item_ids, "user_skipped")

    def reject_candidates(self, job_id: str, item_ids: Iterable[int]) -> JobSummary:
        return self._record_review_decision(
            job_id, item_ids, "user_rejected_candidate"
        )

    def keep_current_items(self, job_id: str, item_ids: Iterable[int]) -> JobSummary:
        return self._record_review_decision(job_id, item_ids, "user_kept_current")

    def _record_review_decision(
        self,
        job_id: str,
        item_ids: Iterable[int],
        reason: str,
    ) -> JobSummary:
        self._job_row(job_id)
        if reason not in {
            "user_skipped",
            "user_rejected_candidate",
            "user_kept_current",
        }:
            raise RemediationError("review_decision_invalid")
        values = sorted({int(value) for value in item_ids})
        if not values:
            return self._summary(self._job_row(job_id))
        placeholders = ",".join("?" for _ in values)
        with self.conn:
            self.conn.execute(
                f"""
                UPDATE {REMEDIATION_ITEMS_TABLE} SET
                    status='skipped', confidence_class='skipped',
                    review_reason=?, updated_at=?
                WHERE job_id=? AND id IN ({placeholders})
                  AND status NOT IN ('applying','applied','rolled_back')
                """,
                (reason, _utc_now(), str(job_id), *values),
            )
        summary = self._refresh_counts(job_id)
        self._write_reports(job_id)
        return summary

    def approve_review_item(
        self,
        job_id: str,
        item_id: int,
        selected_fields: Iterable[str],
        *,
        confirmed: bool = False,
        write_files: bool = False,
        expected_candidate_token: str | None = None,
    ) -> JobSummary:
        if not confirmed:
            raise RemediationError("explicit_review_confirmation_required")
        job_row = self._job_row(job_id)
        row = self.conn.execute(
            f"SELECT * FROM {REMEDIATION_ITEMS_TABLE} WHERE job_id=? AND id=?",
            (str(job_id), int(item_id)),
        ).fetchone()
        if row is None:
            raise RemediationError("remediation_item_not_found")
        item = dict(row)
        item_status = str(item["status"])
        resuming = item_status == "applying"
        if item_status not in {"needs_review", "ambiguous", "approved", "applying"}:
            raise RemediationError("remediation_item_not_reviewable")
        requested = {str(name) for name in selected_fields if str(name) in _REVIEW_FIELDS}
        if resuming:
            mode = str(job_row["mode"])
            if mode not in {"review_apply_files", "review_apply_database"}:
                raise RemediationError("review_apply_mode_invalid")
            if write_files != (mode == "review_apply_files"):
                raise RemediationError("review_apply_mode_conflict")
            selected = {
                str(name)
                for name in _json_list(item.get("approved_fields"))
                if str(name) in _REVIEW_FIELDS
            }
            if requested and requested != selected:
                raise RemediationError("review_approval_fields_conflict")
        else:
            if str(job_row["status"]) not in {"ready", "complete", "complete_with_issues"}:
                raise RemediationError("remediation_job_not_review_ready")
            selected = requested
        if not selected:
            raise RemediationError("no_review_fields_selected")
        candidate = _json_object(item.get("candidate_snapshot"))
        if expected_candidate_token is not None and (
            str(expected_candidate_token) != candidate_review_token(candidate)
        ):
            raise RemediationError("review_candidate_changed")
        patch = {
            name: candidate[name]
            for name in selected - {"artwork"}
            if candidate.get(name) not in (None, "")
        }
        if not patch and "artwork" not in selected:
            raise RemediationError("no_review_fields_selected")
        snapshot = _json_object(item["current_snapshot"])
        if not self._snapshot_still_current(snapshot):
            raise RemediationError("remediation_item_stale")
        snapshot_fields = snapshot.get("fields")
        if isinstance(snapshot_fields, Mapping):
            for name in selected:
                state = snapshot_fields.get(name)
                if isinstance(state, Mapping) and (
                    bool(state.get("is_locked"))
                    or str(state.get("provenance") or "") in _PROTECTED_PROVENANCE
                ):
                    raise RemediationError("locked_field_review_rejected")
        path = Path(str(snapshot.get("path") or ""))
        preflight_fingerprint = None
        if not resuming:
            if write_files and self.tag_writer.supports(path):
                if not self._media_snapshot_still_current(snapshot):
                    raise RemediationError("media_changed_after_analysis")
                try:
                    preflight_fingerprint = self.tag_writer.fingerprint(path)
                except Exception as exc:
                    raise RemediationError("media_preflight_failed") from exc
            self._verify_review_item_disk_space(
                snapshot,
                write_files=write_files,
                artwork_requested="artwork" in selected,
            )
        database_backup = self._ensure_database_backup(job_id)
        if not resuming:
            now = _utc_now()
            with self.conn:
                self.conn.execute(
                    f"""
                    UPDATE {REMEDIATION_ITEMS_TABLE} SET
                        status='applying', review_reason='user_confirmed_pending',
                        approved_fields=?, proposed_patch=?, apply_error=NULL,
                        original_file_hash=COALESCE(original_file_hash, ?),
                        original_audio_payload_hash=COALESCE(original_audio_payload_hash, ?),
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        _json(sorted(selected)),
                        _json(patch),
                        preflight_fingerprint.full_sha256
                        if preflight_fingerprint is not None
                        else None,
                        preflight_fingerprint.audio_payload_sha256
                        if preflight_fingerprint is not None
                        else None,
                        now,
                        int(item_id),
                    ),
                )
                self.conn.execute(
                    f"""
                    UPDATE {REMEDIATION_JOBS_TABLE} SET
                        status='applying', mode=?, finished_at=NULL,
                        last_error=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (
                        "review_apply_files" if write_files else "review_apply_database",
                        now,
                        str(job_id),
                    ),
                )
            row = self.conn.execute(
                f"SELECT * FROM {REMEDIATION_ITEMS_TABLE} WHERE id=?", (int(item_id),)
            ).fetchone()
            if row is None:
                raise RemediationError("remediation_item_not_found")
            item = dict(row)
        recording_id = str(
            item.get("provider_recording_id") or candidate.get("recording_id") or ""
        ).strip() or None
        release_id = None
        if selected & {
            "album",
            "album_artist",
            "release_date",
            "artwork",
        }:
            release_id = str(
                item.get("provider_release_id") or candidate.get("release_id") or ""
            ).strip() or None
        confidence = (
            float(item["confidence_score"])
            if item.get("confidence_score") is not None
            else None
        )
        artwork_path = None
        originals_dir = self.backups_root / str(job_id) / "originals"
        originals_dir.mkdir(parents=True, exist_ok=True)
        backup = None
        file_result = None
        prepared = None
        commit_attempted = False
        file_status = "not_requested"
        try:
            if "artwork" in selected:
                existing_artwork = str(
                    _json_object(item.get("proposed_patch")).get("artwork") or ""
                ).strip()
                if existing_artwork and Path(existing_artwork).is_file():
                    artwork_path = existing_artwork
                else:
                    artwork_item = dict(item)
                    if not artwork_item.get("artwork_candidate") and release_id:
                        artwork_item["artwork_candidate"] = _json(
                            {"provider": "cover_art_archive", "release_id": release_id}
                        )
                    artwork_path, issue = self._prepare_candidate_artwork(
                        artwork_item, snapshot
                    )
                    if artwork_path is None:
                        raise RemediationError(issue or "artwork_not_available")
                patch["artwork"] = artwork_path
                with self.conn:
                    self.conn.execute(
                        f"UPDATE {REMEDIATION_ITEMS_TABLE} SET proposed_patch=?, updated_at=? WHERE id=?",
                        (_json(patch), _utc_now(), int(item_id)),
                    )
            if write_files and self.tag_writer.supports(path):
                tag_patch = dict(patch)
                if recording_id:
                    tag_patch["musicbrainz_recording_id"] = recording_id
                if release_id:
                    tag_patch["musicbrainz_release_id"] = release_id
                recovered = self._recover_prepared_write(item, path)
                if recovered is not None:
                    backup, file_result = recovered
                else:
                    backup = self.tag_writer.create_backup(
                        path,
                        originals_dir,
                        identity=f"track-{int(item['track_id'])}",
                        expected_full_sha256=(
                            str(item["original_file_hash"])
                            if item.get("original_file_hash")
                            else None
                        ),
                    )
                    prepared = self.tag_writer.prepare(
                        path,
                        tag_patch,
                        expected_full_sha256=backup.fingerprint.full_sha256,
                        artwork_path=artwork_path,
                    )
                    with self.conn:
                        self.conn.execute(
                            f"""
                            UPDATE {REMEDIATION_ITEMS_TABLE} SET
                                status='applying', file_write_status='prepared',
                                original_file_hash=?, original_audio_payload_hash=?,
                                backup_file=?, prepared_file=?, updated_file_hash=?,
                                updated_audio_payload_hash=?, updated_at=? WHERE id=?
                            """,
                            (
                                backup.fingerprint.full_sha256,
                                backup.fingerprint.audio_payload_sha256,
                                str(backup.backup_path),
                                str(prepared.temporary_path),
                                prepared.updated.full_sha256,
                                prepared.updated.audio_payload_sha256,
                                _utc_now(),
                                int(item_id),
                            ),
                        )
                    commit_attempted = True
                    file_result = self.tag_writer.commit(prepared, backup=backup)
                file_status = "verified"
            elif write_files:
                file_status = "unsupported"
            with self.conn:
                if not self.conn.in_transaction:
                    self.conn.execute("BEGIN IMMEDIATE")
                if not self._snapshot_still_current(snapshot):
                    raise RemediationError("metadata_precondition_changed")
                result = self.metadata.apply_confirmed_candidate(
                    int(item["track_id"]),
                    patch,
                    recording_id=recording_id,
                    release_id=release_id,
                    confidence=confidence,
                    artwork_path=artwork_path,
                    commit=False,
                )
                if set(result.changed_fields) != set(patch):
                    raise RemediationError("metadata_precondition_changed")
                applied_track = self.db.get_track(int(item["track_id"]))
                if applied_track is None:
                    raise RemediationError("applied_track_missing")
                applied_snapshot = _snapshot_dict(result.after, dict(applied_track))
                now = _utc_now()
                self.conn.execute(
                    f"""
                    UPDATE {REMEDIATION_ITEMS_TABLE} SET
                        status='applied', file_write_status=?, review_reason='user_confirmed',
                        prepared_file=NULL,
                        original_file_hash=COALESCE(original_file_hash, ?),
                        original_audio_payload_hash=COALESCE(original_audio_payload_hash, ?),
                        backup_file=COALESCE(backup_file, ?),
                        updated_file_hash=COALESCE(updated_file_hash, ?),
                        updated_audio_payload_hash=COALESCE(updated_audio_payload_hash, ?),
                        applied_change_group_id=?, applied_snapshot=?,
                        apply_error=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (
                        file_status,
                        backup.fingerprint.full_sha256 if backup else None,
                        backup.fingerprint.audio_payload_sha256 if backup else None,
                        str(backup.backup_path) if backup else None,
                        file_result.updated.full_sha256 if file_result else None,
                        file_result.updated.audio_payload_sha256 if file_result else None,
                        result.change_group_id,
                        _json(applied_snapshot),
                        now,
                        int(item_id),
                    ),
                )
                remaining_high = int(
                    self.conn.execute(
                        f"""
                        SELECT COUNT(*) FROM {REMEDIATION_ITEMS_TABLE}
                        WHERE job_id=? AND status='high_confidence'
                        """,
                        (str(job_id),),
                    ).fetchone()[0]
                )
                next_status = "ready" if remaining_high else "complete"
                self.conn.execute(
                    f"""
                    UPDATE {REMEDIATION_JOBS_TABLE} SET
                        status=?, library_revision=?,
                        finished_at=CASE WHEN ?='complete' THEN ? ELSE NULL END,
                        updated_at=?, last_error=NULL
                    WHERE id=?
                    """,
                    (
                        next_status,
                        self.library_revision(),
                        next_status,
                        now,
                        now,
                        str(job_id),
                    ),
                )
        except Exception as exc:
            restore_status = self._reconcile_failed_file_write(
                item=item,
                path=path,
                backup=backup,
                prepared=prepared,
                file_result=file_result,
                commit_attempted=commit_attempted,
            )
            self._mark_item_issue(
                int(item_id),
                status="conflict" if restore_status == "conflict" else "apply_failed",
                file_status=restore_status,
                error=sanitize_error_text(exc, 200),
            )
            self._set_job_status(job_id, "complete_with_issues", error=str(exc), finish=True)
            self._write_apply_manifests(job_id, database_backup)
            self._write_reports(job_id)
            raise RemediationError("review_item_apply_failed") from exc
        summary = self._refresh_counts(job_id)
        self._write_apply_manifests(job_id, database_backup)
        self._write_reports(job_id)
        return summary

    def clear_completed_job(self, job_id: str) -> None:
        summary = self._summary(self._job_row(job_id))
        if summary.status not in {"cancelled", "rolled_back", "complete"}:
            raise RemediationError("remediation_job_not_clearable")
        if summary.applied and summary.status != "rolled_back":
            raise RemediationError("applied_job_requires_rollback_before_clear")
        with self.conn:
            self.conn.execute(
                f"DELETE FROM {REMEDIATION_JOBS_TABLE} WHERE id=?", (str(job_id),)
            )

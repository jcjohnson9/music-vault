from __future__ import annotations

"""Aggregate-only Batch 10.4 cache and quiescence acceptance helpers.

This module is intentionally suitable for import by tests.  It performs no
work at import time and never returns artist identities, provider queries,
URLs, media paths, or credential contents.  The only operation that writes
inside a runtime root is the explicitly acknowledged SQLite backup command.
All other capture and verification helpers are read-only.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.metadata.artist_images import (  # noqa: E402
    ARTIST_IMAGE_CACHE_SCHEMA_VERSION,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_PIXELS,
    PUBLIC_IMAGE_HOSTS,
    ArtistImageContentError,
    ArtistImageStatus,
    is_safe_artist_source_url,
    validate_image_payload,
)
from tools.dev import batch10_3_acceptance as acceptance  # noqa: E402


SCHEMA_VERSION = 7
MANIFEST_FORMAT_VERSION = 1
ARTIST_CACHE_AUDIT_FORMAT_VERSION = 1
LIVE_ACKNOWLEDGEMENT = "batch10.4-live-schema7-quiescence"
BACKUP_PREFIX = "music_vault_batch10_4_pre_quiescence_acceptance_"
EVIDENCE_PREFIX = "MusicVault_Batch10_4_"

ALLOWED_DEFER_REASONS = frozenset(
    {"migration_startup", "acceptance_no_network", "acceptance_no_secrets"}
)
PRODUCTION_PROVIDER_LABELS = {
    "Discogs": "discogs",
    "Wikimedia Commons": "wikimedia_commons",
}
SYNTHETIC_PROVIDER_LABELS = {
    "Synthetic review provider": "synthetic",
    "Synthetic Wikimedia fallback": "synthetic",
}
EXPECTED_CACHE_RECORD_FIELDS = frozenset(
    {
        "status",
        "requested_display_name",
        "normalized_key",
        "identity_key",
        "matched_artist_name",
        "musicbrainz_artist_id",
        "discogs_artist_id",
        "match_score",
        "image_provider",
        "attribution_text",
        "source_page_url",
        "image_url",
        "cache_file",
        "content_type",
        "fetched_at",
        "retry_after",
        "error_code",
    }
)
REQUIRED_CACHE_RECORD_FIELDS = frozenset(
    {"status", "normalized_key", "cache_file", "content_type"}
)
PRIVATE_STATUS_PATH_FIELDS = frozenset(
    {"project_root", "data_dir", "database", "downloads", "config", "status_file"}
)
PRIVATE_STATUS_PLAYBACK_FIELDS = frozenset(
    {"currently_playing", "current_title", "current_artist", "current_album"}
)
PRIVATE_STATUS_SYNC_FIELDS = frozenset(
    {"last_sync_playlist_title", "last_sync_playlist_id", "last_sync_error"}
)
SUSPICIOUS_SECRET_FIELD_RE = re.compile(
    r"(?:^|_)(?:api_?key|token|secret|authorization|credential)(?:$|_)", re.I
)
SUSPICIOUS_SECRET_VALUE_RE = re.compile(
    r"(?:bearer\s+|authorization\s*[:=]|(?:api[_-]?key|token|secret)\s*[:=])",
    re.I,
)
PRIVATE_LOCATOR_RE = re.compile(r"(?:https?://|[A-Za-z]:[\\/]|\\\\)", re.I)
ITEM_DETAIL_FIELD_RE = re.compile(
    r"(?:^|_)(?:query|image_url|source_url|artist_name|album_name|provider_item)(?:$|_)",
    re.I,
)
CONTENT_FILE_RE = re.compile(r"^[0-9a-f]{64}\.(?:jpg|png|webp)$")
ENTRY_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


class Batch104Failure(acceptance.AcceptanceFailure):
    """A stable failure that cannot contain private runtime values."""


class _DuplicateJSONKey(ValueError):
    pass


def _strict_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONKey("duplicate_json_key")
        result[key] = value
    return result


def _strict_json(path: Path, *, maximum_bytes: int = 4 * 1024 * 1024) -> Any:
    if path.is_symlink() or not path.is_file():
        raise Batch104Failure("json_file_invalid")
    if path.stat().st_size > maximum_bytes:
        raise Batch104Failure("json_file_too_large")
    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object
        )
    except _DuplicateJSONKey as exc:
        raise Batch104Failure("json_duplicate_key") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise Batch104Failure("json_parse_failed") from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _stat_guard(path: Path, *, hash_content: bool = False) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {"exists": False, "size": 0, "mtime_ns": 0, "sha256": None}
    if path.is_symlink() or not path.is_file():
        raise Batch104Failure("guard_file_invalid")
    return {
        "exists": True,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
        "sha256": acceptance.sha256_file(path) if hash_content else None,
    }


def _tree_guard(root: Path) -> dict[str, Any]:
    directory = Path(root).expanduser().resolve(strict=False)
    if not directory.exists():
        return {
            "exists": False,
            "file_count": 0,
            "total_bytes": 0,
            "inventory_digest": acceptance.aggregate_digest(()),
        }
    if directory.is_symlink() or not directory.is_dir():
        raise Batch104Failure("guard_tree_invalid")
    records: list[str] = []
    total_bytes = 0
    count = 0
    for candidate in sorted(directory.rglob("*"), key=lambda item: item.as_posix()):
        if candidate.is_symlink():
            raise Batch104Failure("guard_tree_symlink")
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(directory).as_posix()
        stat = candidate.stat()
        count += 1
        total_bytes += int(stat.st_size)
        records.append(
            acceptance.row_digest(
                (
                    hashlib.sha256(relative.encode("utf-8")).hexdigest(),
                    int(stat.st_size),
                    int(stat.st_mtime_ns),
                    acceptance.sha256_file(candidate),
                )
            )
        )
    return {
        "exists": True,
        "file_count": count,
        "total_bytes": total_bytes,
        "inventory_digest": acceptance.aggregate_digest(records),
    }


def _safe_image_url(value: object) -> bool:
    if value in (None, ""):
        return True
    try:
        parsed = urlsplit(str(value))
        port = parsed.port
    except (TypeError, ValueError):
        return False
    host = (parsed.hostname or "").rstrip(".").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or host not in PUBLIC_IMAGE_HOSTS
        or parsed.fragment
    ):
        return False
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if SUSPICIOUS_SECRET_FIELD_RE.search(key.replace("-", "_")):
            return False
    return True


def _fixed_provider_bucket(value: object, *, allow_synthetic: bool) -> str | None:
    if value in (None, ""):
        return "none"
    text = str(value)
    if text in PRODUCTION_PROVIDER_LABELS:
        return PRODUCTION_PROVIDER_LABELS[text]
    if allow_synthetic and text in SYNTHETIC_PROVIDER_LABELS:
        return SYNTHETIC_PROVIDER_LABELS[text]
    return None


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            SUSPICIOUS_SECRET_FIELD_RE.search(str(key))
            or _contains_secret_field(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_field(child) for child in value)
    return False


def _contains_secret_marker(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_secret_marker(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_marker(child) for child in value)
    return isinstance(value, str) and bool(SUSPICIOUS_SECRET_VALUE_RE.search(value))


def _contains_private_locator(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            ITEM_DETAIL_FIELD_RE.search(str(key))
            or _contains_private_locator(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_private_locator(child) for child in value)
    return isinstance(value, str) and bool(PRIVATE_LOCATOR_RE.search(value))


def _safe_source_url(value: object) -> bool:
    if value in (None, ""):
        return True
    if not is_safe_artist_source_url(value):
        return False
    try:
        parsed = urlsplit(str(value))
    except ValueError:
        return False
    return not any(
        SUSPICIOUS_SECRET_FIELD_RE.search(key.replace("-", "_"))
        for key, _item in parse_qsl(parsed.query, keep_blank_values=True)
    )


def audit_artist_cache(
    cache_root: Path,
    *,
    expected_file_count: int | None = None,
    expected_total_bytes: int | None = None,
    allow_synthetic: bool = False,
) -> dict[str, Any]:
    """Validate the private artist cache while returning aggregates only."""

    root = Path(cache_root).expanduser().resolve(strict=False)
    issue_names = (
        "root_invalid",
        "index_invalid",
        "unexpected_entry",
        "path_violation",
        "missing_image",
        "invalid_image",
        "invalid_provider",
        "unsafe_url",
        "secret_field",
        "temporary_file",
        "unexpected_payload",
        "content_address_mismatch",
        "expected_aggregate_mismatch",
    )
    issues = {name: 0 for name in issue_names}
    provider_counts = {
        "discogs": 0,
        "wikimedia_commons": 0,
        "synthetic": 0,
        "none": 0,
    }
    status_counts = {status.value: 0 for status in ArtistImageStatus}
    result: dict[str, Any] = {
        "audit_format_version": ARTIST_CACHE_AUDIT_FORMAT_VERSION,
        "ok": False,
        "checks": {},
        "issues": issues,
        "counts": {
            "tree_file_count": 0,
            "tree_total_bytes": 0,
            "index_entry_count": 0,
            "resolved_entry_count": 0,
            "referenced_image_count": 0,
            "physical_image_count": 0,
            "orphan_image_count": 0,
            "total_image_pixels": 0,
            "maximum_image_pixels": 0,
            "provider_counts": provider_counts,
            "status_counts": status_counts,
        },
        "raw_identity_values_emitted": False,
        "urls_emitted": False,
        "credential_contents_read": False,
        "read_only": True,
    }
    if not root.exists() or root.is_symlink() or not root.is_dir():
        issues["root_invalid"] += 1
        result["checks"] = {"cache_root_valid": False}
        return result

    physical_files: list[Path] = []
    total_bytes = 0
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if candidate.is_symlink():
            issues["path_violation"] += 1
            continue
        if not candidate.is_file():
            continue
        physical_files.append(candidate)
        total_bytes += int(candidate.stat().st_size)
        if candidate.name.startswith(".") or candidate.suffix.casefold() in {
            ".tmp",
            ".part",
            ".partial",
            ".download",
        }:
            issues["temporary_file"] += 1
    result["counts"]["tree_file_count"] = len(physical_files)
    result["counts"]["tree_total_bytes"] = total_bytes

    index_path = root / "index.json"
    try:
        payload = _strict_json(index_path)
    except Batch104Failure:
        issues["index_invalid"] += 1
        result["checks"] = {"cache_root_valid": True, "index_parses": False}
        return result
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != ARTIST_IMAGE_CACHE_SCHEMA_VERSION
        or set(payload) != {"schema_version", "entries"}
        or not isinstance(payload.get("entries"), dict)
    ):
        issues["index_invalid"] += 1
        result["checks"] = {"cache_root_valid": True, "index_parses": False}
        return result

    entries: Mapping[str, Any] = payload["entries"]
    result["counts"]["index_entry_count"] = len(entries)
    files_dir = (root / "files").resolve(strict=False)
    referenced: dict[Path, str] = {}
    validated_physical: set[Path] = set()

    for entry_key, record in entries.items():
        if not ENTRY_KEY_RE.fullmatch(str(entry_key)) or not isinstance(record, dict):
            issues["unexpected_entry"] += 1
            continue
        record_fields = set(record)
        if (
            not REQUIRED_CACHE_RECORD_FIELDS <= record_fields
            or not record_fields <= EXPECTED_CACHE_RECORD_FIELDS
        ):
            issues["unexpected_entry"] += 1
        if _contains_secret_field(record) or _contains_secret_marker(record):
            issues["secret_field"] += 1

        identity_key = record.get("identity_key")
        normalized_key = record.get("normalized_key")
        possible_keys = {_sha256_bytes(str(normalized_key).encode("utf-8"))}
        if identity_key not in (None, ""):
            possible_keys.add(_sha256_bytes(str(identity_key).encode("utf-8")))
        if entry_key not in possible_keys:
            issues["unexpected_entry"] += 1

        try:
            status = ArtistImageStatus(str(record.get("status")))
        except ValueError:
            issues["unexpected_entry"] += 1
            continue
        status_counts[status.value] += 1

        provider_bucket = _fixed_provider_bucket(
            record.get("image_provider"), allow_synthetic=allow_synthetic
        )
        if provider_bucket is None or (
            status is ArtistImageStatus.RESOLVED and provider_bucket == "none"
        ):
            issues["invalid_provider"] += 1
        else:
            provider_counts[provider_bucket] += 1

        source_url = record.get("source_page_url")
        image_url = record.get("image_url")
        if not _safe_source_url(source_url):
            issues["unsafe_url"] += 1
        if not _safe_image_url(image_url):
            issues["unsafe_url"] += 1

        relative_value = record.get("cache_file")
        if status is not ArtistImageStatus.RESOLVED:
            if relative_value not in (None, ""):
                issues["path_violation"] += 1
            continue

        result["counts"]["resolved_entry_count"] += 1
        relative_text = str(relative_value or "").replace("\\", "/")
        relative = PurePosixPath(relative_text)
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != "files"
            or not CONTENT_FILE_RE.fullmatch(relative.parts[1])
        ):
            issues["path_violation"] += 1
            continue
        candidate = (root / Path(*relative.parts)).resolve(strict=False)
        if candidate.parent != files_dir or not acceptance.is_within(candidate, root):
            issues["path_violation"] += 1
            continue
        if candidate.is_symlink() or not candidate.is_file():
            issues["missing_image"] += 1
            continue
        previous_key = referenced.get(candidate)
        if previous_key is not None and previous_key != entry_key:
            # Content sharing is valid only when both records point at the
            # exact same immutable digest filename. Conflicting metadata is
            # detected by requiring the content address below.
            pass
        referenced[candidate] = entry_key
        try:
            image_bytes = candidate.read_bytes()
            validated = validate_image_payload(
                image_bytes,
                str(record.get("content_type") or ""),
                max_bytes=MAX_IMAGE_BYTES,
                max_pixels=MAX_IMAGE_PIXELS,
            )
        except (OSError, ArtistImageContentError):
            issues["invalid_image"] += 1
            continue
        actual_digest = _sha256_bytes(image_bytes)
        if (
            candidate.stem != actual_digest
            or candidate.suffix.casefold().lstrip(".") != validated.extension
        ):
            issues["content_address_mismatch"] += 1
        validated_physical.add(candidate)
        pixels = int(validated.width) * int(validated.height)
        result["counts"]["total_image_pixels"] += pixels
        result["counts"]["maximum_image_pixels"] = max(
            int(result["counts"]["maximum_image_pixels"]), pixels
        )

    physical_images: set[Path] = set()
    for candidate in physical_files:
        if candidate == index_path:
            continue
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            issues["path_violation"] += 1
            continue
        if (
            len(relative.parts) != 2
            or relative.parts[0] != "files"
            or not CONTENT_FILE_RE.fullmatch(relative.name)
        ):
            issues["unexpected_payload"] += 1
            continue
        physical_images.add(candidate.resolve())
        if candidate.resolve() in validated_physical:
            continue
        mime_by_extension = {
            "jpg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }
        try:
            image_bytes = candidate.read_bytes()
            validated = validate_image_payload(
                image_bytes,
                mime_by_extension[candidate.suffix.casefold().lstrip(".")],
            )
            if candidate.stem != _sha256_bytes(image_bytes):
                raise ArtistImageContentError("content_address_mismatch")
            pixels = int(validated.width) * int(validated.height)
            result["counts"]["total_image_pixels"] += pixels
            result["counts"]["maximum_image_pixels"] = max(
                int(result["counts"]["maximum_image_pixels"]), pixels
            )
        except (KeyError, OSError, ArtistImageContentError):
            issues["invalid_image"] += 1

    result["counts"]["referenced_image_count"] = len(referenced)
    result["counts"]["physical_image_count"] = len(physical_images)
    result["counts"]["orphan_image_count"] = len(physical_images - set(referenced))
    if expected_file_count is not None and len(physical_files) != int(expected_file_count):
        issues["expected_aggregate_mismatch"] += 1
    if expected_total_bytes is not None and total_bytes != int(expected_total_bytes):
        issues["expected_aggregate_mismatch"] += 1

    checks = {
        "cache_root_valid": True,
        "index_parses": issues["index_invalid"] == 0,
        "entries_well_formed": issues["unexpected_entry"] == 0,
        "all_paths_contained": issues["path_violation"] == 0,
        "all_referenced_images_exist": issues["missing_image"] == 0,
        "all_images_decode_within_limits": issues["invalid_image"] == 0,
        "providers_recognized": issues["invalid_provider"] == 0,
        "provenance_urls_safe": issues["unsafe_url"] == 0,
        "no_secret_fields": issues["secret_field"] == 0,
        "no_partial_files": issues["temporary_file"] == 0,
        "no_unexpected_payloads": issues["unexpected_payload"] == 0,
        "content_addresses_valid": issues["content_address_mismatch"] == 0,
        "expected_aggregates_match": issues["expected_aggregate_mismatch"] == 0,
    }
    result["checks"] = checks
    result["ok"] = all(checks.values())
    return result


def _database_file_guard(database: Path) -> dict[str, Any]:
    result = _stat_guard(database, hash_content=True)
    sidecars = {
        suffix: _stat_guard(Path(str(database) + suffix), hash_content=True)
        for suffix in ("-wal", "-shm", "-journal")
    }
    return {"database": result, "sidecars": sidecars}


def _status_file_guard(status_path: Path) -> dict[str, Any]:
    return _stat_guard(status_path, hash_content=True)


def capture_quiescence_baseline(
    *,
    project_root: Path,
    data_dir: Path,
    database: Path,
    expected_cache_file_count: int | None = None,
    expected_cache_total_bytes: int | None = None,
) -> dict[str, Any]:
    """Capture a schema-7 baseline without exposing private values."""

    root = Path(project_root).expanduser().resolve()
    data = Path(data_dir).expanduser().resolve()
    db_path = Path(database).expanduser().resolve()
    if not acceptance.is_within(data, root) or not acceptance.is_within(db_path, data):
        raise Batch104Failure("runtime_scope_invalid")
    database_state = acceptance.capture_database_baseline(
        project_root=root,
        data_dir=data,
        database=db_path,
        expected_schema=SCHEMA_VERSION,
    )
    cache_audit = audit_artist_cache(
        data / "artist_images",
        expected_file_count=expected_cache_file_count,
        expected_total_bytes=expected_cache_total_bytes,
    )
    if not cache_audit["ok"]:
        raise Batch104Failure("artist_cache_audit_failed")
    return {
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "database_state": database_state,
        "database_file": _database_file_guard(db_path),
        "artist_cache_audit": cache_audit,
        "cover_tree": _tree_guard(data / "covers"),
        "discogs_provider_cache": _tree_guard(data / "provider_cache" / "discogs"),
        "discogs_release_art_cache": _tree_guard(
            data / "covers" / "providers" / "cover_art_archive"
        ),
        "app_status_before": _status_file_guard(data / "music_vault_status.json"),
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
        "media_contents_read": False,
        "read_only": True,
    }


def _expected_count_checks(
    baseline: Mapping[str, Any], expected_counts: Mapping[str, int]
) -> dict[str, bool]:
    tables = baseline["database_state"]["database"]["tables"]
    return {
        key: key in tables and int(tables[key]["count"]) == int(value)
        for key, value in expected_counts.items()
    }


def create_schema7_backup(
    *,
    database: Path,
    backup: Path,
    baseline: Mapping[str, Any],
    expected_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Create and verify one schema-7 backup using SQLite's backup API."""

    count_checks = _expected_count_checks(baseline, expected_counts or {})
    if count_checks and not all(count_checks.values()):
        raise Batch104Failure("live_aggregate_precondition_failed")
    backup_evidence = acceptance.create_verified_sqlite_backup(
        database=database,
        backup=backup,
        baseline=baseline["database_state"],
        expected_schema=SCHEMA_VERSION,
    )
    return {
        **backup_evidence,
        "table_count_checks": count_checks,
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
    }


def prepare_live_acceptance(
    *,
    project_root: Path,
    evidence_dir: Path,
    acknowledgement: str,
    expected_counts: Mapping[str, int] | None = None,
    expected_cache_file_count: int | None = None,
    expected_cache_total_bytes: int | None = None,
) -> dict[str, Any]:
    if acknowledgement != LIVE_ACKNOWLEDGEMENT:
        raise Batch104Failure("live_acknowledgement_missing")
    root = Path(project_root).expanduser().resolve()
    evidence = Path(evidence_dir).expanduser().resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if (
        not acceptance.is_within(evidence, temp)
        or evidence == temp
        or acceptance.is_within(evidence, root)
        or not evidence.name.startswith(EVIDENCE_PREFIX)
        or evidence.is_symlink()
    ):
        raise Batch104Failure("evidence_scope_invalid")
    if evidence.exists() and any(evidence.iterdir()):
        raise Batch104Failure("evidence_directory_not_empty")
    evidence.mkdir(parents=True, exist_ok=True)
    data = root / "data"
    database = data / "music_vault.sqlite3"
    pre_backup = capture_quiescence_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_cache_file_count=expected_cache_file_count,
        expected_cache_total_bytes=expected_cache_total_bytes,
    )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]
    backup_name = f"{BACKUP_PREFIX}{timestamp}.sqlite3"
    backup_path = data / "backups" / backup_name
    backup = create_schema7_backup(
        database=database,
        backup=backup_path,
        baseline=pre_backup,
        expected_counts=expected_counts,
    )
    baseline = capture_quiescence_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_cache_file_count=expected_cache_file_count,
        expected_cache_total_bytes=expected_cache_total_bytes,
    )
    if baseline["database_state"]["database"] != pre_backup["database_state"]["database"]:
        raise Batch104Failure("backup_changed_live_database")
    return {
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "baseline": baseline,
        "backup_name": backup_name,
        "backup": backup,
        "expected_counts": dict(expected_counts or {}),
        "expected_cache_file_count": expected_cache_file_count,
        "expected_cache_total_bytes": expected_cache_total_bytes,
        "execution_policy": {
            "no_secrets_required": True,
            "no_network_required": True,
            "graceful_close_required": True,
            "app_status_may_change": True,
        },
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
    }


def validate_safe_app_status(path: Path) -> dict[str, Any]:
    """Validate only stable safety fields; never return item-level values."""

    payload = _strict_json(Path(path), maximum_bytes=2 * 1024 * 1024)
    if not isinstance(payload, dict):
        raise Batch104Failure("app_status_invalid")
    health = payload.get("health")
    paths = payload.get("paths")
    playback = payload.get("playback")
    sync = payload.get("sync")
    checks = {
        "api_ready_false": isinstance(health, dict) and health.get("api_ready") is False,
        "discogs_ready_false": payload.get("discogs_ready") is False,
        "provider_work_deferred": payload.get("provider_work_deferred") is True,
        "defer_reason_safe": payload.get("provider_work_defer_reason")
        in ALLOWED_DEFER_REASONS,
        "private_paths_suppressed": isinstance(paths, dict)
        and all(paths.get(field) is None for field in PRIVATE_STATUS_PATH_FIELDS),
        "playback_identity_suppressed": isinstance(playback, dict)
        and all(playback.get(field) is None for field in PRIVATE_STATUS_PLAYBACK_FIELDS),
        "sync_identity_suppressed": isinstance(sync, dict)
        and all(sync.get(field) is None for field in PRIVATE_STATUS_SYNC_FIELDS),
        "sync_item_details_suppressed": isinstance(sync, dict)
        and sync.get("last_sync_failures") in (None, []),
        "sync_values_aggregate_only": isinstance(sync, dict)
        and all(
            value is None
            or isinstance(value, (str, int, float, bool))
            or (key == "last_sync_failures" and value == [])
            for key, value in sync.items()
        ),
        "no_secret_value_fields": not _contains_secret_field(payload)
        and not _contains_secret_marker(payload),
        "no_private_locator_or_item_fields": not _contains_private_locator(payload),
    }
    return {
        "verified": all(checks.values()),
        "checks": checks,
        "schema_version": int(payload.get("schema_version", 0) or 0),
        "provider_work_defer_reason": (
            payload.get("provider_work_defer_reason")
            if payload.get("provider_work_defer_reason") in ALLOWED_DEFER_REASONS
            else None
        ),
        "identity_values_emitted": False,
        "paths_emitted": False,
        "credential_contents_read": False,
    }


def verify_live_quiescence(
    *,
    project_root: Path,
    manifest: Mapping[str, Any],
    network_report: Path,
    graceful_close_confirmed: bool,
) -> dict[str, Any]:
    if manifest.get("manifest_format_version") != MANIFEST_FORMAT_VERSION:
        raise Batch104Failure("manifest_version_invalid")
    if not graceful_close_confirmed:
        raise Batch104Failure("graceful_close_not_confirmed")
    acceptance.ensure_no_secret_mode()
    if os.environ.get("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK") != "1":
        raise Batch104Failure("no_network_environment_missing")
    root = Path(project_root).expanduser().resolve()
    data = root / "data"
    database = data / "music_vault.sqlite3"
    baseline = manifest.get("baseline")
    if not isinstance(baseline, dict):
        raise Batch104Failure("baseline_invalid")
    current = capture_quiescence_baseline(
        project_root=root,
        data_dir=data,
        database=database,
        expected_cache_file_count=manifest.get("expected_cache_file_count"),
        expected_cache_total_bytes=manifest.get("expected_cache_total_bytes"),
    )
    network = acceptance.verify_acceptance_network_report(network_report)
    status = validate_safe_app_status(data / "music_vault_status.json")
    checks = {
        "database_logical_state_unchanged": current["database_state"]
        == baseline.get("database_state"),
        "database_file_unchanged": current["database_file"]
        == baseline.get("database_file"),
        "media_unchanged": current["database_state"]["media"]
        == baseline.get("database_state", {}).get("media"),
        "track_cover_paths_unchanged": current["database_state"]["database"][
            "track_cover_path_digest"
        ]
        == baseline.get("database_state", {}).get("database", {}).get(
            "track_cover_path_digest"
        ),
        "referenced_covers_unchanged": current["database_state"]["artwork"][
            "referenced_cover_files"
        ]
        == baseline.get("database_state", {}).get("artwork", {}).get(
            "referenced_cover_files"
        ),
        "cover_tree_unchanged": current["cover_tree"] == baseline.get("cover_tree"),
        "artist_cache_unchanged": current["database_state"]["artwork"][
            "artist_image_tree"
        ]
        == baseline.get("database_state", {}).get("artwork", {}).get(
            "artist_image_tree"
        ),
        "artist_cache_still_valid": current["artist_cache_audit"]["ok"] is True,
        "discogs_provider_cache_unchanged": current["discogs_provider_cache"]
        == baseline.get("discogs_provider_cache"),
        "discogs_release_art_cache_unchanged": current["discogs_release_art_cache"]
        == baseline.get("discogs_release_art_cache"),
        "runtime_config_and_sidecars_unchanged": current["database_state"][
            "runtime_guards"
        ]
        == baseline.get("database_state", {}).get("runtime_guards"),
        "credential_metadata_unchanged": current["database_state"][
            "credential_metadata"
        ]
        == baseline.get("database_state", {}).get("credential_metadata"),
        "backup_inventory_unchanged": current["database_state"]["backup_inventory"]
        == baseline.get("database_state", {}).get("backup_inventory"),
        "zero_network_attempts": network["verified"] is True
        and int(network["attempt_count"]) == 0,
        "zero_provider_factory_invocations": int(
            network["provider_factory_invocation_count"]
        )
        == 0,
        "zero_provider_task_dispatches": int(network["provider_task_dispatch_count"])
        == 0,
        "safe_app_status": status["verified"] is True,
        "app_status_updated": current["app_status_before"]
        != baseline.get("app_status_before"),
        "official_dist_data_absent": not (root / "dist" / "MusicVault" / "data").exists(),
        "graceful_close_confirmed": graceful_close_confirmed,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "artist_cache": current["artist_cache_audit"]["counts"],
        "app_status": status,
        "network": network,
        "backup": manifest.get("backup"),
        "raw_library_values_emitted": False,
        "credential_contents_read": False,
        "media_contents_read": False,
    }


def _parse_expected_counts(values: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        name, separator, raw_count = value.partition("=")
        if not separator or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise Batch104Failure("expected_count_invalid")
        try:
            count = int(raw_count)
        except ValueError as exc:
            raise Batch104Failure("expected_count_invalid") from exc
        if count < 0:
            raise Batch104Failure("expected_count_invalid")
        result[name] = count
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit-cache")
    audit.add_argument("--cache-root", type=Path, required=True)
    audit.add_argument("--expected-file-count", type=int)
    audit.add_argument("--expected-total-bytes", type=int)
    audit.add_argument("--allow-synthetic", action="store_true")

    prepare = subparsers.add_parser("prepare-live")
    prepare.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    prepare.add_argument("--evidence-dir", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--acknowledge-live-library", required=True)
    prepare.add_argument("--expected-count", action="append", default=[])
    prepare.add_argument("--expected-cache-file-count", type=int)
    prepare.add_argument("--expected-cache-total-bytes", type=int)

    verify = subparsers.add_parser("verify-live")
    verify.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--network-report", type=Path, required=True)
    verify.add_argument("--graceful-close-confirmed", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "audit-cache":
            result = audit_artist_cache(
                args.cache_root,
                expected_file_count=args.expected_file_count,
                expected_total_bytes=args.expected_total_bytes,
                allow_synthetic=args.allow_synthetic,
            )
        elif args.command == "prepare-live":
            result = prepare_live_acceptance(
                project_root=args.project_root,
                evidence_dir=args.evidence_dir,
                acknowledgement=args.acknowledge_live_library,
                expected_counts=_parse_expected_counts(args.expected_count),
                expected_cache_file_count=args.expected_cache_file_count,
                expected_cache_total_bytes=args.expected_cache_total_bytes,
            )
            acceptance.atomic_write_json(args.manifest, result)
        else:
            result = verify_live_quiescence(
                project_root=args.project_root,
                manifest=acceptance.read_json(args.manifest),
                network_report=args.network_report,
                graceful_close_confirmed=args.graceful_close_confirmed,
            )
    except (Batch104Failure, acceptance.AcceptanceFailure, OSError, sqlite3.Error, KeyError):
        print(json.dumps({"ok": False, "error_code": "batch10_4_acceptance_failed"}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True or args.command == "prepare-live" else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ALLOWED_DEFER_REASONS",
    "ARTIST_CACHE_AUDIT_FORMAT_VERSION",
    "BACKUP_PREFIX",
    "Batch104Failure",
    "LIVE_ACKNOWLEDGEMENT",
    "MANIFEST_FORMAT_VERSION",
    "SCHEMA_VERSION",
    "audit_artist_cache",
    "capture_quiescence_baseline",
    "create_schema7_backup",
    "prepare_live_acceptance",
    "validate_safe_app_status",
    "verify_live_quiescence",
]

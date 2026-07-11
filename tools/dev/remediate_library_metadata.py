from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ["MUSIC_VAULT_ACCEPTANCE_NO_SECRETS"] = "1"

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB  # noqa: E402
from music_vault.core.paths import database_path  # noqa: E402
from music_vault.core.safety import sanitize_error_text  # noqa: E402
from music_vault.metadata.remediation import RemediationError, RemediationService  # noqa: E402


ACTIONS = (
    "status",
    "analyze",
    "apply-high-confidence",
    "resume",
    "export-private-report",
    "rollback",
    "verify-job",
)
_JOB_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,99}$")
_JOB_ID_REQUIRED_ACTIONS = frozenset(
    {
        "apply-high-confidence",
        "resume",
        "export-private-report",
        "rollback",
        "verify-job",
    }
)


class _ReadOnlyDatabase:
    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.db_path = path
        self.conn = connection


class _StatusOnlyDependency:
    def __getattr__(self, _name: str) -> object:
        raise RemediationError("status_is_read_only")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate-only Music Vault metadata remediation maintenance tool."
    )
    parser.add_argument("action", nargs="?", choices=ACTIONS, default="status")
    parser.add_argument("--job-id")
    parser.add_argument("--confirm-live-apply", action="store_true")
    parser.add_argument("--write-files", action="store_true")
    parser.add_argument("--confirm-rollback", action="store_true")
    return parser.parse_args(argv)


def _read_schema_version(path: Path) -> int | None:
    if not path.is_file():
        return None
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _require_job_id(value: str | None) -> str:
    job_id = str(value or "").strip()
    if not job_id:
        raise RemediationError("job_id_required")
    if not _JOB_ID_RE.fullmatch(job_id):
        raise RemediationError("job_id_invalid")
    return job_id.casefold()


def _validate_arguments(args: argparse.Namespace) -> None:
    if args.job_id is not None:
        args.job_id = _require_job_id(args.job_id)
    if args.action in _JOB_ID_REQUIRED_ACTIONS and args.job_id is None:
        raise RemediationError("job_id_required")

    if args.action == "apply-high-confidence":
        if not args.confirm_live_apply:
            if args.write_files:
                raise RemediationError("file_write_requires_live_apply_confirmation")
            raise RemediationError("explicit_apply_confirmation_required")
    elif args.confirm_live_apply or args.write_files:
        raise RemediationError("apply_flags_require_apply_action")

    if args.action == "rollback":
        if not args.confirm_rollback:
            raise RemediationError("explicit_rollback_confirmation_required")
    elif args.confirm_rollback:
        raise RemediationError("rollback_confirmation_requires_rollback_action")


def _safe_error_code(error: object) -> str:
    candidate = sanitize_error_text(error, 100).strip().casefold()
    return candidate if _ERROR_CODE_RE.fullmatch(candidate) else "remediation_tool_failed"


def _status_payload(
    path: Path,
    schema: int | None,
    job_id: str | None,
) -> dict[str, object]:
    base: dict[str, object] = {
        "ok": True,
        "action": "status",
        "database_exists": schema is not None,
        "schema_version": schema,
        "current_schema_version": CURRENT_SCHEMA_VERSION,
        "job": None,
    }
    if schema is None:
        base["migration_required"] = False
        return base
    if schema != CURRENT_SCHEMA_VERSION:
        base["migration_required"] = schema < CURRENT_SCHEMA_VERSION
        base["schema_supported"] = schema < CURRENT_SCHEMA_VERSION
        return base

    connection, service = _open_read_only_service(path)
    try:
        summary = service.status(job_id)
        base["job"] = summary.aggregate_dict() if summary else None
        base["migration_required"] = False
        base["schema_supported"] = True
        return base
    finally:
        connection.close()


def _open_read_only_service(
    path: Path,
) -> tuple[sqlite3.Connection, RemediationService]:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    database = _ReadOnlyDatabase(path, connection)
    disabled = _StatusOnlyDependency()
    service = RemediationService(
        database,
        provider=disabled,
        cover_provider=disabled,
        tag_writer=disabled,
    )
    return connection, service


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    database: MusicVaultDB | None = None
    try:
        _validate_arguments(args)
        path = database_path()
        schema = _read_schema_version(path)
        if args.action == "status":
            _emit(_status_payload(path, schema, args.job_id))
            return 0
        if schema is not None and schema > CURRENT_SCHEMA_VERSION:
            raise RemediationError("database_schema_not_supported")
        if args.action != "analyze":
            if schema is None:
                raise RemediationError("database_missing")
            if schema < CURRENT_SCHEMA_VERSION:
                raise RemediationError("database_schema_migration_required")

        database = MusicVaultDB(path)
        service = RemediationService(database)
        if args.action == "status":
            raise RemediationError("status_dispatch_error")
        elif args.action == "analyze":
            summary, metrics = service.analyze(args.job_id)
            _emit(
                {
                    "ok": True,
                    "action": "analyze",
                    **summary.aggregate_dict(),
                    "provider_requests": metrics.provider_requests,
                    "provider_cache_hits": metrics.cache_hits,
                    "elapsed_provider_seconds": round(metrics.elapsed_provider_seconds, 3),
                }
            )
        elif args.action == "resume":
            summary, metrics = service.resume(_require_job_id(args.job_id))
            _emit(
                {
                    "ok": True,
                    "action": "resume",
                    **summary.aggregate_dict(),
                    "provider_requests": metrics.provider_requests,
                    "provider_cache_hits": metrics.cache_hits,
                    "elapsed_provider_seconds": round(metrics.elapsed_provider_seconds, 3),
                }
            )
        elif args.action == "apply-high-confidence":
            summary, estimate = service.apply_high_confidence(
                args.job_id,
                confirmed=True,
                write_files=args.write_files,
            )
            _emit(
                {
                    "ok": True,
                    "action": "apply-high-confidence",
                    **summary.aggregate_dict(),
                    "estimate": estimate.aggregate_dict(),
                    "file_write_enabled": bool(args.write_files),
                }
            )
        elif args.action == "rollback":
            summary = service.rollback(args.job_id, confirmed=True)
            _emit({"ok": True, "action": "rollback", **summary.aggregate_dict()})
        elif args.action == "verify-job":
            result = service.verify_job(_require_job_id(args.job_id))
            _emit({"action": "verify-job", **result})
            return 0 if result["ok"] else 1
        elif args.action == "export-private-report":
            job_id = _require_job_id(args.job_id)
            service._job_row(job_id)
            service._write_reports(job_id)
            report = service._report_dir(job_id).resolve()
            _emit(
                {
                    "ok": True,
                    "action": "export-private-report",
                    "job_id": job_id,
                    "report_exists": report.is_dir(),
                }
            )
        return 0
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "action": args.action,
                "error": _safe_error_code(exc),
            }
        )
        return 2
    finally:
        if database is not None:
            database.close()


if __name__ == "__main__":
    raise SystemExit(main())

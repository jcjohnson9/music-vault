from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from music_vault.core.db import CURRENT_SCHEMA_VERSION, MusicVaultDB  # noqa: E402
from music_vault.core.paths import database_path  # noqa: E402
from music_vault.metadata.review_reclassification import (  # noqa: E402
    MetadataReviewReclassifier,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reclassify saved metadata-review evidence without provider access."
    )
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--job-id", default=None)
    parser.add_argument("--after-item-id", type=int, default=0)
    parser.add_argument("--limit", type=int, default=250)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist outcome changes. The default is an aggregate-only dry run.",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    path = (args.database or database_path()).expanduser().resolve()
    if not path.is_file():
        raise SystemExit("Metadata database does not exist.")
    readonly = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        version = int(readonly.execute("PRAGMA user_version").fetchone()[0])
    finally:
        readonly.close()
    if args.apply and version != CURRENT_SCHEMA_VERSION:
        raise SystemExit(
            "Apply mode requires a database at the current application schema."
        )

    if args.apply:
        database = MusicVaultDB(path)
    else:
        database = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        report = MetadataReviewReclassifier(database).reclassify(
            job_id=args.job_id,
            after_item_id=args.after_item_id,
            limit=args.limit,
            apply=args.apply,
        )
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    finally:
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Explicit, backed-up relocation of one closed-app, flat source directory.

Never runs automatically or migrates a schema. Call only for an approved source
and destination. No provider, media-tag, credential, or application startup code.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace


MEDIA_AND_ART = {".opus", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".wav", ".webm", ".mp4", ".jpg", ".jpeg", ".png", ".webp"}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def assert_app_closed():
    if os.name == "nt":
        result = subprocess.run(
            [str(Path(os.environ["SystemRoot"]) / "System32/tasklist.exe"),
             "/FI", "IMAGENAME eq MusicVault.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        require('"musicvault.exe"' not in result.stdout.casefold(), "Close Music Vault before relocation.")


def plain_path(value):
    path = Path(value).absolute()
    for part in (path, *path.parents):
        if part.exists():
            stat = part.lstat()
            require(not part.is_symlink() and not getattr(stat, "st_file_attributes", 0) & 0x400,
                    "Reparse paths are not allowed.")
    return path.resolve()


def digest(path):
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def files_in(folder):
    require(folder.is_dir(), "Required folder is missing.")
    files = sorted(folder.iterdir())
    for path in files:
        plain_path(path)
        require(path.is_file() and path.suffix.lower() in MEDIA_AND_ART,
                "Unexpected file or nested directory; manual review required.")
    require(len({p.name.casefold() for p in files}) == len(files), "Case-insensitive file collision.")
    return files


def stamp(path):
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns, digest(path)]


def snapshot(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    return {name: {"columns": [tuple(r) for r in conn.execute(f'PRAGMA table_info("{name}")')],
                   "rows": [tuple(r) for r in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]}
            for name in tables}


def relocate(database, download_root, storage_key, destination_leaf, backup_root, *, move_file=os.rename):
    """Move without overwrite; compensate owned moves on pre-commit failure.

    Returns aggregate evidence. Full rollback database, media copies and private
    old/new path manifest stay in backup_root. Never automatically restores a DB.
    """
    from music_vault.core.source_download_folders import SourceDownloadFolders
    from music_vault.core.sync_sources import SyncSourceService

    assert_app_closed()
    database, root, backup = map(plain_path, (database, download_root, backup_root))
    require(database.is_file(), "Database missing.")
    require(not any(Path(str(database) + x).exists() for x in ("-wal", "-shm", "-journal")), "Database is not quiescent.")
    require(not backup.exists(), "Backup destination already exists.")
    require(backup.parent.is_dir() and not backup.is_relative_to(root), "Unsafe backup location.")
    require(Path(storage_key).name == storage_key and storage_key not in {".", ".."}, "Invalid storage identity.")
    require(Path(destination_leaf).name == destination_leaf and destination_leaf not in {".", ".."}, "Invalid destination name.")
    source, destination = plain_path(root / "sources" / storage_key), plain_path(root / destination_leaf)
    require(source.parent == root / "sources" and destination.parent == root, "Relocation escaped root.")
    require(source != destination, "Same source and destination.")
    sources = files_in(source)
    require(bool(sources), "Source folder is empty.")
    existing = files_in(destination) if destination.exists() else []
    require(not ({p.name.casefold() for p in sources} & {p.name.casefold() for p in existing}), "Destination collision; no overwrite allowed.")
    require(source.stat().st_dev == root.stat().st_dev, "Relocation must stay on one volume.")
    media_before = {p.name: stamp(p) for p in sources}
    existing_before = {p.name: stamp(p) for p in existing}
    db_before_hash = digest(database)
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    moved = []
    committed = False
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        require(digest(database) == db_before_hash, "Database changed before write lock.")
        require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "Database integrity failed.")
        require(not conn.execute("PRAGMA foreign_key_check").fetchall(), "Database foreign-key errors.")
        source_rows = conn.execute("SELECT id FROM sync_sources WHERE storage_key=? AND archived_at IS NULL", (storage_key,)).fetchall()
        require(len(source_rows) == 1, "Source identity is not unique and active.")
        db = SimpleNamespace(conn=conn)
        saved = SyncSourceService(db).get(source_rows[0][0])
        before = snapshot(conn)
        schema = conn.execute("PRAGMA user_version").fetchone()[0]
        replacements = {os.path.normcase(str(p)): str(destination / p.name) for p in sources}
        changes = []
        for row in conn.execute("SELECT id,path FROM tracks"):
            old = Path(row["path"])
            if old.is_relative_to(source):
                require(os.path.normcase(str(old)) in replacements, "A source track has no relocation file.")
                changes.append((row["id"], row["path"], replacements[os.path.normcase(str(old))]))
        require(bool(changes), "No library tracks matched approved source.")
        # Fail closed if another table/current cover reference needs a policy;
        # never rewrite historical evidence or JSON with blind string replacement.
        prefixes = (str(source).casefold(), json.dumps(str(source))[1:-1].casefold(), source.as_posix().casefold())
        for table, state in before.items():
            names = [column[1] for column in state["columns"]]
            for values in state["rows"]:
                for name, value in zip(names, values):
                    if table == "tracks" and name == "path":
                        continue
                    require(not isinstance(value, str) or not any(p in value.casefold() for p in prefixes),
                            "Additional source-path references need explicit review.")
        backup.mkdir()
        (backup / "media").mkdir()
        rollback = backup / "rollback.sqlite3"
        shutil.copy2(database, rollback)
        require(digest(rollback) == db_before_hash, "Rollback database byte verification failed.")
        for file in sources:
            target = backup / "media" / file.name
            shutil.copy2(file, target)
            require(stamp(target) == media_before[file.name], "Full-file backup verification failed.")
        manifest = {"schema": schema, "database_sha256": db_before_hash,
                    "source": str(source), "destination": str(destination),
                    "files": media_before, "existing_destination": existing_before,
                    "track_paths": changes, "created_at": datetime.now(timezone.utc).isoformat()}
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        assert_app_closed()
        folders = SourceDownloadFolders(db, root)
        destination.mkdir(exist_ok=True)
        require(folders.bind_existing(saved, destination_leaf, commit=False) == destination,
                "Folder binding differs from approved destination.")
        require({p.name for p in files_in(source)} == set(media_before), "Source file set changed.")
        require({p.name: stamp(p) for p in files_in(destination)} == existing_before, "Destination changed.")
        for file in sources:
            require(stamp(file) == media_before[file.name], "Source media changed.")
            target = destination / file.name
            require(not target.exists(), "Destination collision appeared.")
            move_file(file, target)
            moved.append((file, target))
            require(stamp(target) == media_before[file.name], "Moved media verification failed.")
        for track_id, old, new in changes:
            cursor = conn.execute("UPDATE tracks SET path=? WHERE id=? AND path=?", (new, track_id, old))
            require(cursor.rowcount == 1, "Track path changed concurrently.")
        after = snapshot(conn)
        expected = json.loads(json.dumps(before))
        actual = json.loads(json.dumps(after))
        columns = [c[1] for c in expected["tracks"]["columns"]]
        id_col, path_col = columns.index("id"), columns.index("path")
        changed = {i: new for i, _old, new in changes}
        for row in expected["tracks"]["rows"]:
            if row[id_col] in changed:
                row[path_col] = changed[row[id_col]]
        old_meta, new_meta = dict(before["app_meta"]["rows"]), dict(after["app_meta"]["rows"])
        require(all(new_meta.get(k) == v for k, v in old_meta.items()), "Existing app metadata changed.")
        require(len(new_meta) - len(old_meta) in {0, 1}, "Unexpected folder binding writes.")
        expected["app_meta"] = actual["app_meta"]
        require(actual == expected and folders.get(saved) == destination, "Unexpected database change.")
        require(conn.execute("PRAGMA user_version").fetchone()[0] == schema, "Schema changed.")
        require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and not conn.execute("PRAGMA foreign_key_check").fetchall(), "Post-move integrity failed.")
        require({p.name: stamp(p) for p in files_in(destination)} == {**existing_before, **media_before}, "Destination preservation failed.")
        assert_app_closed()
        conn.commit()
        committed = True
        result = {"files_moved": len(moved), "track_paths_updated": len(changes),
                  "existing_destination_files_preserved": len(existing_before),
                  "media_bytes_unchanged": True, "metadata_memberships_history_unchanged": True,
                  "schema_unchanged": schema, "integrity": "ok", "foreign_key_errors": 0,
                  "rollback_database_sha256": db_before_hash, "backup_verified": True}
        (backup / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    except Exception:
        if not committed:
            conn.rollback()
            for old, new in reversed(moved):
                require(not old.exists() and new.is_file() and stamp(new) == media_before[old.name],
                        "Partial move requires manual recovery; verified backups retained.")
                os.rename(new, old)
        raise
    finally:
        conn.close()

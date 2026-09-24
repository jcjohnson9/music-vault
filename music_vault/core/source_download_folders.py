"""Persistent, human-readable per-source download folders; never relocate media."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import unicodedata

from .safety import safe_playlist_component


def _fold(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _reject_reparse(path: Path) -> None:
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Download folders cannot traverse symbolic links or reparse points.")


class SourceDownloadFolders:
    """Bindings live in existing app_meta and are scoped to root and identity.

    Renaming a source or remote playlist does not silently move its folder.
    Existing unbound directories are treated as unrelated, even when empty.
    ``bind_existing`` is exclusively for separately authorized relocation;
    commit=False participates in the caller's database transaction.
    """

    def __init__(self, db, download_root: str | Path):
        self.conn = db.conn
        raw_root = Path(os.path.abspath(Path(download_root).expanduser()))
        _reject_reparse(raw_root)
        self.root = raw_root.resolve()
        root_id = hashlib.sha256(_fold(str(self.root)).encode("utf-8")).hexdigest()
        self.prefix = f"source_download_folder_v1:{root_id}:"

    def _key(self, source) -> str:
        identity = f"{source.source_kind}:{source.external_id}"
        return self.prefix + hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _path(self, leaf: str) -> Path:
        if (not leaf or leaf in {".", ".."} or Path(leaf).name != leaf
                or safe_playlist_component(leaf, "folder") != leaf):
            raise ValueError("Invalid saved source download folder.")
        destination = self.root / leaf
        _reject_reparse(destination)
        if destination.resolve().parent != self.root:
            raise ValueError("Source download folder must be directly inside the download root.")
        if destination.exists() and not destination.is_dir():
            raise ValueError("Source download folder conflicts with a file.")
        return destination

    def _bindings(self) -> dict[str, str]:
        return dict(self.conn.execute(
            "SELECT key, value FROM app_meta WHERE substr(key, 1, ?)=?",
            (len(self.prefix), self.prefix),
        ).fetchall())

    def get(self, source) -> Path | None:
        row = self.conn.execute("SELECT value FROM app_meta WHERE key=?", (self._key(source),)).fetchone()
        if row is None:
            return None
        path = self._path(str(row[0]))
        if self.root.exists() and any(
            _fold(child.name) == _fold(path.name) and child.name != path.name
            for child in self.root.iterdir()
        ):
            raise ValueError("Source download folder has a case-insensitive path conflict.")
        return path

    def bind_existing(self, source, leaf_name: str, *, commit: bool = False) -> Path:
        """Bind an explicitly approved existing directory, without moving files."""
        path = self._path(leaf_name)
        if not path.is_dir():
            raise ValueError("The approved source download folder does not exist.")
        key = self._key(source)
        bindings = self._bindings()
        if key in bindings and bindings[key] != leaf_name:
            raise ValueError("This source already has a different download folder binding.")
        if any(other != key and _fold(value) == _fold(leaf_name)
               for other, value in bindings.items()):
            raise ValueError("The download folder belongs to another source.")
        if any(_fold(child.name) == _fold(leaf_name) and child.name != leaf_name
               for child in self.root.iterdir()):
            raise ValueError("The download folder has a case-insensitive path conflict.")
        self.conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES (?, ?)", (key, leaf_name))
        if commit:
            self.conn.commit()
        return path

    def resolve(self, source, remote_title: str) -> Path:
        """Reserve a new safe folder only after the provider supplies its title."""
        bound = self.get(source)
        if bound is not None:
            bound.mkdir(exist_ok=True)
            return self._path(bound.name)
        if self.conn.in_transaction:
            raise RuntimeError("Source folder allocation requires a completed database transaction.")
        self.root.mkdir(parents=True, exist_ok=True)
        _reject_reparse(self.root)
        base = safe_playlist_component(remote_title, source.external_id, max_length=100)
        digest = self._key(source).rsplit(":", 1)[1][:10]
        # Serialize reservations with other connections, then check both the
        # durable bindings and filesystem using Windows-style case folding.
        self.conn.execute("BEGIN IMMEDIATE")
        created = None
        try:
            bound = self.get(source)
            if bound is not None:
                bound.mkdir(exist_ok=True)
                self.conn.commit()
                return bound
            occupied = {_fold(value) for value in self._bindings().values()}
            occupied.update(_fold(child.name) for child in self.root.iterdir())
            for index in range(100):
                suffix = "" if index == 0 else f" [{digest}{'-' + str(index) if index > 1 else ''}]"
                leaf = base + suffix
                if _fold(leaf) in occupied:
                    continue
                destination = self._path(leaf)
                try:
                    destination.mkdir()
                except FileExistsError:
                    continue
                created = destination
                self.bind_existing(source, leaf, commit=False)
                self.conn.commit()
                return destination
            raise ValueError("Unable to reserve an unoccupied source download folder.")
        except Exception:
            self.conn.rollback()
            if created is not None:
                # Only our newly-created empty directory is eligible.
                try:
                    created.rmdir()
                except OSError:
                    pass
            raise

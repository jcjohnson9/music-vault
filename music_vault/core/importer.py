
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.flac import Picture

from .paths import covers_dir


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac"}


def _clean_text(value) -> str | None:
    if value is None:
        return None

    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[0]

    value = str(value).strip()

    return value or None


def _first_tag(tags, *names: str) -> str | None:
    if not tags:
        return None

    for name in names:
        value = tags.get(name)

        if value:
            return _clean_text(value)

    return None


def read_audio_metadata(path: str | Path) -> dict:
    path = Path(path)

    audio = MutagenFile(str(path), easy=True)

    if not audio:
        return {
            "title": path.stem,
            "artist": None,
            "album": None,
            "year": None,
            "duration_seconds": None,
        }

    tags = audio.tags or {}

    title = _first_tag(tags, "title") or path.stem
    artist = _first_tag(tags, "artist", "albumartist", "album_artist")
    album = _first_tag(tags, "album")
    year = _first_tag(tags, "date", "year")

    duration = None

    if getattr(audio, "info", None) is not None:
        duration = getattr(audio.info, "length", None)

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "year": year,
        "duration_seconds": duration,
    }


def _cover_extension(mime: str | None, data: bytes) -> str:
    mime = (mime or "").lower()

    if "png" in mime or data.startswith(b"\x89PNG"):
        return ".png"

    return ".jpg"


def _save_cover(data: bytes, mime: str | None = None) -> str | None:
    if not data:
        return None

    digest = hashlib.sha256(data).hexdigest()[:24]
    ext = _cover_extension(mime, data)

    cover_dir = covers_dir()
    cover_dir.mkdir(parents=True, exist_ok=True)

    target = cover_dir / f"cover_{digest}{ext}"

    if not target.exists():
        target.write_bytes(data)

    return str(target.resolve())


def extract_embedded_cover(path: str | Path) -> str | None:
    path = Path(path)

    # MP3 ID3 APIC artwork
    if path.suffix.lower() == ".mp3":
        try:
            tags = ID3(str(path))

            apic_frames = tags.getall("APIC")

            if apic_frames:
                frame = apic_frames[0]
                return _save_cover(frame.data, frame.mime)
        except ID3NoHeaderError:
            pass
        except Exception:
            pass

    # FLAC native pictures
    if path.suffix.lower() == ".flac":
        try:
            audio = MutagenFile(str(path))

            if getattr(audio, "pictures", None):
                picture = audio.pictures[0]
                return _save_cover(picture.data, picture.mime)
        except Exception:
            pass

    # MP4/M4A covr atom and OGG/OPUS metadata_block_picture
    try:
        audio = MutagenFile(str(path))

        if not audio or not audio.tags:
            return None

        tags = audio.tags

        # MP4/M4A
        covr = tags.get("covr")

        if covr:
            cover = covr[0]
            return _save_cover(bytes(cover), "image/png" if getattr(cover, "imageformat", None) == 14 else "image/jpeg")

        # OGG/OPUS/Vorbis
        block = tags.get("metadata_block_picture")

        if block:
            raw = base64.b64decode(block[0])
            picture = Picture(raw)
            return _save_cover(picture.data, picture.mime)

    except Exception:
        return None

    return None


def import_file(db, path: str | Path) -> bool:
    path = Path(path)

    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        return False

    resolved_path = str(path.resolve())
    metadata = read_audio_metadata(path)
    cover_path = extract_embedded_cover(path)

    db.upsert_track(
        resolved_path,
        title=metadata["title"],
        artist=metadata["artist"],
        album=metadata["album"],
        duration_seconds=metadata["duration_seconds"],
    )

    row = db.conn.execute(
        "SELECT id FROM tracks WHERE path=?",
        (resolved_path,)
    ).fetchone()

    if row:
        updates = {}

        if metadata["year"]:
            updates["year"] = metadata["year"]

        if cover_path:
            updates["cover_path"] = cover_path

        if updates:
            db.update_track_metadata(row["id"], **updates)

    return True


def import_folder(db, folder: str | Path) -> int:
    folder = Path(folder)

    if not folder.exists():
        return 0

    count = 0

    for path in folder.rglob("*"):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS:
            if import_file(db, path):
                count += 1

    return count


def refresh_covers_for_library(db) -> int:
    rows = db.conn.execute("""
        SELECT id, path, cover_path
        FROM tracks
        ORDER BY id
    """).fetchall()

    updated = 0

    for row in rows:
        path = Path(row["path"])

        if not path.exists():
            continue

        cover_path = extract_embedded_cover(path)

        if cover_path and cover_path != row["cover_path"]:
            db.update_track_metadata(row["id"], cover_path=cover_path)
            updated += 1

    return updated

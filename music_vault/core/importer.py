
from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.flac import Picture

from .audio_inspection import AudioInspectionError, inspect_audio_file
from .audio_quality import SUPPORTED_SOURCE_CODECS
from .ffmpeg import discover_ffmpeg
from .paths import covers_dir
from .safety import normalize_source_upload_date
from music_vault.metadata.service import MetadataService
from music_vault.metadata.intelligence_schema import MetadataIntelligenceJobStore
from music_vault.metadata.artist_credits import seed_existing_artist_credits


AUDIO_EXTENSIONS = {
    ".mp3",
    ".m4a",
    ".flac",
    ".wav",
    ".ogg",
    ".opus",
    ".aac",
    ".webm",
}


@dataclass(frozen=True)
class ImportSourceContext:
    source_kind: str
    source_video_id: str | None = None
    source_upload_date: str | None = None


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
            "album_artist": None,
            "release_date": None,
            "year": None,
            "duration_seconds": None,
            "title_provenance": "filename",
        }

    tags = audio.tags or {}

    embedded_title = _first_tag(tags, "title")
    title = embedded_title or path.stem
    artist = _first_tag(tags, "artist")
    album = _first_tag(tags, "album")
    album_artist = _first_tag(tags, "albumartist", "album_artist")
    release_date = _first_tag(tags, "date", "year")

    duration = None

    if getattr(audio, "info", None) is not None:
        duration = getattr(audio.info, "length", None)

    return {
        "title": title,
        "artist": artist,
        "album": album,
        "album_artist": album_artist,
        "release_date": release_date,
        "year": release_date,
        "duration_seconds": duration,
        "title_provenance": "embedded" if embedded_title else "filename",
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


def _is_verified_audio_only_webm(path: Path) -> bool:
    """Fail closed unless read-only inspection proves a safe audio WebM."""

    try:
        discovery = discover_ffmpeg()
    except (OSError, RuntimeError, ValueError):
        return False
    if not discovery.ready or discovery.ffprobe_path is None:
        return False
    try:
        inspection = inspect_audio_file(
            path,
            ffprobe_path=discovery.ffprobe_path,
        )
    except (AudioInspectionError, OSError, RuntimeError, ValueError):
        return False
    return bool(
        inspection.audio_stream_count is not None
        and inspection.audio_stream_count >= 1
        and inspection.video_stream_count == 0
        and inspection.codec in SUPPORTED_SOURCE_CODECS
    )


def import_file(
    db,
    path: str | Path,
    source: ImportSourceContext | None = None,
) -> bool:
    path = Path(path)

    if path.suffix.lower() not in AUDIO_EXTENSIONS:
        return False
    if path.suffix.lower() == ".webm" and not _is_verified_audio_only_webm(path):
        return False

    resolved_path = str(path.resolve())
    metadata = read_audio_metadata(path)
    cover_path = extract_embedded_cover(path)
    source_upload_date = None
    raw_release_date = metadata.get("release_date", metadata.get("year"))

    existing = db.conn.execute(
        "SELECT source_kind, source_video_id, source_upload_date FROM tracks WHERE path=?",
        (resolved_path,),
    ).fetchone()
    is_new_track = existing is None
    raw_source_kind = (
        source.source_kind
        if source is not None
        else (existing["source_kind"] if existing is not None else None)
    )
    effective_source_kind = str(raw_source_kind or "").strip().casefold() or None
    is_youtube = effective_source_kind == "youtube"
    source_video_id = (
        source.source_video_id
        if source is not None
        else (existing["source_video_id"] if existing is not None else None)
    )

    if is_youtube:
        # yt-dlp commonly writes the source upload date to the generic date tag.
        # That date is useful provenance, but it is not a canonical release year.
        source_upload_date = normalize_source_upload_date(
            (source.source_upload_date if source is not None else None)
            or (existing["source_upload_date"] if existing is not None else None)
            or raw_release_date
        )

    provider = "youtube" if is_youtube else "embedded"
    import_reason = (
        ("youtube_import" if is_youtube else "embedded_import")
        if existing is not None
        else ("initial_youtube_import" if is_youtube else "initial_embedded_import")
    )
    filename_title = (
        metadata.get("title")
        if not is_youtube and metadata.get("title_provenance") == "filename"
        else None
    )
    values = {
        "title": None if filename_title is not None else metadata.get("title"),
        "artist": metadata.get("artist"),
        "album": metadata.get("album"),
        "album_artist": metadata.get("album_artist"),
        "release_date": None if is_youtube else raw_release_date,
        "artwork": cover_path,
    }
    if is_youtube:
        values.update(
            {
                "source_video_id": source_video_id,
                "source_upload_date": source_upload_date,
            }
        )
    change_group_id = str(uuid.uuid4())
    with db.conn:
        track_id = db.upsert_track(
            resolved_path,
            duration_seconds=metadata["duration_seconds"],
            source_kind=effective_source_kind,
            source_video_id=source_video_id,
            source_upload_date=source_upload_date,
            commit=False,
        )
        service = MetadataService(db)
        service.record_source_observations(
            track_id,
            provider=provider,
            values=values,
            provider_reference=source_video_id,
            apply_effective=True,
            reason=import_reason,
            change_group_id=change_group_id,
            commit=False,
        )
        if filename_title is not None:
            service.record_source_observations(
                track_id,
                provider="filename",
                values={"title": filename_title},
                apply_effective=True,
                reason=import_reason,
                change_group_id=change_group_id,
                commit=False,
            )
        seed_existing_artist_credits(db.conn, (track_id,))

    if is_new_track:
        # Provider work is deliberately outside the ordinary import
        # transaction. Enqueue failure must never roll back or fail a local
        # import, and no network request is performed here.
        try:
            MetadataIntelligenceJobStore(db).enqueue_track(
                track_id,
                reason="new_import",
            )
        except Exception:
            pass

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
            result = MetadataService(db).record_source_observations(
                int(row["id"]),
                provider="embedded",
                values={"artwork": cover_path},
                apply_effective=True,
                reason="embedded_artwork_refresh",
            )
            if "artwork" in result.changed_fields:
                updated += 1

    return updated

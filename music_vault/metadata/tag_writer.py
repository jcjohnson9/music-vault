from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from mutagen.id3 import APIC, ID3, ID3NoHeaderError, TALB, TDRC, TIT2, TPE1, TPE2, TXXX
from mutagen.mp3 import MP3

from .artwork import PreparedArtwork, prepare_artwork_bytes


_COPY_CHUNK_BYTES = 1024 * 1024
_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TEXT_FRAME_IDS = {
    "title": "TIT2",
    "artist": "TPE1",
    "album": "TALB",
    "album_artist": "TPE2",
    "release_date": "TDRC",
}
_CUSTOM_TEXT_DESCRIPTIONS = {
    "musicbrainz_recording_id": "MusicBrainz Track Id",
    "musicbrainz_release_id": "MusicBrainz Album Id",
    "musicbrainz_artist_ids": "MusicBrainz Artist Ids",
    "discogs_release_id": "Discogs Release Id",
    "discogs_master_id": "Discogs Master Id",
    "discogs_artist_ids": "Discogs Artist Ids",
    "original_release_date": "Music Vault Original Release Date",
    "version_type": "Music Vault Version Type",
    "version_label": "Music Vault Version Label",
}


class TagWriteError(RuntimeError):
    """A sanitized media-backup or tag-write failure."""


@dataclass(frozen=True)
class MediaFingerprint:
    full_sha256: str
    audio_payload_sha256: str
    size_bytes: int
    duration_seconds: float
    codec: str


@dataclass(frozen=True)
class MediaBackup:
    original_path: Path
    backup_path: Path
    fingerprint: MediaFingerprint


@dataclass(frozen=True)
class PreparedTagWrite:
    original_path: Path
    temporary_path: Path
    original: MediaFingerprint
    updated: MediaFingerprint
    expected_patch: Mapping[str, str]
    artwork_sha256: str | None


@dataclass(frozen=True)
class TagWriteResult:
    path: Path
    original: MediaFingerprint
    updated: MediaFingerprint


def _sha256_range(path: Path, start: int, end: int) -> str:
    digest = hashlib.sha256()
    remaining = max(0, end - start)
    with path.open("rb") as stream:
        stream.seek(start)
        while remaining:
            chunk = stream.read(min(_COPY_CHUNK_BYTES, remaining))
            if not chunk:
                raise TagWriteError("media_read_failed")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def full_file_sha256(path: str | Path) -> str:
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as exc:
        raise TagWriteError("media_unavailable") from exc
    return _sha256_range(source, 0, size)


def _syncsafe_size(value: bytes) -> int:
    if len(value) != 4 or any(byte & 0x80 for byte in value):
        raise TagWriteError("mp3_id3_header_invalid")
    return (value[0] << 21) | (value[1] << 14) | (value[2] << 7) | value[3]


def _mp3_audio_bounds(path: Path) -> tuple[int, int]:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            header = stream.read(10)
            start = 0
            if len(header) == 10 and header[:3] == b"ID3":
                start = 10 + _syncsafe_size(header[6:10])
                if header[5] & 0x10:
                    start += 10
            end = size
            if size >= 128:
                stream.seek(size - 128)
                if stream.read(3) == b"TAG":
                    end -= 128
    except OSError as exc:
        raise TagWriteError("media_read_failed") from exc
    if start < 0 or end <= start:
        raise TagWriteError("mp3_audio_payload_invalid")
    return start, end


def mp3_audio_payload_sha256(path: str | Path) -> str:
    source = Path(path)
    start, end = _mp3_audio_bounds(source)
    return _sha256_range(source, start, end)


def inspect_mp3(path: str | Path) -> MediaFingerprint:
    source = Path(path)
    if source.suffix.casefold() != ".mp3":
        raise TagWriteError("media_format_unsupported")
    try:
        parsed = MP3(source)
        info = parsed.info
        size = source.stat().st_size
        duration = float(info.length)
        codec = (
            f"mp3:mpeg-{getattr(info, 'version', 'unknown')}:"
            f"layer-{getattr(info, 'layer', 'unknown')}:"
            f"{getattr(info, 'sample_rate', 0)}hz:"
            f"{getattr(info, 'channels', 0)}ch"
        )
    except Exception as exc:
        raise TagWriteError("media_parse_failed") from exc
    return MediaFingerprint(
        full_sha256=full_file_sha256(source),
        audio_payload_sha256=mp3_audio_payload_sha256(source),
        size_bytes=size,
        duration_seconds=duration,
        codec=codec,
    )


def _safe_backup_identity(value: object) -> str:
    text = str(value or "").strip()
    if _SAFE_IDENTITY.fullmatch(text):
        return text
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:24]


def _load_artwork(path: Path) -> PreparedArtwork:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise TagWriteError("artwork_unavailable") from exc
    try:
        return prepare_artwork_bytes(payload)
    except Exception as exc:
        raise TagWriteError("artwork_invalid") from exc


def _first_text(tags: ID3, frame_id: str) -> str | None:
    frames = tags.getall(frame_id)
    if not frames:
        return None
    text = getattr(frames[0], "text", None)
    if not text:
        return None
    return str(text[0]).strip() or None


def _musicbrainz_text(tags: ID3, description: str) -> str | None:
    for frame in tags.getall("TXXX"):
        if str(getattr(frame, "desc", "")).casefold() != description.casefold():
            continue
        text = getattr(frame, "text", None)
        if text:
            return str(text[0]).strip() or None
    return None


def _same_audio_payload(left: MediaFingerprint, right: MediaFingerprint) -> bool:
    return bool(
        left.audio_payload_sha256 == right.audio_payload_sha256
        and left.codec == right.codec
        and abs(left.duration_seconds - right.duration_seconds) <= 0.05
    )


def _same_file(left: MediaFingerprint, right: MediaFingerprint) -> bool:
    return bool(
        left.full_sha256 == right.full_sha256
        and left.size_bytes == right.size_bytes
        and _same_audio_payload(left, right)
    )


class SafeTagWriter:
    """Verified full-file backup and temporary-copy MP3 metadata writer."""

    supported_suffixes = frozenset({".mp3"})

    @classmethod
    def supports(cls, path: str | Path) -> bool:
        return Path(path).suffix.casefold() in cls.supported_suffixes

    @staticmethod
    def fingerprint(path: str | Path) -> MediaFingerprint:
        return inspect_mp3(path)

    def create_backup(
        self,
        path: str | Path,
        backup_directory: str | Path,
        *,
        identity: object,
        expected_full_sha256: str | None = None,
    ) -> MediaBackup:
        source = Path(path).resolve()
        if not self.supports(source):
            raise TagWriteError("media_format_unsupported")
        original = self.fingerprint(source)
        if expected_full_sha256 and original.full_sha256 != expected_full_sha256:
            raise TagWriteError("media_changed_since_analysis")
        folder = Path(backup_directory).resolve()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TagWriteError("backup_directory_unavailable") from exc
        destination = folder / (
            f"{_safe_backup_identity(identity)}-{original.full_sha256[:16]}{source.suffix.casefold()}"
        )
        if destination.exists():
            if full_file_sha256(destination) != original.full_sha256:
                raise TagWriteError("backup_hash_conflict")
            return MediaBackup(source, destination, original)
        # Keep the atomic staging name short.  The verified destination keeps
        # the full identity/hash, while a repeated long basename here can push
        # otherwise valid Windows runtime roots beyond the legacy path limit.
        temporary = folder / f".mv-backup-{uuid.uuid4().hex[:12]}.tmp"
        try:
            shutil.copy2(source, temporary)
            if full_file_sha256(temporary) != original.full_sha256:
                raise TagWriteError("backup_verification_failed")
            os.replace(temporary, destination)
        except TagWriteError:
            temporary.unlink(missing_ok=True)
            raise
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise TagWriteError("backup_write_failed") from exc
        if full_file_sha256(destination) != original.full_sha256:
            raise TagWriteError("backup_verification_failed")
        return MediaBackup(source, destination, original)

    def prepare(
        self,
        path: str | Path,
        patch: Mapping[str, object],
        *,
        expected_full_sha256: str | None = None,
        artwork_path: str | Path | None = None,
    ) -> PreparedTagWrite:
        source = Path(path).resolve()
        if not self.supports(source):
            raise TagWriteError("media_format_unsupported")
        original = self.fingerprint(source)
        if expected_full_sha256 and original.full_sha256 != expected_full_sha256:
            raise TagWriteError("media_changed_since_analysis")
        normalized = {
            key: str(value).strip()
            for key, value in patch.items()
            if key in {*_TEXT_FRAME_IDS, *_CUSTOM_TEXT_DESCRIPTIONS}
            and value is not None
            and str(value).strip()
        }
        temporary = source.parent / f".{source.stem}.music-vault-{uuid.uuid4().hex}.tmp.mp3"
        artwork = _load_artwork(Path(artwork_path)) if artwork_path is not None else None
        try:
            shutil.copy2(source, temporary)
            try:
                tags = ID3(temporary)
                id3_major_version = int(tags.version[1])
            except ID3NoHeaderError:
                tags = ID3()
                id3_major_version = 3
            if id3_major_version not in {3, 4}:
                id3_major_version = 3
            frame_types = {
                "title": TIT2,
                "artist": TPE1,
                "album": TALB,
                "album_artist": TPE2,
                "release_date": TDRC,
            }
            for field, frame_id in _TEXT_FRAME_IDS.items():
                if field not in normalized:
                    continue
                tags.delall(frame_id)
                tags.add(frame_types[field](encoding=3, text=[normalized[field]]))
            for field, description in _CUSTOM_TEXT_DESCRIPTIONS.items():
                if field not in normalized:
                    continue
                for key in list(tags.keys()):
                    frame = tags.get(key)
                    if (
                        key.startswith("TXXX:")
                        and str(getattr(frame, "desc", "")).casefold() == description.casefold()
                    ):
                        del tags[key]
                tags.add(TXXX(encoding=3, desc=description, text=[normalized[field]]))
            if artwork is not None:
                for frame in list(tags.getall("APIC")):
                    if int(getattr(frame, "type", -1)) == 3:
                        tags.pop(frame.HashKey, None)
                tags.add(
                    APIC(
                        encoding=3,
                        mime=artwork.mime_type,
                        type=3,
                        desc="Cover",
                        data=artwork.data,
                    )
                )
            tags.save(temporary, v2_version=id3_major_version)
            updated = self.fingerprint(temporary)
            if updated.audio_payload_sha256 != original.audio_payload_sha256:
                raise TagWriteError("audio_payload_changed")
            if updated.codec != original.codec:
                raise TagWriteError("codec_changed")
            if abs(updated.duration_seconds - original.duration_seconds) > 0.05:
                raise TagWriteError("duration_changed")
            self._verify_readback(
                temporary,
                normalized,
                artwork_sha256=artwork.sha256 if artwork is not None else None,
            )
        except TagWriteError:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise TagWriteError("tag_write_failed") from exc
        return PreparedTagWrite(
            original_path=source,
            temporary_path=temporary,
            original=original,
            updated=updated,
            expected_patch=normalized,
            artwork_sha256=artwork.sha256 if artwork is not None else None,
        )

    @staticmethod
    def _verify_readback(
        path: Path,
        expected: Mapping[str, str],
        *,
        artwork_sha256: str | None,
    ) -> None:
        try:
            tags = ID3(path)
        except Exception as exc:
            raise TagWriteError("tag_readback_failed") from exc
        for field, frame_id in _TEXT_FRAME_IDS.items():
            if field in expected and _first_text(tags, frame_id) != expected[field]:
                raise TagWriteError("tag_readback_mismatch")
        for field, description in _CUSTOM_TEXT_DESCRIPTIONS.items():
            if field in expected and _musicbrainz_text(tags, description) != expected[field]:
                raise TagWriteError("tag_readback_mismatch")
        if artwork_sha256 is not None:
            covers = [
                frame
                for frame in tags.getall("APIC")
                if int(getattr(frame, "type", -1)) == 3
            ]
            if not covers:
                raise TagWriteError("artwork_readback_failed")
            try:
                decoded = prepare_artwork_bytes(bytes(covers[0].data), covers[0].mime)
            except Exception as exc:
                raise TagWriteError("artwork_readback_failed") from exc
            if decoded.sha256 != artwork_sha256:
                raise TagWriteError("artwork_readback_mismatch")

    def _verified_backup(
        self,
        backup: MediaBackup,
        *,
        source: Path,
        expected: MediaFingerprint,
    ) -> MediaFingerprint:
        if Path(backup.original_path).resolve() != source.resolve():
            raise TagWriteError("backup_source_mismatch")
        if not _same_file(backup.fingerprint, expected):
            raise TagWriteError("backup_fingerprint_mismatch")
        actual = self.fingerprint(backup.backup_path)
        if not _same_file(actual, expected):
            raise TagWriteError("backup_verification_failed")
        return actual

    def _replace_with_verified_backup(
        self,
        destination: Path,
        backup_path: Path,
        expected: MediaFingerprint,
    ) -> MediaFingerprint:
        temporary = destination.parent / (
            f".{destination.stem}.music-vault-restore-{uuid.uuid4().hex}.tmp.mp3"
        )
        try:
            shutil.copy2(backup_path, temporary)
            candidate = self.fingerprint(temporary)
            if not _same_file(candidate, expected):
                raise TagWriteError("restore_verification_failed")
            os.replace(temporary, destination)
            restored = self.fingerprint(destination)
            if not _same_file(restored, expected):
                raise TagWriteError("restore_verification_failed")
            return restored
        except TagWriteError:
            temporary.unlink(missing_ok=True)
            raise
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise TagWriteError("restore_failed") from exc

    def commit(
        self,
        prepared: PreparedTagWrite,
        *,
        backup: MediaBackup,
    ) -> TagWriteResult:
        source = prepared.original_path
        temporary = prepared.temporary_path
        if not temporary.is_file():
            raise TagWriteError("prepared_file_missing")
        try:
            self._verified_backup(backup, source=source, expected=prepared.original)
            current = self.fingerprint(source)
            if not _same_file(current, prepared.original):
                raise TagWriteError("media_changed_since_prepare")
            candidate = self.fingerprint(temporary)
            if not _same_file(candidate, prepared.updated):
                raise TagWriteError("prepared_file_changed")
            if not _same_audio_payload(candidate, prepared.original):
                raise TagWriteError("audio_payload_changed")
            self._verify_readback(
                temporary,
                prepared.expected_patch,
                artwork_sha256=prepared.artwork_sha256,
            )
        except TagWriteError:
            temporary.unlink(missing_ok=True)
            raise
        try:
            os.replace(temporary, source)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise TagWriteError("media_replace_failed") from exc
        try:
            updated = self.fingerprint(source)
            if not _same_file(updated, prepared.updated):
                raise TagWriteError("media_post_replace_verification_failed")
            if not _same_audio_payload(updated, prepared.original):
                raise TagWriteError("media_post_replace_verification_failed")
            self._verify_readback(
                source,
                prepared.expected_patch,
                artwork_sha256=prepared.artwork_sha256,
            )
        except Exception as exc:
            try:
                self._replace_with_verified_backup(
                    source,
                    backup.backup_path,
                    prepared.original,
                )
            except TagWriteError as restore_exc:
                raise TagWriteError("media_post_replace_restore_failed") from restore_exc
            raise TagWriteError("media_post_replace_verification_failed") from exc
        return TagWriteResult(source, prepared.original, updated)

    def restore(
        self,
        path: str | Path,
        backup_path: str | Path,
        *,
        expected_backup_sha256: str,
        expected_current_sha256: str | None = None,
    ) -> MediaFingerprint:
        destination = Path(path).resolve()
        backup = Path(backup_path).resolve()
        if not self.supports(destination) or not self.supports(backup):
            raise TagWriteError("media_format_unsupported")
        backup_fingerprint = self.fingerprint(backup)
        if backup_fingerprint.full_sha256 != expected_backup_sha256:
            raise TagWriteError("backup_verification_failed")
        previous_path: Path | None = None
        previous_sha256: str | None = None
        if destination.exists():
            previous_sha256 = full_file_sha256(destination)
            if (
                expected_current_sha256 is not None
                and previous_sha256 != expected_current_sha256
            ):
                raise TagWriteError("restore_destination_changed")
            previous_path = destination.parent / (
                f".{destination.stem}.music-vault-previous-{uuid.uuid4().hex}.tmp.mp3"
            )
            try:
                shutil.copy2(destination, previous_path)
                if full_file_sha256(previous_path) != previous_sha256:
                    raise TagWriteError("restore_safety_copy_failed")
            except TagWriteError:
                previous_path.unlink(missing_ok=True)
                raise
            except OSError as exc:
                previous_path.unlink(missing_ok=True)
                raise TagWriteError("restore_safety_copy_failed") from exc
        try:
            return self._replace_with_verified_backup(
                destination,
                backup,
                backup_fingerprint,
            )
        except TagWriteError:
            try:
                if previous_path is not None and previous_sha256 is not None:
                    os.replace(previous_path, destination)
                    if full_file_sha256(destination) != previous_sha256:
                        raise TagWriteError("restore_rollback_failed")
                else:
                    destination.unlink(missing_ok=True)
            except (OSError, TagWriteError) as exc:
                raise TagWriteError("restore_rollback_failed") from exc
            raise
        finally:
            if previous_path is not None:
                previous_path.unlink(missing_ok=True)


__all__ = [
    "MediaBackup",
    "MediaFingerprint",
    "PreparedTagWrite",
    "SafeTagWriter",
    "TagWriteError",
    "TagWriteResult",
    "full_file_sha256",
    "inspect_mp3",
    "mp3_audio_payload_sha256",
]

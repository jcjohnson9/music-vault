"""Private, versioned, content-addressed lyrics cache."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping

from music_vault.core.paths import data_dir

from .models import (
    LyricLine,
    LyricsResult,
    LyricsSource,
    LyricsStatus,
    TrackLyricsIdentity,
    safe_error_code,
)
from .parser import MAX_LYRIC_BYTES, LyricsParseError, parse_lrc, parse_plain_text


LYRICS_CACHE_SCHEMA_VERSION = 1
NO_MATCH_TTL = timedelta(days=30)
TEMPORARY_FAILURE_TTL = timedelta(hours=6)
MAX_CACHE_PAYLOAD_BYTES = MAX_LYRIC_BYTES + 512 * 1024
_ROOT_LOCKS_GUARD = threading.Lock()
_ROOT_LOCKS: dict[Path, threading.RLock] = {}


def _root_lock(root: Path) -> threading.RLock:
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(root, threading.RLock())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class LyricsCacheError(RuntimeError):
    """A cache failure whose message never contains lyric content."""


class LyricsCache:
    """Atomic cache with separate manual and automatic records per stable track."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.root = (Path(root) if root is not None else data_dir() / "lyrics").expanduser().resolve()
        self.files_dir = (self.root / "files").resolve()
        self.index_path = (self.root / "index.json").resolve()
        if self.files_dir.parent != self.root or self.index_path.parent != self.root:
            raise ValueError("Lyrics cache paths must remain under the cache root.")
        self.clock = clock
        self._lock = _root_lock(self.root)
        self._manifest: dict[str, Any] | None = None
        self._manifest_signature: tuple[int, int] | None = None

    @staticmethod
    def _entry_key(identity: TrackLyricsIdentity) -> str:
        return hashlib.sha256(identity.stable_id.encode("utf-8")).hexdigest()

    @staticmethod
    def _empty_manifest() -> dict[str, Any]:
        return {"schema_version": LYRICS_CACHE_SCHEMA_VERSION, "entries": {}}

    def _index_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.index_path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _load(self) -> dict[str, Any]:
        signature = self._index_signature()
        if (
            self._manifest is not None
            and signature == self._manifest_signature
        ):
            return self._manifest
        try:
            if self.index_path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("lyrics cache index too large")
            loaded = json.loads(self.index_path.read_text(encoding="utf-8"))
            if (
                not isinstance(loaded, dict)
                or loaded.get("schema_version") != LYRICS_CACHE_SCHEMA_VERSION
                or not isinstance(loaded.get("entries"), dict)
            ):
                raise ValueError("unsupported lyrics cache index")
            self._manifest = loaded
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            self._manifest = self._empty_manifest()
        self._manifest_signature = signature
        return self._manifest

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _write_manifest(self) -> None:
        payload = (
            json.dumps(self._load(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        self._atomic_write(self.index_path, payload)
        self._manifest_signature = self._index_signature()

    def _safe_content_path(self, relative_value: object) -> Path | None:
        relative = PurePosixPath(str(relative_value or "").replace("\\", "/"))
        if (
            relative.is_absolute()
            or len(relative.parts) != 2
            or relative.parts[0] != "files"
            or relative.parts[1] in {"", ".", ".."}
        ):
            return None
        candidate = (self.root / Path(*relative.parts)).resolve()
        return candidate if candidate.parent == self.files_dir else None

    @staticmethod
    def _content_payload(result: LyricsResult) -> bytes:
        if result.status is LyricsStatus.AVAILABLE and not result.synced_lines and not result.plain_text:
            raise LyricsCacheError("available_lyrics_missing")
        payload = {
            "schema_version": LYRICS_CACHE_SCHEMA_VERSION,
            "instrumental": result.instrumental,
            "lines": [[line.timestamp_ms, line.text] for line in result.synced_lines],
            "plain_text": result.plain_text,
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_CACHE_PAYLOAD_BYTES:
            raise LyricsCacheError("lyrics_content_too_large")
        return encoded

    @staticmethod
    def _decode_content(payload: bytes) -> tuple[tuple[LyricLine, ...], str | None, bool]:
        if not payload or len(payload) > MAX_CACHE_PAYLOAD_BYTES:
            raise LyricsCacheError("lyrics_content_invalid")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise LyricsCacheError("lyrics_content_invalid") from exc
        if not isinstance(decoded, dict) or decoded.get("schema_version") != LYRICS_CACHE_SCHEMA_VERSION:
            raise LyricsCacheError("lyrics_content_invalid")
        raw_lines = decoded.get("lines") or []
        if not isinstance(raw_lines, list) or len(raw_lines) > 20_000:
            raise LyricsCacheError("lyrics_content_invalid")
        lines: list[LyricLine] = []
        try:
            for item in raw_lines:
                if not isinstance(item, list) or len(item) != 2:
                    raise ValueError
                text = str(item[1]).replace("\x00", "")
                if len(text) > 16_384:
                    raise ValueError
                lines.append(LyricLine(int(item[0]), text))
        except (TypeError, ValueError, OverflowError) as exc:
            raise LyricsCacheError("lyrics_content_invalid") from exc
        plain = decoded.get("plain_text")
        if plain is not None:
            try:
                plain = parse_plain_text(str(plain)).plain_text
            except LyricsParseError as exc:
                raise LyricsCacheError("lyrics_content_invalid") from exc
        instrumental = decoded.get("instrumental") is True
        if not lines and not plain and not instrumental:
            raise LyricsCacheError("lyrics_content_invalid")
        return tuple(lines), plain, instrumental

    def _record_result(
        self,
        identity: TrackLyricsIdentity,
        record: Mapping[str, Any],
        *,
        manual: bool,
        force: bool,
    ) -> LyricsResult | None:
        if str(record.get("track_id")) != identity.stable_id:
            return None
        if not manual and record.get("metadata_fingerprint") != identity.metadata_fingerprint:
            return None
        try:
            status = LyricsStatus(str(record.get("status")))
        except ValueError:
            return None
        if status in {
            LyricsStatus.NO_MATCH,
            LyricsStatus.AMBIGUOUS,
            LyricsStatus.TEMPORARY_ERROR,
        }:
            if force:
                return None
            retry_after = _parse_iso(record.get("retry_after"))
            if retry_after is None or self.clock().astimezone(timezone.utc) >= retry_after:
                return None
            return LyricsResult(
                status,
                identity,
                fetched_at=str(record.get("fetched_at") or "") or None,
                retry_after=_iso(retry_after),
                error_code=(
                    safe_error_code(record.get("error_code"))
                    if record.get("error_code")
                    else None
                ),
                from_cache=True,
            )

        if status not in {LyricsStatus.AVAILABLE, LyricsStatus.INSTRUMENTAL}:
            return None
        path = self._safe_content_path(record.get("content_file"))
        digest = str(record.get("content_hash") or "")
        if path is None or len(digest) != 64 or not path.is_file() or path.is_symlink():
            return None
        try:
            if not 0 < path.stat().st_size <= MAX_CACHE_PAYLOAD_BYTES:
                return None
            payload = path.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(payload).hexdigest() != digest:
            return None
        try:
            lines, plain, instrumental = self._decode_content(payload)
        except LyricsCacheError:
            return None
        if (status is LyricsStatus.INSTRUMENTAL) != instrumental:
            return None
        source = LyricsSource.MANUAL if manual else (
            LyricsSource.CACHE_SYNCED if lines else LyricsSource.CACHE_PLAIN
        )
        try:
            provider_duration_ms = (
                int(record["provider_duration_ms"])
                if record.get("provider_duration_ms") is not None
                else None
            )
            confidence = (
                float(record["confidence"])
                if record.get("confidence") is not None
                else None
            )
        except (TypeError, ValueError, OverflowError):
            return None
        return LyricsResult(
            status,
            identity,
            source,
            lines,
            plain,
            provider=str(record.get("provider") or "") or None,
            provider_result_id=str(record.get("provider_result_id") or "") or None,
            provider_duration_ms=provider_duration_ms,
            attribution=str(record.get("attribution") or "") or None,
            confidence=confidence,
            fetched_at=str(record.get("fetched_at") or "") or None,
            from_cache=True,
        )

    def _slot(self, identity: TrackLyricsIdentity, name: str) -> Mapping[str, Any] | None:
        entry = self._load()["entries"].get(self._entry_key(identity))
        if not isinstance(entry, Mapping):
            return None
        record = entry.get(name)
        return record if isinstance(record, Mapping) else None

    def lookup_manual(self, identity: TrackLyricsIdentity) -> LyricsResult | None:
        with self._lock:
            record = self._slot(identity, "manual")
            return self._record_result(identity, record, manual=True, force=False) if record else None

    def lookup_automatic(
        self,
        identity: TrackLyricsIdentity,
        *,
        force: bool = False,
    ) -> LyricsResult | None:
        with self._lock:
            record = self._slot(identity, "automatic")
            return self._record_result(identity, record, manual=False, force=force) if record else None

    def lookup(
        self,
        identity: TrackLyricsIdentity,
        *,
        force: bool = False,
    ) -> LyricsResult | None:
        return self.lookup_manual(identity) or self.lookup_automatic(identity, force=force)

    def store(self, result: LyricsResult, *, manual: bool = False) -> LyricsResult:
        if result.status in {LyricsStatus.DISABLED}:
            return result
        now = self.clock().astimezone(timezone.utc)
        successful = result.status in {LyricsStatus.AVAILABLE, LyricsStatus.INSTRUMENTAL}
        negative = result.status in {
            LyricsStatus.NO_MATCH,
            LyricsStatus.AMBIGUOUS,
            LyricsStatus.TEMPORARY_ERROR,
        }
        if not successful and not negative:
            raise LyricsCacheError("lyrics_status_not_cacheable")
        if manual and not successful:
            raise LyricsCacheError("manual_lyrics_must_have_content")

        content_hash: str | None = None
        content_file: str | None = None
        if successful:
            payload = self._content_payload(result)
            content_hash = hashlib.sha256(payload).hexdigest()
            destination = self.files_dir / f"{content_hash}.lyrics"
            if not destination.exists():
                self._atomic_write(destination, payload)
            content_file = destination.relative_to(self.root).as_posix()
            retry_after = None
        else:
            ttl = TEMPORARY_FAILURE_TTL if result.status is LyricsStatus.TEMPORARY_ERROR else NO_MATCH_TTL
            retry_after = _iso(now + ttl)

        try:
            provider_duration_ms = (
                int(result.provider_duration_ms)
                if result.provider_duration_ms is not None
                else None
            )
        except (TypeError, ValueError, OverflowError):
            provider_duration_ms = None
        if provider_duration_ms is not None and not 0 < provider_duration_ms <= 86_400_000:
            provider_duration_ms = None
        try:
            confidence = float(result.confidence) if result.confidence is not None else None
        except (TypeError, ValueError, OverflowError):
            confidence = None
        if confidence is not None and (not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0):
            confidence = None

        def short(value: object, maximum: int = 256) -> str | None:
            text = " ".join(str(value or "").replace("\x00", "").split())
            return text[:maximum] or None

        record = {
            "track_id": result.identity.stable_id,
            "metadata_fingerprint": result.identity.metadata_fingerprint,
            "status": result.status.value,
            "source": (LyricsSource.MANUAL.value if manual else (result.source.value if result.source else None)),
            "provider": short(result.provider, 80),
            "provider_result_id": short(result.provider_result_id, 256),
            "title": result.identity.title,
            "artist": result.identity.artist,
            "album": result.identity.album,
            "duration_ms": result.identity.duration_ms,
            "provider_duration_ms": provider_duration_ms,
            "synchronized": bool(result.synced_lines),
            "instrumental": result.instrumental,
            "content_hash": content_hash,
            "content_file": content_file,
            "fetched_at": _iso(now),
            "retry_after": retry_after,
            "attribution": short(result.attribution, 256),
            "confidence": confidence,
            "error_code": safe_error_code(result.error_code) if result.error_code else None,
            "cache_schema_version": LYRICS_CACHE_SCHEMA_VERSION,
        }
        with self._lock:
            entry_key = self._entry_key(result.identity)
            entries = self._load()["entries"]
            entry = entries.setdefault(entry_key, {})
            if not isinstance(entry, dict):
                entry = {}
                entries[entry_key] = entry
            entry["manual" if manual else "automatic"] = record
            self._write_manifest()
        return replace(
            result,
            source=LyricsSource.MANUAL if manual else result.source,
            provider=record["provider"],
            provider_result_id=record["provider_result_id"],
            provider_duration_ms=provider_duration_ms,
            attribution=record["attribution"],
            confidence=confidence,
            fetched_at=record["fetched_at"],
            retry_after=retry_after,
            error_code=record["error_code"],
        )

    def store_manual(self, result: LyricsResult) -> LyricsResult:
        return self.store(result, manual=True)

    def store_negative(
        self,
        identity: TrackLyricsIdentity,
        status: LyricsStatus,
        *,
        error_code: str | None = None,
    ) -> LyricsResult:
        result = LyricsResult(status, identity, error_code=safe_error_code(error_code) if error_code else None)
        return self.store(result)

    def import_manual(self, identity: TrackLyricsIdentity, source_path: str | Path) -> LyricsResult:
        path = Path(source_path).expanduser().resolve()
        suffix = path.suffix.casefold()
        if suffix not in {".lrc", ".txt"}:
            raise LyricsCacheError("unsupported_lyrics_file")
        try:
            if not path.is_file() or path.stat().st_size > MAX_LYRIC_BYTES:
                raise LyricsCacheError("lyrics_file_invalid")
            payload = path.read_bytes()
            parsed = parse_lrc(payload) if suffix == ".lrc" else parse_plain_text(payload)
        except OSError as exc:
            raise LyricsCacheError("lyrics_file_unavailable") from exc
        except LyricsParseError as exc:
            raise LyricsCacheError(str(exc)) from exc
        if parsed.empty or (suffix == ".lrc" and not parsed.synchronized):
            raise LyricsCacheError("lyrics_file_empty")
        result = LyricsResult(
            LyricsStatus.AVAILABLE,
            identity,
            LyricsSource.MANUAL,
            parsed.lines,
            parsed.plain_text,
        )
        return self.store_manual(result)

    def _referenced_files(self) -> set[str]:
        references: set[str] = set()
        for entry in self._load()["entries"].values():
            if not isinstance(entry, Mapping):
                continue
            for slot in ("manual", "automatic"):
                record = entry.get(slot)
                if isinstance(record, Mapping) and record.get("content_file"):
                    references.add(str(record["content_file"]))
        return references

    def _remove_unreferenced_files(self) -> None:
        references = self._referenced_files()
        if not self.files_dir.is_dir() or self.files_dir.is_symlink():
            return
        for path in self.files_dir.iterdir():
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(self.root).as_posix()
                if relative not in references:
                    path.unlink(missing_ok=True)

    def clear_automatic(self, identity: TrackLyricsIdentity | None = None) -> None:
        """Clear Music Vault-managed automatic records, preserving manual lyrics."""
        with self._lock:
            entries = self._load()["entries"]
            keys = [self._entry_key(identity)] if identity is not None else list(entries)
            for key in keys:
                entry = entries.get(key)
                if not isinstance(entry, dict):
                    continue
                entry.pop("automatic", None)
                if not entry:
                    entries.pop(key, None)
            self._remove_unreferenced_files()
            self._write_manifest()

    def clear_manual(self, identity: TrackLyricsIdentity) -> None:
        with self._lock:
            entries = self._load()["entries"]
            key = self._entry_key(identity)
            entry = entries.get(key)
            if isinstance(entry, dict):
                entry.pop("manual", None)
                if not entry:
                    entries.pop(key, None)
            self._remove_unreferenced_files()
            self._write_manifest()

    def clear(self, identity: TrackLyricsIdentity | None = None) -> None:
        self.clear_automatic(identity)

    def statistics(self) -> dict[str, int]:
        with self._lock:
            entries = self._load()["entries"]
            files = [
                path
                for path in self.files_dir.iterdir()
                if path.is_file() and not path.is_symlink()
            ] if self.files_dir.is_dir() else []
            manual_count = sum(
                1 for entry in entries.values() if isinstance(entry, Mapping) and "manual" in entry
            )
            automatic_count = sum(
                1 for entry in entries.values() if isinstance(entry, Mapping) and "automatic" in entry
            )
            return {
                "track_count": len(entries),
                "manual_count": manual_count,
                "automatic_count": automatic_count,
                "file_count": len(files),
                "total_bytes": sum(path.stat().st_size for path in files),
            }

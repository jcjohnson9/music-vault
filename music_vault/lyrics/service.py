"""Strict-priority local discovery and one-slot lyric lookup service."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .cache import LyricsCache, LyricsCacheError
from .models import (
    LyricLine,
    LyricsQuery,
    LyricsResult,
    LyricsSource,
    LyricsStatus,
    LookupState,
    TrackLyricsIdentity,
)
from .parser import MAX_LYRIC_BYTES, LyricsParseError, parse_lrc, parse_plain_text
from .providers.base import LyricsProvider


SUPPORTED_EMBEDDED_EXTENSIONS = frozenset(
    {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".mp4", ".aac"}
)
MAX_SIDECAR_DIRECTORY_ENTRIES = 4096


@dataclass(frozen=True)
class EmbeddedLyrics:
    synchronized: LyricsResult | None = None
    plain: LyricsResult | None = None


@dataclass(frozen=True)
class _LookupRequest:
    generation: int
    identity: TrackLyricsIdentity
    callback: Callable[[int, LyricsResult], None]
    online_enabled: bool
    force_refresh: bool
    cancel_event: threading.Event


def _read_bounded(path: Path) -> bytes:
    try:
        if not path.is_file() or path.is_symlink():
            raise LyricsParseError("lyrics_file_invalid")
        size = path.stat().st_size
        if size <= 0 or size > MAX_LYRIC_BYTES:
            raise LyricsParseError("lyrics_file_invalid")
        return path.read_bytes()
    except OSError as exc:
        raise LyricsParseError("lyrics_file_unavailable") from exc


def find_adjacent_sidecar(
    media_path: str | Path | None,
    extension: str,
) -> Path | None:
    """Find only a same-directory, same-stem sidecar without recursion."""
    if media_path is None:
        return None
    suffix = str(extension or "").casefold()
    if suffix not in {".lrc", ".txt"}:
        return None
    media = Path(media_path).expanduser()
    directory = media.parent.resolve()
    stem = media.stem.casefold()
    direct = directory / f"{media.stem}{suffix}"
    try:
        if direct.is_file() and not direct.is_symlink() and direct.resolve().parent == directory:
            return direct.resolve()
        for count, candidate in enumerate(directory.iterdir(), 1):
            if count > MAX_SIDECAR_DIRECTORY_ENTRIES:
                break
            if (
                candidate.stem.casefold() == stem
                and candidate.suffix.casefold() == suffix
                and candidate.is_file()
                and not candidate.is_symlink()
            ):
                resolved = candidate.resolve()
                if resolved.parent == directory:
                    return resolved
    except OSError:
        return None
    return None


def read_sidecar(
    identity: TrackLyricsIdentity,
    extension: str,
) -> LyricsResult | None:
    path = find_adjacent_sidecar(identity.media_path, extension)
    if path is None:
        return None
    try:
        payload = _read_bounded(path)
        if extension.casefold() == ".lrc":
            parsed = parse_lrc(payload)
            if not parsed.synchronized:
                return None
            return LyricsResult(
                LyricsStatus.AVAILABLE,
                identity,
                LyricsSource.SIDECAR_SYNCED,
                parsed.lines,
            )
        parsed = parse_plain_text(payload)
        if not parsed.plain_text:
            return None
        return LyricsResult(
            LyricsStatus.AVAILABLE,
            identity,
            LyricsSource.SIDECAR_PLAIN,
            (),
            parsed.plain_text,
        )
    except LyricsParseError:
        return None


def _flatten_text_values(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            if isinstance(item, str):
                yield item


def _embedded_result(
    identity: TrackLyricsIdentity,
    *,
    lines: Sequence[LyricLine] = (),
    plain: str | None = None,
) -> LyricsResult | None:
    if lines:
        if len(lines) > 20_000 or sum(len(line.text.encode("utf-8")) for line in lines) > MAX_LYRIC_BYTES:
            return None
        unique = sorted(set(lines), key=lambda line: line.timestamp_ms)
        return LyricsResult(
            LyricsStatus.AVAILABLE,
            identity,
            LyricsSource.EMBEDDED_SYNCED,
            tuple(unique),
        )
    if plain:
        try:
            parsed = parse_plain_text(plain)
        except LyricsParseError:
            return None
        if parsed.plain_text:
            return LyricsResult(
                LyricsStatus.AVAILABLE,
                identity,
                LyricsSource.EMBEDDED_PLAIN,
                (),
                parsed.plain_text,
            )
    return None


def extract_embedded_lyrics(identity: TrackLyricsIdentity) -> EmbeddedLyrics:
    """Read supported Mutagen tags without ever saving or mutating the file."""
    if identity.media_path is None:
        return EmbeddedLyrics()
    path = Path(identity.media_path).expanduser()
    if path.suffix.casefold() not in SUPPORTED_EMBEDDED_EXTENSIONS:
        return EmbeddedLyrics()
    try:
        if not path.is_file():
            return EmbeddedLyrics()
        import mutagen

        media = mutagen.File(path, easy=False)
        tags = getattr(media, "tags", None)
    except Exception:
        return EmbeddedLyrics()
    if tags is None:
        return EmbeddedLyrics()

    synced_lines: list[LyricLine] = []
    synced_lrc: list[str] = []
    plain_candidates: list[str] = []
    try:
        values = list(tags.values()) if hasattr(tags, "values") else []
    except Exception:
        values = []
    for frame in values:
        frame_name = frame.__class__.__name__.casefold()
        if frame_name == "sylt":
            try:
                timestamp_format = int(getattr(frame, "format", 2))
            except (TypeError, ValueError, OverflowError):
                timestamp_format = 0
            if timestamp_format != 2:
                continue
            text_value = getattr(frame, "text", ())
            if isinstance(text_value, Sequence):
                for item in text_value:
                    if (
                        isinstance(item, Sequence)
                        and not isinstance(item, (str, bytes))
                        and len(item) == 2
                    ):
                        try:
                            lyric_text = str(item[0]).replace("\x00", "")
                            if len(lyric_text) <= 16_384 and len(synced_lines) < 20_000:
                                synced_lines.append(LyricLine(int(item[1]), lyric_text))
                        except (TypeError, ValueError, OverflowError):
                            continue
        elif frame_name == "uslt":
            plain_candidates.extend(_flatten_text_values(getattr(frame, "text", None)))

    if isinstance(tags, Mapping) or hasattr(tags, "keys"):
        try:
            keys = list(tags.keys())
        except Exception:
            keys = []
        for key in keys:
            normalized_key = str(key).casefold().replace(" ", "").replace("_", "")
            try:
                value = tags[key]
            except Exception:
                continue
            text_values = list(_flatten_text_values(value))
            if normalized_key in {"syncedlyrics", "syncedlyric", "syncedlyrics:lrc"}:
                synced_lrc.extend(text_values)
            elif normalized_key in {
                "lyrics",
                "unsyncedlyrics",
                "unsyncedlyric",
                "©lyr",
                "\xa9lyr",
            }:
                plain_candidates.extend(text_values)

    synchronized = _embedded_result(identity, lines=synced_lines)
    if synchronized is None:
        for candidate in synced_lrc:
            try:
                parsed = parse_lrc(candidate)
            except LyricsParseError:
                continue
            if parsed.synchronized:
                synchronized = _embedded_result(identity, lines=parsed.lines)
                break
    plain = None
    for candidate in plain_candidates:
        plain = _embedded_result(identity, plain=candidate)
        if plain is not None:
            break
    return EmbeddedLyrics(synchronized, plain)


class LyricsService:
    """Bounded current-track resolver with generation-based stale suppression."""

    def __init__(
        self,
        provider: LyricsProvider,
        cache: LyricsCache,
        *,
        max_workers: int = 1,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._generation = 0
        self._pending_request: _LookupRequest | None = None
        self._running_generation: int | None = None
        self._running_cancel: threading.Event | None = None
        self._state = LookupState.IDLE
        self._closed = False
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="music-vault-lyrics",
            daemon=True,
        )
        self._worker.start()

    @property
    def current_generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def pending_count(self) -> int:
        with self._lock:
            current_running = self._running_generation == self._generation
            return int(self._pending_request is not None or current_running)

    @property
    def state(self) -> LookupState:
        with self._lock:
            return self._state

    def _is_current(self, generation: int | None, cancel_event: threading.Event | None) -> bool:
        if cancel_event is not None and cancel_event.is_set():
            return False
        if generation is None:
            return True
        with self._lock:
            return not self._closed and generation == self._generation

    def resolve_local(self, identity: TrackLyricsIdentity, *, force_refresh: bool = False) -> tuple[LyricsResult | None, LyricsResult | None]:
        """Return (winning local result, deferred automatic record)."""
        manual = self.cache.lookup_manual(identity)
        if manual is not None:
            return manual, None
        sidecar_synced = read_sidecar(identity, ".lrc")
        if sidecar_synced is not None:
            return sidecar_synced, None
        embedded = extract_embedded_lyrics(identity)
        if embedded.synchronized is not None:
            return embedded.synchronized, None
        automatic = self.cache.lookup_automatic(identity, force=force_refresh)
        if automatic is not None and automatic.synchronized:
            return automatic, None
        sidecar_plain = read_sidecar(identity, ".txt")
        if sidecar_plain is not None:
            return sidecar_plain, automatic
        if embedded.plain is not None:
            return embedded.plain, automatic
        if automatic is not None:
            return automatic, None
        return None, None

    def resolve(
        self,
        identity: TrackLyricsIdentity,
        *,
        online_enabled: bool = False,
        force_refresh: bool = False,
        generation: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> LyricsResult:
        local, _deferred = self.resolve_local(identity, force_refresh=force_refresh)
        if local is not None:
            return local
        if not online_enabled:
            return LyricsResult(LyricsStatus.DISABLED, identity)
        if not self._is_current(generation, cancel_event):
            return LyricsResult(LyricsStatus.TEMPORARY_ERROR, identity, error_code="cancelled")
        result = self.provider.lookup(LyricsQuery(identity), cancel_event)
        if not self._is_current(generation, cancel_event):
            return LyricsResult(LyricsStatus.TEMPORARY_ERROR, identity, error_code="cancelled")
        if (
            result.identity.stable_id != identity.stable_id
            or result.identity.metadata_fingerprint != identity.metadata_fingerprint
        ):
            return LyricsResult(
                LyricsStatus.TEMPORARY_ERROR,
                identity,
                error_code="provider_identity_mismatch",
            )
        try:
            if result.status in {
                LyricsStatus.AVAILABLE,
                LyricsStatus.INSTRUMENTAL,
                LyricsStatus.NO_MATCH,
                LyricsStatus.AMBIGUOUS,
                LyricsStatus.TEMPORARY_ERROR,
            }:
                result = self.cache.store(result)
        except LyricsCacheError:
            # A private-cache failure must not hide a safe in-memory result.
            pass
        return result

    def begin_generation(self) -> int:
        with self._condition:
            if self._closed:
                raise RuntimeError("Lyrics service is closed.")
            self._generation += 1
            if self._running_cancel is not None:
                self._running_cancel.set()
            if self._pending_request is not None:
                self._pending_request.cancel_event.set()
            self._pending_request = None
            self._state = LookupState.IDLE
            self._condition.notify()
            return self._generation

    def request(
        self,
        identity: TrackLyricsIdentity,
        callback: Callable[[int, LyricsResult], None],
        *,
        online_enabled: bool = False,
        force_refresh: bool = False,
    ) -> int:
        with self._condition:
            if self._closed:
                raise RuntimeError("Lyrics service is closed.")
            self._generation += 1
            generation = self._generation
            if self._running_cancel is not None:
                self._running_cancel.set()
            if self._pending_request is not None:
                self._pending_request.cancel_event.set()
            cancel_event = threading.Event()
            self._pending_request = _LookupRequest(
                generation,
                identity,
                callback,
                online_enabled,
                force_refresh,
                cancel_event,
            )
            self._state = LookupState.LOADING
            self._condition.notify()
            return generation

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while self._pending_request is None and not self._closed:
                    self._condition.wait()
                if self._closed:
                    return
                request = self._pending_request
                self._pending_request = None
                if request is None:
                    continue
                self._running_generation = request.generation
                self._running_cancel = request.cancel_event
            try:
                result = self.resolve(
                    request.identity,
                    online_enabled=request.online_enabled,
                    force_refresh=request.force_refresh,
                    generation=request.generation,
                    cancel_event=request.cancel_event,
                )
            except Exception:
                result = LyricsResult(
                    LyricsStatus.TEMPORARY_ERROR,
                    request.identity,
                    error_code="lookup_failed",
                )
            with self._condition:
                current = (
                    not self._closed
                    and request.generation == self._generation
                    and not request.cancel_event.is_set()
                )
                if self._running_generation == request.generation:
                    self._running_generation = None
                    self._running_cancel = None
                if not current:
                    continue
                self._state = LookupState.AVAILABLE if result.available else LookupState.UNAVAILABLE
                # Delivery while holding the re-entrant service lock prevents a
                # concurrent track change from slipping between the generation
                # check and callback. Callback failures never terminate the worker.
                try:
                    request.callback(request.generation, result)
                except Exception:
                    pass

    def cancel(self) -> int:
        return self.begin_generation()

    def import_manual(self, identity: TrackLyricsIdentity, path: str | Path) -> LyricsResult:
        self.begin_generation()
        return self.cache.import_manual(identity, path)

    def clear_automatic(self, identity: TrackLyricsIdentity | None = None) -> None:
        self.begin_generation()
        self.cache.clear_automatic(identity)

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            if self._running_cancel is not None:
                self._running_cancel.set()
            if self._pending_request is not None:
                self._pending_request.cancel_event.set()
            self._pending_request = None
            self._state = LookupState.CLOSED
            self._condition.notify_all()
        self._worker.join(timeout=0.25)

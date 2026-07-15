"""Bounded LRC and plain-text lyric parsing."""

from __future__ import annotations

import re
from typing import Iterable

from .models import LyricLine, ParsedLyrics


MAX_LYRIC_BYTES = 1024 * 1024
MAX_LYRIC_LINES = 10_000
MAX_RENDERED_LINES = 20_000
MAX_LINE_CHARACTERS = 16_384

_TIMESTAMP_RE = re.compile(r"\[(?P<minutes>\d{1,3}):(?P<seconds>\d{1,2})(?:[.:](?P<fraction>\d{1,3}))?\]")
_OFFSET_RE = re.compile(r"^\s*\[offset\s*:\s*([+-]?\d{1,9})\]\s*$", re.IGNORECASE)
_METADATA_RE = re.compile(r"^\s*\[(?:ar|al|ti|au|by|re|ve|length)\s*:[^\]]*\]\s*$", re.IGNORECASE)


class LyricsParseError(ValueError):
    """A safe parser failure without including lyric content."""


def _decode(value: str | bytes) -> str:
    if isinstance(value, bytes):
        if len(value) > MAX_LYRIC_BYTES:
            raise LyricsParseError("lyrics_too_large")
        try:
            text = value.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise LyricsParseError("lyrics_encoding_invalid") from exc
    else:
        text = str(value)
        if len(text.encode("utf-8")) > MAX_LYRIC_BYTES:
            raise LyricsParseError("lyrics_too_large")
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")


def _bounded_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if len(lines) > MAX_LYRIC_LINES:
        raise LyricsParseError("too_many_lyric_lines")
    if any(len(line) > MAX_LINE_CHARACTERS for line in lines):
        raise LyricsParseError("lyric_line_too_long")
    return lines


def _fraction_ms(value: str | None) -> int:
    if not value:
        return 0
    if len(value) == 1:
        return int(value) * 100
    if len(value) == 2:
        return int(value) * 10
    return int(value[:3])


def parse_lrc(value: str | bytes) -> ParsedLyrics:
    """Parse common line-level LRC without interpreting markup or HTML."""
    text = _decode(value)
    lines = _bounded_lines(text)
    offset_ms = 0
    for raw_line in lines:
        offset_match = _OFFSET_RE.fullmatch(raw_line)
        if offset_match:
            offset_ms = max(-3_600_000, min(3_600_000, int(offset_match.group(1))))

    parsed: list[tuple[int, int, str]] = []
    sequence = 0
    for raw_line in lines:
        if _OFFSET_RE.fullmatch(raw_line) or _METADATA_RE.fullmatch(raw_line):
            continue
        matches = list(_TIMESTAMP_RE.finditer(raw_line))
        if not matches:
            continue
        lyric_text = _TIMESTAMP_RE.sub("", raw_line).strip()
        for match in matches:
            seconds = int(match.group("seconds"))
            if seconds >= 60:
                continue
            milliseconds = (
                int(match.group("minutes")) * 60_000
                + seconds * 1000
                + _fraction_ms(match.group("fraction"))
                + offset_ms
            )
            parsed.append((max(0, milliseconds), sequence, lyric_text))
            sequence += 1
            if len(parsed) > MAX_RENDERED_LINES:
                raise LyricsParseError("too_many_timed_lines")

    parsed.sort(key=lambda item: (item[0], item[1]))
    seen: set[tuple[int, str]] = set()
    result: list[LyricLine] = []
    for timestamp_ms, _sequence, lyric_text in parsed:
        key = (timestamp_ms, lyric_text)
        if key in seen:
            continue
        seen.add(key)
        result.append(LyricLine(timestamp_ms, lyric_text))
    return ParsedLyrics(tuple(result), None, offset_ms)


def normalize_plain_text(value: str | bytes) -> str:
    text = _decode(value)
    lines = _bounded_lines(text)
    # Keep literal markup and meaningful internal blank lines. Whitespace-only
    # edges are presentation noise rather than lyric content.
    normalized = [line.rstrip() for line in lines]
    while normalized and not normalized[0].strip():
        normalized.pop(0)
    while normalized and not normalized[-1].strip():
        normalized.pop()
    return "\n".join(normalized)


def parse_plain_text(value: str | bytes) -> ParsedLyrics:
    text = normalize_plain_text(value)
    return ParsedLyrics((), text or None, 0)


def serialize_lrc(lines: Iterable[LyricLine]) -> str:
    """Create a deterministic private-cache representation of parsed timing."""
    output: list[str] = []
    for line in lines:
        timestamp = max(0, int(line.timestamp_ms))
        minutes, remainder = divmod(timestamp, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        output.append(f"[{minutes:02d}:{seconds:02d}.{milliseconds:03d}]{line.text}")
    return "\n".join(output)

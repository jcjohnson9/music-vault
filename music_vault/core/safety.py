from __future__ import annotations

import re
from pathlib import Path


_QUERY_SECRET_RE = re.compile(
    r"([?&](?:key|api_key|access_token|token)=)[^&\s]+",
    re.IGNORECASE,
)
_GOOGLE_KEY_RE = re.compile("AI" + r"za[0-9A-Za-z_-]{20,}")
_BEARER_RE = re.compile(r"\bBear" + r"er\s+[A-Za-z0-9._~+/-]{8,}", re.IGNORECASE)
_AUTHORIZATION_RE = re.compile(
    r"(Authorization\s*:\s*)[^\r\n]+",
    re.IGNORECASE,
)
_PRIVATE_KEY_RE = re.compile(
    "-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?-----END "
    r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_VIDEO_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]$")
_INVALID_WINDOWS_COMPONENT_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WINDOWS_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def sanitize_error_text(value: object, max_length: int = 2000) -> str:
    """Return useful error context without common credential material."""
    text = str(value or "Unknown error")
    text = _PRIVATE_KEY_RE.sub("<redacted-private-key>", text)
    text = _AUTHORIZATION_RE.sub(r"\1<redacted>", text)
    text = _QUERY_SECRET_RE.sub(r"\1<redacted>", text)
    text = _GOOGLE_KEY_RE.sub("<redacted-google-api-key>", text)
    text = _BEARER_RE.sub("Bearer <redacted>", text)
    text = "".join(character for character in text if character in "\r\n\t" or ord(character) >= 32)
    return text[:max_length]


def extract_source_video_id(path: str | Path) -> str | None:
    match = _VIDEO_ID_RE.search(Path(path).stem)
    return match.group(1) if match else None


def normalize_source_upload_date(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}", text):
        return text
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return "-".join(match.groups())
    match = re.match(r"^(\d{4})", text)
    return match.group(1) if match else None


def safe_playlist_component(title: object, playlist_id: str, max_length: int = 120) -> str:
    original = str(title or "").strip()
    stable_id = re.sub(r"[^A-Za-z0-9_-]", "", str(playlist_id or ""))[:48] or "unknown"
    suffix = f" [{stable_id}]"

    candidate = _INVALID_WINDOWS_COMPONENT_RE.sub("_", original).rstrip(" .")
    unsafe = candidate != original or not candidate or candidate in {".", ".."}
    base_name = candidate.split(".", 1)[0].upper() if candidate else ""
    if base_name in _RESERVED_WINDOWS_NAMES:
        unsafe = True
        candidate = ""

    if not candidate:
        return f"YouTube Playlist{suffix}"[:max_length].rstrip(" .")

    if len(candidate) > max_length:
        unsafe = True

    if unsafe:
        available = max(1, max_length - len(suffix))
        candidate = candidate[:available].rstrip(" .") or "YouTube Playlist"
        if not candidate.endswith(suffix):
            candidate = f"{candidate}{suffix}"

    return candidate[:max_length].rstrip(" .")


def playlist_output_directory(output_root: str | Path, title: object, playlist_id: str) -> Path:
    root = Path(output_root).expanduser().resolve()
    component = safe_playlist_component(title, playlist_id)
    destination = (root / component).resolve()
    if not destination.is_relative_to(root):
        raise ValueError("Playlist output path must remain inside the configured download folder.")
    return destination

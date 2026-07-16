from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_TEXT_BYTES = 2 * 1024 * 1024

ALLOWED_DATA_PATHS = {
    "data/.gitkeep",
    "data/README.md",
}
ALLOWED_PUBLIC_IMAGES = {
    "assets/icons/music_vault.ico",
    "assets/icons/music_vault_icon.png",
}

FORBIDDEN_DIRECTORY_NAMES = {
    ".venv",
    "venv",
    "env",
    "_archive",
    "build",
    "dist",
    ".codex",
    ".agents",
    "metadata_reports",
    "metadata_jobs",
    "provider_cache",
    "source_membership_snapshots",
    "sync_source_runtime",
    "sync_source_snapshots",
}
LYRIC_CACHE_DIRECTORY_NAMES = {
    "lyric_cache",
    "lyrics_cache",
}
LYRIC_FIXTURE_DIRECTORY_NAMES = {
    "lyric_fixtures",
    "lyrics_fixtures",
    "lyrics_provider_fixtures",
    "provider_fixtures",
    "provider_lyrics_fixtures",
}
DATABASE_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
}
AUDIO_SUFFIXES = {
    ".mp3",
    ".m4a",
    ".aac",
    ".flac",
    ".wav",
    ".ogg",
    ".opus",
    ".webm",
    ".wma",
}
ARCHIVE_SUFFIXES = {
    ".zip",
    ".7z",
    ".tar",
    ".gz",
    ".tgz",
    ".rar",
}
PACKAGED_BINARY_SUFFIXES = {
    ".exe",
    ".dll",
    ".pyd",
}
UNREVIEWED_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
}
BINARY_SUFFIXES = (
    AUDIO_SUFFIXES
    | ARCHIVE_SUFFIXES
    | PACKAGED_BINARY_SUFFIXES
    | UNREVIEWED_IMAGE_SUFFIXES
    | {".ico", ".pdf"}
)
LOCAL_SECRET_SUFFIXES = {
    ".pem",
    ".pfx",
    ".p12",
    ".key",
}
RUNTIME_BASENAMES = {
    "discogs_token.txt",
    "youtube_api_key.txt",
    "music_vault_config.json",
    "youtube_download_archive.txt",
    "youtube_failed_ids.txt",
}
SOURCE_RUNTIME_JSON_MARKERS = {
    "source_membership_snapshot",
    "sync_source_run",
    "sync_source_snapshot",
}


def _is_lyric_payload_path(relative_path: str) -> bool:
    """Return whether a path can contain cached, sidecar, or fixture lyrics."""

    normalized = relative_path.replace("\\", "/")
    pure_path = PurePosixPath(normalized)
    parts = {part.casefold() for part in pure_path.parts}
    name = pure_path.name.casefold()
    suffix = pure_path.suffix.casefold()

    if suffix in {".lrc", ".lyrics"}:
        return True
    if parts & (LYRIC_CACHE_DIRECTORY_NAMES | LYRIC_FIXTURE_DIRECTORY_NAMES):
        return True
    fixture_markers = ("fixture", "payload", "response")
    if suffix in {".json", ".txt"} and (
        ("lrclib" in name and any(marker in name for marker in fixture_markers))
        or ("lyrics" in name and any(marker in name for marker in fixture_markers))
    ):
        return True
    if suffix != ".txt":
        return False
    return "lyrics" in parts or name in {"lyric.txt", "lyrics.txt"} or name.endswith(
        (".lyrics.txt", "-lyrics.txt", "_lyrics.txt")
    )


def _run_git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={PROJECT_ROOT.as_posix()}",
            "-C",
            str(PROJECT_ROOT),
            *args,
        ],
        check=False,
        capture_output=True,
    )


def _publication_paths() -> list[str] | None:
    result = _run_git("ls-files", "--cached", "-z")
    if result.returncode != 0:
        print(
            "ERROR: publication candidate unavailable | "
            "rule: valid Git index required | "
            "remediation: initialize and stage the explicit public allowlist"
        )
        return None

    paths = [
        value.decode("utf-8", errors="surrogateescape")
        for value in result.stdout.split(b"\0")
        if value
    ]
    return sorted(set(paths), key=str.casefold)


def _index_bytes(relative_path: str) -> bytes | None:
    result = _run_git("show", f":{relative_path}")
    if result.returncode != 0:
        return None
    return result.stdout


def _path_violations(relative_path: str) -> list[tuple[str, str]]:
    normalized = relative_path.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    lower = normalized.casefold()
    pure_path = PurePosixPath(normalized)
    parts = tuple(part.casefold() for part in pure_path.parts)
    name = pure_path.name.casefold()
    suffix = pure_path.suffix.casefold()
    violations: list[tuple[str, str]] = []

    if parts and parts[0] == "data" and normalized not in ALLOWED_DATA_PATHS:
        violations.append(("private runtime data path", "remove runtime data from the Git index"))

    if set(parts) & FORBIDDEN_DIRECTORY_NAMES:
        violations.append(("forbidden local/generated directory", "remove local or generated material from the Git index"))

    if _is_lyric_payload_path(normalized):
        violations.append(("private lyric cache or provider fixture", "remove lyric content and use an in-memory synthetic test fixture"))

    if suffix in DATABASE_SUFFIXES or re.search(r"\.(?:db|sqlite|sqlite3)-", lower):
        violations.append(("database or database sidecar", "remove private database state from the Git index"))

    if suffix in AUDIO_SUFFIXES:
        violations.append(("audio or downloaded media", "remove media from the Git index"))

    if suffix in ARCHIVE_SUFFIXES:
        violations.append(("archive package", "remove local/archive output from the Git index"))

    if suffix in PACKAGED_BINARY_SUFFIXES:
        violations.append(("packaged binary", "remove build output from the Git index"))

    if suffix in UNREVIEWED_IMAGE_SUFFIXES and normalized not in ALLOWED_PUBLIC_IMAGES:
        violations.append(("unreviewed image or screenshot", "remove it or replace it with an approved sanitized public asset"))

    if suffix == ".lnk":
        violations.append(("Windows shortcut", "remove local shortcuts from the Git index"))

    if suffix == ".log":
        violations.append(("runtime log", "remove and sanitize diagnostic material"))

    if suffix in {".tmp", ".bak", ".part"}:
        violations.append(("temporary or backup file", "remove local temporary material from the Git index"))

    if suffix in LOCAL_SECRET_SUFFIXES or name == ".env" or name.startswith(".env."):
        violations.append(("credential or environment file", "remove secrets from the Git index"))

    if name in RUNTIME_BASENAMES or name.startswith("music_vault_status.json"):
        violations.append(("private Music Vault runtime file", "remove runtime state from the Git index"))

    if suffix == ".json" and any(marker in name for marker in SOURCE_RUNTIME_JSON_MARKERS):
        violations.append(("private source-sync runtime file", "remove source membership or sync state from the Git index"))

    if "api_key" in name or "api-key" in name or "apikey" in name:
        violations.append(("API-key file name", "remove credential files from the Git index"))

    if "metadata_report" in lower:
        violations.append(("private metadata report", "remove remediation reports from the Git index"))

    if "provider_cache" in lower or "remediation_job" in lower:
        violations.append(("private remediation runtime state", "remove remediation caches or job data from the Git index"))

    return violations


def _text_patterns() -> list[tuple[re.Pattern[str], str, str]]:
    user_path = "C:" + "\\Users\\" + "jer" + "jo"
    project_path = (
        user_path
        + "\\Documents\\music_vault_youtube_starter"
        + "\\music_vault_youtube_starter"
    )
    google_key = "AI" + "za" + r"[0-9A-Za-z_-]{20,}"
    bearer = r"\bBear" + r"er\s+[A-Za-z0-9._~+/-]{16,}"
    private_key = "-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    github_token = r"\bgh" + r"[pousr]_[A-Za-z0-9_]{20,}"
    github_pat = "github" + r"_pat_[A-Za-z0-9_]{20,}"
    discogs_token = r"(?i)Discogs\s+token\s*=\s*[A-Za-z0-9._~-]{16,}"

    return [
        (re.compile(re.escape(project_path), re.IGNORECASE), "full personal project path", "replace with a neutral project-root example"),
        (re.compile(re.escape(user_path), re.IGNORECASE), "personal Windows user path", "replace with a neutral user or project path"),
        (re.compile(google_key), "likely Google API key", "remove and rotate the credential"),
        (re.compile(bearer, re.IGNORECASE), "likely bearer token", "remove and rotate the credential"),
        (re.compile(private_key), "private-key material", "remove and rotate the credential"),
        (re.compile(github_token), "likely GitHub token", "remove and rotate the credential"),
        (re.compile(github_pat), "likely GitHub personal access token", "remove and rotate the credential"),
        (re.compile(discogs_token), "likely Discogs personal token", "remove and rotate the credential"),
    ]


def main() -> int:
    paths = _publication_paths()
    if paths is None:
        return 2

    violations: list[tuple[str, str, str]] = []
    patterns = _text_patterns()

    for relative_path in paths:
        normalized = relative_path.replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]

        for rule, remediation in _path_violations(normalized):
            violations.append((normalized, rule, remediation))

        content = _index_bytes(relative_path)
        if content is None:
            violations.append((normalized, "indexed file could not be inspected", "restage or remove the file"))
            continue

        suffix = PurePosixPath(normalized).suffix.casefold()
        if suffix in BINARY_SUFFIXES or b"\0" in content[:8192]:
            continue

        if len(content) > MAX_TEXT_BYTES:
            violations.append((normalized, "text file exceeds scanner limit", "review or split the oversized publication file"))
            continue

        text = content.decode("utf-8", errors="replace")
        for pattern, rule, remediation in patterns:
            if pattern.search(text):
                violations.append((normalized, rule, remediation))

    unique_violations = sorted(set(violations), key=lambda item: (item[0].casefold(), item[1]))

    if unique_violations:
        print("Publication safety check failed.")
        for path, rule, remediation in unique_violations:
            print(f"FAIL: {path} | rule: {rule} | remediation: {remediation}")
        return 1

    print(f"Publication safety check passed: {len(paths)} tracked/staged files inspected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

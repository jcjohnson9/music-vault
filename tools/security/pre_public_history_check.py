from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.security.pre_public_commit_check import (  # noqa: E402
    ALLOWED_DATA_PATHS,
    AUDIO_SUFFIXES,
    DATABASE_SUFFIXES,
    _path_violations,
)


REQUIRED_RELEASE_TAG = "v1.0.0"
MAX_INSPECTED_OBJECT_BYTES = 2 * 1024 * 1024
OBJECT_ID = re.compile(r"^[0-9a-f]{40}$")

HISTORY_FORBIDDEN_DIRECTORIES = {
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "_archive",
    "_legacy_backups",
    "artist_images",
    "backups",
    "build",
    "covers",
    "dist",
    "media_backups",
    "metadata_jobs",
    "metadata_reports",
    "provider_cache",
    "release_artifacts",
    "youtube_downloads",
}
HISTORY_MEDIA_SUFFIXES = AUDIO_SUFFIXES | {
    ".aif",
    ".aiff",
    ".alac",
    ".avi",
    ".mkv",
    ".mov",
    ".mp4",
}
PRIVATE_RUNTIME_NAMES = {
    "music_vault.sqlite3",
    "music_vault_config.json",
    "music_vault_status.json",
    "youtube_api_key.txt",
    "youtube_download_archive.txt",
    "youtube_failed_ids.txt",
}
SENSITIVE_PATH_RULE_MARKERS = {
    "api-key",
    "audio",
    "backup",
    "cover",
    "database",
    "media",
    "metadata report",
    "private library",
    "remediation",
    "runtime",
    "screenshot",
}


class GitScanError(RuntimeError):
    """A Git-plumbing operation failed without exposing its raw output."""


@dataclass(frozen=True)
class RefInfo:
    object_id: str
    object_type: str
    name: str


@dataclass(frozen=True)
class ObjectInfo:
    object_id: str
    object_type: str
    size: int


@dataclass(frozen=True)
class ObjectContext:
    commit_id: str | None = None
    path: str | None = None
    ref: str | None = None


@dataclass(frozen=True)
class Finding:
    rule: str
    object_id: str | None = None
    commit_id: str | None = None
    path: str | None = None
    ref: str | None = None

    def sort_key(self) -> tuple[str, str, str, str, str]:
        return (
            self.rule.casefold(),
            self.path or "",
            self.ref or "",
            self.commit_id or "",
            self.object_id or "",
        )

    def render(self) -> str:
        fields = []
        if self.object_id:
            fields.append(f"object={_short_object(self.object_id)}")
        if self.commit_id:
            fields.append(f"commit={_short_object(self.commit_id)}")
        if self.path:
            fields.append(f"path={self.path}")
        if self.ref:
            fields.append(f"ref={_safe_label(self.ref, kind='ref')}")
        fields.append(f"rule={self.rule}")
        return "FAIL: " + " | ".join(fields)


@dataclass(frozen=True)
class ScanReport:
    refs: int
    commits: int
    tags: int
    blobs: int
    paths: int
    required_tag_target: str | None
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


class GitRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def run(self, *args: str, input_bytes: bytes | None = None, operation: str) -> bytes:
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    f"safe.directory={self.root.as_posix()}",
                    "-C",
                    str(self.root),
                    *args,
                ],
                input=input_bytes,
                check=False,
                capture_output=True,
                timeout=120,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise GitScanError(f"Git plumbing failed during {operation}.") from exc
        if result.returncode:
            raise GitScanError(f"Git plumbing failed during {operation}.")
        return result.stdout

    def command(self, *args: str) -> list[str]:
        return [
            "git",
            "-c",
            f"safe.directory={self.root.as_posix()}",
            "-C",
            str(self.root),
            *args,
        ]


def _short_object(value: str) -> str:
    if OBJECT_ID.fullmatch(value):
        return value[:12]
    if value == "INDEX":
        return "INDEX"
    return "<invalid-object>"


def _content_patterns() -> tuple[tuple[str, re.Pattern[bytes]], ...]:
    project_folder = b"music" + b"_vault_youtube_starter"
    return (
        (
            "personal absolute project path",
            re.compile(
                rb"(?i)[A-Z]:[\\/]Users[\\/][^\\/\r\n]+[\\/].{0,160}"
                + re.escape(project_folder)
            ),
        ),
        (
            "personal absolute project path",
            re.compile(rb"(?i)/home/[^/\r\n]+/.{0,160}" + re.escape(project_folder)),
        ),
        ("likely Google API key", re.compile(rb"AIza[0-9A-Za-z_-]{20,}")),
        (
            "likely bearer token",
            re.compile(rb"\bBearer[ \t]+[A-Za-z0-9._~+/-]{16,}", re.IGNORECASE),
        ),
        (
            "private-key material",
            re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        ),
        ("likely GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{20,}")),
        (
            "likely GitHub personal access token",
            re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
        ),
    )


def _safe_label(value: str, *, kind: str) -> str:
    encoded = value.encode("utf-8", errors="surrogateescape")
    unsafe = (
        len(encoded) > 240
        or any(byte < 32 or byte == 127 for byte in encoded)
        or any(pattern.search(encoded) for _rule, pattern in _content_patterns())
    )
    if unsafe:
        digest = hashlib.sha256(encoded).hexdigest()[:16]
        return f"<redacted-{kind}-sha256:{digest}>"
    return value


def _safe_path(path: str, rules: Iterable[str]) -> str:
    lowered_rules = " ".join(rules).casefold()
    normalized = path.replace("\\", "/")
    suffix = PurePosixPath(normalized).suffix.casefold()
    parts = {part.casefold() for part in PurePosixPath(normalized).parts}
    sensitive = (
        suffix in HISTORY_MEDIA_SUFFIXES | DATABASE_SUFFIXES
        or (parts and next(iter(PurePosixPath(normalized).parts), "").casefold() == "data")
        or bool(parts & {"covers", "artist_images", "media_backups", "metadata_reports"})
        or any(marker in lowered_rules for marker in SENSITIVE_PATH_RULE_MARKERS)
    )
    if sensitive:
        digest = hashlib.sha256(
            normalized.encode("utf-8", errors="surrogateescape")
        ).hexdigest()[:16]
        return f"<redacted-path-sha256:{digest}>"
    return _safe_label(normalized, kind="path")


def _history_path_violations(path: str) -> tuple[str, ...]:
    normalized = path.replace("\\", "/")
    pure = PurePosixPath(normalized)
    parts = tuple(part.casefold() for part in pure.parts)
    name = pure.name.casefold()
    suffix = pure.suffix.casefold()
    rules = [rule for rule, _remediation in _path_violations(normalized)]

    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:/", normalized)
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
    ):
        rules.append("unsafe Git tree path")

    if set(parts) & HISTORY_FORBIDDEN_DIRECTORIES:
        rules.append("forbidden historical local/generated directory")

    if suffix in HISTORY_MEDIA_SUFFIXES:
        rules.append("music or media file")

    if name in PRIVATE_RUNTIME_NAMES or name.startswith("music_vault_status.json"):
        rules.append("private Music Vault runtime file")

    if parts and parts[0] == "data" and normalized not in ALLOWED_DATA_PATHS:
        rules.append("private library data")

    if re.search(
        r"(?i)(?:^|[_-])(?:backup|failure|failed|remediation[_-]?job)(?:[_\-.]|$)",
        name,
    ):
        if not normalized.startswith("docs/") and not normalized.startswith("tests/"):
            rules.append("private failure, remediation, or backup state")

    return tuple(sorted(set(rules), key=str.casefold))


def _parse_refs(repository: GitRepository) -> list[RefInfo]:
    output = repository.run(
        "for-each-ref",
        "--format=%(objectname)%09%(objecttype)%09%(refname)",
        "refs/heads",
        "refs/remotes",
        "refs/tags",
        operation="reference enumeration",
    )
    refs: list[RefInfo] = []
    for raw in output.splitlines():
        fields = raw.decode("utf-8", errors="surrogateescape").split("\t", 2)
        if len(fields) != 3 or not OBJECT_ID.fullmatch(fields[0]):
            raise GitScanError("Git returned invalid reference metadata.")
        refs.append(RefInfo(fields[0], fields[1], fields[2]))
    return refs


def _resolve_commit(repository: GitRepository, revision: str, *, operation: str) -> str:
    value = repository.run(
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
        operation=operation,
    ).decode("ascii", errors="strict").strip()
    if not OBJECT_ID.fullmatch(value):
        raise GitScanError("Git returned an invalid commit identifier.")
    return value


def _reachable_commits(repository: GitRepository, roots: Sequence[str]) -> list[str]:
    payload = ("\n".join(roots) + "\n").encode("ascii")
    output = repository.run(
        "rev-list",
        "--reverse",
        "--topo-order",
        "--stdin",
        input_bytes=payload,
        operation="reachable commit enumeration",
    )
    commits = [line.decode("ascii") for line in output.splitlines()]
    if any(not OBJECT_ID.fullmatch(commit) for commit in commits):
        raise GitScanError("Git returned an invalid reachable commit.")
    return commits


def _reachable_objects(
    repository: GitRepository, roots: Sequence[str]
) -> dict[str, str | None]:
    payload = ("\n".join(roots) + "\n").encode("ascii")
    output = repository.run(
        "rev-list",
        "--objects",
        "--stdin",
        input_bytes=payload,
        operation="reachable object enumeration",
    )
    objects: dict[str, str | None] = {}
    for raw in output.splitlines():
        object_id, separator, raw_path = raw.partition(b" ")
        decoded_id = object_id.decode("ascii", errors="strict")
        if not OBJECT_ID.fullmatch(decoded_id):
            raise GitScanError("Git returned an invalid reachable object.")
        path = (
            raw_path.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            if separator
            else None
        )
        objects.setdefault(decoded_id, path)
    return objects


def _parse_tree(
    repository: GitRepository, commit_id: str
) -> Iterator[tuple[str, str, str, str]]:
    output = repository.run(
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit_id,
        operation="historical tree inspection",
    )
    for raw in output.split(b"\0"):
        if not raw:
            continue
        metadata, separator, raw_path = raw.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise GitScanError("Git returned invalid historical tree metadata.")
        mode = fields[0].decode("ascii", errors="strict")
        object_type = fields[1].decode("ascii", errors="strict")
        object_id = fields[2].decode("ascii", errors="strict")
        if not OBJECT_ID.fullmatch(object_id):
            raise GitScanError("Git returned an invalid historical tree object.")
        path = raw_path.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        yield mode, object_type, object_id, path


def _parse_index(repository: GitRepository) -> Iterator[tuple[str, str, str, str]]:
    output = repository.run(
        "ls-files",
        "--stage",
        "-z",
        operation="current tracked index inspection",
    )
    for raw in output.split(b"\0"):
        if not raw:
            continue
        metadata, separator, raw_path = raw.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise GitScanError("Git returned invalid index metadata.")
        mode = fields[0].decode("ascii", errors="strict")
        object_id = fields[1].decode("ascii", errors="strict")
        stage = fields[2].decode("ascii", errors="strict")
        if not OBJECT_ID.fullmatch(object_id):
            raise GitScanError("Git returned an invalid index object.")
        path = raw_path.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        yield mode, object_id, stage, path


def _object_info(
    repository: GitRepository, object_ids: Iterable[str]
) -> dict[str, ObjectInfo]:
    ordered = sorted(set(object_ids))
    if not ordered:
        return {}
    output = repository.run(
        "cat-file",
        "--batch-check=%(objectname)|%(objecttype)|%(objectsize)",
        input_bytes=("\n".join(ordered) + "\n").encode("ascii"),
        operation="object metadata inspection",
    )
    result: dict[str, ObjectInfo] = {}
    for raw in output.splitlines():
        fields = raw.decode("ascii", errors="strict").split("|")
        if len(fields) != 3 or not OBJECT_ID.fullmatch(fields[0]):
            raise GitScanError("Git returned invalid object metadata.")
        try:
            size = int(fields[2])
        except ValueError as exc:
            raise GitScanError("Git returned an invalid object size.") from exc
        if size < 0:
            raise GitScanError("Git returned an invalid object size.")
        result[fields[0]] = ObjectInfo(fields[0], fields[1], size)
    if set(result) != set(ordered):
        raise GitScanError("A reachable Git object could not be inspected.")
    return result


def _read_exact(stream, size: int) -> bytes:
    remaining = size
    blocks: list[bytes] = []
    while remaining:
        block = stream.read(remaining)
        if not block:
            raise GitScanError("A reachable Git object could not be read safely.")
        blocks.append(block)
        remaining -= len(block)
    return b"".join(blocks)


def _stream_object_content(
    repository: GitRepository,
    objects: Sequence[ObjectInfo],
) -> Iterator[tuple[ObjectInfo, bytes]]:
    if not objects:
        return
    try:
        process = subprocess.Popen(
            repository.command("cat-file", "--batch"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise GitScanError("Git plumbing failed during bounded object inspection.") from exc
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise GitScanError("Git plumbing could not open bounded object inspection pipes.")
    try:
        for expected in objects:
            process.stdin.write((expected.object_id + "\n").encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().rstrip(b"\n").split()
            if len(header) != 3:
                raise GitScanError("Git returned invalid bounded object framing.")
            object_id = header[0].decode("ascii", errors="strict")
            object_type = header[1].decode("ascii", errors="strict")
            try:
                size = int(header[2])
            except ValueError as exc:
                raise GitScanError("Git returned invalid bounded object framing.") from exc
            if (
                object_id != expected.object_id
                or object_type != expected.object_type
                or size != expected.size
            ):
                raise GitScanError("Git returned inconsistent bounded object metadata.")
            content = _read_exact(process.stdout, size)
            if process.stdout.read(1) != b"\n":
                raise GitScanError("Git returned invalid bounded object framing.")
            yield expected, content
        process.stdin.close()
        if process.wait(timeout=30) != 0:
            raise GitScanError("Git plumbing failed during bounded object inspection.")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def _finding_for_path(
    *, rule: str, path: str, object_id: str, commit_id: str
) -> Finding:
    return Finding(
        rule=rule,
        object_id=object_id,
        commit_id=commit_id,
        path=_safe_path(path, [rule]),
    )


def scan_repository(
    root: Path,
    *,
    required_tag: str = REQUIRED_RELEASE_TAG,
) -> ScanReport:
    repository = GitRepository(root)
    repository.run("rev-parse", "--git-dir", operation="repository validation")
    refs = _parse_refs(repository)
    findings: list[Finding] = []

    if not refs:
        raise GitScanError("No local, remote-tracking, or tag references are available.")

    head_commit = _resolve_commit(repository, "HEAD", operation="HEAD resolution")
    roots = sorted({reference.object_id for reference in refs} | {head_commit})
    commits = _reachable_commits(repository, roots)
    reachable_objects = _reachable_objects(repository, roots)
    reachable_objects.update({reference.object_id: None for reference in refs})

    tag_refs = [reference for reference in refs if reference.name.startswith("refs/tags/")]
    exact_tag_name = f"refs/tags/{required_tag}"
    exact_tags = [reference for reference in tag_refs if reference.name == exact_tag_name]
    required_tag_target: str | None = None
    if len(exact_tags) != 1:
        findings.append(Finding(rule=f"required tag {required_tag} is missing or ambiguous"))
    else:
        exact_tag = exact_tags[0]
        if exact_tag.object_type != "tag":
            findings.append(
                Finding(
                    rule=f"required tag {required_tag} is not annotated",
                    object_id=exact_tag.object_id,
                    ref=exact_tag.name,
                )
            )
        try:
            required_tag_target = _resolve_commit(
                repository,
                exact_tag.name,
                operation="required release tag resolution",
            )
        except GitScanError:
            findings.append(
                Finding(
                    rule=f"required tag {required_tag} does not resolve to a commit",
                    object_id=exact_tag.object_id,
                    ref=exact_tag.name,
                )
            )
        else:
            if required_tag_target not in set(commits):
                findings.append(
                    Finding(
                        rule=f"required tag {required_tag} history is not fully reachable",
                        object_id=exact_tag.object_id,
                        commit_id=required_tag_target,
                        ref=exact_tag.name,
                    )
                )

    object_contexts: dict[str, ObjectContext] = {}
    historical_paths: set[str] = set()
    path_findings: dict[tuple[str, str], Finding] = {}
    blob_ids: set[str] = set()

    for commit_id in commits:
        for mode, object_type, object_id, path in _parse_tree(repository, commit_id):
            historical_paths.add(path)
            if object_type == "blob":
                blob_ids.add(object_id)
                object_contexts.setdefault(
                    object_id, ObjectContext(commit_id=commit_id, path=path)
                )
            elif mode == "160000" or object_type == "commit":
                key = (path, "gitlink/submodule history is opaque to this scanner")
                path_findings.setdefault(
                    key,
                    _finding_for_path(
                        rule=key[1],
                        path=path,
                        object_id=object_id,
                        commit_id=commit_id,
                    ),
                )
            for rule in _history_path_violations(path):
                key = (path, rule)
                path_findings.setdefault(
                    key,
                    _finding_for_path(
                        rule=rule,
                        path=path,
                        object_id=object_id,
                        commit_id=commit_id,
                    ),
                )

    for mode, object_id, stage, path in _parse_index(repository):
        historical_paths.add(path)
        blob_ids.add(object_id)
        reachable_objects.setdefault(object_id, path)
        object_contexts.setdefault(object_id, ObjectContext(commit_id="INDEX", path=path))
        if stage != "0":
            key = (path, "current tracked index contains an unresolved merge stage")
            path_findings.setdefault(
                key,
                _finding_for_path(
                    rule=key[1], path=path, object_id=object_id, commit_id="INDEX"
                ),
            )
        if mode == "160000":
            key = (path, "current tracked index contains an opaque gitlink/submodule")
            path_findings.setdefault(
                key,
                _finding_for_path(
                    rule=key[1], path=path, object_id=object_id, commit_id="INDEX"
                ),
            )
        for rule in _history_path_violations(path):
            key = (path, rule)
            path_findings.setdefault(
                key,
                _finding_for_path(
                    rule=rule, path=path, object_id=object_id, commit_id="INDEX"
                ),
            )

    findings.extend(path_findings.values())

    all_object_ids = set(reachable_objects) | blob_ids
    metadata = _object_info(repository, all_object_ids)
    for reference in tag_refs:
        if reference.object_id in metadata and metadata[reference.object_id].object_type == "tag":
            object_contexts.setdefault(
                reference.object_id, ObjectContext(ref=reference.name)
            )

    inspectable: list[ObjectInfo] = []
    for info in metadata.values():
        if info.object_type not in {"blob", "commit", "tag"}:
            continue
        context = object_contexts.get(info.object_id, ObjectContext())
        if info.size > MAX_INSPECTED_OBJECT_BYTES:
            path = (
                _safe_path(context.path, ["oversized reachable object"])
                if context.path
                else None
            )
            findings.append(
                Finding(
                    rule=(
                        "reachable object exceeds bounded content inspection limit "
                        f"({MAX_INSPECTED_OBJECT_BYTES} bytes)"
                    ),
                    object_id=info.object_id,
                    commit_id=context.commit_id,
                    path=path,
                    ref=context.ref,
                )
            )
            continue
        inspectable.append(info)

    patterns = _content_patterns()
    content_findings: dict[tuple[str, str], Finding] = {}
    for info, content in _stream_object_content(
        repository, sorted(inspectable, key=lambda item: item.object_id)
    ):
        context = object_contexts.get(info.object_id, ObjectContext())
        if info.object_type == "commit":
            context = ObjectContext(
                commit_id=info.object_id,
                path="<commit-message>",
            )
        for rule, pattern in patterns:
            if not pattern.search(content):
                continue
            path = context.path
            safe_path = _safe_path(path, [rule]) if path else None
            key = (info.object_id, rule)
            content_findings.setdefault(
                key,
                Finding(
                    rule=rule,
                    object_id=info.object_id,
                    commit_id=context.commit_id,
                    path=safe_path,
                    ref=context.ref,
                ),
            )

    findings.extend(content_findings.values())
    unique_findings = tuple(sorted(set(findings), key=Finding.sort_key))
    blob_count = sum(1 for info in metadata.values() if info.object_type == "blob")

    return ScanReport(
        refs=len(refs),
        commits=len(commits),
        tags=len(tag_refs),
        blobs=blob_count,
        paths=len(historical_paths),
        required_tag_target=required_tag_target,
        findings=unique_findings,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only complete reachable Git-history publication scanner."
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=PROJECT_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--required-tag",
        default=REQUIRED_RELEASE_TAG,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = scan_repository(
            arguments.repo,
            required_tag=arguments.required_tag,
        )
    except GitScanError as exc:
        print(f"ERROR: {exc}")
        return 2

    if not report.ok:
        print("Complete Git-history publication safety check failed.")
        for finding in report.findings:
            print(finding.render())
        return 1

    target = _short_object(report.required_tag_target or "")
    print(
        "Complete Git-history publication safety check passed: "
        f"{report.refs} refs, {report.commits} commits, {report.tags} tags, "
        f"{report.blobs} blobs, and {report.paths} historical/current paths inspected; "
        f"{arguments.required_tag} target {target}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

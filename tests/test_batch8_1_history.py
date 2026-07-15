from __future__ import annotations

import subprocess
from pathlib import Path

from tools.security import pre_public_commit_check as publication
from tools.security import pre_public_history_check as history


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={repo.as_posix()}",
            "-C",
            str(repo),
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "synthetic-history"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Synthetic Release Test")
    _git(repo, "config", "user.email", "release-test@example.invalid")
    (repo / "README.md").write_text("Synthetic public repository.\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial synthetic source")
    return repo


def _commit_file(repo: Path, relative: str, content: bytes, message: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    _git(repo, "add", "--", relative)
    _git(repo, "commit", "-m", message)


def _remove_and_tag(repo: Path, relative: str) -> None:
    (repo / relative).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "remove synthetic private fixture")
    _git(repo, "tag", "-a", history.REQUIRED_RELEASE_TAG, "-m", "Synthetic v1.0.0")


def test_history_scanner_detects_historical_api_key_without_printing_it(
    tmp_path: Path, capsys
) -> None:
    repo = _repository(tmp_path)
    secret = "AIza" + "S" * 35
    _commit_file(repo, "notes.txt", secret.encode("ascii"), "add historical fixture")
    _remove_and_tag(repo, "notes.txt")

    result = history.main(["--repo", str(repo)])
    output = capsys.readouterr().out

    assert result == 1
    assert "likely Google API key" in output
    assert secret not in output
    assert "S" * 20 not in output


def test_history_scanner_detects_historical_database_path(
    tmp_path: Path, capsys
) -> None:
    repo = _repository(tmp_path)
    relative = "data/music_vault.sqlite3"
    _commit_file(repo, relative, b"SQLite format 3\x00synthetic", "add database fixture")
    _remove_and_tag(repo, relative)

    result = history.main(["--repo", str(repo)])
    output = capsys.readouterr().out

    assert result == 1
    assert "database" in output.casefold()
    assert "SQLite format" not in output


def test_history_scanner_detects_historical_media_path(tmp_path: Path, capsys) -> None:
    repo = _repository(tmp_path)
    relative = "downloads/synthetic-track.mp3"
    _commit_file(repo, relative, b"ID3\x00synthetic-media", "add media fixture")
    _remove_and_tag(repo, relative)

    result = history.main(["--repo", str(repo)])
    output = capsys.readouterr().out

    assert result == 1
    assert "music or media file" in output
    assert "synthetic-track" not in output


def test_candidate_path_policy_rejects_lyric_payloads_but_not_source() -> None:
    for relative in (
        "adjacent/synthetic.lrc",
        "cache/files/synthetic.lyrics",
        "cache/lyrics/synthetic.txt",
        "tests/provider_fixtures/lrclib.json",
        "tests/lrclib_response.json",
    ):
        rules = {
            rule for rule, _remediation in publication._path_violations(relative)
        }
        assert "private lyric cache or provider fixture" in rules

    assert publication._path_violations("music_vault/lyrics/service.py") == []
    assert publication._path_violations("tests/test_lyrics_provider.py") == []
    assert publication._path_violations("docs/LYRICS.md") == []


def test_history_scanner_detects_lyrics_without_printing_path(
    tmp_path: Path, capsys
) -> None:
    repo = _repository(tmp_path)
    relative = "sidecars/synthetic-private-track.lrc"
    _commit_file(repo, relative, b"[00:01.00]synthetic fixture", "add lyric fixture")
    _remove_and_tag(repo, relative)

    result = history.main(["--repo", str(repo)])
    output = capsys.readouterr().out

    assert result == 1
    assert "lyric" in output.casefold()
    assert "synthetic-private-track" not in output


def test_clean_synthetic_git_history_passes(tmp_path: Path, capsys) -> None:
    repo = _repository(tmp_path)
    source = repo / "music_vault.py"
    source.write_text("APP_VERSION = '1.0.0'\n", encoding="utf-8")
    _git(repo, "add", "music_vault.py")
    _git(repo, "commit", "-m", "add clean synthetic source")
    _git(repo, "tag", "-a", history.REQUIRED_RELEASE_TAG, "-m", "Synthetic v1.0.0")

    result = history.main(["--repo", str(repo)])
    output = capsys.readouterr().out

    assert result == 0
    assert "Complete Git-history publication safety check passed" in output
    assert history.REQUIRED_RELEASE_TAG in output

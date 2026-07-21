from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from music_vault.core.safety import (
    extract_source_video_id,
    playlist_output_directory,
    safe_playlist_component,
    sanitize_error_text,
)
from music_vault.core.sync_result import SyncFailure, SyncImportItem, SyncResult, sync_ui_values
from music_vault.core.youtube_sync import AuthorizedYouTubePlaylistSyncer, YouTubeSyncConfig


def test_secret_redaction_preserves_useful_context():
    fake_key = "AIza" + "A" * 35
    authorization = "Bear" + "er " + "abcdefghijklmnop"
    text = sanitize_error_text(
        f"403 https://example.test?key={fake_key}&x=1 Authorization: {authorization}"
    )
    assert fake_key not in text
    assert "abcdefghijklmnop" not in text
    assert "403" in text and "x=1" in text


def test_private_key_material_is_redacted():
    secret = (
        "-----BEGIN " + "PRIVATE KEY-----\nfake-value\n-----END " + "PRIVATE KEY-----"
    )
    assert "fake-value" not in sanitize_error_text(f"error {secret}")


@pytest.mark.parametrize("title", ["", ".", "..", "CON", "aux.txt", "bad:name", "trail. "])
def test_unsafe_windows_playlist_names_use_stable_safe_fallback(title):
    component = safe_playlist_component(title, "PL-safe-id")
    assert component not in {"", ".", "..", "CON"}
    assert not component.endswith((".", " "))
    assert "PL-safe-id" in component


def test_ordinary_playlist_name_is_preserved():
    assert safe_playlist_component("Road Trip 2026", "playlist") == "Road Trip 2026"


def test_playlist_path_remains_contained_and_bounded(tmp_path):
    destination = playlist_output_directory(tmp_path, "../" + "x" * 500, "playlist")
    assert destination.is_relative_to(tmp_path.resolve())
    assert len(destination.name) <= 120


def test_source_video_id_extraction():
    assert extract_source_video_id("Song [abcdefghijk].mp3") == "abcdefghijk"
    assert extract_source_video_id("Song.mp3") is None


def test_sync_result_statuses_and_ui_counts():
    complete = SyncResult("complete", "p", "Mix", downloaded_count=2, existing_count=3)
    complete.refresh_status()
    assert complete.status == "complete"
    assert sync_ui_values(complete) == {
        "status": "Complete", "downloaded": "2", "existing": "3", "failed": "0"
    }
    complete.add_failure(SyncFailure("abcdefghijk", "Song", "No access", "unavailable"))
    assert complete.status == "complete_with_issues"
    assert sync_ui_values(complete)["failed"] == "1"
    failed = SyncResult.failed_result("API failed")
    assert failed.status == "failed" and failed.failed_count == 1
    assert failed.to_status_dict()["last_sync_failed_count"] == 1


class FakeSyncer(AuthorizedYouTubePlaylistSyncer):
    def __init__(self, config, entries, downloaded_file: Path | None = None):
        super().__init__(config)
        self.entries = entries
        self.downloaded_file = downloaded_file
        self.download_calls: list[str] = []

    def _extract_playlist_entries_via_api(self):
        return "playlist-id", "Synthetic Mix", self.entries

    def _download_one(self, video_id, playlist_id, playlist_title):
        self.download_calls.append(video_id)
        if self.downloaded_file is None:
            raise RuntimeError("synthetic download failure")
        self.downloaded_file.parent.mkdir(parents=True, exist_ok=True)
        self.downloaded_file.write_bytes(b"synthetic")
        return SyncImportItem(str(self.downloaded_file), video_id, "2024-01-02")


def _config(tmp_path, *, existing=()):
    return YouTubeSyncConfig(
        "https://www.youtube.com/playlist?list=playlist-id",
        tmp_path / "downloads",
        tmp_path / "archive.txt",
        existing_video_ids=frozenset(existing),
    )


def _entry(video_id="abcdefghijk"):
    return {"id": video_id, "title": "Song", "unavailable_reason": None}


def test_stale_archive_does_not_suppress_redownload(tmp_path):
    config = _config(tmp_path)
    config.archive_file.write_text("youtube abcdefghijk\n", encoding="utf-8")
    target = config.output_dir / "Synthetic Mix" / "Song [abcdefghijk].mp3"
    syncer = FakeSyncer(config, [_entry()], target)
    result = syncer.sync()
    assert syncer.download_calls == ["abcdefghijk"]
    assert result.downloaded_count == 1 and result.status == "complete"


def test_database_identity_prevents_duplicate_after_folder_change(tmp_path):
    config = _config(tmp_path, existing={"abcdefghijk"})
    syncer = FakeSyncer(config, [_entry()])
    result = syncer.sync()
    assert syncer.download_calls == []
    assert result.existing_count == 1
    assert config.archive_file.read_text(encoding="utf-8").count("abcdefghijk") == 1


def test_existing_unimported_file_is_returned_for_targeted_import(tmp_path, monkeypatch):
    config = _config(tmp_path)
    folder = config.output_dir / "Old"
    folder.mkdir(parents=True)
    media = folder / "Song [abcdefghijk].mp3"
    media.write_bytes(b"synthetic")
    monkeypatch.setattr(
        "music_vault.core.youtube_sync.is_verified_reusable_audio",
        lambda path, **_kwargs: Path(path) == media,
    )
    result = FakeSyncer(config, [_entry()]).sync()
    assert result.existing_count == 1
    assert result.import_items == [SyncImportItem(str(media.resolve()), "abcdefghijk")]


def test_shared_download_index_is_borrowed_without_iteration_or_revalidation(tmp_path):
    media = tmp_path / "prevalidated.mp3"
    media.write_bytes(b"synthetic")

    class NoRestatPath:
        def __str__(self):
            return str(media.resolve())

        def is_file(self):
            raise AssertionError("A prevalidated shared path must not be stated again.")

    class TrackingIndex(Mapping[str, Path]):
        def __init__(self):
            self.iterations = 0
            self.lookups = 0
            self._value = NoRestatPath()

        def __getitem__(self, key):
            self.lookups += 1
            if key != "abcdefghijk":
                raise KeyError(key)
            return self._value

        def __iter__(self) -> Iterator[str]:
            self.iterations += 1
            raise AssertionError("The shared index must not be copied or traversed in full.")

        def __len__(self):
            return 1

    shared = TrackingIndex()
    config = YouTubeSyncConfig(
        "https://www.youtube.com/playlist?list=playlist-id",
        tmp_path / "downloads",
        tmp_path / "archive.txt",
        shared_download_index=shared,
    )
    syncer = FakeSyncer(config, [_entry()])

    assert syncer._existing_downloads() is shared
    result = syncer.sync()

    assert result.existing_count == 1
    assert result.import_items == [SyncImportItem(str(media.resolve()), "abcdefghijk")]
    assert syncer.download_calls == []
    assert shared.iterations == 0
    assert shared.lookups > 0


def test_legacy_known_download_tuple_still_validates_entries(tmp_path):
    media = tmp_path / "legacy.mp3"
    media.write_bytes(b"synthetic")
    missing = tmp_path / "missing.mp3"
    config = YouTubeSyncConfig(
        "https://www.youtube.com/playlist?list=playlist-id",
        tmp_path / "downloads",
        tmp_path / "archive.txt",
        known_downloads=(
            ("abcdefghijk", str(media)),
            ("bcdefghijkl", str(missing)),
            ("invalid", str(media)),
        ),
    )

    assert FakeSyncer(config, [])._existing_downloads() == {
        "abcdefghijk": media.resolve()
    }


def test_deleted_file_and_stale_archive_are_retry_eligible(tmp_path):
    config = _config(tmp_path)
    config.archive_file.write_text("youtube abcdefghijk\nyoutube abcdefghijk\n", encoding="utf-8")
    result = FakeSyncer(config, [_entry()]).sync()
    assert result.status == "complete_with_issues"
    assert result.failed_count == 1
    assert config.archive_file.read_text(encoding="utf-8") == ""


def test_failed_item_is_retried_and_can_succeed_next_manual_sync(tmp_path):
    config = _config(tmp_path)
    first = FakeSyncer(config, [_entry()]).sync()
    assert first.failed_count == 1
    target = config.output_dir / "Synthetic Mix" / "Song [abcdefghijk].mp3"
    second_syncer = FakeSyncer(config, [_entry()], target)
    second = second_syncer.sync()
    assert second_syncer.download_calls == ["abcdefghijk"]
    assert second.status == "complete" and second.downloaded_count == 1


def test_private_item_is_truthful_issue_not_false_complete(tmp_path):
    entry = {
        "id": "abcdefghijk",
        "title": "Private video",
        "unavailable_reason": "Unavailable through public workflow.",
    }
    result = FakeSyncer(_config(tmp_path), [entry]).sync()
    assert result.status == "complete_with_issues"
    assert result.failed_count == 1


def test_top_level_enumeration_error_is_failed_result(tmp_path):
    class FailingSyncer(FakeSyncer):
        def _extract_playlist_entries_via_api(self):
            raise RuntimeError("synthetic API failure")

    result = FailingSyncer(_config(tmp_path), []).sync()
    assert result.status == "failed"
    assert result.failed_count == 1


def test_sync_source_contains_no_browser_cookie_access():
    source = Path("music_vault/core/youtube_sync.py").read_text(encoding="utf-8")
    assert "cookiesfrombrowser" not in source

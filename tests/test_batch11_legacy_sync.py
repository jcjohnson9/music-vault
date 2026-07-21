from __future__ import annotations

from types import SimpleNamespace

import music_vault.app as app_module
from music_vault.app import MusicVaultWindow
from music_vault.core.sync_result import SyncImportItem, SyncResult


class _LegacySyncDB:
    def __init__(self, track_paths: dict[str, tuple[int, str]]) -> None:
        self.track_paths = track_paths
        self.quality_by_track: dict[int, dict[str, object]] = {}
        self.upserts: list[tuple[int, dict[str, object]]] = []

    def canonical_track_id(
        self,
        source_kind: str,
        video_id: str,
        *,
        require_existing_file: bool = False,
    ) -> int | None:
        assert source_kind == "youtube"
        value = self.track_paths.get(video_id)
        return value[0] if value is not None else None

    def get_track(self, track_id: int) -> dict[str, object] | None:
        for candidate_id, path in self.track_paths.values():
            if candidate_id == track_id:
                return {"id": track_id, "path": path}
        return None

    def upsert_track_media_quality(
        self,
        track_id: int,
        **quality_facts: object,
    ) -> None:
        values = dict(quality_facts)
        self.upserts.append((track_id, values))
        self.quality_by_track[track_id] = values

    def get_track_media_quality(self, track_id: int) -> dict[str, object] | None:
        return self.quality_by_track.get(track_id)


def _harness(download_root, db: _LegacySyncDB) -> SimpleNamespace:
    return SimpleNamespace(
        config={"download_folder": str(download_root)},
        db=db,
        import_ffprobe_path=lambda: None,
    )


def _facts(*, profile: str = "best_original", size: int = 12) -> dict[str, object]:
    return {
        "acquisition_profile": profile,
        "stored_codec": "opus" if profile == "best_original" else "mp3",
        "stored_filesize_bytes": size,
        "transformation_kind": (
            "none" if profile == "best_original" else "lossy_transcode"
        ),
    }


def test_legacy_import_rejects_outside_path_and_false_import_before_quality_write(
    tmp_path,
    monkeypatch,
) -> None:
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    inside = download_root / "inside.opus"
    outside = tmp_path / "outside.opus"
    inside.write_bytes(b"inside")
    outside.write_bytes(b"outside")
    db = _LegacySyncDB(
        {
            "abcdefghijk": (1, str(outside.resolve())),
            "bcdefghijkl": (2, str(inside.resolve())),
        }
    )
    imported_paths = []

    def reject_import(_db, path, _source):
        imported_paths.append(path)
        return False

    monkeypatch.setattr(app_module, "import_file", reject_import)
    result = SyncResult(
        "complete",
        "playlist",
        "Synthetic",
        import_items=[
            SyncImportItem(str(outside), "abcdefghijk", quality_facts=_facts()),
            SyncImportItem(str(inside), "bcdefghijkl", quality_facts=_facts()),
        ],
        successful_video_ids={"abcdefghijk", "bcdefghijkl"},
    )

    imported_count = MusicVaultWindow._import_legacy_youtube_items(
        _harness(download_root, db),
        result,
    )

    assert imported_count == 0
    assert imported_paths == [inside.resolve()]
    assert db.upserts == []
    assert result.failed_count == 2
    assert result.status == "complete_with_issues"
    assert result.successful_video_ids == set()


def test_legacy_import_requires_exact_canonical_path_before_quality_write(
    tmp_path,
    monkeypatch,
) -> None:
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    downloaded = download_root / "downloaded.opus"
    canonical = download_root / "canonical.opus"
    downloaded.write_bytes(b"downloaded")
    canonical.write_bytes(b"canonical")
    db = _LegacySyncDB({"abcdefghijk": (1, str(canonical.resolve()))})
    monkeypatch.setattr(app_module, "import_file", lambda *_args: True)
    result = SyncResult(
        "complete",
        "playlist",
        "Synthetic",
        import_items=[
            SyncImportItem(
                str(downloaded),
                "abcdefghijk",
                quality_facts=_facts(),
            )
        ],
        successful_video_ids={"abcdefghijk"},
    )

    imported_count = MusicVaultWindow._import_legacy_youtube_items(
        _harness(download_root, db),
        result,
    )

    assert imported_count == 0
    assert db.upserts == []
    assert result.failed_count == 1
    assert result.successful_video_ids == set()


def test_legacy_sync_records_new_and_reused_quality_without_double_counting(
    tmp_path,
    monkeypatch,
) -> None:
    download_root = tmp_path / "downloads"
    download_root.mkdir()
    downloaded = download_root / "downloaded.opus"
    existing = download_root / "existing.mp3"
    downloaded.write_bytes(b"new-original")
    existing.write_bytes(b"existing-compatibility")
    db = _LegacySyncDB(
        {
            "abcdefghijk": (1, str(downloaded.resolve())),
            "bcdefghijkl": (2, str(existing.resolve())),
        }
    )
    db.quality_by_track[2] = _facts(profile="mp3_320_compatibility", size=99)
    observed_contexts = []

    def successful_import(_db, path, source):
        assert path == downloaded.resolve()
        observed_contexts.append(source)
        return True

    monkeypatch.setattr(app_module, "import_file", successful_import)
    new_facts = _facts(size=len(b"new-original"))
    result = SyncResult(
        "complete",
        "playlist",
        "Synthetic",
        import_items=[
            SyncImportItem(
                str(downloaded),
                "abcdefghijk",
                quality_facts=new_facts,
            )
        ],
        successful_video_ids={"abcdefghijk", "bcdefghijkl"},
    )
    harness = _harness(download_root, db)

    imported_count = MusicVaultWindow._import_legacy_youtube_items(harness, result)
    MusicVaultWindow._record_legacy_youtube_reused_quality_facts(harness, result)

    assert imported_count == 1
    assert observed_contexts[0].source_video_id == "abcdefghijk"
    assert db.upserts == [(1, new_facts)]
    assert result.source_preserved_count == 1
    assert result.mp3_compatibility_transcode_count == 0
    assert result.total_stored_bytes == len(b"new-original")
    assert result.reused_quality_profile_counts == {"mp3_320_compatibility": 1}
    assert result.reused_stored_codec_counts == {"mp3": 1}


def test_legacy_status_payload_includes_all_quality_aggregates_without_identity() -> None:
    result = SyncResult(
        "complete_with_issues",
        "private-playlist-id",
        "Private playlist title",
        source_preserved_count=1,
        source_preserved_remux_count=2,
        mp3_compatibility_transcode_count=3,
        quality_failure_count=4,
        total_stored_bytes=5,
    )

    payload = MusicVaultWindow._legacy_youtube_status_payload(
        result,
        sync_source_count=6,
        enabled_sync_source_count=5,
    )

    assert payload["last_sync_source_preserved_count"] == 1
    assert payload["last_sync_source_preserved_remux_count"] == 2
    assert payload["last_sync_mp3_compatibility_transcode_count"] == 3
    assert payload["last_sync_quality_failure_count"] == 4
    assert payload["last_sync_total_stored_bytes"] == 5
    assert payload["last_sync_playlist_title"] is None
    assert payload["last_sync_playlist_id"] is None
    assert payload["last_sync_failures"] == []
    assert "Private playlist title" not in str(payload)
    assert "private-playlist-id" not in str(payload)

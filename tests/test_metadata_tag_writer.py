from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
from mutagen.id3 import APIC, GEOB, ID3, TALB, TDOR, TDRC, TIT2, TPE1, TPE2, TXXX
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage

from music_vault.metadata import tag_writer as tag_writer_module
from music_vault.metadata.artwork import prepare_artwork_bytes
from music_vault.metadata.tag_writer import (
    SafeTagWriter,
    TagWriteError,
    full_file_sha256,
    inspect_mp3,
)


_SYNTHETIC_MP3_BASE64 = (
    "//sQxAADwAABpAAAACAAADSAAAAETEFNRTMuMTAwVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFX/+xLE"
    "KYPAAAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVUxBTUUzLjEwMFX/+xDEU4PA"
    "AAGkAAAAIAAANIAAAARVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVf/7EsR9A8AAAaQA"
    "AAAgAAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVTEFNRTMuMTAwVf/7EMSnA8AAAaQAAAAg"
    "AAA0gAAABFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVMQU1FMy4xMDBV//sSxNCDwAABpAAAACAAADSA"
    "AAAEVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV//sQxNYDwAABpAAAACAAADSAAAAE"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/+xLE1YPAAAGkAAAAIAAANIAAAARVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVX/+xDE1gPAAAGkAAAAIAAANIAAAARVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVf/7EsTVg8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVf/7EMTWA8AAAaQAAAAgAAA0gAAABFVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV"
    "VVVVVVVVVVVVVVVVVVVV"
)

_BASELINE = {
    "TIT2": "Original Title",
    "TPE1": "Original Artist",
    "TALB": "Original Album",
    "TPE2": "Original Album Artist",
    "TDRC": "1999",
}


@pytest.fixture
def synthetic_mp3(tmp_path: Path) -> Path:
    path = tmp_path / "synthetic.mp3"
    path.write_bytes(base64.b64decode(_SYNTHETIC_MP3_BASE64))
    tags = ID3()
    tags.add(TIT2(encoding=3, text=[_BASELINE["TIT2"]]))
    tags.add(TPE1(encoding=3, text=[_BASELINE["TPE1"]]))
    tags.add(TALB(encoding=3, text=[_BASELINE["TALB"]]))
    tags.add(TPE2(encoding=3, text=[_BASELINE["TPE2"]]))
    tags.add(TDRC(encoding=3, text=[_BASELINE["TDRC"]]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Track Id", text=["old-recording-id"]))
    tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=["old-release-id"]))
    tags.save(path, v2_version=3)
    assert inspect_mp3(path).duration_seconds > 0
    return path


def _image_bytes() -> bytes:
    image = QImage(24, 24, QImage.Format.Format_ARGB32)
    image.fill(0xFF5A4FCF)
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def _text(tags: ID3, frame_id: str) -> str | None:
    frames = tags.getall(frame_id)
    if not frames or not frames[0].text:
        return None
    return str(frames[0].text[0])


def _txxx(tags: ID3, description: str) -> str | None:
    for frame in tags.getall("TXXX"):
        if str(frame.desc).casefold() == description.casefold() and frame.text:
            return str(frame.text[0])
    return None


def _backup(writer: SafeTagWriter, path: Path, backup_dir: Path):
    return writer.create_backup(path, backup_dir, identity="job-1-item-1")


def test_backup_is_exact_safely_named_retained_and_stale_hash_protected(
    synthetic_mp3: Path,
    tmp_path: Path,
):
    writer = SafeTagWriter()
    original = synthetic_mp3.read_bytes()
    backup = writer.create_backup(
        synthetic_mp3,
        tmp_path / "backups",
        identity="../../Private Track: 1",
    )

    assert backup.backup_path.parent == (tmp_path / "backups").resolve()
    assert re.fullmatch(r"[0-9a-f]{24}-[0-9a-f]{16}\.mp3", backup.backup_path.name)
    assert "Private" not in backup.backup_path.name
    assert backup.backup_path.read_bytes() == original
    assert backup.fingerprint.full_sha256 == full_file_sha256(synthetic_mp3)
    modified_at = backup.backup_path.stat().st_mtime_ns

    repeated = writer.create_backup(
        synthetic_mp3,
        tmp_path / "backups",
        identity="../../Private Track: 1",
    )
    assert repeated.backup_path == backup.backup_path
    assert repeated.backup_path.stat().st_mtime_ns == modified_at
    assert list((tmp_path / "backups").glob("*.tmp")) == []

    with pytest.raises(TagWriteError, match="^media_changed_since_analysis$"):
        writer.create_backup(
            synthetic_mp3,
            tmp_path / "other-backups",
            identity="job-2-item-1",
            expected_full_sha256="0" * 64,
        )
    assert not (tmp_path / "other-backups").exists()


def test_mp3_writeback_round_trips_all_approved_fields_and_artwork(
    synthetic_mp3: Path,
    tmp_path: Path,
):
    writer = SafeTagWriter()
    original_bytes = synthetic_mp3.read_bytes()
    original = inspect_mp3(synthetic_mp3)
    backup = _backup(writer, synthetic_mp3, tmp_path / "backups")
    artwork_path = tmp_path / "cover.png"
    artwork_path.write_bytes(_image_bytes())
    artwork = prepare_artwork_bytes(artwork_path.read_bytes(), "image/png")
    patch = {
        "title": "Canonical Title",
        "artist": "Canonical Artist",
        "album": "Canonical Album",
        "album_artist": "Canonical Album Artist",
        "release_date": "2001-02-03",
        "musicbrainz_recording_id": "new-recording-id",
        "musicbrainz_release_id": "new-release-id",
        "source_upload_date": "2025-06-07",
    }

    prepared = writer.prepare(
        synthetic_mp3,
        patch,
        expected_full_sha256=original.full_sha256,
        artwork_path=artwork_path,
    )
    assert synthetic_mp3.read_bytes() == original_bytes
    assert "source_upload_date" not in prepared.expected_patch
    result = writer.commit(prepared, backup=backup)

    tags = ID3(synthetic_mp3)
    assert _text(tags, "TIT2") == "Canonical Title"
    assert _text(tags, "TPE1") == "Canonical Artist"
    assert _text(tags, "TALB") == "Canonical Album"
    assert _text(tags, "TPE2") == "Canonical Album Artist"
    assert _text(tags, "TDRC") == "2001-02-03"
    assert _txxx(tags, "MusicBrainz Track Id") == "new-recording-id"
    assert _txxx(tags, "MusicBrainz Album Id") == "new-release-id"
    assert _txxx(tags, "Source Upload Date") is None
    covers = tags.getall("APIC")
    assert len(covers) == 1
    assert covers[0].mime == "image/png"
    assert prepare_artwork_bytes(bytes(covers[0].data), covers[0].mime).sha256 == artwork.sha256
    assert result.updated.full_sha256 != original.full_sha256
    assert result.updated.audio_payload_sha256 == original.audio_payload_sha256
    assert result.updated.codec == original.codec
    assert result.updated.duration_seconds == pytest.approx(original.duration_seconds, abs=0.05)
    assert backup.backup_path.read_bytes() == original_bytes


def test_artwork_writeback_replaces_only_front_cover_and_preserves_other_apic_frames(
    synthetic_mp3: Path,
    tmp_path: Path,
):
    old_front_data = b"old-front-cover"
    unrelated_data = b"unrelated-back-cover"
    tags = ID3(synthetic_mp3)
    tags.add(
        APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="Old front",
            data=old_front_data,
        )
    )
    tags.add(
        APIC(
            encoding=3,
            mime="image/png",
            type=4,
            desc="Back cover",
            data=unrelated_data,
        )
    )
    tags.save(synthetic_mp3, v2_version=3)

    writer = SafeTagWriter()
    backup = _backup(writer, synthetic_mp3, tmp_path / "backups")
    artwork_path = tmp_path / "replacement-cover.png"
    artwork_path.write_bytes(_image_bytes())
    replacement = prepare_artwork_bytes(artwork_path.read_bytes(), "image/png")

    prepared = writer.prepare(
        synthetic_mp3,
        {},
        artwork_path=artwork_path,
    )
    writer.commit(prepared, backup=backup)

    updated = ID3(synthetic_mp3).getall("APIC")
    front_covers = [frame for frame in updated if int(frame.type) == 3]
    unrelated = [frame for frame in updated if int(frame.type) == 4]
    assert len(front_covers) == 1
    assert front_covers[0].desc == "Cover"
    assert front_covers[0].mime == "image/png"
    assert old_front_data not in {bytes(frame.data) for frame in updated}
    assert prepare_artwork_bytes(
        bytes(front_covers[0].data),
        front_covers[0].mime,
    ).sha256 == replacement.sha256
    assert len(unrelated) == 1
    assert unrelated[0].desc == "Back cover"
    assert unrelated[0].mime == "image/png"
    assert bytes(unrelated[0].data) == unrelated_data


def test_v24_writeback_preserves_version_and_unrelated_text_and_binary_frames(
    synthetic_mp3: Path,
    tmp_path: Path,
):
    binary_payload = b"\x00\x01unrelated-binary-metadata\xff"
    tags = ID3(synthetic_mp3)
    tags.add(TDOR(encoding=3, text=["1987-04-05"]))
    tags.add(
        GEOB(
            encoding=3,
            mime="application/octet-stream",
            filename="metadata.bin",
            desc="Unrelated binary metadata",
            data=binary_payload,
        )
    )
    tags.save(synthetic_mp3, v2_version=4)
    assert ID3(synthetic_mp3).version[1] == 4

    writer = SafeTagWriter()
    backup = _backup(writer, synthetic_mp3, tmp_path / "backups")
    prepared = writer.prepare(synthetic_mp3, {"title": "Updated title"})
    writer.commit(prepared, backup=backup)

    updated = ID3(synthetic_mp3)
    assert updated.version[1] == 4
    assert _text(updated, "TIT2") == "Updated title"
    assert _text(updated, "TDOR") == "1987-04-05"
    binary_frames = updated.getall("GEOB")
    assert len(binary_frames) == 1
    assert binary_frames[0].mime == "application/octet-stream"
    assert binary_frames[0].filename == "metadata.bin"
    assert binary_frames[0].desc == "Unrelated binary metadata"
    assert bytes(binary_frames[0].data) == binary_payload


def test_empty_fields_and_source_upload_date_never_clear_or_replace_existing_tags(
    synthetic_mp3: Path,
    tmp_path: Path,
):
    writer = SafeTagWriter()
    backup = _backup(writer, synthetic_mp3, tmp_path / "backups")
    prepared = writer.prepare(
        synthetic_mp3,
        {
            "title": "",
            "artist": None,
            "album": "   ",
            "album_artist": None,
            "release_date": "",
            "musicbrainz_recording_id": "",
            "musicbrainz_release_id": None,
            "source_upload_date": "2025-06-07",
        },
    )
    assert prepared.expected_patch == {}
    writer.commit(prepared, backup=backup)

    tags = ID3(synthetic_mp3)
    for frame_id, expected in _BASELINE.items():
        assert _text(tags, frame_id) == expected
    assert _txxx(tags, "MusicBrainz Track Id") == "old-recording-id"
    assert _txxx(tags, "MusicBrainz Album Id") == "old-release-id"
    assert _txxx(tags, "Source Upload Date") is None


def test_commit_revalidates_prepared_copy_before_replacing_original(
    synthetic_mp3: Path,
    tmp_path: Path,
):
    writer = SafeTagWriter()
    original = synthetic_mp3.read_bytes()
    backup = _backup(writer, synthetic_mp3, tmp_path / "backups")
    prepared = writer.prepare(synthetic_mp3, {"title": "Changed"})
    with prepared.temporary_path.open("ab") as stream:
        stream.write(b"tampered-after-prepare")

    with pytest.raises(TagWriteError, match="^prepared_file_changed$"):
        writer.commit(prepared, backup=backup)
    assert synthetic_mp3.read_bytes() == original
    assert backup.backup_path.read_bytes() == original
    assert not prepared.temporary_path.exists()


def test_post_replace_failure_restores_the_verified_original_backup(
    synthetic_mp3: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    writer = SafeTagWriter()
    original = synthetic_mp3.read_bytes()
    backup = _backup(writer, synthetic_mp3, tmp_path / "backups")
    prepared = writer.prepare(synthetic_mp3, {"title": "Changed"})
    real_replace = tag_writer_module.os.replace
    corrupted = False

    def replace_then_corrupt(source, destination):
        nonlocal corrupted
        real_replace(source, destination)
        if (
            not corrupted
            and Path(source) == prepared.temporary_path
            and Path(destination) == synthetic_mp3.resolve()
        ):
            corrupted = True
            with synthetic_mp3.open("ab") as stream:
                stream.write(b"simulated-post-replace-corruption")

    monkeypatch.setattr(tag_writer_module.os, "replace", replace_then_corrupt)
    with pytest.raises(TagWriteError, match="^media_post_replace_verification_failed$"):
        writer.commit(prepared, backup=backup)

    assert corrupted is True
    assert synthetic_mp3.read_bytes() == original
    assert backup.backup_path.read_bytes() == original


def test_restore_rejects_stale_backup_hash_then_restores_exact_original(
    synthetic_mp3: Path,
    tmp_path: Path,
):
    writer = SafeTagWriter()
    original = synthetic_mp3.read_bytes()
    original_hash = full_file_sha256(synthetic_mp3)
    backup = _backup(writer, synthetic_mp3, tmp_path / "backups")
    prepared = writer.prepare(synthetic_mp3, {"title": "Changed"})
    writer.commit(prepared, backup=backup)
    changed = synthetic_mp3.read_bytes()
    changed_hash = full_file_sha256(synthetic_mp3)
    assert changed != original

    with pytest.raises(TagWriteError, match="^backup_verification_failed$"):
        writer.restore(
            synthetic_mp3,
            backup.backup_path,
            expected_backup_sha256="0" * 64,
        )
    assert synthetic_mp3.read_bytes() == changed

    with synthetic_mp3.open("ab") as stream:
        stream.write(b"independent-change")
    independently_changed = synthetic_mp3.read_bytes()
    with pytest.raises(TagWriteError, match="^restore_destination_changed$"):
        writer.restore(
            synthetic_mp3,
            backup.backup_path,
            expected_backup_sha256=original_hash,
            expected_current_sha256=changed_hash,
        )
    assert synthetic_mp3.read_bytes() == independently_changed

    synthetic_mp3.write_bytes(changed)

    restored = writer.restore(
        synthetic_mp3,
        backup.backup_path,
        expected_backup_sha256=original_hash,
    )
    assert restored.full_sha256 == original_hash
    assert synthetic_mp3.read_bytes() == original
    assert backup.backup_path.read_bytes() == original


def test_unsupported_formats_and_stale_source_are_non_destructive(
    synthetic_mp3: Path,
    tmp_path: Path,
):
    writer = SafeTagWriter()
    unsupported = tmp_path / "synthetic.wav"
    unsupported.write_bytes(synthetic_mp3.read_bytes())
    before = unsupported.read_bytes()

    assert writer.supports(unsupported) is False
    with pytest.raises(TagWriteError, match="^media_format_unsupported$"):
        writer.create_backup(unsupported, tmp_path / "backups", identity="item")
    with pytest.raises(TagWriteError, match="^media_format_unsupported$"):
        writer.prepare(unsupported, {"title": "Changed"})
    with pytest.raises(TagWriteError, match="^media_format_unsupported$"):
        writer.restore(unsupported, unsupported, expected_backup_sha256="0" * 64)
    assert unsupported.read_bytes() == before

    original = synthetic_mp3.read_bytes()
    with pytest.raises(TagWriteError, match="^media_changed_since_analysis$"):
        writer.prepare(
            synthetic_mp3,
            {"title": "Changed"},
            expected_full_sha256="0" * 64,
        )
    assert synthetic_mp3.read_bytes() == original
    assert list(tmp_path.glob("*.tmp.mp3")) == []

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from music_vault.core.audio_inspection import (
    AudioInspection,
    DeterministicFinalPathTracker,
    FinalPathEvidenceError,
    inspect_audio_file,
    verify_final_audio,
)
from music_vault.core.audio_quality import (
    BEST_ORIGINAL_PROFILE,
    MP3_320_COMPATIBILITY_PROFILE,
    choose_output_extension,
    compare_source_and_stored,
    normalize_codec,
    profile_description,
)
from music_vault.core.youtube_audio_options import (
    SourceAudioFormat,
    SourceFormatSelectionError,
    build_audio_download_plan,
    build_yt_dlp_audio_options,
    select_source_audio_format,
    source_format_eligibility,
)


VIDEO_ID = "AbCdEf12345"


def _format(
    format_id: str,
    *,
    codec: str = "opus",
    extension: str = "webm",
    video_codec: str = "none",
    bitrate: object = 160,
    **extra,
) -> dict:
    return {
        "format_id": format_id,
        "acodec": codec,
        "vcodec": video_codec,
        "ext": extension,
        "abr": bitrate,
        "asr": 48000,
        "audio_channels": 2,
        "duration": 181.5,
        **extra,
    }


def test_codec_normalization_and_deterministic_output_extensions():
    assert normalize_codec("libopus") == "opus"
    assert normalize_codec("mp4a.40.2") == "aac"
    assert normalize_codec("AAC_LATM") == "aac"
    assert normalize_codec("mp3float") == "mp3"
    assert normalize_codec("made-up-codec") is None
    assert choose_output_extension("opus") == ".opus"
    assert choose_output_extension("mp4a.40.2") == ".m4a"
    assert choose_output_extension("vorbis") == ".ogg"
    with pytest.raises(ValueError):
        choose_output_extension("made-up-codec")


def test_selection_prefers_provider_ranked_audio_only_stream_without_fixed_ids():
    formats = [
        _format("provider-low-rank", codec="opus"),
        _format("provider-selected-at-runtime", codec="mp4a.40.2", extension="m4a"),
        _format(
            "higher-ranked-muxed",
            codec="mp4a.40.2",
            extension="mp4",
            video_codec="avc1.640028",
        ),
    ]
    selected = select_source_audio_format(formats)
    assert selected.format_id == "provider-selected-at-runtime"
    assert selected.audio_only is True
    assert selected.codec == "aac"


def test_selection_rejects_drm_video_only_unknown_codec_and_impractical_bitrate():
    rejected = [
        _format("drm", has_drm=True),
        _format("video-only", codec="none", video_codec="avc1"),
        _format("unknown", codec="ac3"),
        _format("oversized", bitrate=900),
    ]
    for index, value in enumerate(rejected):
        parsed = SourceAudioFormat.from_mapping(value, provider_order=index)
        assert source_format_eligibility(parsed).eligible is False
    with pytest.raises(SourceFormatSelectionError):
        select_source_audio_format(rejected)


@pytest.mark.parametrize(
    "format_id",
    ("251/140", "137+140", "best[acodec=opus]", "audio choice"),
)
def test_format_id_must_be_one_atomic_provider_identifier(format_id):
    with pytest.raises(SourceFormatSelectionError):
        select_source_audio_format([_format(format_id)])


def test_unknown_bitrate_is_eligible_and_supported_lossless_can_justify_ceiling():
    unknown = SourceAudioFormat.from_mapping(_format("unknown-rate", bitrate=None))
    assert source_format_eligibility(unknown).eligible is True

    flac = SourceAudioFormat.from_mapping(
        _format("lossless-source", codec="flac", extension="flac", bitrate=900)
    )
    assert flac.high_bitrate_justified is True
    assert source_format_eligibility(flac).eligible is True


def test_best_original_plan_preserves_codec_and_has_no_mp3_encoder_or_bitrate():
    plan = build_audio_download_plan(
        [_format("dynamic-opus-format")], BEST_ORIGINAL_PROFILE
    )
    options = build_yt_dlp_audio_options(
        plan,
        embed_thumbnail=False,
        retain_thumbnail=True,
    )
    extract = options["postprocessors"][0]
    assert options["format"] == "dynamic-opus-format"
    assert "/" not in options["format"]
    assert extract == {"key": "FFmpegExtractAudio", "preferredcodec": "best"}
    assert "preferredquality" not in extract
    assert options["writethumbnail"] is True
    assert options["embedthumbnail"] is False
    assert not any(
        processor["key"] == "EmbedThumbnail"
        for processor in options["postprocessors"]
    )
    assert plan.output_extension == ".opus"
    assert plan.expected_final_codec == "opus"
    assert plan.transformation_kind == "source_preserved_remux"
    assert not any("cookie" in key.casefold() for key in options)


def test_aac_source_can_remain_aac_in_m4a_without_transcode():
    plan = build_audio_download_plan(
        [_format("dynamic-aac-format", codec="mp4a.40.2", extension="m4a")],
        BEST_ORIGINAL_PROFILE,
    )
    assert plan.source.codec == "aac"
    assert plan.expected_final_codec == "aac"
    assert plan.output_extension == ".m4a"
    assert plan.transformation_kind == "none"


def test_muxed_fallback_is_used_only_without_eligible_audio_only_source():
    plan = build_audio_download_plan(
        [
            _format(
                "dynamic-muxed-aac",
                codec="mp4a.40.2",
                extension="mp4",
                video_codec="avc1",
            )
        ],
        BEST_ORIGINAL_PROFILE,
    )
    assert plan.source.has_video is True
    assert plan.expected_final_codec == "aac"
    assert plan.output_extension == ".m4a"
    assert plan.transformation_kind == "source_preserved_remux"


def test_mp3_compatibility_plan_is_explicitly_lossy_and_locked_to_320():
    plan = build_audio_download_plan(
        [_format("dynamic-source")],
        MP3_320_COMPATIBILITY_PROFILE,
        compatibility_mp3_bitrate_kbps=192,
    )
    options = build_yt_dlp_audio_options(plan, embed_thumbnail=False)
    assert options["postprocessors"][0] == {
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "320",
    }
    assert plan.output_extension == ".mp3"
    assert plan.expected_final_codec == "mp3"
    assert plan.transformation_kind == "lossy_transcode"
    assert plan.compatibility_mp3_bitrate_kbps == 320
    assert options["writethumbnail"] is False


def test_mp3_compatibility_never_false_labels_an_mp3_stream_copy():
    plan = build_audio_download_plan(
        [
            _format("lower-opus", codec="opus", extension="webm"),
            _format("higher-mp3", codec="mp3", extension="mp3"),
        ],
        MP3_320_COMPATIBILITY_PROFILE,
    )
    assert plan.source.format_id == "lower-opus"
    assert plan.source.codec == "opus"
    assert plan.transformation_kind == "lossy_transcode"

    with pytest.raises(SourceFormatSelectionError, match="non-MP3 source"):
        build_audio_download_plan(
            [_format("only-mp3", codec="mp3", extension="mp3")],
            MP3_320_COMPATIBILITY_PROFILE,
        )


def test_best_original_still_preserves_a_native_mp3_source():
    plan = build_audio_download_plan(
        [_format("native-mp3", codec="mp3", extension="mp3")],
        BEST_ORIGINAL_PROFILE,
    )
    assert plan.source.codec == "mp3"
    assert plan.output_extension == ".mp3"
    assert plan.transformation_kind == "none"


def test_quality_wording_never_claims_lossless_or_a_fidelity_upgrade():
    best_wording = profile_description(BEST_ORIGINAL_PROFILE)
    assert "does not make YouTube audio lossless" in best_wording
    compatibility_wording = profile_description(MP3_320_COMPATIBILITY_PROFILE)
    assert "cannot improve source fidelity" in compatibility_wording
    comparison = compare_source_and_stored(
        profile=MP3_320_COMPATIBILITY_PROFILE,
        source_codec="opus",
        stored_codec="mp3",
        source_bitrate_kbps=160,
        stored_bitrate_kbps=320,
    )
    assert comparison.transformation_kind == "lossy_transcode"
    assert comparison.transformation_text == (
        "Lossy compatibility transcode; not a fidelity upgrade"
    )
    unknown = compare_source_and_stored(
        profile=BEST_ORIGINAL_PROFILE,
        source_codec=None,
        stored_codec=None,
        source_bitrate_kbps=0,
        stored_bitrate_kbps=0,
    )
    assert unknown.codec_preserved is None
    assert unknown.source_bitrate_kbps is None
    assert unknown.stored_bitrate_kbps is None
    assert unknown.transformation_text == "Source and stored codec comparison unavailable"


def test_final_path_tracker_uses_hook_evidence_and_does_not_scan_directory(tmp_path):
    destination = tmp_path / "source"
    destination.mkdir()
    expected = destination / f"Expected [{VIDEO_ID}].opus"
    unexpected = destination / "unreported [OtherId1234].opus"
    expected.write_bytes(b"audio")
    unexpected.write_bytes(b"audio")

    tracker = DeterministicFinalPathTracker(destination, VIDEO_ID)
    tracker.postprocessor_hook(
        {"status": "finished", "info_dict": {"filepath": str(expected)}}
    )
    assert tracker.resolve_final_path(expected_extension=".opus") == expected.resolve()


def test_final_path_tracker_fails_closed_for_multiple_outside_or_wrong_evidence(tmp_path):
    destination = tmp_path / "source"
    destination.mkdir()
    first = destination / f"First [{VIDEO_ID}].opus"
    second = destination / f"Second [{VIDEO_ID}].opus"
    first.write_bytes(b"a")
    second.write_bytes(b"b")
    tracker = DeterministicFinalPathTracker(destination, VIDEO_ID)
    tracker.record_path(first)
    tracker.record_path(second)
    with pytest.raises(FinalPathEvidenceError) as ambiguous:
        tracker.resolve_final_path(expected_extension="opus")
    assert ambiguous.value.code == "ambiguous_final_path"

    outside = tmp_path / f"Outside [{VIDEO_ID}].opus"
    outside.write_bytes(b"x")
    tracker = DeterministicFinalPathTracker(destination, VIDEO_ID)
    tracker.record_path(outside)
    with pytest.raises(FinalPathEvidenceError) as escaped:
        tracker.resolve_final_path()
    assert escaped.value.code == "final_path_outside_destination"

    wrong = destination / "Wrong [OtherId1234].opus"
    wrong.write_bytes(b"x")
    tracker = DeterministicFinalPathTracker(destination, VIDEO_ID)
    tracker.record_path(wrong)
    with pytest.raises(FinalPathEvidenceError) as mismatch:
        tracker.resolve_final_path()
    assert mismatch.value.code == "source_identity_mismatch"


def test_ffprobe_inspection_is_bounded_shell_free_and_collects_stream_facts(tmp_path):
    media = tmp_path / f"Track [{VIDEO_ID}].opus"
    media.write_bytes(b"audio-bytes")
    ffprobe = tmp_path / "ffprobe.exe"
    ffprobe.write_bytes(b"probe")
    calls: list[tuple[list[str], dict]] = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "format": {
                        "format_name": "ogg",
                        "duration": "180.25",
                        "bit_rate": "162000",
                    },
                    "streams": [
                        {
                            "codec_type": "audio",
                            "codec_name": "opus",
                            "bit_rate": "160000",
                            "sample_rate": "48000",
                            "channels": 2,
                        },
                        {
                            "codec_type": "video",
                            "codec_name": "mjpeg",
                            "disposition": {"attached_pic": 1},
                        },
                    ],
                }
            ),
            stderr="",
        )

    inspection = inspect_audio_file(
        media,
        ffprobe_path=ffprobe,
        timeout=999,
        runner=runner,
    )
    command, kwargs = calls[0]
    assert isinstance(command, list)
    assert "stream_disposition=attached_pic" in command[command.index("-show_entries") + 1]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 10.0
    assert inspection.codec == "opus"
    assert inspection.container == "ogg"
    assert inspection.bitrate_kbps == 160
    assert inspection.sample_rate_hz == 48000
    assert inspection.channels == 2
    assert inspection.audio_stream_count == 1
    assert inspection.video_stream_count == 0
    assert inspection.duration_seconds == 180.25

    verification = verify_final_audio(
        inspection,
        expected_codec="libopus",
        expected_duration_seconds=181.0,
    )
    assert verification.ok is True
    assert verification.failures == ()


def test_final_verification_rejects_codec_change_video_and_duration_mismatch(tmp_path):
    media = tmp_path / f"Track [{VIDEO_ID}].m4a"
    media.write_bytes(b"audio")
    inspection = AudioInspection(
        path=media,
        extension=".m4a",
        container="m4a",
        codec="aac",
        bitrate_kbps=160,
        sample_rate_hz=48000,
        channels=2,
        duration_seconds=150.0,
        filesize_bytes=5,
        audio_stream_count=1,
        video_stream_count=1,
        inspection_method="ffprobe",
    )
    result = verify_final_audio(
        inspection,
        expected_codec="opus",
        expected_duration_seconds=180.0,
    )
    assert result.ok is False
    assert "final_codec_mismatch" in result.failures
    assert "final_video_stream_present_or_unverified" in result.failures
    assert "final_duration_out_of_tolerance" in result.failures


def test_mutagen_fallback_is_sufficient_for_native_opus_but_not_video_check_in_m4a(
    tmp_path,
):
    class Info:
        bitrate = 160000
        sample_rate = 48000
        channels = 2
        length = 120.0

    class FakeOpus:
        info = Info()
        mime = ["audio/ogg; codecs=opus"]

    opus = tmp_path / f"Track [{VIDEO_ID}].opus"
    opus.write_bytes(b"audio")
    inspected = inspect_audio_file(opus, mutagen_loader=lambda _path: FakeOpus())
    assert inspected.codec == "opus"
    assert inspected.video_stream_count == 0
    assert verify_final_audio(inspected, expected_codec="opus").ok is True

    m4a = tmp_path / f"Track [{VIDEO_ID}].m4a"
    m4a.write_bytes(b"audio")
    inspected_m4a = inspect_audio_file(m4a, mutagen_loader=lambda _path: FakeOpus())
    inspected_m4a = replace(inspected_m4a, codec="aac")
    result = verify_final_audio(inspected_m4a, expected_codec="aac")
    assert result.ok is False
    assert "final_video_stream_present_or_unverified" in result.failures

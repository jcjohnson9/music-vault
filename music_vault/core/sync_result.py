from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Literal

from .safety import sanitize_error_text


SyncStatus = Literal["complete", "complete_with_issues", "failed"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class SyncFailure:
    video_id: str | None
    title: str | None
    reason: str
    error_category: str
    source_item_id: str | None = None

    def __post_init__(self) -> None:
        self.reason = sanitize_error_text(self.reason)

    def to_dict(self) -> dict:
        return {
            "video_id": self.video_id,
            "title": self.title,
            "reason": self.reason,
            "error_category": self.error_category,
        }


@dataclass(frozen=True)
class PlaylistSnapshotItem:
    """One durable occurrence in a remote playlist snapshot."""

    source_item_id: str
    video_id: str | None
    source_position: int
    title: str | None = None
    availability_reason: str | None = None

    @property
    def available(self) -> bool:
        return bool(self.video_id) and not self.availability_reason


@dataclass(frozen=True)
class PlaylistSnapshot:
    """A complete playlist enumeration, or a failed non-authoritative attempt."""

    playlist_id: str | None
    playlist_title: str | None
    items: tuple[PlaylistSnapshotItem, ...] = ()
    complete: bool = True
    error: str | None = None

    @classmethod
    def completed(
        cls,
        playlist_id: str,
        playlist_title: str | None,
        items: Iterable[PlaylistSnapshotItem],
    ) -> "PlaylistSnapshot":
        return cls(playlist_id, playlist_title, tuple(items), True, None)

    @classmethod
    def failed(
        cls,
        error: object,
        *,
        playlist_id: str | None = None,
        playlist_title: str | None = None,
    ) -> "PlaylistSnapshot":
        return cls(
            playlist_id,
            playlist_title,
            (),
            False,
            sanitize_error_text(error),
        )

    @property
    def duplicate_occurrence_count(self) -> int:
        seen: set[str] = set()
        duplicates = 0
        for item in self.items:
            if not item.video_id:
                continue
            if item.video_id in seen:
                duplicates += 1
            else:
                seen.add(item.video_id)
        return duplicates


@dataclass(frozen=True)
class SyncImportItem:
    path: str
    video_id: str
    source_upload_date: str | None = None
    # Occurrence linkage is additive bookkeeping and deliberately excluded
    # from equality so the Batch 2 import-item contract remains compatible.
    source_item_ids: tuple[str, ...] = field(default_factory=tuple, compare=False)
    # Batch 11 quality provenance is optional so older provider/test factories
    # remain source-compatible. Values are aggregate-safe facts only; raw
    # provider responses and private query details never cross this boundary.
    quality_facts: Mapping[str, object] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    # A newly downloaded, local thumbnail may be carried to the importer for
    # content-addressed private cover storage. It is never exported to status.
    private_cover_path: str | None = field(
        default=None,
        compare=False,
        repr=False,
    )


@dataclass
class SyncResult:
    status: SyncStatus
    playlist_id: str | None
    playlist_title: str | None
    visible_item_count: int = 0
    new_item_count: int = 0
    downloaded_count: int = 0
    imported_count: int = 0
    existing_count: int = 0
    failures: list[SyncFailure] = field(default_factory=list)
    downloaded_paths: list[str] = field(default_factory=list)
    import_items: list[SyncImportItem] = field(default_factory=list)
    successful_video_ids: set[str] = field(default_factory=set)
    started_at: str = field(default_factory=utc_now)
    finished_at: str = field(default_factory=utc_now)
    saved_source_id: int | None = None
    source_label: str | None = None
    snapshot: PlaylistSnapshot | None = None
    removed_occurrence_count: int = 0
    duplicate_occurrence_count: int = 0
    source_preserved_count: int = 0
    source_preserved_remux_count: int = 0
    mp3_compatibility_transcode_count: int = 0
    quality_failure_count: int = 0
    total_stored_bytes: int = 0
    reused_quality_profile_counts: dict[str, int] = field(default_factory=dict)
    reused_stored_codec_counts: dict[str, int] = field(default_factory=dict)

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    def refresh_status(self) -> None:
        if self.status != "failed":
            self.status = "complete_with_issues" if self.failures else "complete"

    def add_failure(self, failure: SyncFailure) -> None:
        self.failures.append(failure)
        if failure.error_category == "quality":
            self.quality_failure_count += 1
        if failure.video_id:
            self.successful_video_ids.discard(failure.video_id)
        self.refresh_status()

    def record_quality_facts(self, values: Mapping[str, object] | None) -> None:
        if not isinstance(values, Mapping):
            return
        profile = str(values.get("acquisition_profile") or "").strip().casefold()
        transformation = str(values.get("transformation_kind") or "").strip().casefold()
        if transformation == "none":
            self.source_preserved_count += 1
        elif transformation == "source_preserved_remux":
            self.source_preserved_remux_count += 1
        if profile == "mp3_320_compatibility" and transformation == "lossy_transcode":
            self.mp3_compatibility_transcode_count += 1
        try:
            stored_bytes = int(values.get("stored_filesize_bytes") or 0)
        except (TypeError, ValueError, OverflowError):
            stored_bytes = 0
        self.total_stored_bytes += max(0, stored_bytes)

    def record_reused_quality_facts(
        self,
        values: Mapping[str, object] | None,
    ) -> None:
        """Report an existing representation without counting a new acquisition."""

        if not isinstance(values, Mapping):
            return
        profile = str(values.get("acquisition_profile") or "unknown").strip().casefold()
        if profile not in {
            "best_original",
            "mp3_320_compatibility",
            "legacy_youtube_mp3",
            "local_import",
            "unknown",
        }:
            profile = "unknown"
        self.reused_quality_profile_counts[profile] = (
            self.reused_quality_profile_counts.get(profile, 0) + 1
        )
        codec = str(values.get("stored_codec") or "unknown").strip().casefold()
        if codec not in {"opus", "aac", "vorbis", "mp3", "flac", "alac"}:
            codec = "unknown"
        self.reused_stored_codec_counts[codec] = (
            self.reused_stored_codec_counts.get(codec, 0) + 1
        )

    def finish_imports(self, imported_count: int) -> None:
        self.imported_count = imported_count
        self.finished_at = utc_now()
        self.refresh_status()

    @classmethod
    def failed_result(
        cls,
        reason: object,
        *,
        playlist_id: str | None = None,
        playlist_title: str | None = None,
        started_at: str | None = None,
        saved_source_id: int | None = None,
        source_label: str | None = None,
        snapshot: PlaylistSnapshot | None = None,
    ) -> "SyncResult":
        failure = SyncFailure(
            video_id=None,
            title=None,
            reason=sanitize_error_text(reason),
            error_category="sync",
        )
        return cls(
            status="failed",
            playlist_id=playlist_id,
            playlist_title=playlist_title,
            failures=[failure],
            started_at=started_at or utc_now(),
            finished_at=utc_now(),
            saved_source_id=saved_source_id,
            source_label=source_label,
            snapshot=snapshot,
        )

    def to_status_dict(self) -> dict:
        first_error = self.failures[0].reason if self.failures else None
        return {
            "last_sync_at": self.finished_at,
            "last_sync_status": self.status,
            "last_sync_playlist_title": self.playlist_title,
            "last_sync_new_items": self.new_item_count,
            "last_sync_imported_count": self.imported_count,
            "last_sync_error": first_error,
            "last_sync_playlist_id": self.playlist_id,
            "last_sync_visible_item_count": self.visible_item_count,
            "last_sync_downloaded_count": self.downloaded_count,
            "last_sync_existing_count": self.existing_count,
            "last_sync_failed_count": self.failed_count,
            "last_sync_source_preserved_count": self.source_preserved_count,
            "last_sync_source_preserved_remux_count": self.source_preserved_remux_count,
            "last_sync_mp3_compatibility_transcode_count": (
                self.mp3_compatibility_transcode_count
            ),
            "last_sync_quality_failure_count": self.quality_failure_count,
            "last_sync_total_stored_bytes": self.total_stored_bytes,
            "last_sync_failures": [failure.to_dict() for failure in self.failures[:25]],
        }


def sync_ui_values(result: SyncResult) -> dict[str, str]:
    labels = {
        "complete": "Complete",
        "complete_with_issues": "Complete with issues",
        "failed": "Failed",
    }
    return {
        "status": labels[result.status],
        "downloaded": str(result.downloaded_count),
        "existing": str(result.existing_count),
        "failed": str(result.failed_count),
    }


@dataclass
class MultiSourceSyncResult:
    """Truthful aggregate for one sequential multi-source batch."""

    status: SyncStatus
    source_outcomes: list[SyncResult] = field(default_factory=list)
    selected_source_count: int = 0
    completed_source_count: int = 0
    issue_source_count: int = 0
    failed_source_count: int = 0
    total_visible: int = 0
    total_new: int = 0
    total_downloaded: int = 0
    total_imported: int = 0
    total_existing: int = 0
    total_failed_items: int = 0
    total_removed_occurrences: int = 0
    total_duplicate_occurrences: int = 0
    total_source_preserved: int = 0
    total_source_preserved_remux: int = 0
    total_mp3_compatibility_transcodes: int = 0
    total_quality_failures: int = 0
    total_stored_bytes: int = 0
    reused_quality_profile_counts: dict[str, int] = field(default_factory=dict)
    reused_stored_codec_counts: dict[str, int] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now)
    finished_at: str = field(default_factory=utc_now)
    batch_token: str | None = None
    stopped_after_current: bool = False

    @classmethod
    def from_outcomes(
        cls,
        outcomes: Iterable[SyncResult],
        *,
        selected_source_count: int,
        started_at: str,
        batch_token: str | None = None,
        stopped_after_current: bool = False,
    ) -> "MultiSourceSyncResult":
        materialized = list(outcomes)
        reused_quality_profile_counts: dict[str, int] = {}
        for result in materialized:
            for profile, count in result.reused_quality_profile_counts.items():
                reused_quality_profile_counts[profile] = (
                    reused_quality_profile_counts.get(profile, 0) + max(0, int(count))
                )
        reused_stored_codec_counts: dict[str, int] = {}
        for result in materialized:
            for codec, count in result.reused_stored_codec_counts.items():
                reused_stored_codec_counts[codec] = (
                    reused_stored_codec_counts.get(codec, 0) + max(0, int(count))
                )
        completed = sum(result.status == "complete" for result in materialized)
        issues = sum(result.status == "complete_with_issues" for result in materialized)
        failed = sum(result.status == "failed" for result in materialized)
        useful = completed + issues
        if useful == 0:
            status: SyncStatus = "failed"
        elif failed or issues or stopped_after_current or len(materialized) < selected_source_count:
            status = "complete_with_issues"
        else:
            status = "complete"
        return cls(
            status=status,
            source_outcomes=materialized,
            selected_source_count=selected_source_count,
            completed_source_count=completed,
            issue_source_count=issues,
            failed_source_count=failed,
            total_visible=sum(result.visible_item_count for result in materialized),
            total_new=sum(result.new_item_count for result in materialized),
            total_downloaded=sum(result.downloaded_count for result in materialized),
            total_imported=sum(result.imported_count for result in materialized),
            total_existing=sum(result.existing_count for result in materialized),
            total_failed_items=sum(result.failed_count for result in materialized),
            total_removed_occurrences=sum(
                result.removed_occurrence_count for result in materialized
            ),
            total_duplicate_occurrences=sum(
                result.duplicate_occurrence_count for result in materialized
            ),
            total_source_preserved=sum(
                result.source_preserved_count for result in materialized
            ),
            total_source_preserved_remux=sum(
                result.source_preserved_remux_count for result in materialized
            ),
            total_mp3_compatibility_transcodes=sum(
                result.mp3_compatibility_transcode_count for result in materialized
            ),
            total_quality_failures=sum(
                result.quality_failure_count for result in materialized
            ),
            total_stored_bytes=sum(result.total_stored_bytes for result in materialized),
            reused_quality_profile_counts=reused_quality_profile_counts,
            reused_stored_codec_counts=reused_stored_codec_counts,
            started_at=started_at,
            finished_at=utc_now(),
            batch_token=batch_token,
            stopped_after_current=stopped_after_current,
        )

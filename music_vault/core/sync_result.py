from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

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
class SyncImportItem:
    path: str
    video_id: str
    source_upload_date: str | None = None


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

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    def refresh_status(self) -> None:
        if self.status != "failed":
            self.status = "complete_with_issues" if self.failures else "complete"

    def add_failure(self, failure: SyncFailure) -> None:
        self.failures.append(failure)
        if failure.video_id:
            self.successful_video_ids.discard(failure.video_id)
        self.refresh_status()

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

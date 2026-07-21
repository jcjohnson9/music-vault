from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable

from .audio_quality_config import (
    DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS,
    DEFAULT_DOWNLOAD_QUALITY_PROFILE,
    INHERIT_PROFILE,
    normalize_compatibility_mp3_bitrate_kbps,
    normalize_download_quality_profile,
    normalize_source_download_quality_profile,
)
from .ffmpeg import discover_ffmpeg
from .importer import ImportSourceContext, import_file
from .paths import youtube_download_archive_path
from .safety import sanitize_error_text
from .sync_result import (
    MultiSourceSyncResult,
    PlaylistSnapshot,
    SyncFailure,
    SyncImportItem,
    SyncResult,
    utc_now,
)
from .sync_sources import SyncSource, SyncSourceError, SyncSourceService
from .youtube_sync import (
    AuthorizedYouTubePlaylistSyncer,
    YouTubeSyncConfig,
    scan_existing_downloads,
)


ProgressCallback = Callable[["SyncProgressEvent"], None]
ImporterCallback = Callable[[object, SyncImportItem], int | None]
SyncerFactory = Callable[[YouTubeSyncConfig, Callable[[str], None]], object]


class SyncBatchActiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class SyncProgressEvent:
    phase: str
    source_index: int
    source_count: int
    source_id: int | None = None
    source_label: str | None = None
    message: str | None = None
    result: SyncResult | None = None


@dataclass(frozen=True)
class _AcquisitionEvidence:
    path: Path
    quality_facts: Mapping[str, object]
    private_cover_path: str | None = None


def _default_syncer_factory(
    config: YouTubeSyncConfig,
    progress: Callable[[str], None],
):
    return AuthorizedYouTubePlaylistSyncer(config, progress=progress)


class MultiSourceSyncOrchestrator:
    """Run saved sources sequentially and reconcile each before continuing."""

    _process_batch_lock = threading.Lock()

    def __init__(
        self,
        db,
        download_root: str | Path,
        *,
        archive_file: str | Path | None = None,
        audio_quality: str = "320",
        download_quality_profile: str = DEFAULT_DOWNLOAD_QUALITY_PROFILE,
        compatibility_mp3_bitrate_kbps: int = (
            DEFAULT_COMPATIBILITY_MP3_BITRATE_KBPS
        ),
        ffmpeg_location: str | Path | None = None,
        source_service: SyncSourceService | None = None,
        membership_service=None,
        syncer_factory: SyncerFactory | None = None,
        importer: ImporterCallback | None = None,
        progress: ProgressCallback | None = None,
        transition_callback: Callable[[dict], None] | None = None,
    ) -> None:
        self.db = db
        self.conn: sqlite3.Connection = db.conn
        self.download_root = Path(download_root).expanduser().resolve()
        self.archive_file = Path(archive_file or youtube_download_archive_path())
        # Retain the former setting for source compatibility while all new
        # acquisitions use the explicit, honest quality-profile contract.
        self.audio_quality = str(audio_quality or "320")
        self.download_quality_profile = normalize_download_quality_profile(
            download_quality_profile
        )
        self.compatibility_mp3_bitrate_kbps = (
            normalize_compatibility_mp3_bitrate_kbps(
                compatibility_mp3_bitrate_kbps
            )
        )
        self.ffmpeg_location = ffmpeg_location
        self._import_ffprobe_discovered = False
        self._import_ffprobe_path: Path | None = None
        self.membership_service = membership_service
        self.source_service = source_service or SyncSourceService(
            db, membership_service=membership_service
        )
        self.syncer_factory = syncer_factory or _default_syncer_factory
        self.importer = importer or self._default_importer
        self.progress = progress or (lambda _event: None)
        self.transition_callback = transition_callback or (lambda _values: None)
        self._stop_after_current = threading.Event()
        self._active = False
        self._active_source_id: int | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def active_source_id(self) -> int | None:
        return self._active_source_id

    def request_stop_after_current(self) -> bool:
        if not self._active:
            return False
        self._stop_after_current.set()
        return True

    def _emit(
        self,
        phase: str,
        index: int,
        count: int,
        source: SyncSource | None = None,
        *,
        message: str | None = None,
        result: SyncResult | None = None,
    ) -> None:
        try:
            self.progress(
                SyncProgressEvent(
                    phase=phase,
                    source_index=index,
                    source_count=count,
                    source_id=source.id if source else None,
                    source_label=source.display_label if source else None,
                    message=sanitize_error_text(message) if message else None,
                    result=result,
                )
            )
        except Exception:
            # Presentation callbacks must never break a synchronization batch.
            pass

    def _transition(self, values: dict) -> None:
        try:
            self.transition_callback(values)
        except Exception:
            pass

    def sync_all_enabled(self) -> MultiSourceSyncResult:
        sources = self.source_service.list_active(enabled_only=True)
        return self._run(sources)

    def sync_selected(self, source_ids: Iterable[int]) -> MultiSourceSyncResult:
        requested = [int(source_id) for source_id in source_ids]
        if not requested or len(requested) != len(set(requested)):
            raise SyncSourceError("Select at least one saved source exactly once.")
        sources = [self.source_service.get(source_id) for source_id in requested]
        if any(not source.enabled for source in sources):
            raise SyncSourceError("Disabled sources must be enabled before synchronization.")
        sources.sort(key=lambda source: (source.sort_order, source.id))
        return self._run(sources)

    def _run(self, sources: list[SyncSource]) -> MultiSourceSyncResult:
        if not sources:
            raise SyncSourceError("There are no enabled synchronization sources.")
        if not self._process_batch_lock.acquire(blocking=False):
            raise SyncBatchActiveError("Another synchronization batch is already active.")

        self._active = True
        self._stop_after_current.clear()
        started_at = utc_now()
        batch_token = str(uuid.uuid4())
        outcomes: list[SyncResult] = []
        stopped = False
        selected_count = len(sources)
        try:
            self.download_root.mkdir(parents=True, exist_ok=True)
            self._refresh_stale_canonical_identities()
            valid_database_ids = self._valid_database_video_ids()
            media_index = scan_existing_downloads(
                self.download_root,
                ffmpeg_location=self.ffmpeg_location,
                exclude_video_ids=valid_database_ids,
            )
            # Every provider receives the same read-only view. The mutable
            # backing dictionary is updated only by this sequential
            # orchestrator after a source completes, so later sources see new
            # files without a per-source serialization, copy, or stat pass.
            shared_download_index = MappingProxyType(media_index)
            acquisition_evidence: dict[str, _AcquisitionEvidence] = {}
            self._transition(
                {
                    "active_sync_batch": True,
                    "active_sync_source_index": 0,
                    "last_sync_batch_source_count": selected_count,
                }
            )
            self._emit("batch_started", 0, selected_count)
            for index, source in enumerate(sources, start=1):
                self._active_source_id = source.id
                self._transition(
                    {
                        "active_sync_batch": True,
                        "active_sync_source_index": index,
                    }
                )
                self._emit("source_started", index, selected_count, source)
                outcome = self._run_one_source(
                    source,
                    batch_token=batch_token,
                    source_index=index,
                    source_count=selected_count,
                    media_index=media_index,
                    shared_download_index=shared_download_index,
                    valid_database_ids=valid_database_ids,
                    acquisition_evidence=acquisition_evidence,
                )
                outcomes.append(outcome)
                self._emit(
                    "source_finished",
                    index,
                    selected_count,
                    source,
                    result=outcome,
                )
                if self._stop_after_current.is_set() and index < selected_count:
                    stopped = True
                    self._emit("stopped_after_current", index, selected_count, source)
                    break
        finally:
            self._active_source_id = None
            self._active = False
            self._process_batch_lock.release()

        aggregate = MultiSourceSyncResult.from_outcomes(
            outcomes,
            selected_source_count=selected_count,
            started_at=started_at,
            batch_token=batch_token,
            stopped_after_current=stopped,
        )
        self._transition(
            {
                "active_sync_batch": False,
                "active_sync_source_index": None,
                "last_sync_batch_status": aggregate.status,
                "last_sync_batch_source_count": aggregate.selected_source_count,
                "last_sync_batch_complete_count": aggregate.completed_source_count,
                "last_sync_batch_issue_count": aggregate.issue_source_count,
                "last_sync_batch_failed_count": aggregate.failed_source_count,
                "last_sync_batch_downloaded_count": aggregate.total_downloaded,
                "last_sync_batch_imported_count": aggregate.total_imported,
                "last_sync_batch_item_failure_count": aggregate.total_failed_items,
                "last_sync_source_preserved_count": (
                    aggregate.total_source_preserved
                ),
                "last_sync_source_preserved_remux_count": (
                    aggregate.total_source_preserved_remux
                ),
                "last_sync_mp3_compatibility_transcode_count": (
                    aggregate.total_mp3_compatibility_transcodes
                ),
                "last_sync_quality_failure_count": (
                    aggregate.total_quality_failures
                ),
                "last_sync_total_stored_bytes": aggregate.total_stored_bytes,
            }
        )
        self._emit("batch_finished", len(outcomes), selected_count, result=None)
        return aggregate

    def _run_one_source(
        self,
        source: SyncSource,
        *,
        batch_token: str,
        source_index: int,
        source_count: int,
        media_index: dict[str, Path],
        shared_download_index: Mapping[str, Path],
        valid_database_ids: set[str],
        acquisition_evidence: dict[str, _AcquisitionEvidence],
    ) -> SyncResult:
        source_destination = self.download_root / "sources" / source.storage_key
        source_profile = normalize_source_download_quality_profile(
            source.download_quality_profile
        )
        effective_profile = (
            self.download_quality_profile
            if source_profile == INHERIT_PROFILE
            else normalize_download_quality_profile(source_profile)
        )
        config = YouTubeSyncConfig(
            playlist_url=source.source_url,
            output_dir=self.download_root,
            archive_file=self.archive_file,
            audio_format="mp3",
            audio_quality=self.audio_quality,
            existing_video_ids=frozenset(valid_database_ids),
            ffmpeg_location=self.ffmpeg_location,
            source_destination_dir=source_destination,
            saved_source_id=source.id,
            source_label=source.display_label,
            # Keep the legacy explicit-index sentinel for older provider
            # factories; the authoritative batch data is the zero-copy view.
            known_downloads=(),
            shared_download_index=shared_download_index,
            download_quality_profile=effective_profile,
            compatibility_mp3_bitrate_kbps=(
                self.compatibility_mp3_bitrate_kbps
            ),
        )

        def report(message: str) -> None:
            self._emit(
                "source_progress",
                source_index,
                source_count,
                source,
                message=message,
            )

        try:
            syncer = self.syncer_factory(config, report)
            result = syncer.sync()
            if not isinstance(result, SyncResult):
                raise RuntimeError("The source provider returned an invalid result.")
        except Exception as exc:
            result = SyncResult.failed_result(
                exc,
                playlist_id=source.external_id,
                saved_source_id=source.id,
                source_label=source.display_label,
                snapshot=PlaylistSnapshot.failed(exc, playlist_id=source.external_id),
            )

        result.saved_source_id = source.id
        result.source_label = source.display_label
        if result.playlist_id and result.playlist_id != source.external_id:
            result = SyncResult.failed_result(
                "The provider returned a different playlist identity.",
                playlist_id=source.external_id,
                saved_source_id=source.id,
                source_label=source.display_label,
                snapshot=PlaylistSnapshot.failed(
                    "The provider returned a different playlist identity.",
                    playlist_id=source.external_id,
                ),
            )

        try:
            imported_count = self._import_source_items(
                result,
                media_index,
                acquisition_evidence,
            )
            self._record_reused_quality_facts(result)
            result.finish_imports(imported_count)
            self._extend_valid_database_video_ids(
                valid_database_ids, result.successful_video_ids
            )
            self._persist_source_outcome(source, batch_token, result)
        except Exception as exc:
            # A provider may have produced useful files before a local
            # reconciliation/persistence failure. Keep those truthful counts,
            # but make the run failed and the snapshot non-authoritative. The
            # source-outcome transaction has already rolled back any snapshot
            # removals, membership changes, failure-card changes, and run/card
            # state that belonged to the rejected complete snapshot.
            result.add_failure(
                SyncFailure(None, None, sanitize_error_text(exc), "sync")
            )
            result.status = "failed"
            result.finished_at = utc_now()
            result.removed_occurrence_count = 0
            result.snapshot = PlaylistSnapshot.failed(
                exc,
                playlist_id=source.external_id,
                playlist_title=result.playlist_title,
            )
            # Persist the truthful failed outcome in a fresh transaction. It
            # intentionally has no authoritative snapshot to reconcile.
            self._persist_source_outcome(source, batch_token, result)
        return result

    def _persist_source_outcome(
        self,
        source: SyncSource,
        batch_token: str,
        result: SyncResult,
    ) -> None:
        """Atomically publish one source's authoritative database outcome."""

        with self.conn:
            if result.snapshot is not None and result.snapshot.complete:
                result.removed_occurrence_count = self._reconcile_complete_snapshot(
                    source, result
                )
            self._record_item_failures(source, result)
            if result.status != "failed":
                self._resolve_source_failures(source, result)
            self.source_service.update_last_sync(source.id, result, commit=False)
            self._record_source_run(source.id, batch_token, result)

    def _resolve_import_ffprobe_path(self) -> Path | None:
        """Resolve the configured probe once for guarded WebM imports."""

        if self._import_ffprobe_discovered:
            return self._import_ffprobe_path
        self._import_ffprobe_discovered = True
        try:
            result = discover_ffmpeg(configured_location=self.ffmpeg_location)
        except (OSError, RuntimeError, ValueError):
            return None
        if result.ready and result.ffprobe_path is not None:
            self._import_ffprobe_path = Path(result.ffprobe_path).resolve()
        return self._import_ffprobe_path

    def _default_importer(self, db, item: SyncImportItem) -> int | None:
        ffprobe_path = (
            self._resolve_import_ffprobe_path()
            if Path(item.path).suffix.casefold() == ".webm"
            else None
        )
        imported = import_file(
            db,
            item.path,
            ImportSourceContext(
                source_kind="youtube",
                source_video_id=item.video_id,
                source_upload_date=item.source_upload_date,
                private_cover_path=item.private_cover_path,
                ffprobe_path=ffprobe_path,
            ),
        )
        if not imported:
            return None
        row = self.conn.execute(
            "SELECT id FROM tracks WHERE path=?",
            (str(Path(item.path).resolve()),),
        ).fetchone()
        return int(row[0]) if row else None

    def _import_source_items(
        self,
        result: SyncResult,
        media_index: dict[str, Path],
        acquisition_evidence: dict[str, _AcquisitionEvidence],
    ) -> int:
        imported_count = 0
        for item_index, item in enumerate(result.import_items):
            item_path = Path(item.path).resolve()
            # A valid downloaded file remains reusable evidence even when its
            # first metadata import fails. A later source may retry the import,
            # but must not redownload the same video.
            if item_path.is_file():
                media_index[item.video_id] = item_path
            if item.quality_facts is not None and item_path.is_file():
                acquisition_evidence[item.video_id] = _AcquisitionEvidence(
                    item_path,
                    dict(item.quality_facts),
                    item.private_cover_path,
                )
            elif item.quality_facts is None:
                evidence = acquisition_evidence.get(item.video_id)
                if evidence is not None and evidence.path == item_path:
                    item = SyncImportItem(
                        item.path,
                        item.video_id,
                        item.source_upload_date,
                        item.source_item_ids,
                        evidence.quality_facts,
                        evidence.private_cover_path,
                    )
                    result.import_items[item_index] = item
            try:
                returned = self.importer(self.db, item)
                track_id = (
                    int(returned)
                    if isinstance(returned, int) and not isinstance(returned, bool)
                    else self._track_id_for_path(item.path)
                )
                if track_id is None:
                    raise RuntimeError("The imported source track could not be located.")
                canonical_track_id = self._ensure_track_identity(
                    item.video_id, track_id
                )
                if item.quality_facts is not None:
                    canonical_row = self.conn.execute(
                        "SELECT path FROM tracks WHERE id=?",
                        (canonical_track_id,),
                    ).fetchone()
                    canonical_path = (
                        Path(str(canonical_row["path"])).resolve()
                        if canonical_row is not None
                        else None
                    )
                    if canonical_path == item_path:
                        self.db.upsert_track_media_quality(
                            canonical_track_id,
                            **dict(item.quality_facts),
                        )
                        result.record_quality_facts(item.quality_facts)
                    else:
                        # A defensive duplicate claim must never overwrite the
                        # canonical file's actual stored representation with
                        # facts collected from a different file.
                        stored_facts = self.db.get_track_media_quality(
                            canonical_track_id
                        )
                        if stored_facts is not None:
                            result.record_quality_facts(dict(stored_facts))
                result.successful_video_ids.add(item.video_id)
                imported_count += 1
            except Exception as exc:
                result.add_failure(
                    SyncFailure(
                        item.video_id,
                        Path(item.path).stem,
                        sanitize_error_text(exc),
                        "import",
                        item.source_item_ids[0] if item.source_item_ids else None,
                    )
                )
        return imported_count

    def _record_reused_quality_facts(self, result: SyncResult) -> None:
        """Report the canonical representation reused by existing source items."""

        import_video_ids = {item.video_id for item in result.import_items}
        reused_video_ids = result.successful_video_ids - import_video_ids
        for video_id in sorted(reused_video_ids):
            canonical_track_id = self.db.canonical_track_id(
                "youtube",
                video_id,
                require_existing_file=True,
            )
            if canonical_track_id is None:
                continue
            stored_facts = self.db.get_track_media_quality(canonical_track_id)
            if stored_facts is not None:
                result.record_reused_quality_facts(dict(stored_facts))

    def _track_id_for_path(self, path: str | Path) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM tracks WHERE path=?", (str(Path(path).resolve()),)
        ).fetchone()
        return int(row[0]) if row else None

    def _valid_database_video_ids(self) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT i.external_track_id, t.path
            FROM source_track_identities i
            JOIN tracks t ON t.id=i.track_id
            WHERE i.source_kind='youtube'
            """
        ).fetchall()
        return {
            str(row["external_track_id"])
            for row in rows
            if Path(row["path"]).is_file()
        }

    def _extend_valid_database_video_ids(
        self,
        valid_database_ids: set[str],
        candidate_video_ids: Iterable[str],
    ) -> None:
        for video_id in set(candidate_video_ids) - valid_database_ids:
            row = self.conn.execute(
                """
                SELECT tracks.path
                FROM source_track_identities AS identities
                JOIN tracks ON tracks.id=identities.track_id
                WHERE identities.source_kind='youtube'
                  AND identities.external_track_id=?
                """,
                (video_id,),
            ).fetchone()
            if row is not None and Path(str(row["path"])).is_file():
                valid_database_ids.add(video_id)

    def _refresh_stale_canonical_identities(self) -> int:
        """Restore/promote an existing claim when canonical identity is absent/stale."""

        register = getattr(self.db, "register_source_identity", None)
        if not callable(register):
            return 0
        rows = self.conn.execute(
            """
            SELECT trim(claims.source_video_id) AS external_track_id,
                   identities.track_id AS canonical_track_id,
                   canonical.path AS canonical_path,
                   claims.id AS claim_track_id,
                   claims.path AS claim_path
            FROM tracks AS claims
            LEFT JOIN source_track_identities AS identities
              ON identities.source_kind='youtube'
             AND identities.external_track_id=trim(claims.source_video_id)
            LEFT JOIN tracks AS canonical ON canonical.id=identities.track_id
            WHERE lower(trim(COALESCE(claims.source_kind, '')))='youtube'
              AND length(trim(COALESCE(claims.source_video_id, ''))) > 0
            ORDER BY trim(claims.source_video_id), claims.id
            """
        ).fetchall()
        claims_by_id: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            claims_by_id.setdefault(str(row["external_track_id"]), []).append(row)

        promoted = 0
        for external_id, claims in claims_by_id.items():
            try:
                canonical_exists = bool(
                    claims[0]["canonical_track_id"] is not None
                    and Path(str(claims[0]["canonical_path"])).is_file()
                )
            except (OSError, TypeError, ValueError):
                canonical_exists = False
            if canonical_exists:
                continue
            candidates = []
            for row in claims:
                try:
                    exists = Path(str(row["claim_path"])).is_file()
                except (OSError, TypeError, ValueError):
                    exists = False
                if exists:
                    candidates.append(int(row["claim_track_id"]))
            if not candidates:
                continue
            previous_id = (
                int(claims[0]["canonical_track_id"])
                if claims[0]["canonical_track_id"] is not None
                else None
            )
            canonical_id = int(
                register("youtube", external_id, min(candidates), commit=True)
            )
            if canonical_id != previous_id:
                promoted += 1
        return promoted

    def _canonical_track_id(self, video_id: str) -> int | None:
        helper = getattr(self.db, "canonical_track_id", None)
        if callable(helper):
            return helper("youtube", video_id)
        row = self.conn.execute(
            "SELECT track_id FROM source_track_identities "
            "WHERE source_kind='youtube' AND external_track_id=?",
            (video_id,),
        ).fetchone()
        return int(row[0]) if row else None

    def _ensure_track_identity(self, video_id: str, track_id: int) -> int:
        helper = getattr(self.db, "register_source_identity", None)
        if callable(helper):
            return int(helper("youtube", video_id, track_id))
        timestamp = utc_now()
        current = self._canonical_track_id(video_id)
        if current is None:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO source_track_identities (
                        source_kind, external_track_id, track_id, first_seen_at, updated_at
                    ) VALUES ('youtube', ?, ?, ?, ?)
                    """,
                    (video_id, track_id, timestamp, timestamp),
                )
            return self._canonical_track_id(video_id) or track_id
        if current == track_id:
            return current

        current_row = self.conn.execute(
            "SELECT path FROM tracks WHERE id=?", (current,)
        ).fetchone()
        new_row = self.conn.execute(
            "SELECT path FROM tracks WHERE id=?", (track_id,)
        ).fetchone()
        current_exists = bool(current_row and Path(current_row["path"]).is_file())
        new_exists = bool(new_row and Path(new_row["path"]).is_file())
        canonical = track_id if new_exists and not current_exists else current
        conflicting = current if canonical == track_id else track_id
        with self.conn:
            duplicate = self.conn.execute(
                """
                SELECT 1 FROM source_identity_conflicts
                WHERE source_kind='youtube' AND external_track_id=?
                  AND canonical_track_id=? AND conflicting_track_id=?
                  AND resolved_at IS NULL
                """,
                (video_id, canonical, conflicting),
            ).fetchone()
            if duplicate is None:
                self.conn.execute(
                    """
                    INSERT INTO source_identity_conflicts (
                        source_kind, external_track_id, canonical_track_id,
                        conflicting_track_id, reason, created_at
                    ) VALUES ('youtube', ?, ?, ?, ?, ?)
                    """,
                    (
                        video_id,
                        canonical,
                        conflicting,
                        "A second track row claimed the same source identity.",
                        timestamp,
                    ),
                )
            if canonical != current:
                self.conn.execute(
                    """
                    UPDATE source_track_identities
                    SET track_id=?, updated_at=?
                    WHERE source_kind='youtube' AND external_track_id=?
                    """,
                    (canonical, timestamp, video_id),
                )
        return canonical

    def _reconcile_complete_snapshot(
        self,
        source: SyncSource,
        result: SyncResult,
    ) -> int:
        snapshot = result.snapshot
        if snapshot is None or not snapshot.complete:
            return 0
        timestamp = result.finished_at or utc_now()
        failures_by_item = {
            failure.source_item_id: failure.reason
            for failure in result.failures
            if failure.source_item_id
        }
        current_ids = [item.source_item_id for item in snapshot.items]
        first_positions: dict[int, int] = {}
        removed_count = 0
        for item in snapshot.items:
            prior = self.conn.execute(
                """
                SELECT video_id, track_id
                FROM sync_source_items
                WHERE source_id=? AND source_item_id=?
                """,
                (source.id, item.source_item_id),
            ).fetchone()
            prior_video_id = str(prior["video_id"]) if prior and prior["video_id"] else None
            effective_video_id = item.video_id or prior_video_id
            track_id = (
                self._canonical_track_id(effective_video_id)
                if effective_video_id
                else None
            )
            if (
                track_id is None
                and prior is not None
                and prior["track_id"] is not None
                and (item.video_id is None or item.video_id == prior_video_id)
            ):
                track_id = int(prior["track_id"])
            if (
                track_id is not None
                and source.destination_kind == "playlist"
                and source.destination_playlist_id is not None
            ):
                first_positions[track_id] = min(
                    item.source_position,
                    first_positions.get(track_id, item.source_position),
                )
            availability = "available" if item.available else "unavailable"
            last_error = failures_by_item.get(item.source_item_id)
            self.conn.execute(
                """
                INSERT INTO sync_source_items (
                    source_id, source_item_id, video_id, source_position,
                    source_title, availability_status, track_id, first_seen_at,
                    last_seen_at, removed_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                ON CONFLICT(source_id, source_item_id) DO UPDATE SET
                    video_id=excluded.video_id,
                    source_position=excluded.source_position,
                    source_title=excluded.source_title,
                    availability_status=excluded.availability_status,
                    track_id=excluded.track_id,
                    last_seen_at=excluded.last_seen_at,
                    removed_at=NULL,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at
                """,
                (
                    source.id,
                    item.source_item_id,
                    effective_video_id,
                    item.source_position,
                    item.title,
                    availability,
                    track_id,
                    timestamp,
                    timestamp,
                    sanitize_error_text(last_error) if last_error else None,
                    timestamp,
                    timestamp,
                ),
            )
        if current_ids:
            placeholders = ",".join("?" for _ in current_ids)
            row = self.conn.execute(
                f"SELECT COUNT(*) FROM sync_source_items WHERE source_id=? "
                f"AND removed_at IS NULL AND source_item_id NOT IN ({placeholders})",
                (source.id, *current_ids),
            ).fetchone()
            removed_count = int(row[0]) if row else 0
            self.conn.execute(
                f"UPDATE sync_source_items SET removed_at=?, updated_at=? "
                f"WHERE source_id=? AND removed_at IS NULL "
                f"AND source_item_id NOT IN ({placeholders})",
                (timestamp, timestamp, source.id, *current_ids),
            )
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM sync_source_items "
                "WHERE source_id=? AND removed_at IS NULL",
                (source.id,),
            ).fetchone()
            removed_count = int(row[0]) if row else 0
            self.conn.execute(
                "UPDATE sync_source_items SET removed_at=?, updated_at=? "
                "WHERE source_id=? AND removed_at IS NULL",
                (timestamp, timestamp, source.id),
            )
        if (
            source.destination_kind == "playlist"
            and source.destination_playlist_id is not None
        ):
            self._membership().set_source_origins(
                source.id,
                source.destination_playlist_id,
                sorted(first_positions.items(), key=lambda pair: (pair[1], pair[0])),
                commit=False,
            )
        return removed_count

    def _membership(self):
        if self.membership_service is None:
            from .playlist_membership import PlaylistMembershipService

            self.membership_service = PlaylistMembershipService(self.db)
            self.source_service.membership_service = self.membership_service
        return self.membership_service

    def _record_item_failures(self, source: SyncSource, result: SyncResult) -> None:
        timestamp = result.finished_at or utc_now()
        for failure in result.failures:
            if not failure.video_id:
                continue
            self.conn.execute(
                """
                INSERT INTO sync_failures (
                    playlist_id, playlist_title, video_id, title, reason,
                    error_category, attempt_count, first_attempt_at,
                    last_attempt_at, status, resolved_at, sync_source_id,
                    source_item_id
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, 'unresolved', NULL, ?, ?)
                ON CONFLICT(playlist_id, video_id) DO UPDATE SET
                    playlist_title=excluded.playlist_title,
                    title=COALESCE(excluded.title, sync_failures.title),
                    reason=excluded.reason,
                    error_category=excluded.error_category,
                    attempt_count=sync_failures.attempt_count + 1,
                    last_attempt_at=excluded.last_attempt_at,
                    status='unresolved', resolved_at=NULL,
                    sync_source_id=excluded.sync_source_id,
                    source_item_id=excluded.source_item_id
                """,
                (
                    source.external_id,
                    result.playlist_title,
                    failure.video_id,
                    failure.title,
                    sanitize_error_text(failure.reason),
                    failure.error_category,
                    timestamp,
                    timestamp,
                    source.id,
                    failure.source_item_id,
                ),
            )

    def _resolve_source_failures(self, source: SyncSource, result: SyncResult) -> None:
        if not result.successful_video_ids:
            return
        placeholders = ",".join("?" for _ in result.successful_video_ids)
        self.conn.execute(
            f"UPDATE sync_failures SET status='resolved', resolved_at=? "
            f"WHERE sync_source_id=? AND status='unresolved' "
            f"AND video_id IN ({placeholders})",
            (
                result.finished_at,
                source.id,
                *sorted(result.successful_video_ids),
            ),
        )

    def _record_source_run(
        self,
        source_id: int,
        batch_token: str,
        result: SyncResult,
    ) -> None:
        first_error = result.failures[0].reason if result.failures else None
        self.conn.execute(
            """
            INSERT INTO sync_source_runs (
                source_id, batch_token, started_at, finished_at, status,
                visible_item_count, new_item_count, downloaded_count,
                imported_count, existing_count, failed_count, removed_count,
                duplicate_occurrence_count, source_preserved_count,
                source_preserved_remux_count,
                mp3_compatibility_transcode_count, quality_failure_count,
                total_stored_bytes, first_error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                batch_token,
                result.started_at,
                result.finished_at,
                result.status,
                result.visible_item_count,
                result.new_item_count,
                result.downloaded_count,
                result.imported_count,
                result.existing_count,
                result.failed_count,
                result.removed_occurrence_count,
                result.duplicate_occurrence_count,
                result.source_preserved_count,
                result.source_preserved_remux_count,
                result.mp3_compatibility_transcode_count,
                result.quality_failure_count,
                result.total_stored_bytes,
                sanitize_error_text(first_error) if first_error else None,
                utc_now(),
            ),
        )
        self.conn.execute(
            """
            DELETE FROM sync_source_runs
            WHERE source_id=? AND status='complete' AND id NOT IN (
                SELECT id FROM sync_source_runs
                WHERE source_id=? AND status='complete'
                ORDER BY started_at DESC, id DESC LIMIT 50
            )
            """,
            (source_id, source_id),
        )

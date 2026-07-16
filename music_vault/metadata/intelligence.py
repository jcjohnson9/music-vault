from __future__ import annotations

import dataclasses
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from music_vault.core.db import MusicVaultDB
from music_vault.core.safety import sanitize_error_text

from .artist_credits import ArtistCreditInput, ArtistCreditService
from .ensemble import FieldAction, MetadataEnsemble, build_metadata_ensemble
from .intelligence_schema import MetadataIntelligenceJobStore
from .intelligence_settings import (
    DiscogsTokenStore,
    normalize_metadata_intelligence_settings,
)
from .musicbrainz_enricher import MusicBrainzProvider
from .providers import ProviderQuery, ProviderReleaseCandidate
from .schema import EDITABLE_METADATA_FIELDS
from .service import AutomaticMetadataField, MetadataService
from .tag_writer import MediaBackup, SafeTagWriter, TagWriteError, TagWriteResult
from .title_parser import ParsedTitle, parse_youtube_title


AUTOMATIC_IMPORT_JOB_ID = "automatic-new-imports"


@dataclass(frozen=True)
class IntelligenceRunResult:
    job_id: str | None
    processed: int
    applied: int
    review: int
    no_match: int
    failed: int
    cancelled: bool = False


@dataclass(frozen=True)
class _CommittedTagWrite:
    """Enough verified state to restore a media file if SQLite cannot commit."""

    backup: MediaBackup
    result: TagWriteResult


def _as_mapping(value: object) -> dict[str, object]:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _safe_scalar(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, (tuple, list)):
        return [_safe_scalar(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _safe_scalar(item) for key, item in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class MetadataIntelligenceService:
    """Consent-gated, resumable orchestration over normalized provider data.

    Imports only enqueue database work. This service is invoked on a background
    task and opens its own SQLite connection, so the GUI thread is never used
    for network access or media mutation.
    """

    def __init__(
        self,
        database,
        config: Mapping[str, object] | Callable[[], Mapping[str, object]],
        *,
        token_store: DiscogsTokenStore | None = None,
        discogs_provider_factory: Callable[[str], object] | None = None,
        musicbrainz_provider_factory: Callable[[], object] | None = None,
        tag_writer: SafeTagWriter | None = None,
        artwork_store_factory: Callable[[str], object] | None = None,
    ) -> None:
        self.database = database
        self.db_path = Path(database.db_path).resolve()
        self.backup_dir = Path(getattr(database, "backup_dir", self.db_path.parent / "backups"))
        self._config = config
        self.token_store = token_store or DiscogsTokenStore()
        self.discogs_provider_factory = discogs_provider_factory
        self.musicbrainz_provider_factory = musicbrainz_provider_factory
        self.tag_writer = tag_writer or SafeTagWriter()
        self.artwork_store_factory = artwork_store_factory

    def _settings(self) -> dict:
        source = self._config() if callable(self._config) else self._config
        return normalize_metadata_intelligence_settings(source)

    def _worker_database(self) -> MusicVaultDB:
        return MusicVaultDB(self.db_path, backup_dir=self.backup_dir)

    def _discogs_provider(self, token: str):
        if self.discogs_provider_factory is not None:
            return self.discogs_provider_factory(token)
        from .providers.discogs import DiscogsProvider

        return DiscogsProvider(token=token)

    def _musicbrainz_provider(self):
        if self.musicbrainz_provider_factory is not None:
            return self.musicbrainz_provider_factory()
        return MusicBrainzProvider()

    @staticmethod
    def _source_observations(db: MusicVaultDB, track_id: int) -> dict[str, str]:
        values: dict[str, str] = {}
        rows = db.conn.execute(
            """
            SELECT field_name, value
            FROM track_metadata_observations
            WHERE track_id=? AND provider='youtube'
              AND field_name IN ('title', 'artist')
            ORDER BY observed_at DESC, id DESC
            """,
            (int(track_id),),
        ).fetchall()
        for row in rows:
            name = str(row["field_name"])
            value = str(row["value"] or "").strip()
            if value and name not in values:
                values[name] = value
        return values

    @staticmethod
    def _query(
        snapshot,
        track,
        parsed: ParsedTitle,
    ) -> ProviderQuery:
        title = parsed.title_hint or snapshot.value("title") or Path(snapshot.path).stem
        artist = parsed.artist_hint or snapshot.value("artist")
        kwargs = {
            "title": str(title),
            "artist": str(artist).strip() if artist else None,
            "duration_seconds": (
                float(track["duration_seconds"])
                if track["duration_seconds"] is not None
                else None
            ),
            "version_type": parsed.version_type,
            "version_label": parsed.version_label,
            "year_hint": parsed.year_hint,
        }
        # ProviderQuery gained album as an additive Batch 10.1 hint; tolerate a
        # test double based on the smaller early contract.
        try:
            return ProviderQuery(album=snapshot.value("album"), **kwargs)
        except TypeError:
            return ProviderQuery(**kwargs)

    @staticmethod
    def _current_values(snapshot, track) -> dict[str, object]:
        values = {
            name: snapshot.value(name)
            for name in EDITABLE_METADATA_FIELDS
        }
        for name in (
            "discogs_release_id",
            "discogs_master_id",
            "discogs_track_position",
            "recording_group_key",
            "musicbrainz_recording_id",
            "musicbrainz_release_id",
        ):
            values[name] = track[name] if name in track.keys() else None
        return values

    @staticmethod
    def _parsed_summary(parsed: ParsedTitle, uploader: str | None) -> dict[str, object]:
        return {
            "raw_title": parsed.raw_title,
            "title": parsed.title_hint,
            "artist": parsed.artist_hint,
            "featured_artist": parsed.featured_artist_hint,
            "year": parsed.year_hint,
            "version_type": parsed.version_type,
            "version_label": parsed.version_label,
            "presentation_suffixes": list(parsed.presentation_suffixes),
            "pattern": parsed.pattern,
            "uploader": uploader,
        }

    @staticmethod
    def _agreement(ensemble: MetadataEnsemble) -> str:
        if ensemble.provider_disagreement:
            return "conflict"
        if ensemble.provider_agreement:
            return "agreed"
        if ensemble.discogs_candidate is not None:
            return "discogs_only"
        if ensemble.musicbrainz_candidate is not None:
            return "musicbrainz_only"
        return "none"

    @staticmethod
    def _review_reason(ensemble: MetadataEnsemble) -> str | None:
        if ensemble.provider_disagreement:
            return "provider_disagreement"
        if "version_identity_conflict" in ensemble.reasons:
            return "version_conflict"
        for resolution in ensemble.fields:
            if resolution.action is not FieldAction.REVIEW:
                continue
            if resolution.field_name == "album":
                return "album_ambiguity"
            if resolution.field_name in {"release_date", "original_release_date"}:
                return "date_ambiguity"
            if resolution.field_name in {"artist", "artist_credits"}:
                return "artist_ambiguity"
        if "youtube_exclusive_fallback" in ensemble.reasons:
            return "youtube_exclusive"
        return None

    @staticmethod
    def _candidate_summary(candidate: object | None) -> dict[str, object]:
        """Return normalized review facts, never a raw provider response."""

        if candidate is None:
            return {}
        names = (
            "title",
            "artist",
            "album",
            "album_artist",
            "release_date",
            "original_release_date",
            "version_type",
            "version_label",
            "duration_seconds",
            "label",
            "country",
            "release_format",
            "provider_reference",
        )
        summary = {
            name: _safe_scalar(getattr(candidate, name, None))
            for name in names
            if getattr(candidate, name, None) not in (None, "")
        }
        score = getattr(
            candidate,
            "provider_score",
            getattr(candidate, "score", None),
        )
        if score is not None:
            summary["score"] = _safe_scalar(score)
        summary["artwork_available"] = bool(
            getattr(candidate, "artwork", None)
            or getattr(candidate, "artwork_available", False)
        )
        return summary

    def _apply_release_context(
        self,
        db: MusicVaultDB,
        track_id: int,
        candidate: ProviderReleaseCandidate,
        ensemble: MetadataEnsemble,
    ) -> None:
        if candidate.provider_score < 85 or ensemble.provider_disagreement:
            return
        now = db.conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        db.conn.execute(
            """
            INSERT INTO track_release_context (
                track_id, discogs_release_id, discogs_master_id, release_title,
                release_country, release_format, catalog_number, label_name,
                release_date, original_release_date, provider_reference,
                confidence, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                discogs_release_id=excluded.discogs_release_id,
                discogs_master_id=excluded.discogs_master_id,
                release_title=excluded.release_title,
                release_country=excluded.release_country,
                release_format=excluded.release_format,
                label_name=excluded.label_name,
                release_date=excluded.release_date,
                original_release_date=excluded.original_release_date,
                provider_reference=excluded.provider_reference,
                confidence=excluded.confidence,
                updated_at=excluded.updated_at
            """,
            (
                int(track_id),
                candidate.release_id,
                candidate.master_id,
                candidate.album,
                candidate.country,
                candidate.release_format,
                candidate.label,
                candidate.release_date,
                candidate.original_release_date,
                candidate.provider_reference,
                float(candidate.provider_score),
                str(now),
            ),
        )

    def _apply_provider_identity(
        self,
        db: MusicVaultDB,
        track_id: int,
        ensemble: MetadataEnsemble,
    ) -> None:
        candidate = ensemble.discogs_candidate
        if candidate is None or candidate.provider_score < 85 or ensemble.provider_disagreement:
            return
        safe_fields = {item.field_name for item in ensemble.fields if item.safe_to_apply}
        values = {
            "discogs_release_id": candidate.release_id if "discogs_release_id" in safe_fields else None,
            "discogs_master_id": candidate.master_id if "discogs_master_id" in safe_fields else None,
            "discogs_track_position": (
                candidate.track_position if "discogs_track_position" in safe_fields else None
            ),
            "recording_group_key": ensemble.recording_group_key,
        }
        db.conn.execute(
            """
            UPDATE tracks SET
                discogs_release_id=COALESCE(?, discogs_release_id),
                discogs_master_id=COALESCE(?, discogs_master_id),
                discogs_track_position=COALESCE(?, discogs_track_position),
                recording_group_key=COALESCE(?, recording_group_key),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (*values.values(), int(track_id)),
        )
        self._apply_release_context(db, track_id, candidate, ensemble)

    def _apply_musicbrainz_identity(
        self,
        db: MusicVaultDB,
        track_id: int,
        ensemble: MetadataEnsemble,
    ) -> None:
        candidate = ensemble.musicbrainz_candidate
        if candidate is None:
            return
        safe_fields = {item.field_name for item in ensemble.fields if item.safe_to_apply}
        recording_id = (
            getattr(candidate, "recording_id", None)
            if "musicbrainz_recording_id" in safe_fields
            else None
        )
        release_id = (
            getattr(candidate, "release_id", None)
            if "musicbrainz_release_id" in safe_fields
            else None
        )
        if recording_id in (None, "") and release_id in (None, ""):
            return
        db.conn.execute(
            """
            UPDATE tracks SET
                musicbrainz_recording_id=COALESCE(?, musicbrainz_recording_id),
                musicbrainz_release_id=COALESCE(?, musicbrainz_release_id),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (recording_id, release_id, int(track_id)),
        )

    @staticmethod
    def _credit_inputs(candidate: ProviderReleaseCandidate) -> list[ArtistCreditInput]:
        inputs: list[ArtistCreditInput] = []
        previous_join = ""
        for index, credit in enumerate(candidate.artist_credits):
            # Discogs attaches a join phrase to the preceding artist, while
            # Music Vault stores it as the prefix for the following credit.
            inputs.append(
                ArtistCreditInput(
                    display_name=credit.name,
                    role=credit.role,
                    join_phrase=previous_join if index else "",
                    entity_type=credit.entity_type,
                    discogs_artist_id=credit.artist_id,
                )
            )
            previous_join = credit.join_phrase
        return inputs

    def _write_tags(
        self,
        db: MusicVaultDB,
        item,
        result,
    ) -> tuple[str, _CommittedTagWrite | None]:
        if not result.changed:
            return "not_needed", None
        snapshot = result.after
        path = Path(snapshot.path)
        if not self.tag_writer.supports(path):
            return "unsupported", None
        approved = MetadataService(db).approved_snapshot(item.track_id)
        patch = {
            name: getattr(approved, name)
            for name in (
                "title",
                "artist",
                "album",
                "album_artist",
                "release_date",
                "original_release_date",
                "version_type",
                "version_label",
            )
            if name in result.changed_fields and getattr(approved, name) not in (None, "")
        }
        row = db.get_track(item.track_id)
        for name in (
            "discogs_release_id",
            "discogs_master_id",
            "musicbrainz_recording_id",
            "musicbrainz_release_id",
        ):
            if name in row.keys() and row[name] not in (None, ""):
                patch[name] = row[name]
        credits = ArtistCreditService(db).track_credits(item.track_id)
        discogs_artist_ids = [credit.artist.discogs_artist_id for credit in credits if credit.artist.discogs_artist_id]
        musicbrainz_artist_ids = [credit.artist.musicbrainz_artist_id for credit in credits if credit.artist.musicbrainz_artist_id]
        if discogs_artist_ids:
            patch["discogs_artist_ids"] = ";".join(discogs_artist_ids)
        if musicbrainz_artist_ids:
            patch["musicbrainz_artist_ids"] = ";".join(musicbrainz_artist_ids)
        if not patch:
            return "not_needed", None
        backup_directory = Path(db.db_path).parent / "backups" / "metadata_jobs" / str(item.job_id)
        backup = self.tag_writer.create_backup(
            path,
            backup_directory,
            identity=f"{item.job_id}-{item.id}",
        )
        prepared = self.tag_writer.prepare(
            path,
            patch,
            expected_full_sha256=backup.fingerprint.full_sha256,
            artwork_path=None,
        )
        committed = self.tag_writer.commit(prepared, backup=backup)
        return "verified", _CommittedTagWrite(backup, committed)

    def _restore_committed_tags(self, committed: _CommittedTagWrite) -> None:
        """Restore verified media after a later database transaction failure."""

        try:
            self.tag_writer.restore(
                committed.result.path,
                committed.backup.backup_path,
                expected_backup_sha256=committed.backup.fingerprint.full_sha256,
                expected_current_sha256=committed.result.updated.full_sha256,
            )
        except TagWriteError as exc:
            raise TagWriteError("media_database_consistency_restore_failed") from exc

    def _discogs_artwork_for_gap(
        self,
        *,
        token: str,
        candidate: ProviderReleaseCandidate | None,
        snapshot,
        settings: Mapping[str, object],
        accepted: bool,
    ) -> tuple[object | None, str]:
        """Fetch one accepted front image without touching effective metadata."""

        if settings.get("metadata_fill_missing_artwork_enabled") is not True:
            return None, "not_requested"
        if (
            not accepted
            or candidate is None
            or candidate.artwork is None
            or not candidate.release_id
        ):
            return None, "not_available"
        artwork_state = snapshot.fields.get("artwork")
        if artwork_state is None:
            return None, "not_available"
        try:
            if self.artwork_store_factory is not None:
                store = self.artwork_store_factory(token)
            else:
                from .discogs_artwork import DiscogsArtworkCache

                store = DiscogsArtworkCache()
            record = store.fetch_for_gap(
                candidate.artwork,
                accepted_release_id=candidate.release_id,
                provider_score=candidate.provider_score,
                current_cover_path=artwork_state.value,
                manual=artwork_state.is_manual,
                locked=artwork_state.is_locked,
            )
        except Exception as exc:
            return None, sanitize_error_text(exc, max_length=200)
        if record is None:
            return None, "preserved_existing"
        return record, "filled"

    def _process_item(
        self,
        db: MusicVaultDB,
        store: MetadataIntelligenceJobStore,
        item,
        settings: Mapping[str, object],
        cancel_event: threading.Event | None,
    ) -> str:
        if cancel_event is not None and cancel_event.is_set():
            store.mark_item(item.id, "cancelled")
            return "cancelled"
        metadata = MetadataService(db)
        snapshot = metadata.snapshot(item.track_id)
        completion_fields = tuple(
            name for name in EDITABLE_METADATA_FIELDS if name != "artwork"
        )
        if all(snapshot.fields[name].is_locked for name in completion_fields):
            artwork_complete = (
                settings.get("metadata_fill_missing_artwork_enabled") is not True
                or snapshot.fields["artwork"].is_locked
            )
            if artwork_complete:
                store.mark_item(
                    item.id,
                    "skipped",
                    review_reason="manual_or_confirmed_complete",
                    file_write_result="not_requested",
                    artwork_result="not_requested",
                )
                return "skipped"
        track = db.get_track(item.track_id)
        source = self._source_observations(db, item.track_id)
        raw_title = source.get("title") or snapshot.value("title") or Path(snapshot.path).stem
        uploader = source.get("artist") if snapshot.source_kind == "youtube" else None
        parsed = parse_youtube_title(raw_title)
        query = self._query(snapshot, track, parsed)

        discogs_candidates: Sequence[ProviderReleaseCandidate] = ()
        musicbrainz_candidates: Sequence[object] = ()
        provider_failures: list[str] = []
        token = ""
        if settings.get("metadata_discogs_enabled") is True:
            token = self.token_store.read()
            if token:
                try:
                    discogs_candidates = tuple(
                        self._discogs_provider(token).search(
                            query,
                            cancel_event=cancel_event,
                        )
                    )
                except Exception as exc:
                    provider_failures.append(sanitize_error_text(exc))
            else:
                provider_failures.append("discogs_token_required")
        if settings.get("metadata_musicbrainz_secondary_enabled") is True:
            try:
                musicbrainz_candidates = tuple(
                    self._musicbrainz_provider().search(
                        query.title,
                        query.artist,
                        cancel_event=cancel_event,
                    )
                )
            except Exception as exc:
                provider_failures.append(sanitize_error_text(exc))

        youtube_exclusive = bool(
            snapshot.source_kind == "youtube"
            and parsed.strong_pattern
            and not discogs_candidates
            and not musicbrainz_candidates
        )
        current = self._current_values(snapshot, track)
        locked = {
            name for name, field in snapshot.fields.items() if field.is_locked
        }
        source_version = parsed.version_type
        top_discogs = discogs_candidates[0] if discogs_candidates else None
        unofficial_live = bool(
            source_version == "live"
            and (top_discogs is None or not bool(top_discogs.is_official))
        )
        ensemble = build_metadata_ensemble(
            current=current,
            discogs_candidates=discogs_candidates,
            musicbrainz_candidates=musicbrainz_candidates,
            parsed_title=parsed,
            embedded={
                name: field.value
                for name, field in snapshot.fields.items()
                if field.provenance == "embedded" and field.value not in (None, "")
            },
            uploader=uploader or "",
            locked_fields=locked,
            youtube_exclusive=youtube_exclusive,
            unofficial_live=unofficial_live,
        )
        artwork_record, artwork_result = self._discogs_artwork_for_gap(
            token=token,
            candidate=ensemble.discogs_candidate,
            snapshot=snapshot,
            settings=settings,
            accepted=bool(
                not ensemble.provider_disagreement
                and "version_identity_conflict" not in ensemble.reasons
            ),
        )
        proposals: dict[str, object] = {
            field.field_name: _safe_scalar(field.value)
            for field in ensemble.fields
            if field.field_name != "artwork" and field.value not in (None, "")
        }
        proposals["_current"] = {
            name: _safe_scalar(value)
            for name, value in current.items()
            if name in EDITABLE_METADATA_FIELDS and value not in (None, "")
        }
        if track["duration_seconds"] is not None:
            proposals["_current"]["duration_seconds"] = _safe_scalar(
                track["duration_seconds"]
            )
        proposals["_discogs"] = self._candidate_summary(
            ensemble.discogs_candidate
        )
        proposals["_musicbrainz"] = self._candidate_summary(
            ensemble.musicbrainz_candidate
        )
        proposals["_sources"] = {
            field.field_name: field.source
            for field in ensemble.fields
            if field.source and field.field_name != "artwork"
        }
        proposals["_reasons"] = {
            field.field_name: list(field.reasons)
            for field in ensemble.fields
            if field.reasons and field.field_name != "artwork"
        }
        proposals["_artwork"] = {
            "candidate_available": bool(
                ensemble.discogs_candidate is not None
                and ensemble.discogs_candidate.artwork is not None
            ),
            "result": artwork_result,
        }
        confidences = {
            field.field_name: field.score
            for field in ensemble.fields
            if field.field_name != "artwork"
        }
        automatic = {
            field.field_name: AutomaticMetadataField(
                value=field.value,
                confidence=field.score,
                provider=field.source,
                provider_reference=field.provider_reference,
                conflict=field.conflict,
            )
            for field in ensemble.fields
            if field.field_name in EDITABLE_METADATA_FIELDS
            and field.field_name != "artwork"
            and field.value not in (None, "")
        }
        candidate = ensemble.discogs_candidate
        file_write_result = "not_requested"
        committed_tags: _CommittedTagWrite | None = None
        try:
            with db.conn:
                # Lock the writer before the final gap/staleness check so a
                # manual artwork edit on another connection cannot be replaced
                # by a provider result that arrived later.
                db.conn.execute("BEGIN IMMEDIATE")
                effective_automatic = dict(automatic)
                if artwork_record is not None:
                    from .discogs_artwork import is_true_artwork_gap

                    latest_artwork = metadata.snapshot(item.track_id).fields.get("artwork")
                    original_artwork = snapshot.fields.get("artwork")
                    if (
                        latest_artwork is None
                        or original_artwork is None
                        or latest_artwork.value != original_artwork.value
                        or latest_artwork.is_manual != original_artwork.is_manual
                        or latest_artwork.is_locked != original_artwork.is_locked
                    ):
                        artwork_result = "preserved_newer_artwork"
                    elif not is_true_artwork_gap(
                        latest_artwork.value,
                        manual=latest_artwork.is_manual,
                        locked=latest_artwork.is_locked,
                    ):
                        artwork_result = "preserved_existing"
                    else:
                        effective_automatic["artwork"] = AutomaticMetadataField(
                            value=str(artwork_record.path),
                            confidence=(
                                candidate.provider_score
                                if candidate is not None
                                else 85.0
                            ),
                            provider="discogs_high_confidence",
                            provider_reference=artwork_record.provider_page_url,
                        )
                result = metadata.apply_automatic_fields(
                    item.track_id,
                    effective_automatic,
                    minimum_confidence=85,
                    commit=False,
                )
                self._apply_provider_identity(db, item.track_id, ensemble)
                self._apply_musicbrainz_identity(db, item.track_id, ensemble)
                credits_field = ensemble.field("artist_credits")
                if (
                    candidate is not None
                    and credits_field is not None
                    and credits_field.safe_to_apply
                    and candidate.artist_credits
                ):
                    ArtistCreditService(db).replace_track_credits(
                        item.track_id,
                        self._credit_inputs(candidate),
                        provenance="discogs_high_confidence",
                        provider_reference=candidate.provider_reference,
                        confidence=credits_field.score,
                        commit=False,
                    )
                if settings.get("metadata_writeback_enabled") is True and result.changed:
                    file_write_result, committed_tags = self._write_tags(db, item, result)
        except TagWriteError:
            # Preparation/commit failures restore internally; the surrounding
            # SQLite context rolls back fields, IDs, release context and credits.
            file_write_result = "restored"
            store.mark_item(
                item.id,
                "review",
                parsed_hints=self._parsed_summary(parsed, uploader),
                discogs_release_id=(candidate.release_id if candidate else None),
                discogs_master_id=(candidate.master_id if candidate else None),
                field_proposal=proposals,
                field_confidence=confidences,
                provider_agreement=self._agreement(ensemble),
                review_reason="file_write_failed",
                file_write_result=file_write_result,
                artwork_result=artwork_result,
            )
            return "review"
        except Exception:
            if committed_tags is not None:
                self._restore_committed_tags(committed_tags)
            raise

        review_reason = self._review_reason(ensemble)
        if review_reason:
            state = "review"
        elif result.changed or any(field.safe_to_apply for field in ensemble.fields):
            state = "applied"
        elif youtube_exclusive:
            state = "review"
            review_reason = "youtube_exclusive"
        elif provider_failures and not discogs_candidates and not musicbrainz_candidates:
            state = "failed"
            review_reason = "provider_unavailable"
        else:
            state = "no_match"
        candidate = ensemble.discogs_candidate
        mb = ensemble.musicbrainz_candidate
        store.mark_item(
            item.id,
            state,
            parsed_hints=self._parsed_summary(parsed, uploader),
            discogs_release_id=(candidate.release_id if candidate else None),
            discogs_master_id=(candidate.master_id if candidate else None),
            musicbrainz_recording_id=(
                getattr(mb, "recording_id", None) if mb is not None else None
            ),
            musicbrainz_release_id=(
                getattr(mb, "release_id", None) if mb is not None else None
            ),
            field_proposal=proposals,
            field_confidence=confidences,
            provider_agreement=self._agreement(ensemble),
            review_reason=review_reason,
            applied_history_group=result.change_group_id,
            file_write_result=file_write_result,
            artwork_result=artwork_result,
            error=(provider_failures[0] if state == "failed" and provider_failures else None),
        )
        return state

    def _run_job(
        self,
        db: MusicVaultDB,
        job_id: str,
        *,
        cancel_event: threading.Event | None,
    ) -> IntelligenceRunResult:
        store = MetadataIntelligenceJobStore(db)
        settings = self._settings()
        processed = applied = review = no_match = failed = 0
        cancelled = False
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            item = store.claim_next_item(job_id)
            if item is None:
                break
            try:
                state = self._process_item(db, store, item, settings, cancel_event)
            except Exception as exc:
                state = "failed"
                store.mark_item(
                    item.id,
                    "failed",
                    review_reason="provider_or_apply_failure",
                    error=sanitize_error_text(exc),
                )
            processed += 1
            applied += int(state == "applied")
            review += int(state == "review")
            no_match += int(state == "no_match")
            failed += int(state == "failed")
        return IntelligenceRunResult(
            job_id, processed, applied, review, no_match, failed, cancelled
        )

    def process_automatic_queue(
        self,
        *,
        job_id: str = AUTOMATIC_IMPORT_JOB_ID,
        cancel_event: threading.Event | None = None,
    ) -> IntelligenceRunResult:
        if str(job_id) != AUTOMATIC_IMPORT_JOB_ID:
            raise ValueError("automatic_metadata_job_id_invalid")
        if not self._settings()["metadata_intelligence_enabled"]:
            return IntelligenceRunResult(None, 0, 0, 0, 0, 0)
        db = self._worker_database()
        try:
            row = db.conn.execute(
                "SELECT status FROM metadata_intelligence_jobs WHERE id=?",
                (AUTOMATIC_IMPORT_JOB_ID,),
            ).fetchone()
            if row is None:
                return IntelligenceRunResult(AUTOMATIC_IMPORT_JOB_ID, 0, 0, 0, 0, 0)
            if str(row["status"]) in {"created", "analyzing", "applying"}:
                MetadataIntelligenceJobStore(db).recover_interrupted(
                    AUTOMATIC_IMPORT_JOB_ID
                )
            return self._run_job(db, AUTOMATIC_IMPORT_JOB_ID, cancel_event=cancel_event)
        finally:
            db.close()

    def analyze_existing_library(
        self,
        *,
        job_id: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> IntelligenceRunResult:
        if not self._settings()["metadata_intelligence_enabled"]:
            raise RuntimeError("metadata_intelligence_disabled")
        db = self._worker_database()
        try:
            store = MetadataIntelligenceJobStore(db)
            if job_id is None:
                row = db.conn.execute(
                    """
                    SELECT id, job_kind, status FROM metadata_intelligence_jobs
                    WHERE job_kind='existing_library'
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """
                ).fetchone()
            else:
                row = db.conn.execute(
                    "SELECT id, job_kind, status FROM metadata_intelligence_jobs "
                    "WHERE id=?",
                    (str(job_id),),
                ).fetchone()
                if row is None:
                    raise KeyError(f"Metadata-intelligence job {job_id} does not exist.")
                if str(row["job_kind"]) != "existing_library":
                    raise ValueError("metadata_intelligence_job_kind_invalid")

            if row is None or (job_id is None and str(row["status"]) == "cancelled"):
                selected_job_id = store.create_existing_library_job()
            else:
                selected_job_id = str(row["id"])
                status = str(row["status"])
                if status in {"ready", "complete", "complete_with_issues", "failed"}:
                    # The setup scan is one-time and never repeats completed or
                    # review work merely because Settings is saved again.
                    return IntelligenceRunResult(selected_job_id, 0, 0, 0, 0, 0)
                if status == "cancelled":
                    raise ValueError("cancelled_metadata_intelligence_job")
                if status == "paused":
                    store.resume(selected_job_id)
                store.recover_interrupted(selected_job_id)
            return self._run_job(db, selected_job_id, cancel_event=cancel_event)
        finally:
            db.close()

    def pause_job(self, job_id: str) -> None:
        MetadataIntelligenceJobStore(self.database).pause(job_id)

    def apply_review_fields(
        self,
        item_id: int,
        field_names: Sequence[str],
    ):
        """Apply a user's explicit field selection as locked manual metadata."""

        row = self.database.conn.execute(
            "SELECT track_id,state,field_proposal FROM metadata_intelligence_items WHERE id=?",
            (int(item_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"Metadata-intelligence item {item_id} does not exist.")
        if str(row["state"]) not in {"review", "ready"}:
            raise ValueError("Only a review item can apply selected fields.")
        try:
            proposal = json.loads(str(row["field_proposal"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("The saved review proposal is invalid.") from exc
        if not isinstance(proposal, Mapping):
            raise ValueError("The saved review proposal is invalid.")
        selected = list(dict.fromkeys(str(name) for name in field_names))
        values = {
            name: proposal[name]
            for name in selected
            if name in EDITABLE_METADATA_FIELDS
            and name in proposal
            and proposal[name] not in (None, "")
            and not isinstance(proposal[name], (Mapping, list, tuple))
        }
        if not values:
            raise ValueError("Select at least one proposed metadata field.")
        store = MetadataIntelligenceJobStore(self.database)
        with self.database.conn:
            result = MetadataService(self.database).apply_manual_patch(
                int(row["track_id"]),
                values,
                actor="user",
                reason="metadata_intelligence_review_selection",
                commit=False,
            )
            self.database.conn.execute(
                """
                UPDATE metadata_intelligence_items
                SET state='applied', review_reason=NULL,
                    applied_history_group=COALESCE(?, applied_history_group),
                    completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                (result.change_group_id, int(item_id)),
            )
            store._refresh_job(str(self.database.conn.execute(
                "SELECT job_id FROM metadata_intelligence_items WHERE id=?",
                (int(item_id),),
            ).fetchone()[0]))
        return result

    def resume_job(self, job_id: str) -> None:
        store = MetadataIntelligenceJobStore(self.database)
        try:
            store.resume(job_id)
        except ValueError:
            # A provider outage leaves retryable failed items in a completed
            # job. Requeue those normalized items without repeating completed
            # or reviewed tracks.
            with self.database.conn:
                changed = self.database.conn.execute(
                    "UPDATE metadata_intelligence_items SET state='queued', "
                    "completed_at=NULL, updated_at=CURRENT_TIMESTAMP "
                    "WHERE job_id=? AND state='failed'",
                    (str(job_id),),
                ).rowcount
                if not changed:
                    raise
                self.database.conn.execute(
                    "UPDATE metadata_intelligence_jobs SET status='analyzing', "
                    "completed_at=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(job_id),),
                )

    def cancel_job(self, job_id: str) -> None:
        MetadataIntelligenceJobStore(self.database).cancel(job_id)


__all__ = [
    "AUTOMATIC_IMPORT_JOB_ID",
    "IntelligenceRunResult",
    "MetadataIntelligenceService",
]

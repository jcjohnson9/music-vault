from __future__ import annotations

import dataclasses
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from music_vault.core.db import MusicVaultDB
from music_vault.core.runtime_policy import RuntimePolicy, runtime_policy_for
from music_vault.core.safety import sanitize_error_text

from .artist_credits import (
    ArtistCreditInput,
    ArtistCreditService,
    normalize_artist_name,
)
from .canonical_albums import is_uncatalogued_album, upsert_track_canonical_album
from .ensemble import (
    FieldAction,
    MetadataEnsemble,
    build_metadata_ensemble,
    versions_compatible,
)
from .intelligence_schema import MetadataIntelligenceJobStore
from .intelligence_settings import (
    DiscogsTokenStore,
    normalize_metadata_intelligence_settings,
)
from .musicbrainz_enricher import MusicBrainzProvider
from .providers import ProviderQuery, ProviderReleaseCandidate
from .review_policy import ReviewOutcome, classify_ensemble_outcome
from .schema import EDITABLE_METADATA_FIELDS
from .service import AutomaticMetadataField, MetadataService
from .soundtrack import classify_soundtrack
from .tag_writer import MediaBackup, SafeTagWriter, TagWriteError, TagWriteResult
from .title_parser import (
    ParsedTitle,
    parse_youtube_title,
    title_orientation_hypotheses,
)
from .title_orientation import OrientationDecision, assess_orientation, choose_orientation


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
    applied_with_gaps: int = 0
    source_fallback: int = 0


@dataclass(frozen=True)
class _CommittedTagWrite:
    """Enough verified state to restore a media file if SQLite cannot commit."""

    backup: MediaBackup
    result: TagWriteResult


@dataclass(frozen=True)
class _ProviderAdjudication:
    """Normalized, bounded provider evidence for one intelligence item."""

    parsed: ParsedTitle
    orientation: OrientationDecision | None
    discogs_candidates: tuple[object, ...]
    musicbrainz_candidates: tuple[object, ...]
    provider_failures: tuple[str, ...]
    token: str


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
        runtime_policy: RuntimePolicy | None = None,
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
        self.runtime_policy = runtime_policy or runtime_policy_for(database)

    def _settings(self) -> dict:
        source = self._config() if callable(self._config) else self._config
        return normalize_metadata_intelligence_settings(source)

    def _worker_database(self) -> MusicVaultDB:
        return MusicVaultDB(self.db_path, backup_dir=self.backup_dir)

    def _discogs_provider(self, token: str):
        if not self.runtime_policy.allows_provider_construction(token_backed=True):
            raise RuntimeError("metadata_provider_work_deferred")
        if self.discogs_provider_factory is not None:
            return self.discogs_provider_factory(token)
        from .providers.discogs import DiscogsProvider

        return DiscogsProvider(token=token)

    def _musicbrainz_provider(self):
        if not self.runtime_policy.allows_provider_construction(token_backed=False):
            raise RuntimeError("metadata_provider_work_deferred")
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
        album_hint = snapshot.value("album")
        if is_uncatalogued_album(album_hint):
            album_hint = None
        try:
            return ProviderQuery(album=album_hint, **kwargs)
        except TypeError:
            return ProviderQuery(**kwargs)

    @classmethod
    def _query_variants(cls, snapshot, track, parsed: ParsedTitle) -> tuple[ProviderQuery, ...]:
        """Build at most the two valid source-title orientations.

        Soundtrack context is carried on each query rather than expanded into
        additional network searches.  This keeps one metadata item inside the
        Batch 10.6 provider budget while preserving release context.
        """

        primary = cls._query(snapshot, track, parsed)
        work_title = snapshot.value("album")
        if is_uncatalogued_album(work_title):
            work_title = None
        soundtrack = classify_soundtrack(
            title=parsed.title_hint or primary.title,
            album=work_title,
            version_type=parsed.version_type,
            source_title=parsed.raw_title,
            release_format=snapshot.value("release_format"),
            album_artist=snapshot.value("album_artist"),
        )
        if soundtrack.is_soundtrack and parsed.version_type in {"unknown", "soundtrack"}:
            primary = dataclasses.replace(primary, version_type="soundtrack")
        queries = [primary]
        hypotheses = title_orientation_hypotheses(parsed)
        if len(hypotheses) > 1:
            reverse = hypotheses[1]
            kwargs = {
                "title": reverse.title,
                "artist": reverse.artist,
                "duration_seconds": primary.duration_seconds,
                "version_type": primary.version_type,
                "version_label": primary.version_label,
                "year_hint": primary.year_hint,
            }
            try:
                alternate = ProviderQuery(album=primary.album, **kwargs)
            except TypeError:
                alternate = ProviderQuery(**kwargs)
            if (
                alternate.title.casefold(),
                (alternate.artist or "").casefold(),
            ) != (
                primary.title.casefold(),
                (primary.artist or "").casefold(),
            ):
                queries.append(alternate)
        return tuple(queries[:2])

    @staticmethod
    def _query_leaders(
        candidate_groups: Sequence[Sequence[object]],
    ) -> tuple[object, ...]:
        """Keep each provider query's ranked leader before orientation comparison."""

        return tuple(group[0] for group in candidate_groups if group)

    @staticmethod
    def _deduplicate_candidates(candidates: Sequence[object]) -> tuple[object, ...]:
        """Keep the highest-scored copy of each normalized provider result."""

        ordered = sorted(
            candidates,
            key=lambda item: -float(
                getattr(item, "provider_score", getattr(item, "score", 0.0)) or 0.0
            ),
        )
        result: list[object] = []
        seen: set[tuple[str, ...]] = set()
        for item in ordered:
            key = tuple(
                str(getattr(item, name, None) or "").strip().casefold()
                for name in ("provider", "release_id", "recording_id", "title", "artist")
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return tuple(result)

    @staticmethod
    def _unique_local_orientation(
        db: MusicVaultDB,
        track_id: int,
        parsed: ParsedTitle,
    ) -> str | None:
        """Return the sole independently-supported local artist orientation.

        A one-off artist row created only from the track being adjudicated is
        not canonical evidence.  Provider identity, an explicit alias, or a
        credit on another track is required, and both/neither sides fail
        closed.
        """

        hypotheses = title_orientation_hypotheses(parsed)
        if len(hypotheses) != 2:
            return None
        tables = {
            str(row[0])
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "artists" not in tables:
            return None

        def supported_ids(value: str) -> set[int]:
            try:
                normalized = normalize_artist_name(value)
            except ValueError:
                return set()
            ids = {
                int(row[0])
                for row in db.conn.execute(
                    "SELECT id FROM artists WHERE normalized_name=?",
                    (normalized,),
                ).fetchall()
            }
            if "artist_aliases" in tables:
                ids.update(
                    int(row[0])
                    for row in db.conn.execute(
                        "SELECT artist_id FROM artist_aliases WHERE normalized_alias=?",
                        (normalized,),
                    ).fetchall()
                )
            supported: set[int] = set()
            for artist_id in ids:
                artist = db.conn.execute(
                    "SELECT discogs_artist_id,musicbrainz_artist_id FROM artists WHERE id=?",
                    (artist_id,),
                ).fetchone()
                provider_identity = bool(
                    artist
                    and (
                        str(artist[0] or "").strip()
                        or str(artist[1] or "").strip()
                    )
                )
                other_credit = False
                if "track_artist_credits" in tables:
                    other_credit = (
                        db.conn.execute(
                            "SELECT 1 FROM track_artist_credits "
                            "WHERE artist_id=? AND track_id<>? LIMIT 1",
                            (artist_id, int(track_id)),
                        ).fetchone()
                        is not None
                    )
                explicit_alias = False
                if "artist_aliases" in tables:
                    explicit_alias = (
                        db.conn.execute(
                            "SELECT 1 FROM artist_aliases WHERE artist_id=? LIMIT 1",
                            (artist_id,),
                        ).fetchone()
                        is not None
                    )
                if provider_identity or other_credit or explicit_alias:
                    supported.add(artist_id)
            return supported

        matching = [
            hypothesis.orientation
            for hypothesis in hypotheses
            if len(supported_ids(hypothesis.artist)) == 1
        ]
        return matching[0] if len(matching) == 1 else None

    def _adjudicate_providers(
        self,
        db: MusicVaultDB,
        snapshot,
        track,
        parsed: ParsedTitle,
        settings: Mapping[str, object],
        cancel_event: threading.Event | None,
    ) -> _ProviderAdjudication:
        """Query providers sequentially while retaining orientation identity."""

        queries = self._query_variants(snapshot, track, parsed)
        hypotheses = title_orientation_hypotheses(parsed)
        provider_failures: list[str] = []
        token = ""

        # Non-dash and explicit one-orientation input keeps the established
        # single-query provider behavior.
        if len(hypotheses) != 2:
            discogs_candidates: tuple[object, ...] = ()
            musicbrainz_candidates: tuple[object, ...] = ()
            query = queries[0]
            if settings.get("metadata_discogs_enabled") is True:
                token = self.token_store.read()
                if token:
                    try:
                        provider = self._discogs_provider(token)
                        discogs_candidates = self._deduplicate_candidates(
                            tuple(provider.search(query, cancel_event=cancel_event))
                        )
                    except Exception as exc:
                        provider_failures.append(sanitize_error_text(exc))
                else:
                    provider_failures.append("discogs_token_required")
            if settings.get("metadata_musicbrainz_secondary_enabled") is True:
                try:
                    provider = self._musicbrainz_provider()
                    musicbrainz_candidates = self._deduplicate_candidates(
                        tuple(
                            provider.search(
                                query.title,
                                query.artist,
                                cancel_event=cancel_event,
                            )
                        )
                    )
                except Exception as exc:
                    provider_failures.append(sanitize_error_text(exc))
            return _ProviderAdjudication(
                parsed,
                None,
                discogs_candidates,
                musicbrainz_candidates,
                tuple(provider_failures),
                token,
            )

        current_artist = snapshot.value("artist")
        current_title = snapshot.value("title")
        local_duration = (
            float(track["duration_seconds"])
            if track["duration_seconds"] is not None
            else None
        )
        unique_local = self._unique_local_orientation(db, int(track["id"]), parsed)
        discogs_by_orientation: dict[str, tuple[object, ...]] = {}
        if settings.get("metadata_discogs_enabled") is True:
            token = self.token_store.read()
            if token:
                try:
                    provider = self._discogs_provider(token)
                    for index, (hypothesis, query) in enumerate(
                        zip(hypotheses, queries)
                    ):
                        if cancel_event is not None and cancel_event.is_set():
                            break
                        group = self._deduplicate_candidates(
                            tuple(provider.search(query, cancel_event=cancel_event))
                        )
                        discogs_by_orientation[hypothesis.orientation] = group
                        provisional = choose_orientation(
                            hypotheses,
                            discogs_by_orientation,
                            unique_local_orientation=unique_local,
                            local_evidence_evaluated=True,
                            current_artist=current_artist,
                            current_title=current_title,
                            local_duration=local_duration,
                        )
                        if (
                            index == 0
                            and provisional.provider_confirmed
                            and provisional.selected_orientation
                            == hypothesis.orientation
                            and any(
                                assessment.orientation == hypothesis.orientation
                                and assessment.provider == "discogs"
                                and assessment.conclusive
                                for assessment in provisional.assessments
                            )
                        ):
                            break
                except Exception as exc:
                    provider_failures.append(sanitize_error_text(exc))
            else:
                provider_failures.append("discogs_token_required")

        provisional = choose_orientation(
            hypotheses,
            discogs_by_orientation,
            unique_local_orientation=unique_local,
            local_evidence_evaluated=True,
            current_artist=current_artist,
            current_title=current_title,
            local_duration=local_duration,
        )
        musicbrainz_candidates: tuple[object, ...] = ()
        musicbrainz_orientation: str | None = None
        musicbrainz_coherent = False
        musicbrainz_query_attempted = False
        if (
            settings.get("metadata_musicbrainz_secondary_enabled") is True
            and not (cancel_event is not None and cancel_event.is_set())
        ):
            selected_name = provisional.selected_orientation or hypotheses[0].orientation
            selected_index = next(
                (
                    index
                    for index, hypothesis in enumerate(hypotheses)
                    if hypothesis.orientation == selected_name
                ),
                0,
            )
            query = queries[selected_index]
            try:
                provider = self._musicbrainz_provider()
                musicbrainz_query_attempted = True
                musicbrainz_candidates = self._deduplicate_candidates(
                    tuple(
                        provider.search(
                            query.title,
                            query.artist,
                            cancel_event=cancel_event,
                        )
                    )
                )
                if musicbrainz_candidates:
                    leader = musicbrainz_candidates[0]
                    assessments = tuple(
                        assess_orientation(
                            hypothesis,
                            leader,
                            provider="musicbrainz",
                            local_duration=local_duration,
                        )
                        for hypothesis in hypotheses
                    )
                    best = max(
                        assessments,
                        key=lambda item: (item.coherent, item.score),
                    )
                    if best.coherent:
                        musicbrainz_orientation = best.orientation
                        musicbrainz_coherent = True
            except Exception as exc:
                provider_failures.append(sanitize_error_text(exc))

        decision = choose_orientation(
            hypotheses,
            discogs_by_orientation,
            musicbrainz_candidate=(
                musicbrainz_candidates[0] if musicbrainz_candidates else None
            ),
            musicbrainz_orientation=musicbrainz_orientation,
            musicbrainz_query_attempted=musicbrainz_query_attempted,
            unique_local_orientation=unique_local,
            local_evidence_evaluated=True,
            current_artist=current_artist,
            current_title=current_title,
            local_duration=local_duration,
        )
        selected_discogs = discogs_by_orientation.get(
            decision.selected_orientation or "", ()
        )
        if not any(
            candidate is decision.selected_candidate
            for candidate in selected_discogs
        ):
            selected_discogs = ()
        elif decision.selected_candidate is not None:
            # The orientation assessor may select a coherent lower-ranked
            # candidate over a higher provider-score identity mismatch.  The
            # ensemble must receive that exact candidate first and alone.
            selected_discogs = (decision.selected_candidate,)
        if not (
            musicbrainz_coherent
            and musicbrainz_orientation == decision.selected_orientation
        ):
            musicbrainz_candidates = ()
        selected_parsed = (
            parsed.for_orientation(decision.selected)
            if decision.selected is not None
            else parsed
        )
        return _ProviderAdjudication(
            selected_parsed,
            decision,
            tuple(selected_discogs),
            tuple(musicbrainz_candidates),
            tuple(provider_failures),
            token,
        )

    @staticmethod
    def _current_values(snapshot, track, release_context=None) -> dict[str, object]:
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
        context_keys = set(release_context.keys()) if release_context is not None else set()
        for name in (
            "musicbrainz_release_group_id",
            "provider_release_family_id",
        ):
            values[name] = (
                release_context[name]
                if release_context is not None and name in context_keys
                else None
            )
        return values

    @staticmethod
    def _parsed_summary(
        parsed: ParsedTitle,
        uploader: str | None,
        orientation: OrientationDecision | None = None,
    ) -> dict[str, object]:
        summary: dict[str, object] = {
            "raw_title": parsed.raw_title,
            "title": parsed.title_hint,
            "artist": parsed.artist_hint,
            "featured_artist": parsed.featured_artist_hint,
            "year": parsed.year_hint,
            "version_type": parsed.version_type,
            "version_label": parsed.version_label,
            "presentation_suffixes": list(parsed.presentation_suffixes),
            "pattern": parsed.pattern,
            "orientation_hypotheses": [
                {
                    "artist": item.artist,
                    "title": item.title,
                    "orientation": item.orientation,
                    "year_hint": item.year_hint,
                    "version_type": item.version_type,
                    "version_label": item.version_label,
                    "featured_artist": item.featured_artist,
                    "source_pattern": item.source_pattern,
                    "confidence_reasons": list(item.confidence_reasons),
                }
                for item in title_orientation_hypotheses(parsed)
            ],
            "uploader": uploader,
        }
        if orientation is not None:
            summary["orientation"] = orientation.to_dict()
        return summary

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
        critical = {"title", "artist", "artist_credits", "version_type"}
        if set(ensemble.provider_disagreement) & critical:
            return "critical_provider_conflict"
        if "version_identity_conflict" in ensemble.reasons:
            return "version_conflict"
        for resolution in ensemble.fields:
            if resolution.action is not FieldAction.REVIEW:
                continue
            if resolution.field_name in {"artist", "artist_credits"}:
                return "artist_ambiguity"
            if resolution.field_name == "title":
                return "title_ambiguity"
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
            "release_id",
            "master_id",
            "release_family_id",
            "track_position",
            "recording_id",
        )
        summary = {
            name: _safe_scalar(getattr(candidate, name, None))
            for name in names
            if getattr(candidate, name, None) not in (None, "")
        }
        raw_credits = getattr(candidate, "artist_credits", ()) or ()
        if isinstance(raw_credits, Sequence) and not isinstance(
            raw_credits, (str, bytes, bytearray)
        ):
            credits: list[dict[str, object]] = []
            for raw_credit in raw_credits:
                credit = _as_mapping(raw_credit)
                display_name = str(
                    credit.get("name") or credit.get("display_name") or ""
                ).strip()
                if not display_name:
                    continue
                normalized: dict[str, object] = {
                    "name": display_name,
                    "role": str(credit.get("role") or "primary").strip().casefold(),
                    "join_phrase": str(credit.get("join_phrase") or ""),
                    "entity_type": str(
                        credit.get("entity_type") or "unknown"
                    ).strip().casefold(),
                }
                for identity_key in (
                    "artist_id",
                    "discogs_artist_id",
                    "musicbrainz_artist_id",
                ):
                    identity = credit.get(identity_key)
                    if identity not in (None, "") and isinstance(
                        identity, (str, int)
                    ) and not isinstance(identity, bool):
                        normalized[identity_key] = str(identity).strip()
                credits.append(normalized)
            if credits:
                summary["artist_credits"] = credits
        score = getattr(
            candidate,
            "provider_score",
            getattr(candidate, "score", None),
        )
        if score is not None:
            summary["score"] = _safe_scalar(score)
        field_scores = getattr(candidate, "field_scores", None)
        if isinstance(field_scores, Mapping):
            summary["field_scores"] = {
                str(name): _safe_scalar(value)
                for name, value in field_scores.items()
                if isinstance(name, str) and isinstance(value, (int, float))
            }
        summary["artwork_available"] = bool(
            getattr(candidate, "artwork", None)
            or getattr(candidate, "artwork_available", False)
        )
        return summary

    @staticmethod
    def _database_accepted_field(
        ensemble: MetadataEnsemble,
        field_name: str,
        provider: str,
    ) -> bool:
        resolution = ensemble.field(field_name)
        return bool(
            resolution is not None
            and resolution.source == provider
            and resolution.value not in (None, "")
            and resolution.score >= 60.0
            and not resolution.conflict
            and resolution.action is not FieldAction.REVIEW
        )

    @staticmethod
    def _hard_version_conflict(ensemble: MetadataEnsemble) -> bool:
        return "version_identity_conflict" in ensemble.reasons

    def _apply_release_context(
        self,
        db: MusicVaultDB,
        track_id: int,
        candidate: ProviderReleaseCandidate,
        ensemble: MetadataEnsemble,
        *,
        provider_release_family_id: str | None = None,
    ) -> None:
        accepted = {
            name
            for name in (
                "discogs_release_id",
                "discogs_master_id",
                "provider_release_family_id",
                "album",
                "release_date",
                "original_release_date",
            )
            if self._database_accepted_field(ensemble, name, "discogs")
        }
        if not accepted or self._hard_version_conflict(ensemble):
            return
        accepted_scores = [
            float(resolution.score)
            for name in accepted
            if (resolution := ensemble.field(name)) is not None
        ]
        confidence = max(accepted_scores, default=float(candidate.provider_score))
        release_id = (
            candidate.release_id if "discogs_release_id" in accepted else None
        )
        master_id = (
            candidate.master_id if "discogs_master_id" in accepted else None
        )
        family_id = (
            provider_release_family_id
            if "provider_release_family_id" in accepted
            else None
        )
        release_title = candidate.album if "album" in accepted else None
        release_date = (
            candidate.release_date if "release_date" in accepted else None
        )
        original_release_date = (
            candidate.original_release_date
            if "original_release_date" in accepted
            else None
        )
        contextual = confidence >= 60.0 and bool(
            release_id or master_id or family_id or release_title
        )
        now = db.conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        db.conn.execute(
            """
            INSERT INTO track_release_context (
                track_id, discogs_release_id, discogs_master_id,
                provider_release_family_id, release_title,
                release_country, release_format, catalog_number, label_name,
                release_date, original_release_date, provider_reference,
                confidence, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                discogs_release_id=COALESCE(
                    track_release_context.discogs_release_id,
                    excluded.discogs_release_id
                ),
                discogs_master_id=COALESCE(
                    track_release_context.discogs_master_id,
                    excluded.discogs_master_id
                ),
                provider_release_family_id=COALESCE(
                    track_release_context.provider_release_family_id,
                    excluded.provider_release_family_id
                ),
                release_title=COALESCE(
                    track_release_context.release_title,excluded.release_title
                ),
                release_country=COALESCE(
                    track_release_context.release_country,excluded.release_country
                ),
                release_format=COALESCE(
                    track_release_context.release_format,excluded.release_format
                ),
                label_name=COALESCE(
                    track_release_context.label_name,excluded.label_name
                ),
                release_date=COALESCE(
                    track_release_context.release_date,excluded.release_date
                ),
                original_release_date=COALESCE(
                    track_release_context.original_release_date,
                    excluded.original_release_date
                ),
                provider_reference=COALESCE(
                    track_release_context.provider_reference,
                    excluded.provider_reference
                ),
                confidence=MAX(
                    COALESCE(track_release_context.confidence,0),
                    COALESCE(excluded.confidence,0)
                ),
                updated_at=excluded.updated_at
            """,
            (
                int(track_id),
                release_id,
                master_id,
                family_id,
                release_title,
                candidate.country if contextual else None,
                candidate.release_format if contextual else None,
                candidate.label if contextual else None,
                release_date,
                original_release_date,
                candidate.provider_reference,
                confidence,
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
        if candidate is None or self._hard_version_conflict(ensemble):
            return
        accepted_fields = {
            name
            for name in (
                "discogs_release_id",
                "discogs_master_id",
                "discogs_track_position",
                "provider_release_family_id",
            )
            if self._database_accepted_field(ensemble, name, "discogs")
        }
        values = {
            "discogs_release_id": candidate.release_id if "discogs_release_id" in accepted_fields else None,
            "discogs_master_id": candidate.master_id if "discogs_master_id" in accepted_fields else None,
            "discogs_track_position": (
                candidate.track_position if "discogs_track_position" in accepted_fields else None
            ),
            "recording_group_key": (
                ensemble.recording_group_key if accepted_fields else None
            ),
        }
        family_resolution = ensemble.field("provider_release_family_id")
        provider_release_family_id = (
            str(family_resolution.value).strip()
            if family_resolution is not None
            and self._database_accepted_field(
                ensemble, "provider_release_family_id", "discogs"
            )
            and family_resolution.value not in (None, "")
            else None
        )
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
        self._apply_release_context(
            db,
            track_id,
            candidate,
            ensemble,
            provider_release_family_id=provider_release_family_id,
        )

    def _apply_musicbrainz_identity(
        self,
        db: MusicVaultDB,
        track_id: int,
        ensemble: MetadataEnsemble,
    ) -> None:
        candidate = ensemble.musicbrainz_candidate
        if candidate is None or self._hard_version_conflict(ensemble):
            return
        recording_id = (
            getattr(candidate, "recording_id", None)
            if self._database_accepted_field(
                ensemble, "musicbrainz_recording_id", "musicbrainz"
            )
            else None
        )
        release_id = (
            getattr(candidate, "release_id", None)
            if self._database_accepted_field(
                ensemble, "musicbrainz_release_id", "musicbrainz"
            )
            else None
        )
        release_group_resolution = ensemble.field("musicbrainz_release_group_id")
        release_group_id = (
            str(release_group_resolution.value).strip()
            if release_group_resolution is not None
            and self._database_accepted_field(
                ensemble, "musicbrainz_release_group_id", "musicbrainz"
            )
            and release_group_resolution.value not in (None, "")
            else None
        )
        if (
            recording_id in (None, "")
            and release_id in (None, "")
            and release_group_id in (None, "")
        ):
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
        if release_group_id not in (None, ""):
            score = getattr(candidate, "provider_score", getattr(candidate, "score", None))
            db.conn.execute(
                """
                INSERT INTO track_release_context (
                    track_id,musicbrainz_release_group_id,release_title,
                    provider_reference,confidence,updated_at
                ) VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(track_id) DO UPDATE SET
                    musicbrainz_release_group_id=excluded.musicbrainz_release_group_id,
                    updated_at=excluded.updated_at
                """,
                (
                    int(track_id),
                    release_group_id,
                    getattr(candidate, "album", None),
                    getattr(candidate, "provider_reference", None),
                    float(score) if score is not None else None,
                ),
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

    @staticmethod
    def _credit_inputs_from_values(values: Sequence[object]) -> list[ArtistCreditInput]:
        inputs: list[ArtistCreditInput] = []
        previous_join = ""
        for index, raw in enumerate(values):
            value = _as_mapping(raw)
            name = str(value.get("name") or value.get("display_name") or "").strip()
            if not name:
                continue
            inputs.append(
                ArtistCreditInput(
                    display_name=name,
                    role=str(value.get("role") or "primary"),
                    join_phrase=previous_join if index else "",
                    entity_type=str(value.get("entity_type") or "unknown"),
                    discogs_artist_id=(
                        str(value.get("artist_id") or value.get("discogs_artist_id"))
                        if value.get("artist_id") or value.get("discogs_artist_id")
                        else None
                    ),
                    musicbrainz_artist_id=(
                        str(value.get("musicbrainz_artist_id"))
                        if value.get("musicbrainz_artist_id")
                        else None
                    ),
                )
            )
            previous_join = str(value.get("join_phrase") or "")
        return inputs

    def _write_tags(
        self,
        db: MusicVaultDB,
        item,
        result,
        *,
        high_confidence_fields: frozenset[str],
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
            if name in result.changed_fields
            and name in high_confidence_fields
            and getattr(approved, name) not in (None, "")
        }
        row = db.get_track(item.track_id)
        for name in (
            "discogs_release_id",
            "discogs_master_id",
            "musicbrainz_recording_id",
            "musicbrainz_release_id",
        ):
            if (
                name in high_confidence_fields
                and name in row.keys()
                and row[name] not in (None, "")
            ):
                patch[name] = row[name]
        if {"artist", "artist_credits"} & high_confidence_fields:
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
        adjudication = self._adjudicate_providers(
            db,
            snapshot,
            track,
            parsed,
            settings,
            cancel_event,
        )
        if cancel_event is not None and cancel_event.is_set():
            store.mark_item(item.id, "cancelled")
            return "cancelled"
        parsed = adjudication.parsed
        discogs_candidates = adjudication.discogs_candidates
        musicbrainz_candidates = adjudication.musicbrainz_candidates
        provider_failures = list(adjudication.provider_failures)
        token = adjudication.token

        youtube_exclusive = bool(
            snapshot.source_kind == "youtube"
            and parsed.strong_pattern
            and not discogs_candidates
            and not musicbrainz_candidates
            and (
                adjudication.orientation is None
                or adjudication.orientation.fallback_terminalizable
            )
        )
        release_context = db.conn.execute(
            "SELECT * FROM track_release_context WHERE track_id=?",
            (int(item.track_id),),
        ).fetchone()
        current = self._current_values(snapshot, track, release_context)
        locked = {
            name
            for name, field in snapshot.fields.items()
            if field.is_locked or field.is_manual
        }
        source_version = parsed.version_type
        top_discogs = discogs_candidates[0] if discogs_candidates else None
        unofficial_live = bool(
            source_version == "live"
            and (
                top_discogs is None
                or not bool(top_discogs.is_official)
                or not versions_compatible(
                    source_version,
                    getattr(top_discogs, "version_type", "unknown"),
                )
            )
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
        provider_duration_mismatch = {
            "discogs": False,
            "musicbrainz": False,
        }
        if track["duration_seconds"] is not None:
            local_duration = float(track["duration_seconds"])
            for provider_name, provider_candidate in (
                ("discogs", ensemble.discogs_candidate),
                ("musicbrainz", ensemble.musicbrainz_candidate),
            ):
                candidate_duration = getattr(
                    provider_candidate, "duration_seconds", None
                )
                if candidate_duration is None:
                    continue
                if abs(local_duration - float(candidate_duration)) > max(
                    30.0, local_duration * 0.2
                ):
                    provider_duration_mismatch[provider_name] = True
        artwork_record, artwork_result = self._discogs_artwork_for_gap(
            token=token,
            candidate=ensemble.discogs_candidate,
            snapshot=snapshot,
            settings=settings,
            accepted=bool(
                not ensemble.provider_disagreement
                and "version_identity_conflict" not in ensemble.reasons
                and not provider_duration_mismatch["discogs"]
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
        if adjudication.orientation is not None:
            proposals["_orientation"] = adjudication.orientation.to_dict()
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
                conflict=(
                    field.conflict
                    or bool(
                        field.source in provider_duration_mismatch
                        and provider_duration_mismatch[field.source]
                    )
                ),
            )
            for field in ensemble.fields
            if field.field_name in EDITABLE_METADATA_FIELDS
            and field.field_name != "artwork"
            and field.value not in (None, "")
            and field.action is not FieldAction.REVIEW
        }
        high_confidence_fields = frozenset(
            field.field_name
            for field in ensemble.fields
            if field.safe_to_apply
            and not field.conflict
            and not (
                field.source in provider_duration_mismatch
                and provider_duration_mismatch[field.source]
            )
        )
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
                    minimum_confidence=60,
                    reason="best_available_automatic_metadata",
                    commit=False,
                )
                if not provider_duration_mismatch["discogs"]:
                    self._apply_provider_identity(db, item.track_id, ensemble)
                if not provider_duration_mismatch["musicbrainz"]:
                    self._apply_musicbrainz_identity(db, item.track_id, ensemble)
                credits_field = ensemble.field("artist_credits")
                credit_inputs: list[ArtistCreditInput] = []
                credit_provenance = ""
                credit_reference = None
                if (
                    candidate is not None
                    and not provider_duration_mismatch["discogs"]
                    and credits_field is not None
                    and credits_field.source == "discogs"
                    and credits_field.score >= 60.0
                    and not credits_field.conflict
                    and credits_field.action is not FieldAction.REVIEW
                    and candidate.artist_credits
                ):
                    credit_inputs = self._credit_inputs(candidate)
                    credit_provenance = "discogs_best_available"
                    credit_reference = candidate.provider_reference
                elif (
                    credits_field is not None
                    and credits_field.source == "youtube_title_parsed"
                    and credits_field.score >= 60.0
                    and not credits_field.conflict
                    and credits_field.action is not FieldAction.REVIEW
                    and isinstance(credits_field.value, Sequence)
                ):
                    credit_inputs = self._credit_inputs_from_values(
                        credits_field.value
                    )
                    credit_provenance = "youtube_title_parsed"
                latest_artist = metadata.snapshot(item.track_id).fields["artist"]
                protected_credit = db.conn.execute(
                    "SELECT 1 FROM track_artist_credits "
                    "WHERE track_id=? AND (is_manual=1 OR is_locked=1) LIMIT 1",
                    (int(item.track_id),),
                ).fetchone()
                if (
                    credit_inputs
                    and not latest_artist.is_manual
                    and not latest_artist.is_locked
                    and protected_credit is None
                ):
                    ArtistCreditService(db).replace_track_credits(
                        item.track_id,
                        credit_inputs,
                        provenance=credit_provenance,
                        provider_reference=credit_reference,
                        confidence=credits_field.score,
                        commit=False,
                    )
                # Build the album identity only after both durable release IDs
                # and accepted structured credits are saved.  A fallback album
                # must use the new primary credit, never the legacy combined
                # artist display that the accepted evidence just replaced.
                upsert_track_canonical_album(db.conn, int(item.track_id))
                if settings.get("metadata_writeback_enabled") is True and result.changed:
                    file_write_result, committed_tags = self._write_tags(
                        db,
                        item,
                        result,
                        high_confidence_fields=high_confidence_fields,
                    )
        except TagWriteError:
            # Preparation/commit failures restore internally; the surrounding
            # SQLite context rolls back fields, IDs, release context and credits.
            file_write_result = "restored"
            store.mark_item(
                item.id,
                "failed",
                parsed_hints=self._parsed_summary(
                    parsed, uploader, adjudication.orientation
                ),
                discogs_release_id=(candidate.release_id if candidate else None),
                discogs_master_id=(candidate.master_id if candidate else None),
                field_proposal=proposals,
                field_confidence=confidences,
                provider_agreement=self._agreement(ensemble),
                review_reason="file_write_rollback_failure",
                file_write_result=file_write_result,
                artwork_result=artwork_result,
                error="media_tag_write_failed",
            )
            return "failed"
        except Exception:
            if committed_tags is not None:
                self._restore_committed_tags(committed_tags)
            raise

        parsed_summary = self._parsed_summary(
            parsed, uploader, adjudication.orientation
        )
        decision = classify_ensemble_outcome(
            ensemble,
            current=current,
            parsed_hints=parsed_summary,
            changed=result.changed,
            youtube_exclusive=youtube_exclusive,
            provider_failures=provider_failures,
            local_duration=(
                float(track["duration_seconds"])
                if track["duration_seconds"] is not None
                else None
            ),
        )
        state = decision.outcome.value
        review_reason = decision.reason
        proposals["_review_policy"] = decision.to_dict()
        candidate = ensemble.discogs_candidate
        mb = ensemble.musicbrainz_candidate
        store.mark_item(
            item.id,
            state,
            parsed_hints=parsed_summary,
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
            error=(
                provider_failures[0]
                if decision.outcome is ReviewOutcome.FAILED and provider_failures
                else None
            ),
        )
        if state in {"applied", "applied_with_gaps"}:
            # Relationship evidence is already normalized and persisted above.
            # This importer performs no provider lookup and rejects an invalid
            # item without creating a partial member-of graph.
            from .artist_relationships import (
                import_accepted_saved_artist_relationships,
            )

            import_accepted_saved_artist_relationships(db, (item.id,))
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
        processed = applied = applied_with_gaps = source_fallback = 0
        review = no_match = failed = 0
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
            applied += int(state in {"applied", "applied_with_gaps"})
            applied_with_gaps += int(state == "applied_with_gaps")
            source_fallback += int(state == "source_fallback")
            review += int(state == "review")
            no_match += int(state == "no_match")
            failed += int(state == "failed")
        return IntelligenceRunResult(
            job_id,
            processed,
            applied,
            review,
            no_match,
            failed,
            cancelled,
            applied_with_gaps,
            source_fallback,
        )

    def process_automatic_queue(
        self,
        *,
        job_id: str = AUTOMATIC_IMPORT_JOB_ID,
        cancel_event: threading.Event | None = None,
    ) -> IntelligenceRunResult:
        if str(job_id) != AUTOMATIC_IMPORT_JOB_ID:
            raise ValueError("automatic_metadata_job_id_invalid")
        if not self.runtime_policy.background_provider_work_allowed:
            return IntelligenceRunResult(job_id, 0, 0, 0, 0, 0)
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
        if not self.runtime_policy.background_provider_work_allowed:
            raise RuntimeError("metadata_provider_work_deferred")
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
        if not self.runtime_policy.background_provider_work_allowed:
            raise RuntimeError("metadata_provider_work_deferred")
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

from __future__ import annotations

import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .intelligence_schema import ARTIST_CREDIT_ROLES, ARTIST_ENTITY_TYPES


_SPACE_RE = re.compile(r"\s+")
_REPAIR_ALIAS_KINDS = (
    "corrected_version_suffix",
    "legacy_credit_string",
    "display_variant",
)


def normalize_artist_name(value: object) -> str:
    """Return a stable fallback identity while preserving punctuation semantics."""

    display = _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", str(value or ""))).strip()
    if not display:
        raise ValueError("Artist display name cannot be empty.")
    return display.casefold()


def _display_name(value: object) -> str:
    display = _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", str(value or ""))).strip()
    if not display:
        raise ValueError("Artist display name cannot be empty.")
    return display


def _provider_id(value: object) -> str | None:
    identifier = str(value or "").strip()
    return identifier or None


@dataclass(frozen=True)
class Artist:
    id: int
    display_name: str
    normalized_name: str
    sort_name: str
    entity_type: str
    discogs_artist_id: str | None
    musicbrainz_artist_id: str | None


@dataclass(frozen=True)
class ArtistCreditInput:
    display_name: str
    role: str = "primary"
    join_phrase: str = ""
    entity_type: str = "unknown"
    discogs_artist_id: str | None = None
    musicbrainz_artist_id: str | None = None


@dataclass(frozen=True)
class TrackArtistCredit:
    id: int
    track_id: int
    artist: Artist
    role: str
    credit_order: int
    join_phrase: str
    provenance: str
    provider_reference: str | None
    confidence: float | None
    is_manual: bool
    is_locked: bool


def _artist_from_row(row: sqlite3.Row) -> Artist:
    return Artist(
        id=int(row["artist_id"] if "artist_id" in row.keys() else row["id"]),
        display_name=str(row["display_name"]),
        normalized_name=str(row["normalized_name"]),
        sort_name=str(row["sort_name"]),
        entity_type=str(row["entity_type"]),
        discogs_artist_id=row["discogs_artist_id"],
        musicbrainz_artist_id=row["musicbrainz_artist_id"],
    )


def _repair_alias_candidate(
    conn: sqlite3.Connection,
    normalized_alias: str,
) -> tuple[sqlite3.Row | None, bool]:
    """Resolve only explicit repair aliases, failing closed on ambiguity."""

    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artist_aliases'"
    ).fetchone()
    if table is None:
        return None, False
    rows = conn.execute(
        """
        SELECT artist.*
        FROM artist_aliases alias
        JOIN artists artist ON artist.id=alias.artist_id
        WHERE alias.normalized_alias=?
          AND alias.alias_kind IN (?, ?, ?)
        ORDER BY artist.id
        """,
        (normalized_alias, *_REPAIR_ALIAS_KINDS),
    ).fetchall()
    candidates = {int(row["id"]): row for row in rows}
    if len(candidates) == 1:
        return next(iter(candidates.values())), False
    return None, len(candidates) > 1


def seed_existing_artist_credits(
    conn: sqlite3.Connection,
    track_ids: Iterable[int] | None = None,
) -> None:
    """Create exactly one conservative credit for each non-empty legacy artist."""

    conn.row_factory = sqlite3.Row
    ids = list(dict.fromkeys(int(value) for value in track_ids or ()))
    if track_ids is not None and not ids:
        return
    identity_filter = (
        f"AND t.id IN ({','.join('?' for _ in ids)})" if track_ids is not None else ""
    )
    rows = conn.execute(
        f"""
        SELECT t.id, t.artist, t.updated_at,
               f.provenance, f.provider_reference, f.confidence,
               COALESCE(f.is_manual, 0) AS is_manual,
               COALESCE(f.is_locked, 0) AS is_locked,
               f.updated_at AS field_updated_at
        FROM tracks t
        LEFT JOIN track_metadata_fields f
          ON f.track_id=t.id AND f.field_name='artist'
        WHERE NULLIF(TRIM(t.artist), '') IS NOT NULL
          {identity_filter}
          AND NOT EXISTS (
              SELECT 1 FROM track_artist_credits credit WHERE credit.track_id=t.id
          )
        ORDER BY t.id
        """,
        ids,
    ).fetchall()
    for row in rows:
        display = _display_name(row["artist"])
        from .soundtrack import is_various_artists

        if is_various_artists(display):
            # Various Artists is release context, not a performer identity.
            # Preserve the flat legacy display while avoiding a fabricated
            # primary artist entity during future schema migrations.
            continue
        normalized = normalize_artist_name(display)
        timestamp = str(row["field_updated_at"] or row["updated_at"] or "1970-01-01T00:00:00Z")
        artist_row, alias_ambiguous = _repair_alias_candidate(conn, normalized)
        if alias_ambiguous:
            # The legacy display string remains available on the track, but
            # an ambiguous alias must never silently recreate or mis-credit an
            # artist identity.
            continue
        if artist_row is None:
            artist_row = conn.execute(
                """
                SELECT id FROM artists
                WHERE normalized_name=?
                  AND NULLIF(TRIM(discogs_artist_id), '') IS NULL
                  AND NULLIF(TRIM(musicbrainz_artist_id), '') IS NULL
                ORDER BY id
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
        if artist_row is None:
            artist_id = int(
                conn.execute(
                    """
                    INSERT INTO artists (
                        display_name, normalized_name, sort_name, entity_type,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, 'unknown', ?, ?)
                    """,
                    (display, normalized, normalized, timestamp, timestamp),
                ).lastrowid
            )
        else:
            artist_id = int(artist_row[0])
        conn.execute(
            """
            INSERT INTO track_artist_credits (
                track_id, artist_id, role, credit_order, join_phrase,
                provenance, provider_reference, confidence, is_manual, is_locked,
                created_at, updated_at
            ) VALUES (?, ?, 'primary', 0, '', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["id"]),
                artist_id,
                str(row["provenance"] or "unknown"),
                row["provider_reference"],
                row["confidence"],
                int(row["is_manual"]),
                int(row["is_locked"]),
                timestamp,
                timestamp,
            ),
        )


class ArtistCreditService:
    def __init__(self, database: Any) -> None:
        self.conn: sqlite3.Connection = getattr(database, "conn", database)
        if not isinstance(self.conn, sqlite3.Connection):
            raise TypeError("ArtistCreditService requires a SQLite connection.")
        self.conn.row_factory = sqlite3.Row

    @contextmanager
    def _transaction(self, *, commit: bool = True):
        if not commit:
            yield
            return
        if not self.conn.in_transaction:
            with self.conn:
                yield
            return
        name = f"artist_credits_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self.conn.execute(f"RELEASE SAVEPOINT {name}")

    def upsert_artist(
        self,
        display_name: object,
        *,
        entity_type: str = "unknown",
        discogs_artist_id: object = None,
        musicbrainz_artist_id: object = None,
        commit: bool = True,
    ) -> Artist:
        display = _display_name(display_name)
        normalized = normalize_artist_name(display)
        kind = str(entity_type or "unknown").strip().casefold()
        if kind not in ARTIST_ENTITY_TYPES:
            raise ValueError(f"Unsupported artist entity type: {entity_type}")
        discogs_id = _provider_id(discogs_artist_id)
        musicbrainz_id = _provider_id(musicbrainz_artist_id)
        provider_candidates: list[sqlite3.Row] = []
        if discogs_id is not None:
            row = self.conn.execute(
                "SELECT * FROM artists WHERE discogs_artist_id=?", (discogs_id,)
            ).fetchone()
            if row is not None:
                provider_candidates.append(row)
        if musicbrainz_id is not None:
            row = self.conn.execute(
                "SELECT * FROM artists WHERE musicbrainz_artist_id=?", (musicbrainz_id,)
            ).fetchone()
            if row is not None:
                provider_candidates.append(row)
        identity_ids = {int(row["id"]) for row in provider_candidates}
        if len(identity_ids) > 1:
            raise ValueError("Provider IDs resolve to conflicting artist identities.")

        candidate = provider_candidates[0] if provider_candidates else None
        candidate_from_alias = False
        if candidate is not None:
            alias_candidate, alias_ambiguous = _repair_alias_candidate(
                self.conn, normalized
            )
            if alias_ambiguous:
                raise ValueError("Artist repair alias resolves to multiple identities.")
            if alias_candidate is not None:
                if int(alias_candidate["id"]) != int(candidate["id"]):
                    raise ValueError(
                        "Provider identity conflicts with a corrected artist alias."
                    )
                candidate_from_alias = True
        if candidate is None and discogs_id is None and musicbrainz_id is None:
            candidate, alias_ambiguous = _repair_alias_candidate(self.conn, normalized)
            if alias_ambiguous:
                raise ValueError("Artist repair alias resolves to multiple identities.")
            candidate_from_alias = candidate is not None
        if candidate is None and discogs_id is None and musicbrainz_id is None:
            # Name-only edits use (or create) one deterministic provider-free
            # fallback.  A new provider identity deliberately does not claim a
            # legacy name row: that row may be shared by other tracks whose
            # identity has not been established yet.
            candidate = self.conn.execute(
                """
                SELECT * FROM artists
                WHERE normalized_name=?
                  AND NULLIF(TRIM(discogs_artist_id), '') IS NULL
                  AND NULLIF(TRIM(musicbrainz_artist_id), '') IS NULL
                ORDER BY id
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()

        if candidate is not None:
            stored_discogs = _provider_id(candidate["discogs_artist_id"])
            stored_musicbrainz = _provider_id(candidate["musicbrainz_artist_id"])
            if discogs_id is not None and stored_discogs not in (None, discogs_id):
                raise ValueError("Discogs artist ID cannot be reassigned.")
            if (
                musicbrainz_id is not None
                and stored_musicbrainz not in (None, musicbrainz_id)
            ):
                raise ValueError("MusicBrainz artist ID cannot be reassigned.")

        now = datetime_now()
        with self._transaction(commit=commit):
            if candidate is not None:
                artist_id = int(candidate["id"])
                if candidate_from_alias:
                    # A corrected legacy spelling is lookup evidence, not a
                    # request to rename the canonical artist back to the
                    # malformed display string.
                    self.conn.execute(
                        """
                        UPDATE artists SET
                            entity_type=CASE WHEN entity_type='unknown' THEN ? ELSE entity_type END,
                            updated_at=?
                        WHERE id=?
                        """,
                        (kind, now, artist_id),
                    )
                else:
                    self.conn.execute(
                        """
                        UPDATE artists SET
                            display_name=?, normalized_name=?, sort_name=?,
                            entity_type=CASE WHEN entity_type='unknown' THEN ? ELSE entity_type END,
                            discogs_artist_id=COALESCE(discogs_artist_id, ?),
                            musicbrainz_artist_id=COALESCE(musicbrainz_artist_id, ?),
                            updated_at=?
                        WHERE id=?
                        """,
                        (
                            display,
                            normalized,
                            normalized,
                            kind,
                            discogs_id,
                            musicbrainz_id,
                            now,
                            artist_id,
                        ),
                    )
            else:
                artist_id = int(
                    self.conn.execute(
                        """
                        INSERT INTO artists (
                            display_name, normalized_name, sort_name, entity_type,
                            discogs_artist_id, musicbrainz_artist_id, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            display,
                            normalized,
                            normalized,
                            kind,
                            discogs_id,
                            musicbrainz_id,
                            now,
                            now,
                        ),
                    ).lastrowid
                )
            stored = self.conn.execute("SELECT * FROM artists WHERE id=?", (artist_id,)).fetchone()
        assert stored is not None
        return _artist_from_row(stored)

    @staticmethod
    def _coerce_credit(value: ArtistCreditInput | Mapping[str, object]) -> ArtistCreditInput:
        if isinstance(value, ArtistCreditInput):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("Artist credits must be ArtistCreditInput or mapping values.")
        return ArtistCreditInput(
            display_name=str(value.get("display_name") or ""),
            role=str(value.get("role") or "primary"),
            join_phrase=str(value.get("join_phrase") or ""),
            entity_type=str(value.get("entity_type") or "unknown"),
            discogs_artist_id=(
                str(value["discogs_artist_id"])
                if value.get("discogs_artist_id") not in (None, "")
                else None
            ),
            musicbrainz_artist_id=(
                str(value["musicbrainz_artist_id"])
                if value.get("musicbrainz_artist_id") not in (None, "")
                else None
            ),
        )

    @staticmethod
    def formatted_credit(credits: Sequence[TrackArtistCredit]) -> str:
        parts: list[str] = []
        for index, credit in enumerate(credits):
            if index == 0:
                parts.append(credit.artist.display_name)
                continue
            join = credit.join_phrase or ", "
            if join and not join[0].isspace() and join not in {",", "/", "&", "x"}:
                join = f" {join} "
            elif join in {",", "/", "&", "x"}:
                join = f" {join} " if join != "," else ", "
            parts.append(f"{join}{credit.artist.display_name}")
        return "".join(parts).strip()

    def replace_track_credits(
        self,
        track_id: int,
        credits: Iterable[ArtistCreditInput | Mapping[str, object]],
        *,
        provenance: str,
        provider_reference: object = None,
        confidence: float | None = None,
        is_manual: bool = False,
        is_locked: bool = False,
        actor: str = "metadata_intelligence",
        reason: str = "artist_credit_update",
        update_display: bool = True,
        commit: bool = True,
    ) -> tuple[TrackArtistCredit, ...]:
        if self.conn.execute("SELECT 1 FROM tracks WHERE id=?", (int(track_id),)).fetchone() is None:
            raise KeyError(f"Track {track_id} does not exist.")
        prepared = [self._coerce_credit(value) for value in credits]
        if not prepared or not any(value.role.strip().casefold() == "primary" for value in prepared):
            raise ValueError("At least one primary artist credit is required.")
        identities: set[tuple[str, str, str]] = set()
        for value in prepared:
            role = value.role.strip().casefold()
            if role not in ARTIST_CREDIT_ROLES:
                raise ValueError(f"Unsupported artist-credit role: {value.role}")
            discogs_id = _provider_id(value.discogs_artist_id)
            musicbrainz_id = _provider_id(value.musicbrainz_artist_id)
            if discogs_id is not None:
                identity = (f"discogs:{discogs_id}", role, "provider")
            elif musicbrainz_id is not None:
                identity = (f"musicbrainz:{musicbrainz_id}", role, "provider")
            else:
                identity = (normalize_artist_name(value.display_name), role, "name")
            if identity in identities:
                raise ValueError("Duplicate artist-credit rows are not allowed.")
            identities.add(identity)
            if len(value.join_phrase) > 80:
                raise ValueError("Artist join phrase is too long.")
        score = float(confidence) if confidence is not None else None
        if score is not None and not 0 <= score <= 100:
            raise ValueError("Artist-credit confidence must be between 0 and 100.")

        # Structured automatic credits must never bypass the effective artist lock.
        field = self.conn.execute(
            "SELECT is_manual,is_locked FROM track_metadata_fields "
            "WHERE track_id=? AND field_name='artist'",
            (int(track_id),),
        ).fetchone()
        if (
            not is_manual
            and field is not None
            and (bool(field["is_manual"]) or bool(field["is_locked"]))
        ):
            return self.track_credits(track_id)
        existing_credits = self.track_credits(track_id)
        if not is_manual and any(
            credit.is_manual or credit.is_locked for credit in existing_credits
        ):
            return existing_credits
        if (
            not is_manual
            and str(provenance or "").strip().casefold()
            in {"youtube_title_parsed", "adjudicated_source_title"}
            and any(credit.role != "primary" for credit in existing_credits)
        ):
            # A parsed source-title credit may refine a lone legacy primary,
            # but it must not erase richer structured provider/manual roles.
            return existing_credits

        reference = (
            str(provider_reference).strip()
            if provider_reference not in (None, "")
            else None
        )
        now = datetime_now()
        with self._transaction(commit=commit):
            artist_ids: list[int] = []
            resolved_identities: set[tuple[int, str]] = set()
            for value in prepared:
                artist = self.upsert_artist(
                    value.display_name,
                    entity_type=value.entity_type,
                    discogs_artist_id=value.discogs_artist_id,
                    musicbrainz_artist_id=value.musicbrainz_artist_id,
                    commit=False,
                )
                resolved_identity = (artist.id, value.role.strip().casefold())
                if resolved_identity in resolved_identities:
                    raise ValueError("Duplicate artist-credit rows are not allowed.")
                resolved_identities.add(resolved_identity)
                artist_ids.append(artist.id)
            self.conn.execute("DELETE FROM track_artist_credits WHERE track_id=?", (int(track_id),))
            for order, (value, artist_id) in enumerate(zip(prepared, artist_ids, strict=True)):
                self.conn.execute(
                    """
                    INSERT INTO track_artist_credits (
                        track_id, artist_id, role, credit_order, join_phrase,
                        provenance, provider_reference, confidence, is_manual, is_locked,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(track_id),
                        artist_id,
                        value.role.strip().casefold(),
                        order,
                        value.join_phrase,
                        str(provenance or "unknown").strip().casefold(),
                        reference,
                        score,
                        int(is_manual),
                        int(is_locked),
                        now,
                        now,
                    ),
                )
            stored = self.track_credits(track_id)
            if update_display:
                display = self.formatted_credit(stored)
                from .service import MetadataAction, MetadataService

                metadata = MetadataService(self.conn)
                if is_manual:
                    metadata.apply_actions(
                        track_id,
                        {"artist": MetadataAction.set(display)},
                        actor=actor,
                        reason=reason,
                        commit=False,
                    )
                else:
                    metadata.record_source_observations(
                        track_id,
                        provider=str(provenance or "unknown"),
                        values={"artist": display},
                        provider_reference=reference,
                        confidence=score,
                        apply_effective=True,
                        actor=actor,
                        reason=reason,
                        commit=False,
                    )
        return self.track_credits(track_id)

    def track_credits(self, track_id: int) -> tuple[TrackArtistCredit, ...]:
        rows = self.conn.execute(
            """
            SELECT credit.*, artist.id AS artist_id, artist.display_name,
                   artist.normalized_name, artist.sort_name, artist.entity_type,
                   artist.discogs_artist_id, artist.musicbrainz_artist_id
            FROM track_artist_credits credit
            JOIN artists artist ON artist.id=credit.artist_id
            WHERE credit.track_id=?
            ORDER BY credit.credit_order, credit.id
            """,
            (int(track_id),),
        ).fetchall()
        return tuple(
            TrackArtistCredit(
                id=int(row["id"]),
                track_id=int(row["track_id"]),
                artist=_artist_from_row(row),
                role=str(row["role"]),
                credit_order=int(row["credit_order"]),
                join_phrase=str(row["join_phrase"]),
                provenance=str(row["provenance"]),
                provider_reference=row["provider_reference"],
                confidence=(float(row["confidence"]) if row["confidence"] is not None else None),
                is_manual=bool(row["is_manual"]),
                is_locked=bool(row["is_locked"]),
            )
            for row in rows
        )


def datetime_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "Artist",
    "ArtistCreditInput",
    "TrackArtistCredit",
    "ArtistCreditService",
    "normalize_artist_name",
    "seed_existing_artist_credits",
]

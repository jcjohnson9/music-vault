"""Conservative canonical-artist consolidation for Music Vault.

This module deliberately keeps artist identity repair separate from browser
queries.  It can describe every proposed mutation without writing, and its
apply path uses one SQLite transaction.  Provider conflicts, person/group
conflicts, locked credit strings, and ambiguous same-name identities remain
untouched and are returned as aggregate-safe diagnostics.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, Sequence

from .artist_credits import ArtistCreditInput, ArtistCreditService, normalize_artist_name
from .service import MetadataService
from .title_parser import classify_artist_version_label


_PRESENTATION_SEPARATOR_RE = re.compile(r"[\s\-_.‐‑‒–—―'\"`‘’“”]+")
_SPACE_RE = re.compile(r"\s+")
_VERSION_SUFFIX_RE = re.compile(
    r"^(?P<artist>.+?)\s+(?P<label>"
    r"live\s+(?:at|in|from)\s+.+|"
    r"(?:live\s+)?concert(?:\s+.+)?|"
    r"acoustic\s+session(?:\s+.+)?|"
    r"studio\s+session(?:\s+.+)?|"
    r"radio\s+session(?:\s+.+)?|"
    r"tiny\s+desk(?:\s+concert)?|"
    r"festival(?:\s+.+)?"
    r")$",
    re.IGNORECASE,
)
_EXPLICIT_CREDIT_PHRASE_RE = re.compile(
    r"^(?P<primary>.+?)\s+(?P<join>feat\.?|ft\.?|featuring|with|x|vs\.?)\s+"
    r"(?P<related>.+)$",
    re.IGNORECASE,
)
_GROUP_TYPES = frozenset({"group", "band", "duo", "orchestra", "collective"})
_PERSON_TYPES = frozenset({"person"})
_MUSICBRAINZ_ARTIST_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_artist_presentation(value: object) -> str:
    """Return a punctuation-tolerant comparison key without splitting names.

    Ampersands, plus signs, commas, and slashes intentionally remain semantic;
    they are never treated as credit separators by consolidation.
    """

    display = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    display = _PRESENTATION_SEPARATOR_RE.sub(" ", display)
    return _SPACE_RE.sub(" ", display).strip()


def _clean_display(value: object) -> str:
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def _provider_id(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_names(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    escaped = table.replace('"', '""')
    return frozenset(str(row[1]) for row in conn.execute(f'PRAGMA table_info("{escaped}")'))


def _strict_proposal_artist_id(value: object, provider: str) -> str | None:
    """Validate one provider artist ID without coercing malformed JSON values."""

    if value is None or isinstance(value, (bool, Mapping, Sequence)) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return None
    if isinstance(value, (bytes, bytearray)):
        return None
    text = str(value).strip()
    if not text:
        return None
    if provider == "discogs":
        return text if re.fullmatch(r"[1-9]\d{0,17}", text) else None
    return text if _MUSICBRAINZ_ARTIST_ID_RE.fullmatch(text) else None


@dataclass(frozen=True, slots=True)
class ArtistMerge:
    canonical_artist_id: int
    duplicate_artist_ids: tuple[int, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArtistIdentityConflict:
    artist_ids: tuple[int, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class VersionArtistRepair:
    track_id: int
    malformed_artist_id: int
    canonical_artist_id: int
    canonical_display_name: str
    version_type: str
    version_label: str
    evidence: str


@dataclass(frozen=True, slots=True)
class StructuredCredit:
    display_name: str
    role: str
    join_phrase: str = ""
    entity_type: str = "unknown"
    discogs_artist_id: str | None = None
    musicbrainz_artist_id: str | None = None


@dataclass(frozen=True, slots=True)
class FullCreditRepair:
    track_id: int
    malformed_artist_id: int
    credits: tuple[StructuredCredit, ...]
    provenance: str
    provider_reference: str | None
    confidence: float


@dataclass(frozen=True, slots=True)
class ArtistConsolidationPlan:
    merges: tuple[ArtistMerge, ...] = ()
    conflicts: tuple[ArtistIdentityConflict, ...] = ()
    version_repairs: tuple[VersionArtistRepair, ...] = ()
    full_credit_repairs: tuple[FullCreditRepair, ...] = ()

    @property
    def duplicate_artist_count(self) -> int:
        return sum(len(item.duplicate_artist_ids) for item in self.merges)


@dataclass(frozen=True, slots=True)
class ArtistConsolidationReport:
    dry_run: bool
    merge_group_count: int
    merged_artist_count: int
    reassigned_credit_count: int
    aliases_preserved: int
    relationships_preserved: int
    version_repairs: int
    full_credit_repairs: int
    conflict_count: int
    deleted_artist_count: int = 0
    deleted_credit_count: int = 0


class ArtistConsolidationError(RuntimeError):
    """Raised when a planned consolidation can no longer be applied safely."""


class ArtistConsolidationService:
    """Plan and apply safe canonical artist identity corrections."""

    def __init__(
        self,
        database: object,
        *,
        portrait_available: Callable[[str], bool] | None = None,
        portrait_rekey: Callable[[str, str], None] | None = None,
    ) -> None:
        self.conn: sqlite3.Connection = getattr(database, "conn", database)
        if not isinstance(self.conn, sqlite3.Connection):
            raise TypeError("ArtistConsolidationService requires a SQLite connection.")
        self.conn.row_factory = sqlite3.Row
        self.portrait_available = portrait_available or (lambda _name: False)
        self.portrait_rekey = portrait_rekey

    def _require_schema(self) -> None:
        required = {
            "artists",
            "track_artist_credits",
            "artist_aliases",
            "artist_relationships",
        }
        missing = sorted(name for name in required if not _table_exists(self.conn, name))
        if missing:
            raise ArtistConsolidationError(
                "Canonical artist schema is unavailable: " + ", ".join(missing)
            )

    @contextmanager
    def _transaction(self):
        if not self.conn.in_transaction:
            with self.conn:
                yield
            return
        name = f"artist_consolidation_{uuid.uuid4().hex}"
        self.conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.conn.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self.conn.execute(f"RELEASE SAVEPOINT {name}")

    def _artist_rows(self) -> tuple[sqlite3.Row, ...]:
        return tuple(
            self.conn.execute(
                """
                SELECT a.*,
                       COUNT(DISTINCT CASE WHEN c.role='primary' THEN c.track_id END)
                           AS primary_usage
                FROM artists a
                LEFT JOIN track_artist_credits c ON c.artist_id=a.id
                GROUP BY a.id
                ORDER BY a.id
                """
            ).fetchall()
        )

    @staticmethod
    def _types_conflict(rows: Sequence[sqlite3.Row]) -> bool:
        kinds = {str(row["entity_type"] or "unknown").casefold() for row in rows}
        return bool(kinds & _GROUP_TYPES and kinds & _PERSON_TYPES)

    @staticmethod
    def _provider_conflict(rows: Sequence[sqlite3.Row], column: str) -> bool:
        values = {_provider_id(row[column]) for row in rows}
        values.discard(None)
        return len(values) > 1

    @staticmethod
    def _ambiguous_exact_name(rows: Sequence[sqlite3.Row]) -> bool:
        """Fail closed for same-display identities without shared provider proof.

        Case, spacing, and conservative punctuation variants are useful merge
        evidence. Two rows with the exact same stored display, however, may be
        distinct same-name artists. Provider equality can prove that pair;
        provider absence cannot.
        """

        for index, left in enumerate(rows):
            left_display = unicodedata.normalize(
                "NFKC", str(left["display_name"] or "")
            ).strip()
            for right in rows[index + 1 :]:
                right_display = unicodedata.normalize(
                    "NFKC", str(right["display_name"] or "")
                ).strip()
                if left_display != right_display:
                    continue
                same_discogs = bool(
                    _provider_id(left["discogs_artist_id"])
                    and _provider_id(left["discogs_artist_id"])
                    == _provider_id(right["discogs_artist_id"])
                )
                same_musicbrainz = bool(
                    _provider_id(left["musicbrainz_artist_id"])
                    and _provider_id(left["musicbrainz_artist_id"])
                    == _provider_id(right["musicbrainz_artist_id"])
                )
                provider_backed_pair = bool(
                    _provider_id(left["discogs_artist_id"])
                    or _provider_id(left["musicbrainz_artist_id"])
                    or _provider_id(right["discogs_artist_id"])
                    or _provider_id(right["musicbrainz_artist_id"])
                )
                # A provider-backed replacement may safely absorb an otherwise
                # unqualified legacy row, and complementary Discogs/MB rows are
                # intentional Batch 10.5 evidence. Two unqualified rows with an
                # identical stored display remain ambiguous: the browser can
                # cluster them without deleting either stored identity.
                if not (same_discogs or same_musicbrainz or provider_backed_pair):
                    return True
        return False

    def _canonical_sort_key(
        self, row: sqlite3.Row
    ) -> tuple[int, int, int, int, int, int, int]:
        discogs = bool(_provider_id(row["discogs_artist_id"]))
        musicbrainz = bool(_provider_id(row["musicbrainz_artist_id"]))
        return (
            -int(self.portrait_available(str(row["display_name"]))),
            -(int(discogs) + int(musicbrainz)),
            -int(discogs and musicbrainz),
            -int(discogs),
            -int(musicbrainz),
            -int(row["primary_usage"] or 0),
            int(row["id"]),
        )

    def _credit_collision(self, artist_ids: Sequence[int]) -> bool:
        placeholders = ",".join("?" for _ in artist_ids)
        rows = self.conn.execute(
            f"""
            SELECT track_id,role,credit_order,join_phrase,provenance,
                   provider_reference,confidence,is_manual,is_locked
            FROM track_artist_credits
            WHERE artist_id IN ({placeholders})
            ORDER BY track_id,role,id
            """,
            tuple(int(value) for value in artist_ids),
        ).fetchall()
        grouped: dict[tuple[int, str], list[tuple[object, ...]]] = {}
        for row in rows:
            grouped.setdefault(
                (int(row["track_id"]), str(row["role"])), []
            ).append(self._credit_evidence(row))
        return any(
            len(values) > 1 and len(set(values)) > 1
            for values in grouped.values()
        )

    @staticmethod
    def _credit_evidence(row: Mapping[str, object]) -> tuple[object, ...]:
        return (
            int(row["credit_order"]),
            str(row["join_phrase"] or ""),
            str(row["provenance"] or ""),
            str(row["provider_reference"] or ""),
            float(row["confidence"]) if row["confidence"] is not None else None,
            int(row["is_manual"] or 0),
            int(row["is_locked"] or 0),
        )

    def _accepted_provider_context_conflict(
        self, rows: Sequence[sqlite3.Row]
    ) -> str | None:
        """Return a diagnostic when accepted track evidence forbids a merge.

        This deliberately reads only normalized summaries already saved in
        SQLite.  It never constructs a provider client.  Presentation-only
        consolidation must fail closed when an accepted item is malformed,
        cannot be attributed to the credited artist, or identifies component
        artists as different Discogs/MusicBrainz entities.
        """

        if not _table_exists(self.conn, "metadata_intelligence_items"):
            return None
        artist_ids = tuple(sorted(int(row["id"]) for row in rows))
        if not artist_ids:
            return None
        display_keys = {
            int(row["id"]): normalize_artist_presentation(row["display_name"])
            for row in rows
        }
        placeholders = ",".join("?" for _ in artist_ids)
        accepted = self.conn.execute(
            f"""
            SELECT credit.artist_id, item.field_proposal, item.provider_agreement
            FROM track_artist_credits AS credit
            JOIN metadata_intelligence_items AS item
              ON item.track_id=credit.track_id
            WHERE credit.artist_id IN ({placeholders})
              AND item.state IN ('applied','applied_with_gaps')
            ORDER BY credit.artist_id, credit.track_id, item.id
            """,
            artist_ids,
        ).fetchall()
        identities: dict[str, dict[int, set[str]]] = {
            "discogs": {artist_id: set() for artist_id in artist_ids},
            "musicbrainz": {artist_id: set() for artist_id in artist_ids},
        }
        for stored in accepted:
            artist_id = int(stored["artist_id"])
            if str(stored["provider_agreement"] or "").strip().casefold() in {
                "conflict",
                "disagree",
                "provider_disagreement",
            }:
                return "accepted_provider_context_ambiguous"
            try:
                proposal = json.loads(str(stored["field_proposal"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                return "accepted_provider_context_malformed"
            if not isinstance(proposal, Mapping):
                return "accepted_provider_context_malformed"
            for provider, proposal_key, id_keys in (
                (
                    "discogs",
                    "_discogs",
                    ("discogs_artist_id", "artist_id"),
                ),
                (
                    "musicbrainz",
                    "_musicbrainz",
                    ("musicbrainz_artist_id", "artist_id"),
                ),
            ):
                if proposal_key not in proposal:
                    continue
                provider_payload = proposal[proposal_key]
                if provider_payload in (None, {}):
                    continue
                if not isinstance(provider_payload, Mapping):
                    return "accepted_provider_context_malformed"
                if "artist_credits" not in provider_payload:
                    continue
                raw_credits = provider_payload["artist_credits"]
                if not isinstance(raw_credits, Sequence) or isinstance(
                    raw_credits, (str, bytes, bytearray)
                ):
                    return "accepted_provider_context_malformed"
                if not raw_credits:
                    continue
                matching_ids: set[str] = set()
                matched_credit = False
                for raw_credit in raw_credits:
                    if not isinstance(raw_credit, Mapping):
                        return "accepted_provider_context_malformed"
                    names = [
                        _clean_display(raw_credit[key])
                        for key in ("name", "display_name")
                        if raw_credit.get(key) not in (None, "")
                    ]
                    if not names or len(
                        {normalize_artist_presentation(name) for name in names}
                    ) != 1:
                        return "accepted_provider_context_malformed"
                    supplied_ids: set[str] = set()
                    for key in id_keys:
                        raw_id = raw_credit.get(key)
                        if raw_id in (None, ""):
                            continue
                        accepted_id = _strict_proposal_artist_id(raw_id, provider)
                        if accepted_id is None:
                            return "accepted_provider_context_malformed"
                        supplied_ids.add(accepted_id.casefold())
                    if len(supplied_ids) > 1:
                        return "accepted_provider_context_ambiguous"
                    if normalize_artist_presentation(names[0]) != display_keys[artist_id]:
                        continue
                    matched_credit = True
                    matching_ids.update(supplied_ids)
                if not matched_credit:
                    return "accepted_provider_context_ambiguous"
                if len(matching_ids) > 1:
                    return "accepted_provider_context_ambiguous"
                identities[provider][artist_id].update(matching_ids)

        for provider in ("discogs", "musicbrainz"):
            provider_ids = {
                provider_id
                for values in identities[provider].values()
                for provider_id in values
            }
            if len(provider_ids) > 1:
                return f"accepted_{provider}_artist_id_conflict"
        return None

    def _relationship_evidence_collision(self, artist_ids: Sequence[int]) -> bool:
        """Reject relationship merges that cannot preserve one audit record."""

        identifiers = {int(value) for value in artist_ids}
        placeholders = ",".join("?" for _ in identifiers)
        rows = self.conn.execute(
            f"""
            SELECT * FROM artist_relationships
            WHERE subject_artist_id IN ({placeholders})
               OR related_artist_id IN ({placeholders})
            ORDER BY id
            """,
            (*identifiers, *identifiers),
        ).fetchall()
        # A relationship whose endpoints would collapse into one artist is
        # identity evidence, not disposable duplication.  In particular,
        # member/group and collaboration-project links must never be turned
        # into a self-edge and silently deleted by consolidation.  Fail closed
        # for every internal relationship so its provenance remains intact.
        for row in rows:
            if (
                int(row["subject_artist_id"]) in identifiers
                and int(row["related_artist_id"]) in identifiers
            ):
                return True
        groups: dict[tuple[int, int, str], list[sqlite3.Row]] = {}
        for row in rows:
            subject = 0 if int(row["subject_artist_id"]) in identifiers else int(
                row["subject_artist_id"]
            )
            related = 0 if int(row["related_artist_id"]) in identifiers else int(
                row["related_artist_id"]
            )
            if subject == related:
                continue
            groups.setdefault(
                (subject, related, str(row["relationship_kind"])), []
            ).append(row)
        for collisions in groups.values():
            if len(collisions) < 2:
                continue
            manual = [
                row
                for row in collisions
                if str(row["provenance"] or "").casefold() == "manual"
            ]
            if len(manual) == 1:
                continue
            evidence = {
                (
                    str(row["provenance"] or ""),
                    str(row["provider_reference"] or ""),
                )
                for row in collisions
            }
            if len(evidence) > 1:
                return True
        return False

    def _merge_candidates(
        self,
    ) -> tuple[tuple[ArtistMerge, ...], tuple[ArtistIdentityConflict, ...]]:
        rows = self._artist_rows()
        row_by_id = {int(row["id"]): row for row in rows}
        parent = {artist_id: artist_id for artist_id in row_by_id}
        members = {artist_id: {artist_id} for artist_id in row_by_id}
        component_evidence = {artist_id: set() for artist_id in row_by_id}
        edges: dict[tuple[int, int], set[str]] = {}

        def find(value: int) -> int:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        def add_edge(left: int, right: int, reason: str) -> None:
            if left == right:
                return
            edges.setdefault(tuple(sorted((left, right))), set()).add(reason)

        conflicts_by_key: dict[
            tuple[tuple[int, ...], str], ArtistIdentityConflict
        ] = {}

        def reject(artist_ids: Sequence[int], reason: str) -> None:
            identifiers = tuple(sorted(set(int(value) for value in artist_ids)))
            key = (identifiers, reason)
            conflicts_by_key[key] = ArtistIdentityConflict(identifiers, reason)

        indexes: tuple[tuple[str, Callable[[sqlite3.Row], str]], ...] = (
            (
                "same_discogs_artist_id",
                lambda row: f"discogs:{_provider_id(row['discogs_artist_id']) or ''}",
            ),
            (
                "same_musicbrainz_artist_id",
                lambda row: f"musicbrainz:{_provider_id(row['musicbrainz_artist_id']) or ''}",
            ),
            (
                "same_presentation_identity",
                lambda row: "presentation:"
                + normalize_artist_presentation(row["display_name"]),
            ),
        )
        for reason, key_for in indexes:
            groups: dict[str, list[int]] = {}
            for row in rows:
                key = key_for(row)
                if key.endswith(":"):
                    continue
                groups.setdefault(key, []).append(int(row["id"]))
            for ids in groups.values():
                if len(ids) < 2:
                    continue
                group_rows = [row_by_id[value] for value in ids]
                if reason == "same_presentation_identity":
                    if self._provider_conflict(group_rows, "discogs_artist_id"):
                        reject(ids, "discogs_id_conflict")
                        continue
                    if self._provider_conflict(
                        group_rows, "musicbrainz_artist_id"
                    ):
                        reject(ids, "musicbrainz_id_conflict")
                        continue
                    if self._ambiguous_exact_name(group_rows):
                        reject(ids, "ambiguous_exact_same_name")
                        continue
                for index, left in enumerate(ids):
                    for right in ids[index + 1 :]:
                        add_edge(left, right, reason)

        if _table_exists(self.conn, "artist_aliases"):
            ids_by_name: dict[str, list[int]] = {}
            for row in rows:
                ids_by_name.setdefault(str(row["normalized_name"]), []).append(
                    int(row["id"])
                )
            alias_owners: dict[str, set[int]] = {}
            for alias in self.conn.execute(
                "SELECT artist_id,normalized_alias FROM artist_aliases ORDER BY id"
            ).fetchall():
                owner = int(alias["artist_id"])
                normalized_alias = str(alias["normalized_alias"])
                if owner in row_by_id and normalized_alias:
                    alias_owners.setdefault(normalized_alias, set()).add(owner)
            for normalized_alias, owners in alias_owners.items():
                candidates = ids_by_name.get(normalized_alias, ())
                if len(owners) > 1:
                    reject(
                        tuple(sorted(owners | set(candidates))),
                        "ambiguous_preserved_alias",
                    )
                    continue
                owner = next(iter(owners))
                for candidate in candidates:
                    add_edge(owner, candidate, "preserved_alias_identity")

        priority = {
            "same_discogs_artist_id": 0,
            "same_musicbrainz_artist_id": 0,
            "preserved_alias_identity": 1,
            "same_presentation_identity": 2,
        }
        ordered_edges = sorted(
            edges.items(),
            key=lambda item: (
                min(priority.get(reason, 9) for reason in item[1]),
                item[0],
            ),
        )
        for (left, right), reasons in ordered_edges:
            first, second = find(left), find(right)
            if first == second:
                component_evidence[first].update(reasons)
                continue
            artist_ids = sorted(members[first] | members[second])
            component_rows = [row_by_id[value] for value in artist_ids]
            if self._provider_conflict(component_rows, "discogs_artist_id"):
                reject(artist_ids, "discogs_id_conflict")
                continue
            if self._provider_conflict(component_rows, "musicbrainz_artist_id"):
                reject(artist_ids, "musicbrainz_id_conflict")
                continue
            provider_context_conflict = self._accepted_provider_context_conflict(
                component_rows
            )
            if provider_context_conflict is not None:
                reject(artist_ids, provider_context_conflict)
                continue
            if self._types_conflict(component_rows):
                reject(artist_ids, "person_group_conflict")
                continue
            if self._ambiguous_exact_name(component_rows):
                reject(artist_ids, "ambiguous_exact_same_name")
                continue
            if self._relationship_evidence_collision(artist_ids):
                reject(artist_ids, "relationship_evidence_conflict")
                continue
            if self._credit_collision(artist_ids):
                reject(artist_ids, "credit_collision")
                continue

            target, source = (first, second) if first < second else (second, first)
            parent[source] = target
            members[target].update(members.pop(source))
            component_evidence[target].update(component_evidence.pop(source))
            component_evidence[target].update(reasons)

        merges: list[ArtistMerge] = []
        for artist_ids in members.values():
            if len(artist_ids) < 2:
                continue
            component_rows = [row_by_id[value] for value in artist_ids]
            canonical = min(component_rows, key=self._canonical_sort_key)
            canonical_id = int(canonical["id"])
            reasons = component_evidence[find(canonical_id)]
            merges.append(
                ArtistMerge(
                    canonical_id,
                    tuple(sorted(value for value in artist_ids if value != canonical_id)),
                    tuple(sorted(reasons)),
                )
            )
        return (
            tuple(sorted(merges, key=lambda item: item.canonical_artist_id)),
            tuple(
                sorted(
                    conflicts_by_key.values(),
                    key=lambda item: (item.artist_ids, item.reason),
                )
            ),
        )

    def _version_repairs(self) -> tuple[VersionArtistRepair, ...]:
        if not _table_exists(self.conn, "track_metadata_fields"):
            return ()
        rows = self.conn.execute(
            """
            SELECT c.track_id, c.artist_id, a.display_name, a.entity_type,
                   a.discogs_artist_id, a.musicbrainz_artist_id,
                   c.provenance AS credit_provenance,
                   vt.value AS version_type, vl.value AS version_label,
                   COALESCE(ar.is_locked, 0) AS artist_locked,
                   COALESCE(vt.is_locked, 0) AS type_locked,
                   COALESCE(vl.is_locked, 0) AS label_locked,
                   COALESCE(c.is_manual, 0) AS credit_manual,
                   COALESCE(c.is_locked, 0) AS credit_locked
            FROM track_artist_credits c
            JOIN artists a ON a.id=c.artist_id
            LEFT JOIN track_metadata_fields ar
              ON ar.track_id=c.track_id AND ar.field_name='artist'
            LEFT JOIN track_metadata_fields vt
              ON vt.track_id=c.track_id AND vt.field_name='version_type'
            LEFT JOIN track_metadata_fields vl
              ON vl.track_id=c.track_id AND vl.field_name='version_label'
            WHERE c.role='primary'
              AND NOT EXISTS (
                  SELECT 1 FROM track_artist_credits other
                  WHERE other.track_id=c.track_id AND other.role='primary' AND other.id<>c.id
              )
            ORDER BY c.track_id, c.id
            """
        ).fetchall()
        repairs: list[VersionArtistRepair] = []
        for row in rows:
            if bool(
                row["artist_locked"]
                or row["type_locked"]
                or row["label_locked"]
                or row["credit_manual"]
                or row["credit_locked"]
            ):
                continue
            display = _clean_display(row["display_name"])
            match = _VERSION_SUFFIX_RE.fullmatch(display)
            if match is None:
                continue
            base = _clean_display(match.group("artist"))
            label = _clean_display(match.group("label"))
            stored_label = _clean_display(row["version_label"])
            stored_type = str(row["version_type"] or "").strip().casefold()
            version_type = classify_artist_version_label(label)
            stored_version_support = bool(
                stored_label
                and stored_type
                and normalize_artist_name(label) == normalize_artist_name(stored_label)
                and stored_type
                in {
                    version_type,
                    "live" if version_type == "session" else version_type,
                }
            )
            candidates = self.conn.execute(
                """
                SELECT id, display_name, entity_type,
                       discogs_artist_id, musicbrainz_artist_id
                FROM artists WHERE normalized_name=? AND id<>? ORDER BY id
                """,
                (normalize_artist_name(base), int(row["artist_id"])),
            ).fetchall()
            if len(candidates) != 1:
                continue
            canonical = candidates[0]
            if self._types_conflict((row, canonical)):
                continue
            if (
                _provider_id(row["discogs_artist_id"])
                and _provider_id(canonical["discogs_artist_id"])
                and _provider_id(row["discogs_artist_id"])
                != _provider_id(canonical["discogs_artist_id"])
            ) or (
                _provider_id(row["musicbrainz_artist_id"])
                and _provider_id(canonical["musicbrainz_artist_id"])
                and _provider_id(row["musicbrainz_artist_id"])
                != _provider_id(canonical["musicbrainz_artist_id"])
            ):
                continue
            shared_provider_support = bool(
                _provider_id(row["discogs_artist_id"])
                and _provider_id(row["discogs_artist_id"])
                == _provider_id(canonical["discogs_artist_id"])
            ) or bool(
                _provider_id(row["musicbrainz_artist_id"])
                and _provider_id(row["musicbrainz_artist_id"])
                == _provider_id(canonical["musicbrainz_artist_id"])
            )
            parsed_title_support = (
                "title" in str(row["credit_provenance"] or "").casefold()
                and "pars" in str(row["credit_provenance"] or "").casefold()
            )
            if not (stored_version_support or shared_provider_support or parsed_title_support):
                continue
            repairs.append(
                VersionArtistRepair(
                    int(row["track_id"]),
                    int(row["artist_id"]),
                    int(canonical["id"]),
                    str(canonical["display_name"]),
                    version_type,
                    label,
                    (
                        "stored_version_fields"
                        if stored_version_support
                        else (
                            "shared_provider_identity"
                            if shared_provider_support
                            else "explicit_source_title_phrase"
                        )
                    ),
                )
            )
        return tuple(repairs)

    @staticmethod
    def _structured_credits(value: object) -> tuple[StructuredCredit, ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return ()
        credits: list[StructuredCredit] = []
        for item in value:
            if not isinstance(item, Mapping):
                return ()
            name = _clean_display(item.get("name") or item.get("display_name"))
            role = str(item.get("role") or "primary").strip().casefold()
            if not name or role not in {"primary", "featured", "collaborator"}:
                return ()
            credits.append(
                StructuredCredit(
                    name,
                    role,
                    str(item.get("join_phrase") or ""),
                    str(item.get("entity_type") or "unknown").strip().casefold(),
                    _provider_id(item.get("artist_id") or item.get("discogs_artist_id")),
                    _provider_id(item.get("musicbrainz_artist_id")),
                )
            )
        if len(credits) < 2 or not any(item.role == "primary" for item in credits):
            return ()
        return tuple(credits)

    def _full_credit_repairs(self) -> tuple[FullCreditRepair, ...]:
        rows = (
            self.conn.execute(
                """
            SELECT item.track_id, item.field_proposal, item.field_confidence,
                   c.artist_id, a.display_name, a.discogs_artist_id,
                   a.musicbrainz_artist_id,
                   COALESCE(f.is_locked, 0) AS artist_locked,
                   COALESCE(c.is_manual, 0) AS credit_manual,
                   COALESCE(c.is_locked, 0) AS credit_locked
            FROM metadata_intelligence_items item
            JOIN track_artist_credits c ON c.track_id=item.track_id AND c.role='primary'
            JOIN artists a ON a.id=c.artist_id
            LEFT JOIN track_metadata_fields f
              ON f.track_id=item.track_id AND f.field_name='artist'
            WHERE item.field_proposal IS NOT NULL
              AND item.state IN ('applied','applied_with_gaps','source_fallback')
              AND COALESCE(item.provider_agreement, '') NOT IN (
                  'conflict','disagree','provider_disagreement'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM track_artist_credits other
                  WHERE other.track_id=item.track_id AND other.id<>c.id
              )
            ORDER BY item.track_id, item.id DESC
                """
            ).fetchall()
            if _table_exists(self.conn, "metadata_intelligence_items")
            else ()
        )
        repairs: dict[int, FullCreditRepair] = {}
        for row in rows:
            track_id = int(row["track_id"])
            if track_id in repairs or bool(
                row["artist_locked"] or row["credit_manual"] or row["credit_locked"]
            ):
                continue
            try:
                proposal = json.loads(str(row["field_proposal"] or "{}"))
                confidence_payload = json.loads(str(row["field_confidence"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(proposal, Mapping) or not isinstance(confidence_payload, Mapping):
                continue
            provider = proposal.get("_discogs")
            if not isinstance(provider, Mapping):
                continue
            credits = self._structured_credits(provider.get("artist_credits"))
            try:
                confidence = float(confidence_payload.get("artist_credits", 0))
            except (TypeError, ValueError, OverflowError):
                continue
            visible = _clean_display(provider.get("artist"))
            if not credits or confidence < 85 or normalize_artist_name(visible) != normalize_artist_name(row["display_name"]):
                continue
            malformed_discogs = _provider_id(row["discogs_artist_id"])
            malformed_musicbrainz = _provider_id(row["musicbrainz_artist_id"])
            if malformed_discogs or malformed_musicbrainz:
                # A provider ID attached to a combined full-credit string is
                # ambiguous: it may identify one member or a provider-specific
                # collaboration entity. Never guess which structured credit
                # should inherit it during automatic migration.
                continue
            repairs[track_id] = FullCreditRepair(
                track_id,
                int(row["artist_id"]),
                credits,
                "discogs_high_confidence",
                _provider_id(provider.get("provider_reference")),
                confidence,
            )

        # Explicit title-parser role phrases are the second deterministic
        # source after provider-structured credits.  Punctuation and ordinary
        # conjunctions are intentionally absent from this pattern: ``A & B``,
        # ``A, B``, ``A/B`` and ``A and B`` remain one unsplit display identity.
        if not _table_exists(self.conn, "track_metadata_fields"):
            return tuple(repairs.values())
        explicit_rows = self.conn.execute(
            """
            SELECT credit.track_id,credit.artist_id,credit.provenance,
                   credit.provider_reference,credit.confidence,
                   credit.is_manual,credit.is_locked,
                   artist.display_name,artist.discogs_artist_id,
                   artist.musicbrainz_artist_id,
                   COALESCE(field.is_locked,0) AS artist_locked
            FROM track_artist_credits AS credit
            JOIN artists AS artist ON artist.id=credit.artist_id
            LEFT JOIN track_metadata_fields AS field
              ON field.track_id=credit.track_id AND field.field_name='artist'
            WHERE credit.role='primary'
              AND NOT EXISTS (
                  SELECT 1 FROM track_artist_credits AS other
                  WHERE other.track_id=credit.track_id AND other.id<>credit.id
              )
            ORDER BY credit.track_id,credit.id
            """
        ).fetchall()
        for row in explicit_rows:
            track_id = int(row["track_id"])
            if track_id in repairs or bool(
                row["artist_locked"] or row["is_manual"] or row["is_locked"]
            ):
                continue
            provenance = str(row["provenance"] or "").casefold()
            if "title" not in provenance or "pars" not in provenance:
                continue
            if _provider_id(row["discogs_artist_id"]) or _provider_id(
                row["musicbrainz_artist_id"]
            ):
                continue
            display = _clean_display(row["display_name"])
            match = _EXPLICIT_CREDIT_PHRASE_RE.fullmatch(display)
            if match is None:
                continue
            primary = _clean_display(match.group("primary"))
            related = _clean_display(match.group("related"))
            phrase = _clean_display(match.group("join"))
            if not primary or not related:
                continue
            role = (
                "featured"
                if phrase.casefold().rstrip(".") in {"feat", "ft", "featuring"}
                else "collaborator"
            )
            try:
                confidence = float(row["confidence"] or 86.0)
            except (TypeError, ValueError, OverflowError):
                confidence = 86.0
            if confidence < 80:
                continue
            repairs[track_id] = FullCreditRepair(
                track_id,
                int(row["artist_id"]),
                (
                    StructuredCredit(primary, "primary"),
                    StructuredCredit(related, role, f" {phrase} "),
                ),
                "explicit_source_title_phrase",
                _provider_id(row["provider_reference"]),
                confidence,
            )
        return tuple(repairs.values())

    def plan(self) -> ArtistConsolidationPlan:
        self._require_schema()
        merges, conflicts = self._merge_candidates()
        return ArtistConsolidationPlan(
            merges=merges,
            conflicts=conflicts,
            version_repairs=self._version_repairs(),
            full_credit_repairs=self._full_credit_repairs(),
        )

    def _insert_alias(
        self,
        artist_id: int,
        alias_name: str,
        *,
        alias_kind: str,
        provenance: str,
        confidence: float,
        provider_reference: str | None = None,
    ) -> int:
        before = self.conn.total_changes
        self.conn.execute(
            """
            INSERT OR IGNORE INTO artist_aliases (
                artist_id, alias_name, normalized_alias, alias_kind,
                provenance, provider_reference, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(artist_id),
                alias_name,
                normalize_artist_name(alias_name),
                alias_kind,
                provenance,
                provider_reference,
                float(confidence),
                _now(),
            ),
        )
        return int(self.conn.total_changes > before)

    def _merge_artist(self, merge: ArtistMerge) -> tuple[int, int, int]:
        canonical = self.conn.execute(
            "SELECT * FROM artists WHERE id=?", (merge.canonical_artist_id,)
        ).fetchone()
        if canonical is None:
            raise ArtistConsolidationError("Canonical artist disappeared before apply.")
        reassigned = aliases = relationships = 0
        for duplicate_id in merge.duplicate_artist_ids:
            duplicate = self.conn.execute(
                "SELECT * FROM artists WHERE id=?", (duplicate_id,)
            ).fetchone()
            if duplicate is None:
                raise ArtistConsolidationError("Duplicate artist disappeared before apply.")
            rows = [canonical, duplicate]
            if (
                self._provider_conflict(rows, "discogs_artist_id")
                or self._provider_conflict(rows, "musicbrainz_artist_id")
                or self._types_conflict(rows)
                or self._credit_collision((merge.canonical_artist_id, duplicate_id))
            ):
                raise ArtistConsolidationError("Artist identity changed after dry-run planning.")

            aliases += self._insert_alias(
                merge.canonical_artist_id,
                str(duplicate["display_name"]),
                alias_kind="display_variant",
                provenance="canonical_consolidation",
                confidence=100,
            )
            # Preserve existing aliases before removing their old parent.
            for alias in self.conn.execute(
                "SELECT * FROM artist_aliases WHERE artist_id=? ORDER BY id",
                (duplicate_id,),
            ).fetchall():
                before = self.conn.total_changes
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO artist_aliases (
                        artist_id, alias_name, normalized_alias, alias_kind,
                        provenance, provider_reference, confidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        merge.canonical_artist_id,
                        alias["alias_name"],
                        alias["normalized_alias"],
                        alias["alias_kind"],
                        alias["provenance"],
                        alias["provider_reference"],
                        alias["confidence"],
                        alias["created_at"],
                    ),
                )
                aliases += int(self.conn.total_changes > before)
            self.conn.execute("DELETE FROM artist_aliases WHERE artist_id=?", (duplicate_id,))

            canonical_discogs = _provider_id(canonical["discogs_artist_id"])
            canonical_mb = _provider_id(canonical["musicbrainz_artist_id"])
            duplicate_discogs = _provider_id(duplicate["discogs_artist_id"])
            duplicate_mb = _provider_id(duplicate["musicbrainz_artist_id"])
            if duplicate_discogs or duplicate_mb:
                # Release complementary provider IDs before assigning them to
                # the canonical row; schema-v7 partial unique indexes apply
                # even inside this transaction.
                self.conn.execute(
                    """
                    UPDATE artists SET discogs_artist_id=NULL,
                        musicbrainz_artist_id=NULL,updated_at=? WHERE id=?
                    """,
                    (_now(), duplicate_id),
                )
            self.conn.execute(
                """
                UPDATE artists SET
                    discogs_artist_id=COALESCE(NULLIF(TRIM(discogs_artist_id), ''), ?),
                    musicbrainz_artist_id=COALESCE(NULLIF(TRIM(musicbrainz_artist_id), ''), ?),
                    entity_type=CASE WHEN entity_type='unknown' THEN ? ELSE entity_type END,
                    updated_at=?
                WHERE id=?
                """,
                (
                    duplicate_discogs,
                    duplicate_mb,
                    str(duplicate["entity_type"]),
                    _now(),
                    merge.canonical_artist_id,
                ),
            )

            duplicate_credits = self.conn.execute(
                "SELECT * FROM track_artist_credits WHERE artist_id=? ORDER BY id",
                (duplicate_id,),
            ).fetchall()
            for duplicate_credit in duplicate_credits:
                canonical_credit = self.conn.execute(
                    """
                    SELECT * FROM track_artist_credits
                    WHERE artist_id=? AND track_id=? AND role=?
                    """,
                    (
                        merge.canonical_artist_id,
                        int(duplicate_credit["track_id"]),
                        duplicate_credit["role"],
                    ),
                ).fetchone()
                if canonical_credit is None:
                    continue
                if self._credit_evidence(canonical_credit) != self._credit_evidence(
                    duplicate_credit
                ):
                    raise ArtistConsolidationError(
                        "Artist credit evidence changed after planning."
                    )
                self.conn.execute(
                    "DELETE FROM track_artist_credits WHERE id=?",
                    (int(duplicate_credit["id"]),),
                )

            before = self.conn.total_changes
            self.conn.execute(
                "UPDATE track_artist_credits SET artist_id=?, updated_at=? WHERE artist_id=?",
                (merge.canonical_artist_id, _now(), duplicate_id),
            )
            reassigned += self.conn.total_changes - before

            relationship_rows = self.conn.execute(
                """
                SELECT * FROM artist_relationships
                WHERE subject_artist_id=? OR related_artist_id=? ORDER BY id
                """,
                (duplicate_id, duplicate_id),
            ).fetchall()
            for relation in relationship_rows:
                subject = (
                    merge.canonical_artist_id
                    if int(relation["subject_artist_id"]) == duplicate_id
                    else int(relation["subject_artist_id"])
                )
                related = (
                    merge.canonical_artist_id
                    if int(relation["related_artist_id"]) == duplicate_id
                    else int(relation["related_artist_id"])
                )
                if subject == related:
                    continue
                before = self.conn.total_changes
                columns = _column_names(self.conn, "artist_relationships")
                existing_relation = self.conn.execute(
                    """
                    SELECT * FROM artist_relationships
                    WHERE subject_artist_id=? AND related_artist_id=?
                      AND relationship_kind=? AND id<>?
                    ORDER BY id LIMIT 1
                    """,
                    (
                        subject,
                        related,
                        relation["relationship_kind"],
                        int(relation["id"]),
                    ),
                ).fetchone()
                if existing_relation is not None:
                    existing_manual = (
                        str(existing_relation["provenance"] or "").casefold()
                        == "manual"
                    )
                    incoming_manual = (
                        str(relation["provenance"] or "").casefold() == "manual"
                    )
                    same_audit = (
                        str(existing_relation["provenance"] or "")
                        == str(relation["provenance"] or "")
                        and str(existing_relation["provider_reference"] or "")
                        == str(relation["provider_reference"] or "")
                    )
                    if incoming_manual and not existing_manual:
                        self.conn.execute(
                            """
                            UPDATE artist_relationships SET provenance='manual',
                                provider_reference=?,confidence=100,updated_at=?
                            WHERE id=?
                            """,
                            (
                                relation["provider_reference"],
                                _now(),
                                int(existing_relation["id"]),
                            ),
                        )
                    elif existing_manual:
                        pass
                    elif same_audit:
                        self.conn.execute(
                            """
                            UPDATE artist_relationships SET
                                confidence=MAX(COALESCE(confidence,0),?),updated_at=?
                            WHERE id=?
                            """,
                            (
                                float(relation["confidence"] or 0),
                                _now(),
                                int(existing_relation["id"]),
                            ),
                        )
                    else:
                        raise ArtistConsolidationError(
                            "Relationship audit evidence changed after planning."
                        )
                elif "updated_at" in columns:
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO artist_relationships (
                            subject_artist_id, related_artist_id, relationship_kind,
                            provenance, provider_reference, confidence, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            subject,
                            related,
                            relation["relationship_kind"],
                            relation["provenance"],
                            relation["provider_reference"],
                            relation["confidence"],
                            relation["created_at"],
                            relation["updated_at"],
                        ),
                    )
                else:
                    self.conn.execute(
                        """
                        INSERT OR IGNORE INTO artist_relationships (
                            subject_artist_id, related_artist_id, relationship_kind,
                            provenance, provider_reference, confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            subject,
                            related,
                            relation["relationship_kind"],
                            relation["provenance"],
                            relation["provider_reference"],
                            relation["confidence"],
                            relation["created_at"],
                        ),
                    )
                relationships += int(self.conn.total_changes > before)
            self.conn.execute(
                "DELETE FROM artist_relationships WHERE subject_artist_id=? OR related_artist_id=?",
                (duplicate_id, duplicate_id),
            )
            remaining = self.conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM track_artist_credits WHERE artist_id=?) +
                    (SELECT COUNT(*) FROM artist_relationships
                     WHERE subject_artist_id=? OR related_artist_id=?)
                """,
                (duplicate_id, duplicate_id, duplicate_id),
            ).fetchone()[0]
            if int(remaining) != 0:
                raise ArtistConsolidationError("Duplicate artist remains referenced.")
            self.conn.execute("DELETE FROM artists WHERE id=?", (duplicate_id,))
            if self.portrait_rekey is not None:
                self.portrait_rekey(
                    str(duplicate["display_name"]), str(canonical["display_name"])
                )
            canonical = self.conn.execute(
                "SELECT * FROM artists WHERE id=?", (merge.canonical_artist_id,)
            ).fetchone()
            assert canonical is not None
            if canonical_discogs or canonical_mb:
                # Values were already retained on the canonical row; these
                # locals make the intent explicit for auditability.
                pass
        return reassigned, aliases, relationships

    def _preserve_artist_identity_evidence(
        self,
        source_artist_id: int,
        target_artist_id: int,
        *,
        alias_kind: str,
        provenance: str,
        confidence: float,
        provider_reference: str | None = None,
    ) -> int:
        """Move aliases and non-conflicting provider IDs before row removal."""

        source = self.conn.execute(
            "SELECT * FROM artists WHERE id=?", (int(source_artist_id),)
        ).fetchone()
        target = self.conn.execute(
            "SELECT * FROM artists WHERE id=?", (int(target_artist_id),)
        ).fetchone()
        if source is None or target is None:
            raise ArtistConsolidationError(
                "Artist identity evidence disappeared before preservation."
            )
        if (
            self._provider_conflict((source, target), "discogs_artist_id")
            or self._provider_conflict((source, target), "musicbrainz_artist_id")
        ):
            raise ArtistConsolidationError(
                "Conflicting provider identity cannot be repaired automatically."
            )

        source_discogs = _provider_id(source["discogs_artist_id"])
        source_musicbrainz = _provider_id(source["musicbrainz_artist_id"])
        if source_discogs or source_musicbrainz:
            # Partial unique indexes require the source row to release an ID
            # before the canonical row can receive the same evidence.
            self.conn.execute(
                """
                UPDATE artists SET discogs_artist_id=NULL,
                    musicbrainz_artist_id=NULL,updated_at=? WHERE id=?
                """,
                (_now(), int(source_artist_id)),
            )

        preserved = self._insert_alias(
            int(target_artist_id),
            str(source["display_name"]),
            alias_kind=alias_kind,
            provenance=provenance,
            confidence=confidence,
            provider_reference=provider_reference,
        )
        for alias in self.conn.execute(
            "SELECT * FROM artist_aliases WHERE artist_id=? ORDER BY id",
            (int(source_artist_id),),
        ).fetchall():
            before = self.conn.total_changes
            self.conn.execute(
                """
                INSERT OR IGNORE INTO artist_aliases (
                    artist_id,alias_name,normalized_alias,alias_kind,provenance,
                    provider_reference,confidence,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    int(target_artist_id),
                    alias["alias_name"],
                    alias["normalized_alias"],
                    alias["alias_kind"],
                    alias["provenance"],
                    alias["provider_reference"],
                    alias["confidence"],
                    alias["created_at"],
                ),
            )
            preserved += int(self.conn.total_changes > before)
        self.conn.execute(
            """
            UPDATE artists SET
                discogs_artist_id=COALESCE(NULLIF(TRIM(discogs_artist_id), ''), ?),
                musicbrainz_artist_id=COALESCE(
                    NULLIF(TRIM(musicbrainz_artist_id), ''), ?
                ),
                updated_at=?
            WHERE id=?
            """,
            (
                source_discogs,
                source_musicbrainz,
                _now(),
                int(target_artist_id),
            ),
        )
        self.conn.execute(
            "DELETE FROM artist_aliases WHERE artist_id=?", (int(source_artist_id),)
        )
        return preserved

    def _apply_version_repair(self, repair: VersionArtistRepair) -> int:
        current = self.conn.execute(
            """
            SELECT c.id, c.artist_id FROM track_artist_credits c
            WHERE c.track_id=? AND c.role='primary'
            ORDER BY c.credit_order, c.id
            """,
            (repair.track_id,),
        ).fetchall()
        if len(current) != 1 or int(current[0]["artist_id"]) != repair.malformed_artist_id:
            raise ArtistConsolidationError("Version repair credit changed after planning.")
        self.conn.execute(
            "UPDATE track_artist_credits SET artist_id=?, updated_at=? WHERE id=?",
            (repair.canonical_artist_id, _now(), int(current[0]["id"])),
        )
        self._preserve_artist_identity_evidence(
            repair.malformed_artist_id,
            repair.canonical_artist_id,
            alias_kind="corrected_version_suffix",
            provenance=repair.evidence,
            confidence=100,
        )
        MetadataService(self.conn).record_source_observations(
            repair.track_id,
            provider="confirmed_provider",
            values={
                "artist": repair.canonical_display_name,
                "version_type": repair.version_type,
                "version_label": repair.version_label,
            },
            confidence=100,
            apply_effective=True,
            actor="artist_consolidation",
            reason="version_suffix_removed_from_artist",
            commit=False,
        )
        # Keep the materialized compatibility columns aligned even when the
        # field-state values already contained the accepted version evidence.
        self.conn.execute(
            """
            UPDATE tracks SET version_type=?, version_label=?
            WHERE id=? AND (
                COALESCE(version_type, '')<>? OR COALESCE(version_label, '')<>?
            )
            """,
            (
                repair.version_type,
                repair.version_label,
                repair.track_id,
                repair.version_type,
                repair.version_label,
            ),
        )
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM track_artist_credits WHERE artist_id=?",
            (repair.malformed_artist_id,),
        ).fetchone()[0]
        relationships = self.conn.execute(
            """
            SELECT COUNT(*) FROM artist_relationships
            WHERE subject_artist_id=? OR related_artist_id=?
            """,
            (repair.malformed_artist_id, repair.malformed_artist_id),
        ).fetchone()[0]
        if int(remaining) == 0 and int(relationships) == 0:
            self.conn.execute(
                "DELETE FROM artist_aliases WHERE artist_id=?",
                (repair.malformed_artist_id,),
            )
            self.conn.execute("DELETE FROM artists WHERE id=?", (repair.malformed_artist_id,))
        return 1

    def _apply_full_credit_repair(self, repair: FullCreditRepair) -> int:
        malformed = self.conn.execute(
            "SELECT display_name FROM artists WHERE id=?",
            (repair.malformed_artist_id,),
        ).fetchone()
        if malformed is None:
            raise ArtistConsolidationError(
                "Full-credit artist disappeared before apply."
            )
        inputs = tuple(
            ArtistCreditInput(
                credit.display_name,
                role=credit.role,
                join_phrase=credit.join_phrase,
                entity_type=credit.entity_type,
                discogs_artist_id=credit.discogs_artist_id,
                musicbrainz_artist_id=credit.musicbrainz_artist_id,
            )
            for credit in repair.credits
        )
        ArtistCreditService(self.conn).replace_track_credits(
            repair.track_id,
            inputs,
            provenance=repair.provenance,
            provider_reference=repair.provider_reference,
            confidence=repair.confidence,
            actor="artist_consolidation",
            reason="structured_full_credit_repair",
            update_display=False,
            commit=False,
        )
        primary = self.conn.execute(
            """
            SELECT artist_id FROM track_artist_credits
            WHERE track_id=? AND role='primary'
            ORDER BY credit_order,id LIMIT 1
            """,
            (repair.track_id,),
        ).fetchone()
        if primary is None:
            raise ArtistConsolidationError(
                "Structured full-credit repair produced no primary artist."
            )
        self._preserve_artist_identity_evidence(
            repair.malformed_artist_id,
            int(primary["artist_id"]),
            alias_kind="legacy_credit_string",
            provenance=repair.provenance,
            confidence=repair.confidence,
            provider_reference=repair.provider_reference,
        )
        if self.conn.execute(
            "SELECT COUNT(*) FROM track_artist_credits WHERE artist_id=?",
            (repair.malformed_artist_id,),
        ).fetchone()[0] == 0:
            relation_count = self.conn.execute(
                """
                SELECT COUNT(*) FROM artist_relationships
                WHERE subject_artist_id=? OR related_artist_id=?
                """,
                (repair.malformed_artist_id, repair.malformed_artist_id),
            ).fetchone()[0]
            if int(relation_count) == 0:
                self.conn.execute(
                    "DELETE FROM artist_aliases WHERE artist_id=?",
                    (repair.malformed_artist_id,),
                )
                self.conn.execute(
                    "DELETE FROM artists WHERE id=?", (repair.malformed_artist_id,)
                )
        return 1

    def apply(self, plan: ArtistConsolidationPlan | None = None) -> ArtistConsolidationReport:
        self._require_schema()
        accepted = plan or self.plan()
        artist_ids_before = {
            int(row[0]) for row in self.conn.execute("SELECT id FROM artists")
        }
        credit_context_before = {
            int(row["id"]): (int(row["artist_id"]), int(row["track_id"]))
            for row in self.conn.execute(
                "SELECT id,artist_id,track_id FROM track_artist_credits"
            ).fetchall()
        }
        planned_removals = {
            duplicate
            for merge in accepted.merges
            for duplicate in merge.duplicate_artist_ids
        }
        planned_removals.update(
            repair.malformed_artist_id for repair in accepted.version_repairs
        )
        planned_removals.update(
            repair.malformed_artist_id for repair in accepted.full_credit_repairs
        )
        full_credit_tracks = {
            repair.track_id for repair in accepted.full_credit_repairs
        }
        planned_credit_removals = {
            credit_id
            for credit_id, (artist_id, track_id) in credit_context_before.items()
            if artist_id in planned_removals or track_id in full_credit_tracks
        }
        reassigned = aliases = relationships = version_count = full_count = 0
        with self._transaction():
            for merge in accepted.merges:
                moved, alias_count, relation_count = self._merge_artist(merge)
                reassigned += moved
                aliases += alias_count
                relationships += relation_count
            for repair in accepted.version_repairs:
                version_count += self._apply_version_repair(repair)
            for repair in accepted.full_credit_repairs:
                full_count += self._apply_full_credit_repair(repair)
            artist_ids_after = {
                int(row[0]) for row in self.conn.execute("SELECT id FROM artists")
            }
            credit_ids_after = {
                int(row[0])
                for row in self.conn.execute("SELECT id FROM track_artist_credits")
            }
            deleted_artist_ids = artist_ids_before - artist_ids_after
            if not deleted_artist_ids <= planned_removals:
                raise ArtistConsolidationError(
                    "Artist consolidation removed an unplanned identity."
                )
            deleted_credit_ids = set(credit_context_before) - credit_ids_after
            if not deleted_credit_ids <= planned_credit_removals:
                raise ArtistConsolidationError(
                    "Artist consolidation removed unplanned credit evidence."
                )
            if self.conn.execute("PRAGMA foreign_key_check").fetchall():
                raise ArtistConsolidationError("Foreign-key validation failed.")
        return ArtistConsolidationReport(
            False,
            len(accepted.merges),
            accepted.duplicate_artist_count,
            reassigned,
            aliases,
            relationships,
            version_count,
            full_count,
            len(accepted.conflicts),
            len(deleted_artist_ids),
            len(deleted_credit_ids),
        )

    def run(self, *, dry_run: bool = True) -> ArtistConsolidationReport:
        plan = self.plan()
        if not dry_run:
            return self.apply(plan)
        return ArtistConsolidationReport(
            True,
            len(plan.merges),
            plan.duplicate_artist_count,
            sum(
                int(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM track_artist_credits WHERE artist_id=?",
                        (duplicate,),
                    ).fetchone()[0]
                )
                for merge in plan.merges
                for duplicate in merge.duplicate_artist_ids
            ),
            plan.duplicate_artist_count,
            0,
            len(plan.version_repairs),
            len(plan.full_credit_repairs),
            len(plan.conflicts),
        )


def analyze_existing_artist_consolidation(
    conn: sqlite3.Connection,
) -> ArtistConsolidationPlan:
    """Idempotent, local-only dry-run entry point for schema-v7 acceptance."""

    return ArtistConsolidationService(conn).plan()


def consolidate_existing_artists(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
) -> ArtistConsolidationReport:
    """Run canonical consolidation using only already-stored SQLite evidence."""

    return ArtistConsolidationService(conn).run(dry_run=dry_run)


__all__ = [
    "ArtistConsolidationError",
    "ArtistConsolidationPlan",
    "ArtistConsolidationReport",
    "ArtistConsolidationService",
    "ArtistIdentityConflict",
    "ArtistMerge",
    "FullCreditRepair",
    "StructuredCredit",
    "VersionArtistRepair",
    "analyze_existing_artist_consolidation",
    "consolidate_existing_artists",
    "normalize_artist_presentation",
]

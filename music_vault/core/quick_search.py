"""Bounded, local-only search over explicitly supplied desktop entities.

The index is a snapshot: its owner rebuilds it after library/identity changes.
Payloads carry already-resolved canonical keys; they are never inspected,
serialized or searched here. Searching cannot play, sync, query a provider or
write runtime data. Session recency is deliberately small and non-persistent.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass, field
from itertools import chain, islice
import unicodedata
from typing import Iterable, Literal


SearchKind = Literal["track", "album", "artist", "playlist", "action"]
GROUP_ORDER: tuple[SearchKind, ...] = ("track", "album", "artist", "playlist", "action")
MAX_RESULTS = 100
MAX_FUZZY_CANDIDATES = 256
_MAX_QUERY_CHARS = 256
_MAX_QUERY_TOKENS = 8
_MAX_INDEX_CHARS = 2048
_MAX_DOCUMENT_TOKENS = 64
_MAX_PREFIX_TOKENS = 64
_MAX_POSTING_SAMPLE = 512
_MAX_LEXICAL_CANDIDATES = 2048
_MAX_COARSE_CANDIDATES = 2048
_KIND_ORDER = {kind: i for i, kind in enumerate(GROUP_ORDER)}


def normalize_search_text(value: str) -> str:
    """Accent/case-insensitive matching, not an artist identity transformation."""

    decomposed = unicodedata.normalize("NFKD", value).casefold()
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in plain).split())


@dataclass(frozen=True, slots=True)
class SearchEntity:
    kind: SearchKind
    key: str
    label: str
    detail: str = ""
    payload: object | None = field(default=None, repr=False, compare=False)
    track_id: int | None = None
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in _KIND_ORDER:
            raise ValueError("Unsupported search entity kind")
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("Search entities require a stable non-empty string key")
        if not isinstance(self.label, str) or not isinstance(self.detail, str):
            raise TypeError("Search labels and details must be literal strings")
        aliases = tuple(self.aliases)
        if not all(isinstance(alias, str) for alias in aliases):
            raise TypeError("Search aliases must be literal strings")
        object.__setattr__(self, "aliases", aliases)
        if self.track_id is not None and (
            self.kind != "track" or type(self.track_id) is not int or self.track_id <= 0
        ):
            raise ValueError("Only track entities may carry a positive integer track ID")

    @property
    def identity(self) -> tuple[SearchKind, str]:
        return self.kind, self.key


@dataclass(frozen=True, slots=True)
class SearchGroup:
    kind: SearchKind
    items: tuple[SearchEntity, ...]


def group_results(results: Iterable[SearchEntity]) -> tuple[SearchGroup, ...]:
    """Group a ranked result snapshot, preserving rank within each group."""

    grouped: dict[SearchKind, list[SearchEntity]] = defaultdict(list)
    for entity in results:
        grouped[entity.kind].append(entity)
    return tuple(SearchGroup(kind, tuple(grouped[kind])) for kind in GROUP_ORDER if grouped[kind])


def track_result_ids(results: Iterable[SearchEntity]) -> tuple[int, ...]:
    """Capture explicit track-result order for a new Search playback context."""

    return tuple(dict.fromkeys(
        entity.track_id for entity in results
        if entity.kind == "track" and entity.track_id is not None
    ))


@dataclass(frozen=True, slots=True)
class SearchStatistics:
    """Aggregate diagnostics only: no query text, metadata, IDs or payloads."""

    lexical_candidates: int = 0
    fuzzy_candidates: int = 0
    trigram_postings_read: int = 0


@dataclass(frozen=True, slots=True)
class _Document:
    entity: SearchEntity
    label: str
    label_tokens: tuple[str, ...]
    aliases: tuple[str, ...]
    tokens: tuple[str, ...]


def _grams(token: str) -> frozenset[str]:
    return frozenset(token[i:i + 3] for i in range(len(token) - 2))


def _token_prefixes_match(query: tuple[str, ...], tokens: tuple[str, ...]) -> bool:
    return all(any(token.startswith(part) for token in tokens) for part in query)


def _edit_distance(left: str, right: str, maximum: int) -> int:
    """Bounded optimal-string-alignment distance, including adjacent typos."""

    if left == right:
        return 0
    if abs(len(left) - len(right)) > maximum:
        return maximum + 1
    previous = list(range(len(right) + 1))
    before_previous: list[int] | None = None
    for i, left_char in enumerate(left, 1):
        current = [maximum + 1] * (len(right) + 1)
        current[0] = i
        for j in range(max(1, i - maximum), min(len(right), i + maximum) + 1):
            cost = left_char != right[j - 1]
            current[j] = min(current[j - 1] + 1, previous[j] + 1, previous[j - 1] + cost)
            if (
                before_previous is not None and i > 1 and j > 1
                and left_char == right[j - 2] and left[i - 2] == right[j - 1]
            ):
                current[j] = min(current[j], before_previous[j - 2] + 1)
        if min(current) > maximum:
            return maximum + 1
        before_previous, previous = previous, current
    return previous[-1]


def _fuzzy_score(query: tuple[str, ...], document: _Document) -> float:
    scores: list[float] = []
    for part in query:
        if any(token.startswith(part) for token in document.tokens):
            scores.append(1.0)
            continue
        # Short queries intentionally use exact/prefix retrieval, not noisy fuzz.
        if len(part) < 4:
            return 0.0
        maximum = 1 if len(part) <= 5 else 2
        best = 0.0
        for token in document.tokens:
            if abs(len(part) - len(token)) > maximum:
                continue
            distance = _edit_distance(part, token, maximum)
            if distance <= maximum:
                best = max(best, 1.0 - distance / max(len(part), len(token)))
        if best < 0.67:
            return 0.0
        scores.append(best)
    return sum(scores) / len(scores) if scores else 0.0


class LocalSearchIndex:
    """Immutable indexed documents plus bounded, in-memory usage tie-breakers.

    ``context_keys`` contains stable entity keys, not display names. Context and
    recency never promote a fuzzy match above an exact/prefix match. Broad
    queries retrieve bounded candidates; they do not enumerate an entire 20k
    library or calculate edit distance against every document per keystroke.
    """

    def __init__(
        self,
        entities: Iterable[SearchEntity],
        *,
        fuzzy_candidate_limit: int = MAX_FUZZY_CANDIDATES,
        recency_limit: int = 64,
    ) -> None:
        if not 0 <= fuzzy_candidate_limit <= MAX_FUZZY_CANDIDATES:
            raise ValueError("Fuzzy candidate limit must be between 0 and 256")
        if not 0 <= recency_limit <= 256:
            raise ValueError("Recency limit must be between 0 and 256")
        documents: list[_Document] = []
        by_identity: dict[tuple[SearchKind, str], int] = {}
        by_key: dict[str, list[int]] = defaultdict(list)
        exact: dict[str, list[int]] = defaultdict(list)
        postings: dict[str, list[int]] = defaultdict(list)
        trigram_postings: dict[str, list[int]] = defaultdict(list)
        for entity in entities:
            if entity.identity in by_identity:
                raise ValueError("Search entity identities must be unique")
            label = normalize_search_text(entity.label[:_MAX_INDEX_CHARS])
            detail = normalize_search_text(entity.detail[:_MAX_INDEX_CHARS])
            aliases = tuple(
                normalize_search_text(alias[:_MAX_INDEX_CHARS]) for alias in entity.aliases[:16]
            )
            label_tokens = tuple(dict.fromkeys(label.split()))[:_MAX_DOCUMENT_TOKENS]
            tokens = tuple(dict.fromkeys(" ".join((label, detail, *aliases)).split()))[:_MAX_DOCUMENT_TOKENS]
            index = len(documents)
            documents.append(_Document(entity, label, label_tokens, aliases, tokens))
            by_identity[entity.identity] = index
            by_key[entity.key].append(index)
            if label:
                exact[label].append(index)
            for token in tokens:
                postings[token].append(index)
            for gram in set().union(*(_grams(token) for token in tokens)) if tokens else ():
                trigram_postings[gram].append(index)
        self._documents = tuple(documents)
        self._by_identity = by_identity
        self._by_key = {key: tuple(values) for key, values in by_key.items()}
        self._exact = {key: tuple(values) for key, values in exact.items()}
        self._postings = {key: tuple(values) for key, values in postings.items()}
        self._trigram_postings = {key: tuple(values) for key, values in trigram_postings.items()}
        self._tokens = tuple(sorted(postings))
        self._labels = tuple(sorted((document.label, i) for i, document in enumerate(documents)))
        self._actions = tuple(i for i, document in enumerate(documents) if document.entity.kind == "action")
        self._fuzzy_candidate_limit = fuzzy_candidate_limit
        self._recency_limit = recency_limit
        self._recent: OrderedDict[tuple[SearchKind, str], int] = OrderedDict()
        self._use_sequence = 0
        self.last_search_stats = SearchStatistics()

    def __len__(self) -> int:
        return len(self._documents)

    @property
    def recent_count(self) -> int:
        return len(self._recent)

    def mark_used(self, kind: SearchKind, key: str) -> None:
        """Remember a resolved user action; stale identities are ignored."""

        identity = (kind, key)
        if not self._recency_limit or identity not in self._by_identity:
            return
        self._use_sequence += 1
        self._recent.pop(identity, None)
        self._recent[identity] = self._use_sequence
        while len(self._recent) > self._recency_limit:
            self._recent.popitem(last=False)

    def search(
        self,
        query: str,
        limit: int = 40,
        *,
        context_keys: Iterable[str] = (),
    ) -> tuple[SearchEntity, ...]:
        limit = min(MAX_RESULTS, max(0, limit))
        self.last_search_stats = SearchStatistics()
        if not limit:
            return ()
        normalized = normalize_search_text(query[:_MAX_QUERY_CHARS])
        query_tokens = tuple(normalized.split())[:_MAX_QUERY_TOKENS]
        normalized = " ".join(query_tokens)
        context = frozenset(islice(context_keys, 256))
        if not normalized:
            recent = (self._by_identity[identity] for identity in reversed(self._recent))
            indices = tuple(islice(dict.fromkeys(chain(recent, self._actions)), limit))
            return tuple(self._documents[index].entity for index in indices)

        candidates: set[int] = set(islice(self._exact.get(normalized, ()), _MAX_LEXICAL_CANDIDATES))
        start = bisect_left(self._labels, (normalized, -1))
        for label, index in self._labels[start:start + _MAX_POSTING_SAMPLE]:
            if not label.startswith(normalized):
                break
            candidates.add(index)

        # Start from the rarest queried token's postings. This avoids spending
        # the budget on a common word when another query word is distinctive.
        token_sources: list[list[tuple[int, ...]]] = []
        for part in query_tokens:
            token_start = bisect_left(self._tokens, part)
            sources: list[tuple[int, ...]] = []
            for token in self._tokens[token_start:token_start + _MAX_PREFIX_TOKENS]:
                if not token.startswith(part):
                    break
                sources.append(self._postings[token])
            if sources:
                token_sources.append(sources)
        if token_sources:
            sources = min(token_sources, key=lambda group: sum(map(len, group)))
            for source in sorted(sources, key=len):
                for index in islice(source, _MAX_POSTING_SAMPLE):
                    if len(candidates) >= _MAX_LEXICAL_CANDIDATES:
                        break
                    candidates.add(index)

        # Recent items are cheap to include even if they were beyond a broad
        # prefix's sample; matching still decides whether they appear at all.
        candidates.update(self._by_identity[identity] for identity in self._recent)
        for key in context:
            candidates.update(self._by_key.get(key, ()))
        ranked: dict[int, tuple[int, float]] = {}
        for index in candidates:
            lexical = self._lexical_rank(normalized, query_tokens, self._documents[index])
            if lexical is not None:
                ranked[index] = lexical

        fuzzy_count = 0
        postings_read = 0
        if self._fuzzy_candidate_limit and any(len(part) >= 4 for part in query_tokens):
            grams = set().union(*(_grams(part) for part in query_tokens))
            coarse: Counter[int] = Counter()
            sources = sorted((self._trigram_postings[gram] for gram in grams if gram in self._trigram_postings), key=len)
            for source in sources:
                for index in islice(source, _MAX_POSTING_SAMPLE):
                    postings_read += 1
                    if index in ranked:
                        continue
                    if index in coarse or len(coarse) < _MAX_COARSE_CANDIDATES:
                        coarse[index] += 1
            for index, _shared in sorted(coarse.items(), key=lambda item: (-item[1], item[0]))[:self._fuzzy_candidate_limit]:
                fuzzy_count += 1
                score = _fuzzy_score(query_tokens, self._documents[index])
                if score:
                    ranked[index] = (5, -score)

        self.last_search_stats = SearchStatistics(len(candidates), fuzzy_count, postings_read)

        def ordering(index: int) -> tuple:
            document = self._documents[index]
            entity = document.entity
            return (
                *ranked[index], entity.key not in context,
                -self._recent.get(entity.identity, 0), _KIND_ORDER[entity.kind],
                document.label, entity.key,
            )

        return tuple(self._documents[index].entity for index in sorted(ranked, key=ordering)[:limit])

    @staticmethod
    def _lexical_rank(
        query: str, parts: tuple[str, ...], document: _Document,
    ) -> tuple[int, float] | None:
        if document.label == query:
            return (0, 0.0)
        if document.label.startswith(query):
            return (1, 0.0)
        if _token_prefixes_match(parts, document.label_tokens):
            return (2, 0.0)
        if any(alias == query or alias.startswith(query) for alias in document.aliases):
            return (3, 0.0)
        if _token_prefixes_match(parts, document.tokens):
            return (4, 0.0)
        return None

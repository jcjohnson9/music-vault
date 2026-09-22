from dataclasses import FrozenInstanceError

import pytest

from music_vault.core.library_browser import AlbumKey, ArtistKey
from music_vault.core.quick_search import (
    GROUP_ORDER,
    MAX_FUZZY_CANDIDATES,
    MAX_RESULTS,
    LocalSearchIndex,
    SearchEntity,
    group_results,
    normalize_search_text,
    track_result_ids,
)


def track(track_id, label, detail="", **kwargs):
    return SearchEntity("track", str(track_id), label, detail, track_id=track_id, **kwargs)


def keys(results):
    return [entity.key for entity in results]


def test_exact_then_label_prefix_then_token_then_detail_then_fuzzy():
    index = LocalSearchIndex([
        track(5, "Other", "Starlight"),
        track(4, "Evening Starlight"),
        track(3, "Starlight Echo"),
        track(2, "Starlight"),
        track(1, "Starliht"),
    ])
    assert keys(index.search("starlight")) == ["2", "3", "4", "5", "1"]


def test_accent_casefold_and_unicode_whitespace_do_not_change_labels():
    entity = track(1, "Beyoncé\u00a0CAFÉ — Straße")
    index = LocalSearchIndex([entity])
    assert index.search("BEYONCE cafe strasse") == (entity,)
    assert entity.label == "Beyoncé\u00a0CAFÉ — Straße"
    assert normalize_search_text("  e\u0301COLE\u2003Ångström ") == "ecole angstrom"


@pytest.mark.parametrize("query", ["bohemain", "bohemion", "bohemia", "bhemi an"])
def test_typo_retrieval_is_bounded_and_conservative(query):
    index = LocalSearchIndex([track(1, "Bohemian Rhapsody"), track(2, "Nocturne")])
    result = index.search(query)
    if query == "bhemi an":
        assert result == ()  # No fabricated match for several incompatible tokens.
    else:
        assert keys(result) == ["1"]
    assert index.last_search_stats.fuzzy_candidates <= MAX_FUZZY_CANDIDATES


def test_two_word_typo_uses_both_words_not_one_incidental_match():
    index = LocalSearchIndex([
        track(1, "Silver Mountain"), track(2, "Silver Morning"), track(3, "Golden Mountain"),
    ])
    assert keys(index.search("silvre mountian")) == ["1"]


def test_short_queries_do_not_fuzzily_match_unrelated_words():
    index = LocalSearchIndex([track(1, "Cat"), track(2, "Cut"), track(3, "Scatter")])
    assert keys(index.search("cat")) == ["1"]
    assert keys(index.search("sc")) == ["3"]
    assert index.last_search_stats.fuzzy_candidates == 0


def test_duplicate_album_labels_retain_distinct_canonical_keys_and_payloads():
    first_key = AlbumKey("home", "performer one", canonical_album_id=1)
    second_key = AlbumKey("home", "performer two", canonical_album_id=2)
    first = SearchEntity("album", first_key.browser_key, "Home", "Performer One", payload=first_key)
    second = SearchEntity("album", second_key.browser_key, "Home", "Performer Two", payload=second_key)
    results = LocalSearchIndex([first, second]).search("home")
    assert len(results) == 2
    assert {entity.key for entity in results} == {first_key.browser_key, second_key.browser_key}
    assert any(entity.payload is first_key for entity in results)
    assert any(entity.payload is second_key for entity in results)


def test_literal_ampersand_artist_is_not_split_or_converted_to_identity():
    canonical = ArtistKey("earth wind & fire", artist_id=42)
    entity = SearchEntity("artist", canonical.browser_key, "Earth, Wind & Fire", payload=canonical)
    index = LocalSearchIndex([entity])
    assert index.search("earth wind fire") == (entity,)
    assert index.search("fire")[0].payload is canonical
    assert len(index) == 1


def test_aliases_resolve_same_entity_without_creating_extra_identities():
    entity = SearchEntity("artist", "canonical:1", "Canonical Name", aliases=("Old Name",))
    index = LocalSearchIndex([entity])
    assert index.search("old name") == (entity,)
    assert len(index) == 1


def test_results_group_in_product_order_preserving_rank_within_each_group():
    entities = [
        SearchEntity("action", "settings", "Open Settings"),
        SearchEntity("artist", "artist:1", "Artist"),
        track(2, "Second"),
        SearchEntity("playlist", "playlist:1", "Mix"),
        SearchEntity("album", "album:1", "Album"),
        track(1, "First"),
    ]
    grouped = group_results(entities)
    assert tuple(group.kind for group in grouped) == GROUP_ORDER
    assert keys(grouped[0].items) == ["2", "1"]
    assert group_results(()) == ()


def test_track_results_form_explicit_unique_ordered_playback_snapshot():
    results = (
        track(7, "Seven"), SearchEntity("album", "a", "Album"),
        track(3, "Three"), track(7, "Seven again"),
        SearchEntity("track", "unresolved", "Unavailable"),
    )
    assert track_result_ids(results) == (7, 3)


def test_requested_limit_and_absolute_result_cap():
    index = LocalSearchIndex([track(i, f"Signal {i:04}") for i in range(1, 301)])
    assert len(index.search("signal")) == 40
    assert len(index.search("signal", limit=7)) == 7
    assert len(index.search("signal", limit=10000)) == MAX_RESULTS
    assert index.search("signal", limit=0) == ()
    assert index.search("signal", limit=-3) == ()


def test_session_recency_breaks_ties_but_never_outweighs_exact_quality():
    index = LocalSearchIndex([track(1, "Glow"), track(2, "Glow Extended"), track(3, "Glow Extended")])
    index.mark_used("track", "3")
    assert keys(index.search("glow")) == ["1", "3", "2"]
    index.mark_used("track", "2")
    assert keys(index.search("glow")) == ["1", "2", "3"]


def test_context_breaks_equal_quality_ties_using_keys_not_display_names():
    index = LocalSearchIndex([track(1, "Glow"), track(2, "Glow"), track(3, "Glow Extended")])
    assert keys(index.search("glow", context_keys=["2", "3"])) == ["2", "1", "3"]
    assert keys(index.search("glow", context_keys=["Glow"])) == ["1", "2", "3"]


def test_context_and_recency_can_retrieve_beyond_broad_prefix_sample():
    index = LocalSearchIndex([track(i, f"Ambient {i:05}") for i in range(1, 5001)])
    index.mark_used("track", "5000")
    results = index.search("ambient", limit=2, context_keys=["4999"])
    assert keys(results) == ["4999", "5000"]


def test_recency_has_bounded_nonpersistent_lifetime_and_ignores_stale_keys():
    entities = [track(i, "Glow") for i in range(1, 6)]
    index = LocalSearchIndex(entities, recency_limit=2)
    for i in range(1, 6):
        index.mark_used("track", str(i))
    index.mark_used("track", "not-present")
    assert index.recent_count == 2
    assert keys(index.search("glow"))[:2] == ["5", "4"]
    assert LocalSearchIndex(entities).recent_count == 0
    disabled = LocalSearchIndex(entities, recency_limit=0)
    disabled.mark_used("track", "1")
    assert disabled.recent_count == 0


def test_empty_query_shows_recent_items_and_destinations_not_entire_library():
    action = SearchEntity("action", "sync-center", "Open Sync Center")
    index = LocalSearchIndex([track(1, "Hidden"), action, track(2, "Recent")])
    assert index.search("") == (action,)
    index.mark_used("track", "2")
    assert keys(index.search("  ")) == ["2", "sync-center"]
    assert keys(index.search("", limit=1)) == ["2"]


def test_search_never_evaluates_or_searches_opaque_payload():
    class OpaquePayload:
        def __str__(self):
            raise AssertionError("Payload must not be converted to text")

        def __repr__(self):
            raise AssertionError("Payload must not be printed")

        def __getattr__(self, name):
            raise AssertionError("Payload must not be introspected")

    entity = SearchEntity("action", "sync-center", "Open Sync Center", payload=OpaquePayload())
    index = LocalSearchIndex([entity])
    assert index.search("sync") == (entity,)
    assert "OpaquePayload" not in repr(entity)
    assert index.search("youtube_api_key") == ()
    assert index.search("Discogs") == ()


def test_label_markup_is_preserved_as_literal_metadata_not_executed():
    entity = track(1, '<img src="x"> & <script>literal</script>')
    index = LocalSearchIndex([entity])
    assert index.search("literal") == (entity,)
    assert entity.label == '<img src="x"> & <script>literal</script>'


def test_documents_are_immutable_snapshots_with_explicit_rebuild():
    source = [track(1, "Original")]
    index = LocalSearchIndex(source)
    source.append(track(2, "Added Later"))
    assert index.search("added") == ()
    assert keys(LocalSearchIndex(source).search("added")) == ["2"]
    with pytest.raises(FrozenInstanceError):
        source[0].label = "Changed"


def test_same_key_in_different_entity_kinds_does_not_collide():
    index = LocalSearchIndex([track(1, "Match"), SearchEntity("playlist", "1", "Match")])
    assert [entity.kind for entity in index.search("match")] == ["track", "playlist"]
    with pytest.raises(ValueError, match="unique"):
        LocalSearchIndex([track(1, "First"), track(1, "Duplicate")])


@pytest.mark.parametrize("kwargs", [
    {"fuzzy_candidate_limit": -1}, {"fuzzy_candidate_limit": 257},
    {"recency_limit": -1}, {"recency_limit": 257},
])
def test_candidate_and_recency_budgets_cannot_be_unbounded(kwargs):
    with pytest.raises(ValueError):
        LocalSearchIndex([], **kwargs)


def test_twenty_thousand_documents_do_not_get_full_fuzzy_scan():
    entities = [track(i, f"Northern Lantern {i:05}", "Synthetic performer") for i in range(1, 20001)]
    index = LocalSearchIndex(entities, fuzzy_candidate_limit=23)
    results = index.search("northrn lantern")
    assert results
    assert index.last_search_stats.fuzzy_candidates <= 23
    assert index.last_search_stats.lexical_candidates <= 2048
    assert index.last_search_stats.trigram_postings_read < 20000
    assert len(results) <= 40


def test_disabling_fuzzy_still_preserves_exact_and_token_results():
    index = LocalSearchIndex([track(1, "Northern Lantern")], fuzzy_candidate_limit=0)
    assert keys(index.search("lantern north")) == ["1"]
    assert index.search("northrn") == ()
    assert index.last_search_stats.fuzzy_candidates == 0


def test_multiword_retrieval_starts_from_distinctive_token():
    entities = [track(i, f"Common {i:05}") for i in range(1, 3001)]
    entities.append(track(3001, "Common Distinctive"))
    assert keys(LocalSearchIndex(entities).search("common distinctive")) == ["3001"]


def test_aggregate_diagnostics_do_not_record_query_text_or_metadata():
    index = LocalSearchIndex([track(1, "Private-looking title")])
    index.search("Private-looking title")
    assert "Private" not in repr(index.last_search_stats)
    assert "title" not in repr(index.last_search_stats)


@pytest.mark.parametrize("kwargs", [
    {"kind": "provider", "key": "x", "label": "Unknown"},
    {"kind": "track", "key": "", "label": "Missing key"},
    {"kind": "track", "key": "x", "label": "Invalid ID", "track_id": True},
    {"kind": "track", "key": "x", "label": "Invalid ID", "track_id": -1},
    {"kind": "artist", "key": "x", "label": "Invalid ID", "track_id": 1},
])
def test_entity_requires_valid_stable_identity(kwargs):
    with pytest.raises(ValueError):
        SearchEntity(**kwargs)

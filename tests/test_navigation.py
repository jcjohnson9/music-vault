import pytest

from music_vault.core.library_browser import AlbumKey, ArtistKey
from music_vault.core.navigation import NavigationHistory, Route, ViewState


def test_history_restores_filter_selection_scroll_without_playback_authority():
    history = NavigationHistory()
    state = ViewState(Route(), "synthetic", (7, 3), 7, 280, 1, True)
    history.save(state)
    playlist = Route("custom", playlist_id=4, label="Example Mix")
    history.visit(playlist)
    history.save(ViewState(playlist, "other", (2,), 2, 90))
    assert history.back() == state
    assert history.forward().current_id == 2
    assert history.current.route == playlist


def test_visit_after_back_discards_forward_and_keeps_per_route_state():
    history = NavigationHistory()
    history.save(ViewState(Route(), "remember"))
    history.visit(Route("albums", label="Albums"))
    history.back()
    history.visit(Route("artists", label="Artists"))
    assert history.forward() is None
    assert history.back().query == "remember"


def test_refresh_or_renaming_route_does_not_add_history_entry():
    route = Route("custom", 4, label="Old name")
    history = NavigationHistory(route)
    history.save(ViewState(route, "query", (2,), 2, 40))
    renamed = Route("custom", 4, label="New name")
    assert history.visit(renamed).route.label == "New name"
    assert history.current.query == "query"
    assert not history.can_back


def test_detail_identity_never_carries_a_stale_playlist_id():
    album = AlbumKey("record", "artist", canonical_album_id=2)
    artist = ArtistKey("artist", artist_id=3)
    assert Route("album_tracks", entity_key=album).playlist_id is None
    assert Route("artist_tracks", entity_key=artist).playlist_id is None
    with pytest.raises(ValueError):
        Route("album_tracks", playlist_id=4, entity_key=album)
    with pytest.raises(ValueError):
        Route("custom")
    with pytest.raises(ValueError):
        Route("artist_tracks")


def test_history_and_saved_states_are_bounded():
    history = NavigationHistory(limit=3)
    for number in range(10):
        history.visit(Route("custom", number))
        history.save(ViewState(history.current.route, str(number)))
    assert len(history._states) == 3
    assert history.back().query == "8"
    assert history.back().query == "7"
    assert history.back() is None
    with pytest.raises(ValueError):
        history.save(ViewState(Route()))

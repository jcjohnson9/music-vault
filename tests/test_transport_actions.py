from dataclasses import replace

import pytest

from music_vault.core.transport_actions import (
    TransportActions, TransportSnapshot, context_capabilities,
)


def test_explicit_play_pause_are_idempotent_but_toggle_is_not():
    state = TransportSnapshot(loaded=True, state="paused")
    calls = []

    def toggle():
        nonlocal state
        calls.append("toggle")
        state = replace(state, state="paused" if state.state == "playing" else "playing")

    actions = TransportActions(lambda: state, toggle=toggle,
                               next_track=lambda: calls.append("next"),
                               previous_track=lambda: calls.append("previous"))
    for command in ("play", "play", "pause", "pause", "toggle", "toggle"):
        assert actions.dispatch(command)
    assert calls == ["toggle"] * 4
    actions.closed = True
    assert not actions.dispatch("play")
    assert len(calls) == 4


@pytest.mark.parametrize("command", ["play", "pause", "toggle", "next", "previous", "volume_up"])
def test_empty_player_never_starts_browsed_selection(command):
    actions = TransportActions(lambda: TransportSnapshot(),
                               toggle=lambda: pytest.fail("unexpected toggle"),
                               next_track=lambda: pytest.fail("unexpected next"),
                               previous_track=lambda: pytest.fail("unexpected previous"))
    assert not actions.dispatch(command)


def test_navigation_delegates_without_debounce_or_queue_mutation():
    calls = []
    state = TransportSnapshot(can_next=True, can_previous=True)
    actions = TransportActions(lambda: state, toggle=lambda: None,
                               next_track=lambda: calls.append("next"),
                               previous_track=lambda: calls.append("previous"))
    assert actions.dispatch("next")
    assert actions.dispatch("next")
    assert actions.dispatch("previous")
    assert calls == ["next", "next", "previous"]


@pytest.mark.parametrize("ids,current,queue,shuffle,repeat,expected", [
    ([], None, 0, False, "off", (False, False)),
    ([], None, 1, False, "off", (True, False)),
    ([1, 2, 3], 1, 0, False, "off", (True, False)),
    ([1, 2, 3], 2, 0, False, "off", (True, True)),
    ([1, 2, 3], 3, 0, False, "off", (False, True)),
    ([1, 2, 3], 3, 1, False, "off", (True, True)),
    ([1], 1, 0, False, "one", (False, False)),
    ([1], 1, 0, True, "one", (True, False)),
    ([1], 1, 0, False, "all", (True, True)),
    ([1, 2], 99, 0, False, "off", (True, False)),
])
def test_capabilities_use_only_captured_base_and_fifo(ids, current, queue, shuffle, repeat, expected):
    assert context_capabilities(ids, current, queue, shuffle=shuffle, repeat=repeat) == expected

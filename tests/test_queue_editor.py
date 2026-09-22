from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from music_vault.core.queue_editor import ManualQueueEditor, preview_continuation


def test_fifo_uses_the_host_list_and_preserves_duplicate_occurrences():
    queue = [4, 4]
    identity = id(queue)
    editor = ManualQueueEditor(lambda: queue)
    first = editor.snapshot()
    assert first.entries[0].token != first.entries[1].token
    last = editor.append(8)
    assert id(queue) == identity
    assert queue == [4, 4, 8]
    assert last.entries[:2] == first.entries
    assert [editor.pop_next(), editor.pop_next(), editor.pop_next(), editor.pop_next()] == [4, 4, 8, None]
    assert queue == []


def test_remove_is_occurrence_specific_and_stale_revision_is_rejected():
    queue = [7, 2, 7]
    editor = ManualQueueEditor(lambda: queue)
    old = editor.snapshot()
    assert editor.remove(old.entries[2].token, old.revision)
    new = editor.snapshot()
    assert queue == [7, 2]
    assert new.entries == old.entries[:2]
    assert not editor.remove(old.entries[0].token, old.revision)
    assert editor.snapshot() == new
    assert not editor.remove(99999, new.revision)
    assert editor.snapshot() == new


def test_move_uses_occurrences_and_before_token_not_track_id():
    queue = [8, 2, 8, 3]
    editor = ManualQueueEditor(lambda: queue)
    old = editor.snapshot()
    assert editor.move(old.entries[2].token, old.entries[0].token, old.revision)
    moved = editor.snapshot()
    assert queue == [8, 8, 2, 3]
    assert moved.entries[0] == old.entries[2]
    assert editor.move(moved.entries[0].token, None, moved.revision)
    assert queue == [8, 2, 3, 8]


def test_identical_track_move_is_still_an_occurrence_edit():
    queue = [8, 8]
    editor = ManualQueueEditor(lambda: queue)
    old = editor.snapshot()
    assert editor.move(old.entries[1].token, old.entries[0].token, old.revision)
    new = editor.snapshot()
    assert queue == [8, 8]
    assert new.entries == tuple(reversed(old.entries))
    assert new.revision > old.revision


@pytest.mark.parametrize("case", ["self", "same_place", "last_to_end", "bad_source", "bad_destination", "stale"])
def test_noop_moves_do_not_change_revision_or_existing_undo(case):
    queue = [1, 2, 3, 4]
    editor = ManualQueueEditor(lambda: queue)
    initial = editor.snapshot()
    assert editor.remove(initial.entries[-1].token, initial.revision)
    old = editor.snapshot()
    first, second, last = old.entries
    args = {
        "self": (first.token, first.token, old.revision),
        "same_place": (first.token, second.token, old.revision),
        "last_to_end": (last.token, None, old.revision),
        "bad_source": (-1, second.token, old.revision),
        "bad_destination": (first.token, -1, old.revision),
        "stale": (first.token, None, old.revision - 1),
    }
    assert not editor.move(*args[case])
    assert editor.snapshot() == old
    assert queue == [1, 2, 3]


def test_clear_and_single_level_undo_restore_occurrences_in_place():
    queue = [1, 1, 2]
    editor = ManualQueueEditor(lambda: queue)
    old = editor.snapshot()
    assert editor.clear(old.revision)
    cleared = editor.snapshot()
    assert queue == [] and cleared.can_undo
    assert not editor.clear(cleared.revision)
    assert not editor.undo(old.revision)
    assert editor.undo(cleared.revision)
    restored = editor.snapshot()
    assert queue == [1, 1, 2]
    assert restored.entries == old.entries
    assert not restored.can_undo
    assert not editor.undo(restored.revision)


def test_second_edit_replaces_not_stacks_undo():
    queue = [1, 2, 3]
    editor = ManualQueueEditor(lambda: queue)
    old = editor.snapshot()
    editor.remove(old.entries[0].token, old.revision)
    after_remove = editor.snapshot()
    editor.clear(after_remove.revision)
    editor.undo(editor.snapshot().revision)
    assert queue == [2, 3]
    assert not editor.undo(editor.snapshot().revision)


@pytest.mark.parametrize("advance", ["append", "consume"])
def test_playback_or_append_expires_undo_and_rejects_old_commands(advance):
    queue = [1, 2, 3]
    editor = ManualQueueEditor(lambda: queue)
    old = editor.snapshot()
    editor.remove(old.entries[1].token, old.revision)
    edited = editor.snapshot()
    if advance == "append":
        editor.append(4)
        assert queue == [1, 3, 4]
    else:
        assert editor.pop_next() == 1
        assert queue == [3]
    new = editor.snapshot()
    assert not new.can_undo
    assert not editor.undo(edited.revision)
    assert not editor.undo(new.revision)
    assert not editor.remove(edited.entries[0].token, edited.revision)


@pytest.mark.parametrize("change", ["replace_same_contents", "external_append", "external_reorder", "external_pop"])
def test_unknown_external_host_changes_invalidate_all_tokens_and_undo(change):
    host = {"queue": [1, 2, 3]}
    editor = ManualQueueEditor(lambda: host["queue"])
    original = editor.snapshot()
    editor.remove(original.entries[-1].token, original.revision)
    old = editor.snapshot()
    if change == "replace_same_contents":
        host["queue"] = [1, 2]
    elif change == "external_append":
        host["queue"].append(4)
    elif change == "external_reorder":
        host["queue"].reverse()
    else:
        host["queue"].pop(0)
    new = editor.snapshot()
    assert new.revision > old.revision
    assert not new.can_undo
    assert not {entry.token for entry in old.entries} & {entry.token for entry in new.entries}
    assert not editor.clear(old.revision)
    assert not editor.remove(old.entries[0].token, new.revision)
    assert not editor.undo(new.revision)


def test_external_mutation_is_detected_even_without_requesting_snapshot():
    queue = [1, 2]
    editor = ManualQueueEditor(lambda: queue)
    old = editor.snapshot()
    queue.pop(0)
    assert not editor.clear(old.revision)
    assert queue == [2]


def test_snapshots_are_immutable_and_not_a_playback_authority():
    queue = [1]
    editor = ManualQueueEditor(lambda: queue)
    old = editor.snapshot()
    with pytest.raises(FrozenInstanceError):
        old.entries[0].track_id = 5
    editor.append(2)
    assert tuple(entry.track_id for entry in old.entries) == (1,)
    assert queue == [1, 2]


def test_nonlist_host_queue_is_rejected():
    with pytest.raises(TypeError):
        ManualQueueEditor(lambda: (1, 2))


def test_sequential_preview_anchors_to_last_base_song_not_current_queue_song():
    preview = preview_continuation([11, 12, 13, 14], 12, False, True, "off", True)
    assert preview.track_ids == (13, 14)
    assert preview.mode == "sequential"
    assert preview.automatic
    assert not preview.wraps


def test_preview_does_not_mutate_the_captured_context():
    ids = [1, 2, 3]
    preview_continuation(ids, 2, True, False, "off", True)
    assert ids == [1, 2, 3]


def test_auto_off_still_explains_automatic_fifo_but_manual_base_next():
    preview = preview_continuation([1, 2, 3], 1, False, False, "off", True)
    assert preview.track_ids == (2, 3)
    assert not preview.automatic
    assert "Auto is off" in preview.explanation
    assert "use Next" in preview.explanation
    assert "even when Auto is off" in preview.queue_explanation


@pytest.mark.parametrize("shuffle", [False, True])
def test_repeat_one_blocks_automatic_queue_and_base_but_next_bypasses(shuffle):
    preview = preview_continuation([1, 2, 3], 1, shuffle, not shuffle, "one", True)
    assert preview.repeat_one_blocks
    assert not preview.automatic
    assert "Next bypasses" in preview.explanation
    assert "current track" in preview.queue_explanation
    assert "Next" in preview.queue_explanation


def test_shuffle_is_a_possible_pool_excluding_current_base_not_exact_order():
    preview = preview_continuation([1, 2, 3], 2, True, False, "off", True)
    assert preview.mode == "shuffle"
    assert preview.track_ids == (1, 3)
    assert preview.automatic
    assert "not chosen yet" in preview.explanation


def test_shuffle_single_track_falls_back_like_existing_transport():
    preview = preview_continuation([5], 5, True, False, "off", True)
    assert preview.track_ids == (5,)
    assert preview.automatic


def test_repeat_all_shows_one_complete_cycle_even_when_auto_is_off():
    preview = preview_continuation([1, 2, 3, 4], 2, False, False, "all", True)
    assert preview.track_ids == (3, 4, 1, 2)
    assert preview.wraps and preview.automatic


def test_repeat_all_at_end_shows_start_and_off_shows_end():
    wrapped = preview_continuation([1, 2, 3], 3, False, False, "all", True)
    assert wrapped.track_ids == (1, 2, 3)
    stopped = preview_continuation([1, 2, 3], 3, False, True, "off", True)
    assert stopped.track_ids == ()
    assert not stopped.automatic
    assert "End" in stopped.explanation


@pytest.mark.parametrize("missing", [None, 99])
def test_missing_base_anchor_starts_at_beginning(missing):
    preview = preview_continuation([1, 2, 3], missing, False, True, "off", True)
    assert preview.track_ids == (1, 2, 3)


def test_no_loaded_track_never_claims_automatic_progression_or_repeat_block():
    preview = preview_continuation([1, 2, 3], 1, False, True, "one", False)
    assert not preview.automatic and not preview.repeat_one_blocks
    assert "Nothing is playing" in preview.explanation
    assert "Use Next" in preview.queue_explanation


def test_empty_context_is_explicit():
    preview = preview_continuation([], None, False, True, "off", True)
    assert preview.mode == "empty" and preview.track_ids == ()
    assert not preview.automatic
    assert "No captured playback context" in preview.explanation

"""Tests for the pure-data queue. No discord/yt-dlp imports involved."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from services.guild_queue import GuildQueue, GuildQueueRegistry, LoopMode


@dataclass(frozen=True)
class FakeTrack:
    """Stand-in for ``TrackInfo`` so queue tests don't depend on yt-dlp."""

    title: str


def _track(title: str) -> FakeTrack:
    return FakeTrack(title=title)


# -- advance / FIFO ----------------------------------------------------------


def test_advance_returns_none_when_queue_empty() -> None:
    assert GuildQueue().advance() is None


def test_advance_pops_in_fifo_order() -> None:
    q = GuildQueue()
    q.enqueue(_track("a"))
    q.enqueue(_track("b"))
    assert q.advance().title == "a"
    assert q.advance().title == "b"
    assert q.advance() is None


def test_advance_sets_current_to_popped_track() -> None:
    q = GuildQueue()
    q.enqueue(_track("a"))
    q.advance()
    assert q.current is not None and q.current.title == "a"


def test_advance_clears_current_when_exhausted_in_off_mode() -> None:
    q = GuildQueue()
    q.enqueue(_track("only"))
    q.advance()
    q.advance()
    assert q.current is None


# -- enqueue -----------------------------------------------------------------


def test_enqueue_returns_one_based_position() -> None:
    q = GuildQueue()
    assert q.enqueue(_track("a")) == 1
    assert q.enqueue(_track("b")) == 2
    assert q.enqueue(_track("c")) == 3


def test_enqueue_front_jumps_the_line() -> None:
    q = GuildQueue()
    q.enqueue(_track("a"))
    q.enqueue(_track("b"))
    q.enqueue_front(_track("urgent"))
    assert [t.title for t in q.upcoming()] == ["urgent", "a", "b"]


# -- regression: !play while playing should enqueue, not error --------------


def test_play_while_playing_appends_instead_of_raising() -> None:
    """Regression for the original ``ClientException: Already playing audio``.

    The cog uses ``voice_client.is_playing()`` to decide whether to start or
    enqueue, but the queue itself must accept multiple appends without
    complaint — that's the data invariant the cog relies on.
    """
    q = GuildQueue()
    q.enqueue(_track("first"))
    q.advance()  # "first" is now playing
    # second `!play` arrives while voice_client is busy -> enqueue path
    position = q.enqueue(_track("second"))
    assert position == 1
    assert q.current is not None and q.current.title == "first"
    assert [t.title for t in q.upcoming()] == ["second"]


# -- loop modes --------------------------------------------------------------


def test_track_loop_repeats_current_indefinitely() -> None:
    q = GuildQueue()
    q.enqueue(_track("only"))
    q.advance()
    q.loop_mode = LoopMode.TRACK
    for _ in range(5):
        assert q.advance().title == "only"


def test_queue_loop_recycles_finished_track_to_back() -> None:
    q = GuildQueue()
    q.enqueue(_track("a"))
    q.enqueue(_track("b"))
    q.loop_mode = LoopMode.QUEUE
    assert q.advance().title == "a"
    assert q.advance().title == "b"
    # "a" was recycled when we left it for "b"
    assert q.advance().title == "a"


def test_off_mode_drops_finished_track() -> None:
    q = GuildQueue()
    q.enqueue(_track("a"))
    q.enqueue(_track("b"))
    assert q.advance().title == "a"
    assert q.advance().title == "b"
    assert q.advance() is None


# -- removal -----------------------------------------------------------------


def test_remove_at_pops_one_based_index() -> None:
    q = GuildQueue()
    for title in ("a", "b", "c"):
        q.enqueue(_track(title))
    removed = q.remove_at(2)
    assert removed.title == "b"
    assert [t.title for t in q.upcoming()] == ["a", "c"]


def test_remove_at_invalid_index_raises_with_range_in_message() -> None:
    q = GuildQueue()
    q.enqueue(_track("a"))
    with pytest.raises(IndexError, match=r"out of range; expected 1\.\.1"):
        q.remove_at(99)


def test_remove_at_zero_is_rejected() -> None:
    q = GuildQueue()
    q.enqueue(_track("a"))
    with pytest.raises(IndexError):
        q.remove_at(0)


def test_remove_last_pops_back() -> None:
    q = GuildQueue()
    for title in ("a", "b", "c"):
        q.enqueue(_track(title))
    assert q.remove_last().title == "c"
    assert [t.title for t in q.upcoming()] == ["a", "b"]


def test_remove_last_on_empty_queue_raises() -> None:
    with pytest.raises(IndexError, match="queue is empty"):
        GuildQueue().remove_last()


# -- clear / shuffle ---------------------------------------------------------


def test_clear_pending_returns_count_and_empties_queue() -> None:
    q = GuildQueue()
    for title in ("a", "b", "c"):
        q.enqueue(_track(title))
    assert q.clear_pending() == 3
    assert q.upcoming() == []


def test_clear_pending_does_not_touch_current() -> None:
    q = GuildQueue()
    q.enqueue(_track("playing"))
    q.enqueue(_track("queued"))
    q.advance()
    q.clear_pending()
    assert q.current is not None and q.current.title == "playing"


def test_shuffle_preserves_membership() -> None:
    q = GuildQueue()
    titles = [f"t{i}" for i in range(20)]
    for title in titles:
        q.enqueue(_track(title))
    q.shuffle()
    assert sorted(t.title for t in q.upcoming()) == sorted(titles)


# -- volume ------------------------------------------------------------------


def test_set_volume_accepts_in_range() -> None:
    q = GuildQueue()
    q.set_volume(0.0)
    assert q.volume == 0.0
    q.set_volume(2.0)
    assert q.volume == 2.0


def test_set_volume_rejects_negative_with_offending_value() -> None:
    q = GuildQueue()
    with pytest.raises(ValueError, match=r"-0\.5 out of range"):
        q.set_volume(-0.5)


def test_set_volume_rejects_above_max_with_expected_range() -> None:
    q = GuildQueue()
    with pytest.raises(ValueError, match=r"expected 0\.0\.\.2\.0"):
        q.set_volume(2.5)


# -- registry ---------------------------------------------------------------


def test_for_guild_returns_same_instance_on_repeat_call() -> None:
    reg = GuildQueueRegistry()
    assert reg.for_guild(42) is reg.for_guild(42)


def test_for_guild_different_ids_get_independent_queues() -> None:
    reg = GuildQueueRegistry()
    a = reg.for_guild(1)
    b = reg.for_guild(2)
    a.enqueue(_track("only-in-1"))
    assert a is not b
    assert b.upcoming() == []


def test_drop_resets_a_guild_queue() -> None:
    reg = GuildQueueRegistry()
    original = reg.for_guild(1)
    original.enqueue(_track("a"))
    reg.drop(1)
    assert reg.for_guild(1) is not original
    assert reg.for_guild(1).upcoming() == []

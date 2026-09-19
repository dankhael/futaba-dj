"""Tests for pure helpers in the Music cog (no discord client needed)."""

from __future__ import annotations

from cogs.music import _break_track_loop_on_failure
from services.guild_queue import GuildQueue, LoopMode


def test_track_loop_is_dropped_so_a_broken_track_cannot_spin_forever() -> None:
    queue = GuildQueue()
    queue.loop_mode = LoopMode.TRACK

    _break_track_loop_on_failure(queue)

    assert queue.loop_mode is LoopMode.OFF


def test_other_loop_modes_are_left_alone() -> None:
    queue = GuildQueue()
    queue.loop_mode = LoopMode.QUEUE

    _break_track_loop_on_failure(queue)

    assert queue.loop_mode is LoopMode.QUEUE

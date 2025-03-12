"""Per-guild playback queue. Pure data — no Discord or asyncio imports."""

from __future__ import annotations

import enum
import random
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Avoid pulling discord into pure-data modules so queue logic is
    # importable (and testable) without the voice stack.
    from .track_source import TrackInfo


class LoopMode(enum.Enum):
    OFF = "off"
    TRACK = "track"  # repeat the currently-playing track forever
    QUEUE = "queue"  # finished tracks go to the back of the queue


_VOLUME_DEFAULT = 0.5
_VOLUME_MAX = 2.0


@dataclass
class GuildQueue:
    """Mutable queue state for a single guild.

    ``current`` holds the track that is (or was last) playing — kept
    separate from ``pending`` so loop modes can re-queue it on advance.
    """

    pending: deque[TrackInfo] = field(default_factory=deque)
    current: TrackInfo | None = None
    loop_mode: LoopMode = LoopMode.OFF
    volume: float = _VOLUME_DEFAULT

    def enqueue(self, track: TrackInfo) -> int:
        """Append to the back. Returns the new 1-based position."""
        self.pending.append(track)
        return len(self.pending)

    def enqueue_front(self, track: TrackInfo) -> None:
        self.pending.appendleft(track)

    def advance(self) -> TrackInfo | None:
        """Pick the next track to play, honoring loop mode.

        Returns ``None`` when the queue has been exhausted.
        """
        if self.loop_mode is LoopMode.TRACK and self.current is not None:
            return self.current
        if self.loop_mode is LoopMode.QUEUE and self.current is not None:
            self.pending.append(self.current)
        if not self.pending:
            self.current = None
            return None
        self.current = self.pending.popleft()
        return self.current

    def remove_at(self, position: int) -> TrackInfo:
        """Remove the track at the given 1-based queue position."""
        size = len(self.pending)
        if position < 1 or position > size:
            raise IndexError(
                f"queue position {position} out of range; expected 1..{size}"
            )
        items = list(self.pending)
        removed = items.pop(position - 1)
        self.pending = deque(items)
        return removed

    def remove_last(self) -> TrackInfo:
        if not self.pending:
            raise IndexError("queue is empty; expected at least one pending track")
        return self.pending.pop()

    def clear_pending(self) -> int:
        """Drop pending tracks. Returns how many were removed."""
        size = len(self.pending)
        self.pending.clear()
        return size

    def shuffle(self) -> None:
        items = list(self.pending)
        random.shuffle(items)
        self.pending = deque(items)

    def upcoming(self) -> list[TrackInfo]:
        return list(self.pending)

    def set_volume(self, fraction: float) -> None:
        if fraction < 0.0 or fraction > _VOLUME_MAX:
            raise ValueError(
                f"volume {fraction} out of range; expected 0.0..{_VOLUME_MAX}"
            )
        self.volume = fraction


class GuildQueueRegistry:
    """Lazily-allocated map of ``guild_id -> GuildQueue``."""

    def __init__(self) -> None:
        self._queues: dict[int, GuildQueue] = {}

    def for_guild(self, guild_id: int) -> GuildQueue:
        queue = self._queues.get(guild_id)
        if queue is None:
            queue = GuildQueue()
            self._queues[guild_id] = queue
        return queue

    def drop(self, guild_id: int) -> None:
        self._queues.pop(guild_id, None)

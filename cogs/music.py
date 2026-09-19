"""Discord-facing music commands. Delegates state to ``services``."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from services.guild_queue import GuildQueue, GuildQueueRegistry, LoopMode
from services.track_source import TrackInfo, TrackSource

_LOG_PLAYBACK = logging.getLogger("futaba.playback")


class Music(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        source: TrackSource,
        queues: GuildQueueRegistry,
    ) -> None:
        self.bot = bot
        self._source = source
        self._queues = queues
        # Text channel of the last music command per guild, so auto-advance
        # (which has no ctx) can still report tracks it had to skip.
        self._announce_channels: dict[int, discord.abc.Messageable] = {}

    # -- helpers ----------------------------------------------------------

    async def _ensure_voice(self, ctx: commands.Context) -> bool:
        if ctx.voice_client is not None:
            return True
        if ctx.author.voice is None:
            await ctx.send("You are not connected to a voice channel!")
            return False
        await ctx.author.voice.channel.connect()
        return True

    def _queue_for(self, ctx: commands.Context) -> GuildQueue:
        if ctx.guild is None:
            raise commands.CommandError("Music commands require a server context.")
        self._announce_channels[ctx.guild.id] = ctx.channel
        return self._queues.for_guild(ctx.guild.id)

    async def _announce_skip(
        self, guild_id: int, track: TrackInfo | None, exc: Exception
    ) -> None:
        channel = self._announce_channels.get(guild_id)
        if channel is None or track is None:
            return
        await channel.send(f"Skipped **{track.title}** — could not stream it: {exc}")

    async def _start_next(
        self, voice_client: discord.VoiceClient, queue: GuildQueue
    ) -> TrackInfo | None:
        nxt = queue.advance()
        if nxt is None:
            return None
        audio = await self._source.build_audio(
            nxt.query, loop=self.bot.loop, volume=queue.volume
        )
        voice_client.play(
            audio,
            after=lambda err: self._on_track_end(voice_client, queue, err),
        )
        return nxt

    def _on_track_end(
        self,
        voice_client: discord.VoiceClient,
        queue: GuildQueue,
        err: Exception | None,
    ) -> None:
        # Runs on a discord.py audio thread, NOT the asyncio loop, so we
        # bounce the advance back onto the loop with run_coroutine_threadsafe.
        if err is not None:
            _LOG_PLAYBACK.error("playback error: %s", err)
        if not voice_client.is_connected():
            return
        asyncio.run_coroutine_threadsafe(
            self._advance_after_finish(voice_client, queue), self.bot.loop
        )

    async def _start_next_playable(
        self, voice_client: discord.VoiceClient, queue: GuildQueue
    ) -> TrackInfo | None:
        """Start the next track, skipping any that fail to load.

        Returns the track that actually started, or ``None`` when the
        queue is exhausted. Loops (not recurses) so a streak of broken
        tracks — common with playlists containing private/deleted videos —
        can't blow the stack.
        """
        while voice_client.is_connected():
            try:
                return await self._start_next(voice_client, queue)
            except Exception as exc:
                _LOG_PLAYBACK.error("failed to start next track: %s", exc)
                await self._announce_skip(voice_client.guild.id, queue.current, exc)
                _break_track_loop_on_failure(queue)
                continue
        return None

    async def _advance_after_finish(
        self, voice_client: discord.VoiceClient, queue: GuildQueue
    ) -> None:
        await self._start_next_playable(voice_client, queue)

    # -- commands ---------------------------------------------------------

    @commands.command(name="join")
    async def join(self, ctx: commands.Context) -> None:
        """Joins the voice channel of the command caller."""
        if await self._ensure_voice(ctx):
            await ctx.send(f"Joined {ctx.voice_client.channel}")

    @commands.command(name="play")
    async def play(self, ctx: commands.Context, *, url: str) -> None:
        """Enqueues a URL/search; starts playback if the bot is idle.

        Playlist URLs (``list=...`` or ``/playlist?...``) enqueue every
        video in the playlist.

        Example: ``!play https://youtu.be/dQw4w9WgXcQ``
        """
        if not await self._ensure_voice(ctx):
            return
        async with ctx.typing():
            try:
                infos = await self._source.probe_many(
                    url, loop=self.bot.loop, requested_by=str(ctx.author)
                )
            except Exception as exc:
                await ctx.send(f"Could not load track: {exc}")
                return
            queue = self._queue_for(ctx)
            voice = ctx.voice_client
            was_idle = not (voice.is_playing() or voice.is_paused())
            first_position = len(queue.pending) + 1
            for info in infos:
                queue.enqueue(info)
            if was_idle:
                track = await self._start_next_playable(voice, queue)
                if track is None:
                    await ctx.send(
                        "Could not start any track — every entry was unavailable."
                    )
                    return
                extra = len(infos) - 1
                suffix = f" (+{extra} more queued)" if extra > 0 else ""
                await ctx.send(f"Now playing: **{track.title}**{suffix}")
                return
            if len(infos) == 1:
                await ctx.send(
                    f"Queued **{infos[0].title}** (position #{first_position})"
                )
                return
            await ctx.send(
                f"Queued {len(infos)} tracks from playlist "
                f"(starting at position #{first_position})."
            )

    @commands.command(name="playnext")
    async def playnext(self, ctx: commands.Context, *, url: str) -> None:
        """Inserts a track at the front of the queue."""
        if not await self._ensure_voice(ctx):
            return
        async with ctx.typing():
            try:
                info = await self._source.probe(
                    url, loop=self.bot.loop, requested_by=str(ctx.author)
                )
            except Exception as exc:
                await ctx.send(f"Could not load track: {exc}")
                return
            queue = self._queue_for(ctx)
            queue.enqueue_front(info)
            voice = ctx.voice_client
            if not voice.is_playing() and not voice.is_paused():
                track = await self._start_next_playable(voice, queue)
                if track is not None:
                    await ctx.send(f"Now playing: **{track.title}**")
                    return
            await ctx.send(f"Up next: **{info.title}**")

    @commands.command(name="skip", aliases=["next"])
    async def skip(self, ctx: commands.Context) -> None:
        """Stops the current track; queue advances automatically."""
        voice = ctx.voice_client
        if voice is None or not (voice.is_playing() or voice.is_paused()):
            await ctx.send("Nothing is playing.")
            return
        # `stop()` triggers the `after=` callback, which advances the queue.
        # Suppress one-shot loop so skip actually moves on.
        queue = self._queue_for(ctx)
        if queue.loop_mode is LoopMode.TRACK:
            queue.current = None
        voice.stop()
        await ctx.send("Skipped.")

    @commands.command(name="pause")
    async def pause(self, ctx: commands.Context) -> None:
        voice = ctx.voice_client
        if voice is None or not voice.is_playing():
            await ctx.send("Nothing is playing.")
            return
        voice.pause()
        await ctx.send("Paused.")

    @commands.command(name="resume")
    async def resume(self, ctx: commands.Context) -> None:
        voice = ctx.voice_client
        if voice is None or not voice.is_paused():
            await ctx.send("Nothing is paused.")
            return
        voice.resume()
        await ctx.send("Resumed.")

    @commands.command(name="stop")
    async def stop(self, ctx: commands.Context) -> None:
        """Clears the queue and disconnects from voice."""
        voice = ctx.voice_client
        if voice is None:
            await ctx.send("I am not connected to a voice channel.")
            return
        if ctx.guild is not None:
            self._queues.drop(ctx.guild.id)
        await voice.disconnect()
        await ctx.send("Stopped and disconnected.")

    @commands.command(name="queue", aliases=["q"])
    async def show_queue(self, ctx: commands.Context) -> None:
        queue = self._queue_for(ctx)
        lines: list[str] = []
        if queue.current is not None:
            lines.append(f"**Now:** {queue.current.title}")
        upcoming = queue.upcoming()
        if upcoming:
            lines.append("**Up next:**")
            for i, track in enumerate(upcoming[:20], start=1):
                lines.append(f"`{i}.` {track.title}")
            if len(upcoming) > 20:
                lines.append(f"…and {len(upcoming) - 20} more")
        elif queue.current is None:
            lines.append("_Queue is empty._")
        if queue.loop_mode is not LoopMode.OFF:
            lines.append(f"_Loop mode: {queue.loop_mode.value}_")
        await ctx.send("\n".join(lines))

    @commands.command(name="nowplaying", aliases=["np"])
    async def nowplaying(self, ctx: commands.Context) -> None:
        queue = self._queue_for(ctx)
        if queue.current is None:
            await ctx.send("Nothing is playing.")
            return
        await ctx.send(f"Now playing: **{queue.current.title}**")

    @commands.command(name="remove", aliases=["rm"])
    async def remove(self, ctx: commands.Context, position: str) -> None:
        """Removes a track. Pass a 1-based position or ``last``."""
        queue = self._queue_for(ctx)
        try:
            if position.lower() == "last":
                removed = queue.remove_last()
            else:
                removed = queue.remove_at(int(position))
        except (ValueError, IndexError) as exc:
            await ctx.send(f"Cannot remove: {exc}")
            return
        await ctx.send(f"Removed **{removed.title}**.")

    @commands.command(name="clear")
    async def clear(self, ctx: commands.Context) -> None:
        """Clears pending tracks; current track keeps playing."""
        queue = self._queue_for(ctx)
        cleared = queue.clear_pending()
        await ctx.send(f"Cleared {cleared} track(s) from the queue.")

    @commands.command(name="shuffle")
    async def shuffle(self, ctx: commands.Context) -> None:
        queue = self._queue_for(ctx)
        queue.shuffle()
        await ctx.send("Queue shuffled.")

    @commands.command(name="loop")
    async def loop(self, ctx: commands.Context, mode: str = "toggle") -> None:
        """Sets loop mode. Modes: off, track, queue, toggle (cycles)."""
        queue = self._queue_for(ctx)
        try:
            queue.loop_mode = _resolve_loop_mode(queue.loop_mode, mode)
        except ValueError as exc:
            await ctx.send(str(exc))
            return
        await ctx.send(f"Loop mode: {queue.loop_mode.value}")

    @commands.command(name="volume", aliases=["vol"])
    async def volume(self, ctx: commands.Context, level: int) -> None:
        """Sets playback volume in percent (0..200)."""
        if level < 0 or level > 200:
            await ctx.send(f"Volume must be 0..200; got {level}")
            return
        queue = self._queue_for(ctx)
        queue.set_volume(level / 100)
        voice = ctx.voice_client
        if voice is not None and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = queue.volume
        await ctx.send(f"Volume set to {level}%.")


_LOOP_CYCLE: dict[LoopMode, LoopMode] = {
    LoopMode.OFF: LoopMode.TRACK,
    LoopMode.TRACK: LoopMode.QUEUE,
    LoopMode.QUEUE: LoopMode.OFF,
}


def _resolve_loop_mode(current: LoopMode, requested: str) -> LoopMode:
    requested = requested.lower()
    if requested == "toggle":
        return _LOOP_CYCLE[current]
    try:
        return LoopMode(requested)
    except ValueError as exc:
        raise ValueError(
            f"unknown loop mode {requested!r}; expected off/track/queue/toggle"
        ) from exc


def _break_track_loop_on_failure(queue: GuildQueue) -> None:
    # ``advance()`` under LoopMode.TRACK hands back the same track forever;
    # if that track can't stream, retrying it would never end. Drop to OFF
    # so the queue moves on instead of spinning.
    if queue.loop_mode is LoopMode.TRACK:
        queue.loop_mode = LoopMode.OFF


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot, TrackSource.with_defaults(), GuildQueueRegistry()))

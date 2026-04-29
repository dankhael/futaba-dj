"""yt-dlp + FFmpeg adapter owned by this project.

The rest of the bot only knows about ``TrackInfo`` (metadata) and
``discord.PCMVolumeTransformer`` (audio). Swapping the underlying
extractor would only touch this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import discord
import yt_dlp


@dataclass(frozen=True)
class TrackInfo:
    """Lightweight metadata used by the queue and display layer."""

    query: str  # original user-supplied URL or search string
    title: str
    duration_seconds: int
    webpage_url: str
    requested_by: str


# yt-dlp prints noisy bug-report banners on extraction errors; silence them.
yt_dlp.utils.bug_reports_message = lambda *args, **kwargs: ""


_YTDL_OPTS: dict[str, Any] = {
    "format": "bestaudio/best",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "source_address": "0.0.0.0",  # ipv6 routes are flaky from some hosts
}


# Playlist enumeration uses extract_flat so we don't pay the per-video
# extraction cost for 100-item playlists; each track's stream URL is
# resolved lazily at playback time by ``build_audio``.
_PLAYLIST_YTDL_OPTS: dict[str, Any] = {
    **_YTDL_OPTS,
    "noplaylist": False,
    "extract_flat": "in_playlist",
    "ignoreerrors": True,  # skip private/deleted videos instead of aborting
}


# These reconnect flags exist because yt-dlp's resolved URLs frequently
# drop mid-stream — keep them when editing the FFmpeg invocation.
_FFMPEG_OPTS: dict[str, str] = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


class TrackSource:
    """Two-phase track resolver.

    1. ``probe`` extracts metadata for queue display without touching FFmpeg.
    2. ``build_audio`` resolves a fresh stream URL and constructs the
       Discord audio source. Done at playback time because YouTube's
       signed URLs expire while a track sits in the queue.

    Example::

        source = TrackSource.with_defaults()
        info = await source.probe("https://youtu.be/...", loop=loop)
        audio = await source.build_audio(info.query, loop=loop)
    """

    def __init__(
        self,
        ytdl: yt_dlp.YoutubeDL,
        playlist_ytdl: yt_dlp.YoutubeDL | None = None,
    ) -> None:
        self._ytdl = ytdl
        # Fall back to the single-track extractor when no playlist
        # variant is supplied — keeps existing tests/callers working.
        self._playlist_ytdl = playlist_ytdl if playlist_ytdl is not None else ytdl

    @classmethod
    def with_defaults(cls) -> TrackSource:
        return cls(
            yt_dlp.YoutubeDL(_YTDL_OPTS),
            yt_dlp.YoutubeDL(_PLAYLIST_YTDL_OPTS),
        )

    async def probe(
        self,
        query: str,
        *,
        loop: asyncio.AbstractEventLoop,
        requested_by: str = "",
    ) -> TrackInfo:
        data = await self._extract(query, loop=loop)
        return TrackInfo(
            query=query,
            title=data.get("title") or query,
            duration_seconds=int(data.get("duration") or 0),
            webpage_url=data.get("webpage_url") or query,
            requested_by=requested_by,
        )

    async def probe_many(
        self,
        query: str,
        *,
        loop: asyncio.AbstractEventLoop,
        requested_by: str = "",
    ) -> list[TrackInfo]:
        """Resolve a query to one TrackInfo per video.

        For non-playlist queries this returns ``[probe(query)]``. For
        playlist URLs (``list=...`` or ``/playlist``) it enumerates every
        entry via the flat extractor; each entry's stream URL is
        resolved later by ``build_audio`` at playback time.
        """
        if not _is_playlist_query(query):
            return [await self.probe(query, loop=loop, requested_by=requested_by)]
        data = await loop.run_in_executor(
            None,
            lambda: self._playlist_ytdl.extract_info(query, download=False),
        )
        if not isinstance(data, dict):
            raise RuntimeError(
                f"yt-dlp returned {type(data).__name__} for {query!r}; expected dict"
            )
        entries = data.get("entries")
        if not entries:
            raise RuntimeError(
                f"yt-dlp playlist {query!r} contained no entries; expected >=1"
            )
        infos = [
            info
            for entry in entries
            if (info := _entry_to_track_info(entry, requested_by)) is not None
        ]
        if not infos:
            raise RuntimeError(
                f"yt-dlp playlist {query!r} had {len(entries)} entries but "
                f"none yielded a playable URL"
            )
        return infos

    async def build_audio(
        self,
        query: str,
        *,
        loop: asyncio.AbstractEventLoop,
        volume: float,
    ) -> discord.PCMVolumeTransformer:
        data = await self._extract(query, loop=loop)
        stream_url = data.get("url")
        if not stream_url:
            raise RuntimeError(
                f"yt-dlp returned no streamable URL for {query!r}; "
                f"expected dict with non-empty 'url' key, got keys={list(data)}"
            )
        ffmpeg_audio = discord.FFmpegPCMAudio(stream_url, **_FFMPEG_OPTS)
        return discord.PCMVolumeTransformer(ffmpeg_audio, volume=volume)

    async def _extract(
        self, query: str, *, loop: asyncio.AbstractEventLoop
    ) -> dict[str, Any]:
        # extract_info is synchronous; run it on the default executor so
        # the asyncio loop keeps servicing Discord heartbeats.
        data = await loop.run_in_executor(
            None, lambda: self._ytdl.extract_info(query, download=False)
        )
        if not isinstance(data, dict):
            raise RuntimeError(
                f"yt-dlp returned {type(data).__name__} for {query!r}; expected dict"
            )
        if "entries" in data:
            entries = data["entries"]
            if not entries:
                raise RuntimeError(
                    f"yt-dlp playlist {query!r} contained no entries; expected >=1"
                )
            data = entries[0]
        return data


def _is_playlist_query(query: str) -> bool:
    """Heuristic: does this look like a YouTube playlist URL?

    Matches both ``/playlist?list=...`` and watch URLs that carry a
    ``list=`` param (``youtu.be/ID?list=...``). Bare search strings
    and single-video URLs go through the single-track path.
    """
    lower = query.lower()
    return "list=" in lower or "/playlist" in lower


def _entry_to_track_info(
    entry: dict[str, Any] | None, requested_by: str
) -> TrackInfo | None:
    """Map one ``extract_flat`` playlist entry to a TrackInfo.

    Returns ``None`` for entries yt-dlp couldn't resolve (private,
    deleted, region-blocked) so the caller can skip them silently.
    """
    if not isinstance(entry, dict):
        return None
    url = entry.get("url") or entry.get("webpage_url")
    if not url:
        return None
    return TrackInfo(
        query=url,
        title=entry.get("title") or url,
        duration_seconds=int(entry.get("duration") or 0),
        webpage_url=entry.get("webpage_url") or url,
        requested_by=requested_by,
    )

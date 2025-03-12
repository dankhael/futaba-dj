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

    def __init__(self, ytdl: yt_dlp.YoutubeDL) -> None:
        self._ytdl = ytdl

    @classmethod
    def with_defaults(cls) -> TrackSource:
        return cls(yt_dlp.YoutubeDL(_YTDL_OPTS))

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

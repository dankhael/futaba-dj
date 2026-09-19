"""yt-dlp + FFmpeg adapter owned by this project.

The rest of the bot only knows about ``TrackInfo`` (metadata) and
``discord.PCMVolumeTransformer`` (audio). Swapping the underlying
extractor would only touch this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
import yt_dlp

from services.stream_preflight import (
    StreamStatusProbe,
    is_stream_rejected,
    urllib_stream_status,
)

_LOG_EXTRACT = logging.getLogger("futaba.track_source")


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


class StreamRejectedError(RuntimeError):
    """Every resolved URL for a query was refused by the CDN (HTTP 403).

    Raised by ``TrackSource.resolve_stream_url`` after exhausting its
    re-extraction budget; callers treat it like any other unplayable track.
    """

    def __init__(self, query: str, attempts: int) -> None:
        super().__init__(
            f"stream for {query!r} was rejected with HTTP 403 on all "
            f"{attempts} attempts; expected a URL the CDN serves at least once"
        )
        self.query = query
        self.attempts = attempts


# Measured on the VPS: ~50% of web_embedded URLs are refused, independently
# per extraction, so 4 draws leave ~6% of tracks unplayable without a PO
# Token provider (see docker-compose.yml / README).
_MAX_STREAM_ATTEMPTS = 4


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


# YouTube answers datacenter IPs (e.g. the VPS) with "Sign in to confirm
# you're not a bot" for every player_client; the only reliable workaround
# is a Netscape cookies.txt from a logged-in account. Optional so local
# runs on residential IPs keep working without one.
_COOKIES_FILE_ENV = "YTDL_COOKIES_FILE"
_DEFAULT_COOKIES_FILE = "cookies.txt"

# Even with cookies, web_embedded stream URLs need a GVS PO Token or the CDN
# refuses ~half of them with 403. The bgutil-ytdlp-pot-provider plugin
# (requirements.txt) fetches tokens from its companion server, whose URL
# comes from this env var (docker-compose.yml). Optional: local runs on a
# residential IP work without it.
_POT_PROVIDER_URL_ENV = "YTDL_POT_PROVIDER_URL"


def cookie_ytdl_opts(path: str | None = None) -> dict[str, str]:
    """Return ``{"cookiefile": path}`` when a non-empty cookies file exists.

    Example::

        opts = {**_YTDL_OPTS, **cookie_ytdl_opts()}
    """
    resolved = path or os.environ.get(_COOKIES_FILE_ENV, _DEFAULT_COOKIES_FILE)
    cookie_path = Path(resolved)
    if not cookie_path.is_file() or cookie_path.stat().st_size == 0:
        return {}
    return {"cookiefile": str(cookie_path)}


def pot_provider_ytdl_opts(url: str | None = None) -> dict[str, Any]:
    """Return yt-dlp ``extractor_args`` pointing the bgutil plugin at ``url``.

    Empty when no provider URL is configured, so the plugin (if installed)
    falls back to its own default and plain installs are unaffected.

    Example::

        opts = {**_YTDL_OPTS, **pot_provider_ytdl_opts("http://bgutil:4416")}
    """
    resolved = url or os.environ.get(_POT_PROVIDER_URL_ENV, "")
    if not resolved:
        return {}
    # yt-dlp's Python API takes extractor-arg values as lists of strings.
    # fetch_pot=always is essential: web_embedded has no GVS PO Token policy
    # in yt-dlp, so the default "auto" never asks the provider for one and
    # the 403s continue even with the sidecar running (verified on the VPS).
    return {
        "extractor_args": {
            "youtube": {"fetch_pot": ["always"]},
            "youtubepot-bgutilhttp": {"base_url": [resolved]},
        }
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
        stream_status: StreamStatusProbe = urllib_stream_status,
    ) -> None:
        self._ytdl = ytdl
        # Fall back to the single-track extractor when no playlist
        # variant is supplied — keeps existing tests/callers working.
        self._playlist_ytdl = playlist_ytdl if playlist_ytdl is not None else ytdl
        self._stream_status = stream_status

    @classmethod
    def with_defaults(cls) -> TrackSource:
        extra = {**cookie_ytdl_opts(), **pot_provider_ytdl_opts()}
        return cls(
            yt_dlp.YoutubeDL({**_YTDL_OPTS, **extra}),
            yt_dlp.YoutubeDL({**_PLAYLIST_YTDL_OPTS, **extra}),
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

    async def resolve_stream_url(
        self, query: str, *, loop: asyncio.AbstractEventLoop
    ) -> str:
        """Resolve a direct stream URL the CDN is confirmed to serve.

        Each attempt re-extracts (a fresh URL gets a fresh verdict) and
        pre-flights it; see ``services.stream_preflight`` for why.

        Example::

            url = await source.resolve_stream_url("https://youtu.be/x", loop=loop)
        """
        for attempt in range(1, _MAX_STREAM_ATTEMPTS + 1):
            data = await self._extract(query, loop=loop)
            stream_url = _stream_url_from(data, query)
            headers: dict[str, str] = data.get("http_headers") or {}
            status = await loop.run_in_executor(
                None, self._stream_status, stream_url, headers
            )
            if not is_stream_rejected(status):
                return stream_url
            _LOG_EXTRACT.warning(
                "stream rejected query=%r status=%s attempt=%s/%s",
                query,
                status,
                attempt,
                _MAX_STREAM_ATTEMPTS,
            )
            evict_cached_po_tokens()
        raise StreamRejectedError(query, _MAX_STREAM_ATTEMPTS)

    async def build_audio(
        self,
        query: str,
        *,
        loop: asyncio.AbstractEventLoop,
        volume: float,
    ) -> discord.PCMVolumeTransformer:
        stream_url = await self.resolve_stream_url(query, loop=loop)
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


def evict_cached_po_tokens() -> None:
    """Drop yt-dlp's in-memory PO Token cache so the next extraction gets a
    fresh token.

    yt-dlp caches PO Tokens per video in a process-global LRU
    (``yt_dlp.extractor.youtube.pot._registry._pot_memory_cache``, seen in
    2026.08.19). A retry that re-extracts but reuses the cached token
    carries the CDN's previous verdict along — on the VPS four attempts
    produced only two tokens and four 403s. Private API, so a missing
    attribute in a future yt-dlp degrades to "no eviction" rather than a
    crash.

    Example::

        evict_cached_po_tokens()
        data = ytdl.extract_info(url, download=False)
    """
    try:
        from yt_dlp.extractor.youtube.pot._registry import _pot_memory_cache

        cache = _pot_memory_cache.value.get("cache")
        lock = _pot_memory_cache.value.get("lock")
    except (ImportError, AttributeError) as exc:
        _LOG_EXTRACT.warning("po token cache eviction unavailable: %s", exc)
        return
    if cache is None or lock is None:
        return
    with lock:
        cache.clear()


def _stream_url_from(data: dict[str, Any], query: str) -> str:
    stream_url = data.get("url")
    if not stream_url:
        raise RuntimeError(
            f"yt-dlp returned no streamable URL for {query!r}; "
            f"expected dict with non-empty 'url' key, got keys={list(data)}"
        )
    return str(stream_url)


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

"""Tests for the yt-dlp adapter.

Only the metadata-extraction layer (``probe``) is exercised here —
``build_audio`` would spawn FFmpeg, which is out of scope for unit tests.
External I/O is replaced with a named ``FakeYoutubeDL`` per CLAUDE.md.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from services.track_source import TrackInfo, TrackSource


class FakeYoutubeDL:
    """Stand-in for ``yt_dlp.YoutubeDL`` that returns a canned payload.

    Records each call so tests can assert on dispatch behaviour. If the
    payload is an Exception, it is raised — used to verify error mapping.
    """

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.calls: list[tuple[str, bool]] = []

    def extract_info(self, query: str, *, download: bool = True) -> Any:
        self.calls.append((query, download))
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class ThreadCapturingYoutubeDL:
    """Records the thread ``extract_info`` runs on."""

    def __init__(self) -> None:
        self.thread: threading.Thread | None = None

    def extract_info(self, query: str, *, download: bool = True) -> dict[str, Any]:
        self.thread = threading.current_thread()
        return {"title": "t", "url": "stream://x", "webpage_url": "https://x"}


@pytest.fixture
def loop():
    new_loop = asyncio.new_event_loop()
    try:
        yield new_loop
    finally:
        new_loop.close()


def _await(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    return loop.run_until_complete(coro)


# -- happy-path metadata mapping --------------------------------------------


def test_probe_maps_yt_dlp_payload_to_track_info(loop) -> None:
    fake = FakeYoutubeDL(
        {
            "title": "Song",
            "duration": 215,
            "webpage_url": "https://watch/abc",
            "url": "stream://abc",
        }
    )
    src = TrackSource(fake)  # type: ignore[arg-type]

    info = _await(loop, src.probe("query-string", loop=loop, requested_by="alice"))

    assert info == TrackInfo(
        query="query-string",
        title="Song",
        duration_seconds=215,
        webpage_url="https://watch/abc",
        requested_by="alice",
    )


def test_probe_calls_extract_info_with_download_false(loop) -> None:
    fake = FakeYoutubeDL({"title": "t", "url": "u"})
    _await(loop, TrackSource(fake).probe("q", loop=loop))  # type: ignore[arg-type]
    assert fake.calls == [("q", False)]


def test_probe_falls_back_to_query_when_title_missing(loop) -> None:
    fake = FakeYoutubeDL({"url": "stream://x"})
    info = _await(loop, TrackSource(fake).probe("https://example/audio", loop=loop))  # type: ignore[arg-type]
    assert info.title == "https://example/audio"


def test_probe_zero_duration_when_field_missing(loop) -> None:
    fake = FakeYoutubeDL({"title": "live", "url": "u"})
    info = _await(loop, TrackSource(fake).probe("q", loop=loop))  # type: ignore[arg-type]
    assert info.duration_seconds == 0


# -- playlist normalization --------------------------------------------------


def test_probe_takes_first_entry_when_payload_is_a_playlist(loop) -> None:
    fake = FakeYoutubeDL(
        {
            "entries": [
                {"title": "Track 1", "url": "s1"},
                {"title": "Track 2", "url": "s2"},
            ],
        }
    )
    info = _await(loop, TrackSource(fake).probe("playlist-url", loop=loop))  # type: ignore[arg-type]
    assert info.title == "Track 1"


def test_probe_raises_on_empty_playlist_with_query_in_message(loop) -> None:
    fake = FakeYoutubeDL({"entries": []})
    with pytest.raises(RuntimeError, match=r"'empty-list' contained no entries"):
        _await(loop, TrackSource(fake).probe("empty-list", loop=loop))  # type: ignore[arg-type]


# -- error mapping -----------------------------------------------------------


def test_probe_raises_when_extract_info_returns_non_dict(loop) -> None:
    fake = FakeYoutubeDL(payload=None)
    with pytest.raises(RuntimeError, match="expected dict"):
        _await(loop, TrackSource(fake).probe("q", loop=loop))  # type: ignore[arg-type]


def test_probe_propagates_yt_dlp_exceptions(loop) -> None:
    fake = FakeYoutubeDL(payload=ValueError("yt-dlp blew up"))
    with pytest.raises(ValueError, match="yt-dlp blew up"):
        _await(loop, TrackSource(fake).probe("q", loop=loop))  # type: ignore[arg-type]


# -- threading invariant -----------------------------------------------------


def test_probe_runs_extract_info_off_the_event_loop_thread(loop) -> None:
    """``yt_dlp.extract_info`` is synchronous; running it on the asyncio
    thread would stall Discord heartbeats. This pins the run_in_executor
    contract so a future refactor can't quietly regress it.
    """
    capture = ThreadCapturingYoutubeDL()
    main_thread = threading.current_thread()

    _await(loop, TrackSource(capture).probe("q", loop=loop))  # type: ignore[arg-type]

    assert capture.thread is not None
    assert capture.thread is not main_thread

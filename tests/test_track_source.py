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

from services.track_source import TrackInfo, TrackSource, cookie_ytdl_opts


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


# -- probe_many: playlist enumeration ---------------------------------------


def test_probe_many_single_url_returns_one_track(loop) -> None:
    fake = FakeYoutubeDL({"title": "Solo", "url": "stream://s", "duration": 100})
    src = TrackSource(fake)  # type: ignore[arg-type]

    infos = _await(loop, src.probe_many("https://youtu.be/abc", loop=loop))

    assert len(infos) == 1
    assert infos[0].title == "Solo"
    # Single-track path goes through the regular extractor, not the playlist one.
    assert fake.calls == [("https://youtu.be/abc", False)]


def test_probe_many_search_query_uses_single_track_path(loop) -> None:
    fake = FakeYoutubeDL({"title": "found", "url": "u"})
    src = TrackSource(fake)  # type: ignore[arg-type]

    infos = _await(loop, src.probe_many("never gonna give you up", loop=loop))

    assert len(infos) == 1
    assert infos[0].title == "found"


def test_probe_many_playlist_url_enumerates_all_entries(loop) -> None:
    track_fake = FakeYoutubeDL({"title": "single", "url": "u"})
    playlist_fake = FakeYoutubeDL(
        {
            "entries": [
                {
                    "title": "T1",
                    "url": "https://youtu.be/v1",
                    "webpage_url": "https://youtu.be/v1",
                    "duration": 60,
                },
                {
                    "title": "T2",
                    "url": "https://youtu.be/v2",
                    "webpage_url": "https://youtu.be/v2",
                    "duration": 90,
                },
                {
                    "title": "T3",
                    "url": "https://youtu.be/v3",
                    "webpage_url": "https://youtu.be/v3",
                    "duration": 120,
                },
            ],
        }
    )
    src = TrackSource(track_fake, playlist_fake)  # type: ignore[arg-type]

    infos = _await(
        loop,
        src.probe_many(
            "https://youtube.com/playlist?list=PLxyz",
            loop=loop,
            requested_by="bob",
        ),
    )

    assert [i.title for i in infos] == ["T1", "T2", "T3"]
    assert [i.duration_seconds for i in infos] == [60, 90, 120]
    assert all(i.requested_by == "bob" for i in infos)
    # Each entry's query is its own video URL so build_audio later resolves
    # the right stream — not the playlist URL.
    assert [i.query for i in infos] == [
        "https://youtu.be/v1",
        "https://youtu.be/v2",
        "https://youtu.be/v3",
    ]
    # Single-track extractor must not have been touched for a playlist URL.
    assert track_fake.calls == []


def test_probe_many_skips_unresolved_playlist_entries(loop) -> None:
    """Private/deleted videos surface as ``None`` entries with extract_flat;
    those should be silently skipped, not abort the whole playlist.
    """
    playlist_fake = FakeYoutubeDL(
        {
            "entries": [
                None,
                {"title": "ok", "url": "https://youtu.be/ok"},
                {"title": "no-url"},  # missing both url + webpage_url
            ],
        }
    )
    src = TrackSource(FakeYoutubeDL({}), playlist_fake)  # type: ignore[arg-type]

    infos = _await(
        loop, src.probe_many("https://www.youtube.com/playlist?list=X", loop=loop)
    )

    assert [i.title for i in infos] == ["ok"]


def test_probe_many_raises_when_playlist_has_no_playable_entries(loop) -> None:
    playlist_fake = FakeYoutubeDL({"entries": [None, {"title": "no-url"}]})
    src = TrackSource(FakeYoutubeDL({}), playlist_fake)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="none yielded a playable URL"):
        _await(loop, src.probe_many("https://youtube.com/playlist?list=X", loop=loop))


def test_probe_many_raises_on_empty_playlist(loop) -> None:
    playlist_fake = FakeYoutubeDL({"entries": []})
    src = TrackSource(FakeYoutubeDL({}), playlist_fake)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="contained no entries"):
        _await(loop, src.probe_many("https://youtube.com/playlist?list=X", loop=loop))


def test_probe_many_detects_list_param_on_watch_url(loop) -> None:
    """``youtu.be/ID?list=...`` should route through the playlist extractor."""
    track_fake = FakeYoutubeDL({"title": "would-be-single", "url": "u"})
    playlist_fake = FakeYoutubeDL(
        {"entries": [{"title": "from-list", "url": "https://youtu.be/x"}]}
    )
    src = TrackSource(track_fake, playlist_fake)  # type: ignore[arg-type]

    infos = _await(loop, src.probe_many("https://youtu.be/abc?list=PLxyz", loop=loop))

    assert [i.title for i in infos] == ["from-list"]
    assert track_fake.calls == []


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


# --- cookie_ytdl_opts --------------------------------------------------------


def test_cookie_opts_empty_when_file_missing(tmp_path) -> None:
    assert cookie_ytdl_opts(str(tmp_path / "nope.txt")) == {}


def test_cookie_opts_empty_when_file_is_blank(tmp_path) -> None:
    # The repo ships an empty cookies.txt placeholder; yt-dlp would choke
    # on a zero-byte Netscape file, so treat it as "no cookies".
    blank = tmp_path / "cookies.txt"
    blank.write_text("")
    assert cookie_ytdl_opts(str(blank)) == {}


def test_cookie_opts_points_at_non_empty_file(tmp_path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    assert cookie_ytdl_opts(str(cookies)) == {"cookiefile": str(cookies)}


def test_cookie_opts_reads_path_from_env(tmp_path, monkeypatch) -> None:
    cookies = tmp_path / "from-env.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n")
    monkeypatch.setenv("YTDL_COOKIES_FILE", str(cookies))
    assert cookie_ytdl_opts() == {"cookiefile": str(cookies)}

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

from services.track_source import (
    StreamRejectedError,
    TrackInfo,
    TrackSource,
    cookie_ytdl_opts,
    evict_cached_po_tokens,
    player_client_ytdl_opts,
    pot_provider_ytdl_opts,
)


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


# -- resolve_stream_url: 403 preflight + retry -------------------------------


class FakeStreamStatusProbe:
    """Scripted ``StreamStatusProbe``: returns statuses in order, records calls."""

    def __init__(self, statuses: list[int]) -> None:
        self._statuses = list(statuses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url: str, headers: dict[str, str]) -> int:
        self.calls.append((url, dict(headers)))
        return self._statuses.pop(0)


class CountingYoutubeDL:
    """Returns a distinct stream URL per ``extract_info`` call."""

    def __init__(self) -> None:
        self.calls = 0

    def extract_info(self, query: str, *, download: bool = True) -> dict[str, Any]:
        self.calls += 1
        return {
            "title": "t",
            "url": f"stream://{self.calls}",
            "http_headers": {"User-Agent": "UA"},
        }


def test_resolve_returns_first_url_when_preflight_accepts(loop) -> None:
    ytdl = CountingYoutubeDL()
    probe = FakeStreamStatusProbe([206])
    src = TrackSource(ytdl, stream_status=probe)  # type: ignore[arg-type]

    url = _await(loop, src.resolve_stream_url("q", loop=loop))

    assert url == "stream://1"
    assert ytdl.calls == 1


def test_resolve_re_extracts_until_a_url_is_accepted(loop) -> None:
    """A 403'd URL stays 403; only a *fresh* extraction gets a new verdict."""
    ytdl = CountingYoutubeDL()
    probe = FakeStreamStatusProbe([403, 403, 206])
    src = TrackSource(ytdl, stream_status=probe)  # type: ignore[arg-type]

    url = _await(loop, src.resolve_stream_url("q", loop=loop))

    assert url == "stream://3"
    assert [u for u, _ in probe.calls] == ["stream://1", "stream://2", "stream://3"]


def test_resolve_passes_extractor_http_headers_to_probe(loop) -> None:
    probe = FakeStreamStatusProbe([206])
    src = TrackSource(CountingYoutubeDL(), stream_status=probe)  # type: ignore[arg-type]

    _await(loop, src.resolve_stream_url("q", loop=loop))

    assert probe.calls[0][1] == {"User-Agent": "UA"}


def test_resolve_raises_after_exhausting_attempts(loop) -> None:
    ytdl = CountingYoutubeDL()
    probe = FakeStreamStatusProbe([403, 403, 403])
    src = TrackSource(ytdl, stream_status=probe)  # type: ignore[arg-type]

    with pytest.raises(StreamRejectedError, match=r"'q' was rejected .* 3 attempts"):
        _await(loop, src.resolve_stream_url("q", loop=loop))
    assert ytdl.calls == 3


def test_resolve_does_not_retry_on_non_403_status(loop) -> None:
    # 404/5xx are not the PO-token lottery; hand the URL to FFmpeg whose
    # -reconnect flags deal with transient CDN errors.
    ytdl = CountingYoutubeDL()
    probe = FakeStreamStatusProbe([503])
    src = TrackSource(ytdl, stream_status=probe)  # type: ignore[arg-type]

    assert _await(loop, src.resolve_stream_url("q", loop=loop)) == "stream://1"
    assert ytdl.calls == 1


def test_resolve_raises_when_payload_has_no_url(loop) -> None:
    fake = FakeYoutubeDL({"title": "no-url"})
    src = TrackSource(fake, stream_status=FakeStreamStatusProbe([206]))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="no streamable URL for 'q'"):
        _await(loop, src.resolve_stream_url("q", loop=loop))


def test_resolve_runs_probe_off_the_event_loop_thread(loop) -> None:
    seen: list[threading.Thread] = []

    def probe(url: str, headers: dict[str, str]) -> int:
        seen.append(threading.current_thread())
        return 206

    src = TrackSource(CountingYoutubeDL(), stream_status=probe)  # type: ignore[arg-type]
    _await(loop, src.resolve_stream_url("q", loop=loop))

    assert seen and seen[0] is not threading.current_thread()


# --- pot_provider_ytdl_opts --------------------------------------------------


def test_pot_provider_opts_empty_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("YTDL_POT_PROVIDER_URL", raising=False)
    assert pot_provider_ytdl_opts() == {}


def test_pot_provider_opts_point_plugin_at_url_and_force_fetch() -> None:
    # Without fetch_pot=always yt-dlp never requests a token for web_embedded.
    assert pot_provider_ytdl_opts("http://bgutil:4416") == {
        "extractor_args": {
            "youtube": {"fetch_pot": ["always"]},
            "youtubepot-bgutilhttp": {"base_url": ["http://bgutil:4416"]},
        }
    }


def test_pot_provider_opts_read_url_from_env(monkeypatch) -> None:
    monkeypatch.setenv("YTDL_POT_PROVIDER_URL", "http://env:4416")
    assert pot_provider_ytdl_opts()["extractor_args"]["youtubepot-bgutilhttp"] == {
        "base_url": ["http://env:4416"]
    }


# --- evict_cached_po_tokens ---------------------------------------------------


def test_evict_clears_yt_dlp_global_po_token_cache() -> None:
    from yt_dlp.extractor.youtube.pot._builtin.memory_cache import (
        initialize_global_cache,
    )

    cache, lock, _max_size = initialize_global_cache(25)
    with lock:
        cache["web_embedded:gvs:vid"] = ("token", 2**40)

    evict_cached_po_tokens()

    assert cache == {}


def test_resolve_evicts_po_tokens_between_rejected_attempts(loop, monkeypatch) -> None:
    import services.track_source as track_source_module

    evictions: list[int] = []
    monkeypatch.setattr(
        track_source_module, "evict_cached_po_tokens", lambda: evictions.append(1)
    )
    probe = FakeStreamStatusProbe([403, 403, 206])
    src = TrackSource(CountingYoutubeDL(), stream_status=probe)  # type: ignore[arg-type]

    _await(loop, src.resolve_stream_url("q", loop=loop))

    # One eviction per rejection, none after the accepted URL.
    assert len(evictions) == 2


# -- resolve_stream_url: extractor cascade -----------------------------------


class UnavailableYoutubeDL:
    """Mimics a catalogue-restricted client (web_music) for an ordinary video."""

    def __init__(self) -> None:
        self.calls = 0

    def extract_info(self, query: str, *, download: bool = True) -> dict[str, Any]:
        self.calls += 1
        raise ValueError("Video unavailable. This video is not available")


def test_cascade_uses_first_extractor_when_it_succeeds(loop) -> None:
    first, second = CountingYoutubeDL(), CountingYoutubeDL()
    src = TrackSource(
        FakeYoutubeDL({}),  # type: ignore[arg-type]
        stream_status=FakeStreamStatusProbe([206]),
        stream_ytdls=[first, second],  # type: ignore[list-item]
    )

    _await(loop, src.resolve_stream_url("q", loop=loop))

    assert (first.calls, second.calls) == (1, 0)


def test_cascade_falls_through_when_first_extractor_cannot_resolve(loop) -> None:
    first, second = UnavailableYoutubeDL(), CountingYoutubeDL()
    src = TrackSource(
        FakeYoutubeDL({}),  # type: ignore[arg-type]
        stream_status=FakeStreamStatusProbe([206]),
        stream_ytdls=[first, second],  # type: ignore[list-item]
    )

    url = _await(loop, src.resolve_stream_url("q", loop=loop))

    assert url == "stream://1"
    # One failed extraction is enough to move on — no 403-style retries.
    assert (first.calls, second.calls) == (1, 1)


def test_cascade_falls_through_after_first_extractor_exhausts_403s(loop) -> None:
    first, second = CountingYoutubeDL(), CountingYoutubeDL()
    src = TrackSource(
        FakeYoutubeDL({}),  # type: ignore[arg-type]
        stream_status=FakeStreamStatusProbe([403, 403, 403, 206]),
        stream_ytdls=[first, second],  # type: ignore[list-item]
    )

    _await(loop, src.resolve_stream_url("q", loop=loop))

    assert (first.calls, second.calls) == (3, 1)


def test_cascade_surfaces_last_extractor_error(loop) -> None:
    first, second = UnavailableYoutubeDL(), CountingYoutubeDL()
    src = TrackSource(
        FakeYoutubeDL({}),  # type: ignore[arg-type]
        stream_status=FakeStreamStatusProbe([403, 403, 403]),
        stream_ytdls=[first, second],  # type: ignore[list-item]
    )

    with pytest.raises(StreamRejectedError, match="HTTP 403 on all 3 attempts"):
        _await(loop, src.resolve_stream_url("q", loop=loop))


def test_probe_never_uses_the_stream_cascade(loop) -> None:
    metadata = FakeYoutubeDL({"title": "meta", "url": "u"})
    restricted = UnavailableYoutubeDL()
    src = TrackSource(metadata, stream_ytdls=[restricted])  # type: ignore[arg-type]

    info = _await(loop, src.probe("q", loop=loop))

    assert info.title == "meta"
    assert restricted.calls == 0


# --- player_client_ytdl_opts -------------------------------------------------


def test_player_client_opts_none_returns_base_unchanged() -> None:
    base = {
        "format": "bestaudio",
        "extractor_args": {"youtube": {"fetch_pot": ["always"]}},
    }
    assert player_client_ytdl_opts(base, None) is base


def test_player_client_opts_deep_merge_keeps_pot_settings() -> None:
    base = {
        "format": "bestaudio",
        "extractor_args": {
            "youtube": {"fetch_pot": ["always"]},
            "youtubepot-bgutilhttp": {"base_url": ["http://bgutil:4416"]},
        },
    }

    opts = player_client_ytdl_opts(base, "web_music")

    assert opts["format"] == "bestaudio"
    assert opts["extractor_args"] == {
        "youtube": {"fetch_pot": ["always"], "player_client": ["web_music"]},
        "youtubepot-bgutilhttp": {"base_url": ["http://bgutil:4416"]},
    }
    # Input must not be mutated: it is shared by every cascade entry.
    assert "player_client" not in base["extractor_args"]["youtube"]

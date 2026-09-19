"""Pre-flight check for a resolved stream URL before FFmpeg opens it.

YouTube's ``googlevideo`` URLs can be rejected with HTTP 403 at fetch
time even though yt-dlp resolved them fine: on datacenter IPs the only
client that yields formats (``web_embedded``) requires a GVS PO Token
(yt-dlp wiki/PO-Token-Guide), and without one roughly half the URLs are
refused at random. FFmpeg then exits before decoding a single frame,
discord.py reports the track as "finished" and the bot skips silently.

A 1-byte ranged GET reproduces the verdict deterministically (a rejected
URL stays rejected; re-extracting rolls a fresh one), which lets
``TrackSource`` retry *before* announcing "Now playing".
"""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Protocol

_PREFLIGHT_TIMEOUT_SECONDS = 10
_REJECTED_STATUS = 403


class StreamStatusProbe(Protocol):
    """Callable returning the HTTP status a ranged GET on ``url`` yields.

    Blocking — callers run it in an executor. Injected into
    ``TrackSource`` so tests can substitute a fake.
    """

    def __call__(self, url: str, headers: Mapping[str, str]) -> int: ...


def urllib_stream_status(url: str, headers: Mapping[str, str]) -> int:
    """Return the status of a ``Range: bytes=0-0`` GET against ``url``.

    ``headers`` are the ``http_headers`` yt-dlp attached to the format so
    the probe presents the same User-Agent FFmpeg would be given.

    Example::

        status = urllib_stream_status(info["url"], info["http_headers"])
    """
    request = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
    try:
        with urllib.request.urlopen(
            request, timeout=_PREFLIGHT_TIMEOUT_SECONDS
        ) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code


def is_stream_rejected(status: int) -> bool:
    """True when the CDN refused the URL and re-extraction is worth a try."""
    return status == _REJECTED_STATUS

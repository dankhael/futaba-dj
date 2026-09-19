"""Tests for the ranged-GET stream preflight.

HTTP is replaced with a named ``FakeHttpOpener`` patched over
``urllib.request.urlopen`` so no socket is touched.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Any

import pytest

from services.stream_preflight import is_stream_rejected, urllib_stream_status


class FakeHttpResponse:
    def __init__(self, status: int) -> None:
        self.status = status

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeHttpOpener:
    """Stand-in for ``urllib.request.urlopen``; records the request it got."""

    def __init__(self, status: int, *, raise_http_error: bool = False) -> None:
        self._status = status
        self._raise = raise_http_error
        self.request: urllib.request.Request | None = None

    def __call__(
        self, request: urllib.request.Request, timeout: float
    ) -> FakeHttpResponse:
        self.request = request
        if self._raise:
            raise urllib.error.HTTPError(
                request.full_url, self._status, "Forbidden", {}, None  # type: ignore[arg-type]
            )
        return FakeHttpResponse(self._status)


def test_status_returned_from_successful_ranged_get(monkeypatch) -> None:
    opener = FakeHttpOpener(206)
    monkeypatch.setattr(urllib.request, "urlopen", opener)

    assert urllib_stream_status("https://cdn/x", {}) == 206


def test_http_error_code_is_returned_not_raised(monkeypatch) -> None:
    opener = FakeHttpOpener(403, raise_http_error=True)
    monkeypatch.setattr(urllib.request, "urlopen", opener)

    assert urllib_stream_status("https://cdn/x", {}) == 403


def test_probe_sends_range_header_and_extractor_headers(monkeypatch) -> None:
    opener = FakeHttpOpener(206)
    monkeypatch.setattr(urllib.request, "urlopen", opener)

    urllib_stream_status("https://cdn/x", {"User-Agent": "UA/1"})

    assert opener.request is not None
    assert opener.request.get_header("Range") == "bytes=0-0"
    assert opener.request.get_header("User-agent") == "UA/1"


@pytest.mark.parametrize(
    ("status", "rejected"), [(403, True), (200, False), (206, False), (404, False)]
)
def test_is_stream_rejected_only_on_403(status: int, rejected: bool) -> None:
    assert is_stream_rejected(status) is rejected

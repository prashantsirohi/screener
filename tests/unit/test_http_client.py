"""HttpClient behaviour, exercised entirely against stubbed responses."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import requests

from market_screener.http.client import HttpClient, UA_POOL
from market_screener.http.errors import (PermanentHttpError, TemporaryHttpError,
                                         classify_status)


class FakeResponse:
    def __init__(self, status=200, text="ok", url="https://example.test/x"):
        self.status_code = status
        self.text = text
        self.url = url
        self.content = text.encode()

    def json(self):
        import json
        return json.loads(self.text)


def make_client(responses, **kw):
    """Client whose session.get pops from a scripted list of responses."""
    c = HttpClient(min_request_gap_sec=0.0, timeout_sec=1, **kw)
    seq = list(responses)
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(url)
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    c._session = SimpleNamespace(get=fake_get, headers={}, cookies=None,
                                 close=lambda: None)
    c.calls = calls  # type: ignore[attr-defined]
    return c


# ---------------- status classification ----------------

@pytest.mark.parametrize("status", [200, 201, 204])
def test_success_statuses_are_not_errors(status):
    assert classify_status(status) is None


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 504])
def test_overload_statuses_are_temporary(status):
    assert isinstance(classify_status(status), TemporaryHttpError)


@pytest.mark.parametrize("status", [400, 404, 410, 451])
def test_client_statuses_are_permanent(status):
    assert isinstance(classify_status(status), PermanentHttpError)


# ---------------- interstitial detection ----------------

def test_bot_wall_with_200_is_temporary():
    c = make_client([FakeResponse(200, "<html><body>Access Denied</body></html>")])
    with pytest.raises(TemporaryHttpError, match="bot wall"):
        c.get("https://www.nseindia.com/api/thing")


def test_html_where_json_expected_is_temporary():
    c = make_client([FakeResponse(200, "<!doctype html><html>nope</html>")])
    with pytest.raises(TemporaryHttpError, match="expected JSON"):
        c.get("https://www.nseindia.com/api/thing", expect="json")


def test_valid_json_passes():
    c = make_client([FakeResponse(200, '{"data": [1, 2]}')])
    assert c.get_json("https://x.test/api")["data"] == [1, 2]


def test_malformed_json_is_temporary():
    c = make_client([FakeResponse(200, '{"data": ')])
    with pytest.raises(TemporaryHttpError, match="malformed JSON"):
        c.get_json("https://x.test/api")


# ---------------- retries ----------------

def test_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    c = make_client([FakeResponse(503), FakeResponse(503), FakeResponse(200, "fine")])
    c.warmup_urls = ()
    out = c.fetch_with_retries(lambda: c.get("https://x.test/a"), max_attempts=3)
    assert out.text == "fine"
    assert len(c.calls) == 3


def test_permanent_error_is_not_retried(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    c = make_client([FakeResponse(404), FakeResponse(200, "never reached")])
    c.warmup_urls = ()
    with pytest.raises(PermanentHttpError):
        c.fetch_with_retries(lambda: c.get("https://x.test/a"), max_attempts=3)
    assert len(c.calls) == 1, "a 404 must not consume retry budget"


def test_exhausted_retries_raise_the_last_error(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    c = make_client([FakeResponse(503)] * 3)
    c.warmup_urls = ()
    with pytest.raises(TemporaryHttpError) as ei:
        c.fetch_with_retries(lambda: c.get("https://x.test/a"), max_attempts=3)
    assert ei.value.status == 503


def test_transport_exception_is_temporary():
    c = make_client([requests.ConnectionError("refused")])
    with pytest.raises(TemporaryHttpError, match="transport error"):
        c.get("https://x.test/a")


def test_timeout_is_temporary():
    c = make_client([requests.Timeout("slow")])
    with pytest.raises(TemporaryHttpError, match="timeout"):
        c.get("https://x.test/a")


# ---------------- pacing ----------------

def test_throttle_enforces_minimum_gap(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    c = make_client([FakeResponse(), FakeResponse()])
    c.min_request_gap_sec = 1.5
    c.get("https://x.test/1")
    c.get("https://x.test/2")
    assert slept, "second request should have been paced"
    assert 1.5 <= slept[0] <= 1.5 + c.jitter[1] + 0.001


def test_first_request_is_not_delayed(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    c = make_client([FakeResponse()])
    c.min_request_gap_sec = 1.5
    c._last_request_ts = 0.0
    c.get("https://x.test/1")
    assert slept == []


# ---------------- session reset ----------------

def test_reset_rotates_user_agent_only_when_asked():
    c = HttpClient()
    original = c.user_agent
    c.reset_session()
    assert c.user_agent == original
    c.reset_session(rotate_user_agent=True)
    assert c.user_agent != original
    assert c.user_agent in UA_POOL

"""
Shared HTTP client for every collector.

Ported from the market_intel `http_utils` pattern so all collectors inherit the
same browser-like headers, cookie warmup, polite pacing, and retry-with-backoff
rather than each reinventing them.

Two behaviours are specific to Indian market sites and are the reason this
exists:

* **NSE requires a cookie handshake.** A cold request to nsearchives returns 503.
  Fetching the site root and the relevant landing page first seeds the cookies
  that make archive files downloadable.
* **Overload is signalled as HTTP 200.** NSE serves a WAF interstitial and
  screener.in serves an empty page template. Both look fine to the transport
  layer, so content-level detection is part of the client, not an afterthought
  in the caller.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence, TypeVar

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .errors import (HttpError, PermanentHttpError, TemporaryHttpError,
                     classify_status)

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Rotated only on repeated blank/interstitial responses, never per request:
# a UA that changes every call is itself a bot signal.
UA_POOL = (
    DEFAULT_UA,
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
     "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 "
     "Firefox/127.0"),
)

# Fragments that mean "a bot wall, not the page you asked for".
INTERSTITIAL_MARKERS = (
    "access denied", "request unsuccessful", "incapsula", "distil",
    "captcha", "are you a human", "cf-browser-verification",
    "checking your browser", "resource not available",
)


@dataclass
class HttpClient:
    """One session per collector. Not thread-safe by design; the pipeline is
    sequential and a shared session would defeat the pacing."""

    min_request_gap_sec: float = 1.5
    jitter: tuple[float, float] = (0.1, 0.4)
    max_attempts: int = 3
    adapter_retries: int = 3
    backoff_factor: float = 1.5
    timeout_sec: int = 40
    user_agent: str = DEFAULT_UA
    warmup_urls: Sequence[str] = ()
    default_headers: Mapping[str, str] = field(default_factory=dict)

    _session: requests.Session | None = field(default=None, init=False, repr=False)
    _last_request_ts: float = field(default=0.0, init=False, repr=False)
    _warmed_up: bool = field(default=False, init=False, repr=False)
    _ua_index: int = field(default=0, init=False, repr=False)

    # ---------------- session ----------------
    @property
    def session(self) -> requests.Session:
        if self._session is None:
            self._session = self._build_session()
        return self._session

    def _build_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
            **dict(self.default_headers),
        })
        retry = Retry(
            total=self.adapter_retries,
            backoff_factor=self.backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    def reset_session(self, *, rotate_user_agent: bool = False) -> None:
        """Drop cookies and connections. Used between escalating retries."""
        if self._session is not None:
            self._session.close()
        self._session = None
        self._warmed_up = False
        if rotate_user_agent:
            self._ua_index = (self._ua_index + 1) % len(UA_POOL)
            self.user_agent = UA_POOL[self._ua_index]
            log.debug("rotated user agent to index %d", self._ua_index)

    def set_cookie(self, name: str, value: str, domain: str) -> None:
        self.session.cookies.set(name, value, domain=domain)

    # ---------------- pacing ----------------
    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request_ts
        if elapsed < self.min_request_gap_sec:
            time.sleep(self.min_request_gap_sec - elapsed
                       + random.uniform(*self.jitter))

    # ---------------- warmup ----------------
    def warmup(self, force: bool = False) -> None:
        """Seed cookies by visiting the landing pages before hitting an API."""
        if self._warmed_up and not force:
            return
        for url in self.warmup_urls:
            try:
                self._throttle()
                self.session.get(url, timeout=self.timeout_sec)
                self._last_request_ts = time.time()
                log.debug("warmup ok: %s", url)
            except requests.RequestException as exc:
                log.warning("warmup failed for %s: %s", url, exc)
        self._warmed_up = True

    # ---------------- requests ----------------
    def get(self, url: str, *, params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None,
            expect: str = "any") -> requests.Response:
        """
        Single paced GET. Raises Temporary/PermanentHttpError on failure.

        `expect` is "any" | "json" | "xml" - used to catch a bot wall that
        returns HTML with a 200 where structured data was requested.
        """
        self._throttle()
        try:
            resp = self.session.get(url, params=params, headers=dict(headers or {}),
                                    timeout=self.timeout_sec)
        except requests.Timeout as exc:
            raise TemporaryHttpError(f"timeout: {exc}", url=url) from exc
        except requests.RequestException as exc:
            raise TemporaryHttpError(f"transport error: {exc}", url=url) from exc
        finally:
            self._last_request_ts = time.time()

        excerpt = resp.text[:300] if resp.content else ""
        err = classify_status(resp.status_code, url=url, body_excerpt=excerpt)
        if err:
            raise err

        self._check_interstitial(resp, expect)
        return resp

    def _check_interstitial(self, resp: requests.Response, expect: str) -> None:
        head = (resp.text[:2000] or "").lower()
        for marker in INTERSTITIAL_MARKERS:
            if marker in head:
                raise TemporaryHttpError(f"bot wall detected ({marker!r})",
                                         url=resp.url, status=resp.status_code,
                                         body_excerpt=resp.text[:300])
        if expect in ("json", "xml"):
            stripped = head.lstrip()
            looks_html = stripped.startswith("<!doctype html") or stripped.startswith("<html")
            if expect == "json" and not stripped.startswith(("{", "[")):
                raise TemporaryHttpError(
                    "expected JSON, got HTML" if looks_html else "expected JSON",
                    url=resp.url, status=resp.status_code, body_excerpt=resp.text[:300])
            if expect == "xml" and looks_html:
                raise TemporaryHttpError("expected XML, got HTML", url=resp.url,
                                         status=resp.status_code,
                                         body_excerpt=resp.text[:300])

    def get_json(self, url: str, **kw) -> Any:
        resp = self.get(url, expect="json", **kw)
        try:
            return resp.json()
        except ValueError as exc:
            raise TemporaryHttpError(f"malformed JSON: {exc}", url=url,
                                     body_excerpt=resp.text[:300]) from exc

    # ---------------- retry wrapper ----------------
    def fetch_with_retries(self, fn: Callable[[], T], *,
                           max_attempts: int | None = None,
                           description: str = "fetch") -> T:
        """
        Application-level retry on top of the adapter's transport retries.

        Re-warms the session on every attempt after the first, because on NSE a
        temporary failure usually means the cookies went stale.
        """
        attempts = max_attempts or self.max_attempts
        last: HttpError | None = None
        for attempt in range(1, attempts + 1):
            try:
                if attempt > 1:
                    self.warmup(force=True)
                return fn()
            except PermanentHttpError:
                raise
            except TemporaryHttpError as exc:
                last = exc
                if attempt == attempts:
                    break
                delay = min(2 ** attempt, 10) + random.uniform(0.3, 0.9)
                log.warning("%s attempt %d/%d failed (%s); retrying in %.1fs",
                            description, attempt, attempts, exc, delay)
                time.sleep(delay)
        assert last is not None
        raise last

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None


# Light pages that reliably return quickly. Some NSE pages - /get-quotes/equity
# in particular - are heavy client-rendered apps that stall a plain GET until it
# times out, so the Referer header and the warmup navigation must be chosen
# separately rather than reusing one URL for both.
DEFAULT_NSE_WARMUP = (
    "https://www.nseindia.com",
    "https://www.nseindia.com/market-data/securities-available-for-trading",
)


def nse_client(referer: str | None = None,
               warmup_urls: Sequence[str] | None = None, **overrides) -> HttpClient:
    """Pre-warmed for NSE: without the cookie handshake, archives answer 503."""
    headers = {"Origin": "https://www.nseindia.com"}
    if referer:
        headers["Referer"] = referer
    return HttpClient(
        warmup_urls=tuple(warmup_urls) if warmup_urls else DEFAULT_NSE_WARMUP,
        default_headers=headers,
        **overrides,
    )


def screener_client(session_cookie: str | None = None, **overrides) -> HttpClient:
    c = HttpClient(
        warmup_urls=("https://www.screener.in/",),
        default_headers={"Referer": "https://www.screener.in/"},
        **overrides,
    )
    if session_cookie:
        c.set_cookie("sessionid", session_cookie, domain=".screener.in")
    return c

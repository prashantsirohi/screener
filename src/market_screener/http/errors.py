"""
Fetch error taxonomy.

The distinction that matters is retryable vs not. NSE and screener.in both
answer overload with something that looks like success at the HTTP layer - a
WAF interstitial, or a page template with every number stripped out - so the
taxonomy has to cover content-level failures, not just status codes.
"""

from __future__ import annotations


class HttpError(Exception):
    """Base class. Carries whatever context the caller needs to log."""

    def __init__(self, message: str, *, url: str | None = None,
                 status: int | None = None, body_excerpt: str | None = None):
        super().__init__(message)
        self.url = url
        self.status = status
        self.body_excerpt = body_excerpt

    def __str__(self) -> str:
        bits = [super().__str__()]
        if self.status is not None:
            bits.append(f"status={self.status}")
        if self.url:
            bits.append(f"url={self.url}")
        return " ".join(bits)


class TemporaryHttpError(HttpError):
    """Worth retrying: throttling, transient server faults, WAF interstitials."""


class PermanentHttpError(HttpError):
    """Not worth retrying: 404, 410, a malformed URL, an unknown symbol."""


class BlankPageError(TemporaryHttpError):
    """
    HTTP 200 with the page skeleton but no data.

    screener.in answers sustained scraping this way: row labels render, every
    numeric span is empty. It is retryable, but only on a long backoff - a
    uniform 3s retry recovered 0 of 20 when measured.
    """

    def __init__(self, message: str, *, url: str | None = None,
                 reason: str = "numeric_spans_empty"):
        super().__init__(message, url=url, status=200)
        self.reason = reason


# Statuses that mean "come back later" rather than "this will never work".
TEMPORARY_STATUSES = frozenset({401, 403, 408, 425, 429, 500, 502, 503, 504, 522, 524})
PERMANENT_STATUSES = frozenset({400, 404, 405, 410, 451})


def classify_status(status: int, *, url: str | None = None,
                    body_excerpt: str | None = None) -> HttpError | None:
    """Map a status code to an exception, or None when it is a success."""
    if 200 <= status < 300:
        return None
    if status in TEMPORARY_STATUSES:
        return TemporaryHttpError(f"temporary HTTP failure", url=url, status=status,
                                  body_excerpt=body_excerpt)
    if status in PERMANENT_STATUSES:
        return PermanentHttpError(f"permanent HTTP failure", url=url, status=status,
                                  body_excerpt=body_excerpt)
    if 500 <= status < 600:
        return TemporaryHttpError(f"server error", url=url, status=status,
                                  body_excerpt=body_excerpt)
    return PermanentHttpError(f"unexpected HTTP status", url=url, status=status,
                              body_excerpt=body_excerpt)

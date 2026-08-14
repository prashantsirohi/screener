"""
Raw HTML retention: the layer that makes a parser fix replayable.

`screener_page_raw` stored only the parser's output, so "raw" meant "raw parser
result". `rebuild-facts` could replay the fact explosion over that, but not the
parse - whatever the parser dropped was gone with the response. These cover the
round trip that has to be lossless for a re-parse to mean anything.
"""

from __future__ import annotations

import gzip
import hashlib

import pytest

from market_screener.ingest.fundamentals_sync import PARSER_VERSION, RawPage


class FakeResponse:
    def __init__(self, text, url="https://www.screener.in/company/X/consolidated/",
                 status=200, content_type="text/html; charset=utf-8"):
        self.text = text
        self.url = url
        self.status_code = status
        self.headers = {"Content-Type": content_type}


SAMPLE = "<html><body><span class='number'>1,234.5</span> ₹ crore — ünïcode</body></html>"


def test_captures_the_body_and_its_metadata():
    r = RawPage.from_response(FakeResponse(SAMPLE))
    assert r.body == SAMPLE
    assert r.status == 200
    assert r.content_type.startswith("text/html")
    assert r.final_url.endswith("/consolidated/")


def test_round_trip_through_compression_is_lossless():
    """Non-ascii is the case that breaks naive encoding - rupee signs, dashes."""
    r = RawPage.from_response(FakeResponse(SAMPLE))
    assert gzip.decompress(r.gzipped()).decode("utf-8") == SAMPLE


def test_checksum_is_over_the_uncompressed_body():
    """
    So it identifies the response independently of how it was stored, and a
    re-fetch returning identical HTML is recognisable as such.
    """
    r = RawPage.from_response(FakeResponse(SAMPLE))
    assert r.sha256 == hashlib.sha256(SAMPLE.encode()).hexdigest()
    assert r.n_bytes == len(SAMPLE.encode())


def test_checksum_verifies_the_stored_bytes():
    """The re-parse path recomputes this; a mismatch must be detectable."""
    r = RawPage.from_response(FakeResponse(SAMPLE))
    restored = gzip.decompress(r.gzipped()).decode("utf-8", "replace")
    assert hashlib.sha256(restored.encode("utf-8", "replace")).hexdigest() == r.sha256

    corrupted = SAMPLE.replace("1,234.5", "9,999.9")
    assert hashlib.sha256(corrupted.encode()).hexdigest() != r.sha256


def test_compression_is_deterministic():
    """
    mtime=0 in the gzip header, so identical HTML stores as identical bytes.
    Without it every capture of an unchanged page looks different on disk.
    """
    a = RawPage.from_response(FakeResponse(SAMPLE)).gzipped()
    b = RawPage.from_response(FakeResponse(SAMPLE)).gzipped()
    assert a == b


def test_compression_is_worth_doing():
    """Real pages are 120-170KB of repetitive markup."""
    page = "<div class='row'><span>Sales</span><span>1,234</span></div>" * 2000
    r = RawPage.from_response(FakeResponse(page))
    assert len(r.gzipped()) < 0.2 * r.n_bytes


def test_final_url_is_recorded_separately_from_the_requested_one():
    """
    screener.in redirects /consolidated/ to the standalone page when no
    consolidated statements exist, which changes the basis of everything parsed
    from it. source_url is what we asked for; this is what we got.
    """
    r = RawPage.from_response(FakeResponse(
        SAMPLE, url="https://www.screener.in/company/X/"))
    assert not r.final_url.endswith("/consolidated/")


def test_missing_headers_do_not_break_capture():
    """A response object without headers must not lose the body."""
    class Bare:
        text = SAMPLE
        url = "https://www.screener.in/company/X/"

    r = RawPage.from_response(Bare())
    assert r.body == SAMPLE and r.status is None and r.content_type is None


def test_empty_body_is_captured_rather_than_crashing():
    r = RawPage.from_response(FakeResponse(""))
    assert r.n_bytes == 0
    assert gzip.decompress(r.gzipped()) == b""


def test_parser_version_is_stamped():
    assert PARSER_VERSION


@pytest.mark.parametrize("fn,needs", [
    ("reparse_pages_from_html", ("raw_html", "raw_sha256")),
])
def test_reparse_reads_html_and_verifies_it(fn, needs):
    import inspect

    from market_screener.ingest import fundamentals_sync

    src = inspect.getsource(getattr(fundamentals_sync, fn))
    for token in needs:
        assert token in src
    assert "gzip.decompress" in src
    assert "sha256" in src, "a re-parse that skips the checksum can replay corruption"


def test_store_persists_the_raw_columns():
    import inspect

    from market_screener.ingest import fundamentals_sync

    src = inspect.getsource(fundamentals_sync._store)
    for col in ("raw_html", "raw_sha256", "raw_bytes", "http_status",
                "content_type", "final_url"):
        assert col in src, f"{col} is not persisted"

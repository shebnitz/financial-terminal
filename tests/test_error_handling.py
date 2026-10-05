"""
Tests for _get_json()'s Tier 4 retry/backoff logic: a network failure,
an SEC 429 (rate-limited), or an SEC 5xx should be retried a couple of
times with backoff before giving up -- and giving up should always
raise a plain-English SecEdgarError, never a raw requests exception.

requests.get is monkeypatched to a small fake that can be told to fail
N times before succeeding (or to always fail); time.sleep is
monkeypatched to a no-op so these tests run instantly instead of
actually waiting out the backoff delays.

Run with:  python -m pytest tests/test_error_handling.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

import config  # noqa: E402
import sec_edgar  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")


def _install_common_fakes(monkeypatch, tmp_path):
    # A real EDGAR_CONTACT_EMAIL (not the placeholder) and a scratch
    # cache dir, so _get_json actually reaches the retry loop instead
    # of short-circuiting on the placeholder-email check or an old
    # cached file from a previous test.
    monkeypatch.setattr(config, "EDGAR_CONTACT_EMAIL", "kevin@example.com")
    monkeypatch.setattr(config, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(sec_edgar.time, "sleep", lambda seconds: None)  # skip real backoff delays


def test_get_json_retries_a_connection_error_then_succeeds(monkeypatch, tmp_path):
    _install_common_fakes(monkeypatch, tmp_path)
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError("no route to host")
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(requests, "get", fake_get)
    data = sec_edgar._get_json("https://example.com/x", cache_key="retry_success")
    assert data == {"ok": True}
    assert calls["n"] == 3  # failed twice, succeeded on the third attempt


def test_get_json_gives_up_after_max_attempts_with_a_clear_message(monkeypatch, tmp_path):
    _install_common_fakes(monkeypatch, tmp_path)

    def always_fails(url, headers=None, timeout=None):
        raise requests.exceptions.Timeout("SEC took too long")

    monkeypatch.setattr(requests, "get", always_fails)
    try:
        sec_edgar._get_json("https://example.com/x", cache_key="retry_exhausted")
        assert False, "expected a SecEdgarError"
    except sec_edgar.SecEdgarError as e:
        # Never leak the raw requests exception type/message alone --
        # the message should read like something a person can act on.
        assert "SEC EDGAR" in str(e)
        assert str(sec_edgar._MAX_HTTP_ATTEMPTS) in str(e)


def test_get_json_respects_retry_after_header_on_429(monkeypatch, tmp_path):
    _install_common_fakes(monkeypatch, tmp_path)
    calls = {"n": 0}
    sleeps: list[float] = []
    monkeypatch.setattr(sec_edgar.time, "sleep", lambda seconds: sleeps.append(seconds))

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(429, headers={"Retry-After": "7"})
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(requests, "get", fake_get)
    data = sec_edgar._get_json("https://example.com/x", cache_key="retry_after")
    assert data == {"ok": True}
    assert sleeps == [7.0]  # honored SEC's own Retry-After value, not the default backoff


def test_get_json_raises_clear_error_after_repeated_429(monkeypatch, tmp_path):
    _install_common_fakes(monkeypatch, tmp_path)

    def always_429(url, headers=None, timeout=None):
        return _FakeResponse(429)

    monkeypatch.setattr(requests, "get", always_429)
    try:
        sec_edgar._get_json("https://example.com/x", cache_key="retry_429_exhausted")
        assert False, "expected a SecEdgarError"
    except sec_edgar.SecEdgarError as e:
        assert "rate-limiting" in str(e) or "429" in str(e)


def test_get_json_retries_a_5xx_then_succeeds(monkeypatch, tmp_path):
    _install_common_fakes(monkeypatch, tmp_path)
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(503)
        return _FakeResponse(200, {"ok": True})

    monkeypatch.setattr(requests, "get", fake_get)
    data = sec_edgar._get_json("https://example.com/x", cache_key="retry_5xx")
    assert data == {"ok": True}
    assert calls["n"] == 2


def test_get_json_404_and_403_are_never_retried(monkeypatch, tmp_path):
    # A 404/403 means "this request is wrong" (bad ticker/CIK, or a
    # rejected User-Agent) -- retrying it would just waste time getting
    # the exact same answer, so these should fail on the FIRST attempt.
    _install_common_fakes(monkeypatch, tmp_path)
    calls = {"n": 0}

    def fake_get(url, headers=None, timeout=None):
        calls["n"] += 1
        return _FakeResponse(404)

    monkeypatch.setattr(requests, "get", fake_get)
    try:
        sec_edgar._get_json("https://example.com/x", cache_key="404_no_retry")
        assert False, "expected a SecEdgarError"
    except sec_edgar.SecEdgarError:
        pass
    assert calls["n"] == 1

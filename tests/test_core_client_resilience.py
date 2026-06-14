"""LingoCoreClient resilience — bounded retry/backoff + circuit breaker.

The client shares one module-level breaker across instances, so each test
resets it and the shared call count via the autouse fixture below.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest

import app.http.lingo_core_client as mod
from app.config import settings
from app.http.circuit_breaker import CircuitBreaker, CircuitOpenError
from app.http.lingo_core_client import LingoCoreClient


@pytest.fixture(autouse=True)
def _reset_breaker_and_settings(monkeypatch):
    monkeypatch.setattr(settings, "LINGO_CORE_URL", "http://lc")
    monkeypatch.setattr(settings, "INTERNAL_SERVICE_TOKEN", "tok")
    monkeypatch.setattr(settings, "CORE_CLIENT_MAX_RETRIES", 2)
    monkeypatch.setattr(settings, "CORE_CLIENT_BACKOFF_BASE_S", 0.01)
    monkeypatch.setattr(settings, "CORE_CLIENT_BACKOFF_MAX_S", 0.02)
    # Fresh breaker per test so state doesn't leak across tests.
    monkeypatch.setattr(
        mod,
        "_breaker",
        CircuitBreaker(name="test", threshold=5, cooldown_s=30.0),
    )
    # Don't actually sleep during backoff.
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)


def _resp(status_code: int) -> MagicMock:
    """A fake httpx.Response. raise_for_status raises HTTPStatusError for
    >=400, mirroring real httpx behaviour."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = {"ok": True}
    if status_code >= 400:
        request = httpx.Request("GET", "http://lc")
        real = httpx.Response(status_code, request=request)
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"{status_code}", request=request, response=real
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def _client_returning(*responses):
    """Patch httpx.Client so each successive call returns the next
    response (or raises, if the item is an Exception)."""
    calls = {"n": 0}
    items = list(responses)

    def _make_client():
        fake = MagicMock()

        def _send(*_a, **_kw):
            item = items[min(calls["n"], len(items) - 1)]
            calls["n"] += 1
            if isinstance(item, Exception):
                raise item
            return item

        fake.get.side_effect = _send
        fake.post.side_effect = _send
        return fake

    return patch("app.http.lingo_core_client.httpx.Client", side_effect=_make_client), calls


def test_success_first_try_no_retry():
    cm, calls = _client_returning(_resp(200))
    with cm:
        out = LingoCoreClient().list_quests("u-1")
    assert out == {"ok": True}
    assert calls["n"] == 1


def test_transient_5xx_then_success_retries():
    cm, calls = _client_returning(_resp(503), _resp(200))
    with cm:
        out = LingoCoreClient().list_quests("u-1")
    assert out == {"ok": True}
    assert calls["n"] == 2  # one retry


def test_transient_failure_exhausts_then_gives_up():
    # 1 + CORE_CLIENT_MAX_RETRIES(2) = 3 attempts, all 503.
    cm, calls = _client_returning(_resp(503), _resp(503), _resp(503))
    with cm:
        with pytest.raises(httpx.HTTPStatusError):
            LingoCoreClient().list_quests("u-1")
    assert calls["n"] == 3  # bounded — does NOT hammer indefinitely


def test_network_error_is_transient_and_retried():
    err = httpx.ConnectError("refused", request=httpx.Request("GET", "http://lc"))
    cm, calls = _client_returning(err, _resp(200))
    with cm:
        out = LingoCoreClient().list_quests("u-1")
    assert out == {"ok": True}
    assert calls["n"] == 2


def test_non_transient_4xx_not_retried():
    cm, calls = _client_returning(_resp(401))
    with cm:
        with pytest.raises(httpx.HTTPStatusError):
            LingoCoreClient().list_quests("u-1")
    assert calls["n"] == 1  # 401 surfaces immediately, no retry


def test_429_is_retried():
    cm, calls = _client_returning(_resp(429), _resp(200))
    with cm:
        out = LingoCoreClient().list_quests("u-1")
    assert out == {"ok": True}
    assert calls["n"] == 2


def test_breaker_opens_and_fails_fast(monkeypatch):
    # Lower threshold so we trip quickly. No retries to keep the math
    # simple — every attempt is one failure.
    monkeypatch.setattr(settings, "CORE_CLIENT_MAX_RETRIES", 0)
    monkeypatch.setattr(
        mod, "_breaker",
        CircuitBreaker(name="test", threshold=3, cooldown_s=30.0),
    )
    cm, calls = _client_returning(_resp(503))
    with cm:
        client = LingoCoreClient()
        # 3 failed calls trip the breaker.
        for _ in range(3):
            with pytest.raises(httpx.HTTPStatusError):
                client.list_quests("u-1")
        n_after_trip = calls["n"]
        # Next call must fail FAST via the open breaker — no new HTTP call.
        with pytest.raises(CircuitOpenError):
            client.list_quests("u-1")
    assert calls["n"] == n_after_trip  # breaker short-circuited, no socket


def test_non_transient_does_not_trip_breaker(monkeypatch):
    monkeypatch.setattr(settings, "CORE_CLIENT_MAX_RETRIES", 0)
    monkeypatch.setattr(
        mod, "_breaker",
        CircuitBreaker(name="test", threshold=2, cooldown_s=30.0),
    )
    cm, _ = _client_returning(_resp(404))
    with cm:
        client = LingoCoreClient()
        for _ in range(5):
            with pytest.raises(httpx.HTTPStatusError):
                client.list_quests("u-1")
        # 404s are the caller's fault, not core being down — breaker stays closed.
        assert mod._breaker.state == "closed"

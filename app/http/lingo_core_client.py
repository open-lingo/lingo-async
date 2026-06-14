"""HTTP client for callbacks into lingo-core.

Single-shot calls, short timeout. No connection pool — the worker batch
size is small (≤10) and each call is independent. If volume ever spikes
to the point this matters, switch to a module-level ``httpx.Client``.

Resilience (H3 operability)
---------------------------
Each call goes through ``_request``, which layers two protections so a
transient core blip doesn't escalate into a full SQS retry storm:

  1. **Bounded in-process retry with exponential backoff** on *transient*
     failures (network errors + 5xx + 429). Auth/validation failures
     (4xx other than 429) are NOT retried — they won't fix themselves.
  2. **A process-local circuit breaker** shared across every call. After
     a run of consecutive transient failures it opens and fails fast for
     a cooldown window, so we stop hammering a core that's plainly down.

When the retries are exhausted (or the breaker is open) we raise. The
handler dispatch loop catches that, marks the event failed, and lets SQS
requeue the message after the visibility timeout — coarse retry at the
queue layer, where it belongs. The point of the in-process layer is to
smooth over a sub-second core blip without a requeue, NOT to keep
pounding core; that's why the retry count is small and the breaker exists.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.config import settings
from app.http.circuit_breaker import CircuitBreaker, CircuitOpenError

logger = logging.getLogger("lingo_async.http")

_TIMEOUT = 5.0

# Shared across all LingoCoreClient instances in a warm container — the
# whole point is that one call learning "core is down" trips the breaker
# for the next call too. Constructed from settings at import time.
_breaker = CircuitBreaker(
    name="lingo_core",
    threshold=settings.CORE_CLIENT_BREAKER_THRESHOLD,
    cooldown_s=settings.CORE_CLIENT_BREAKER_COOLDOWN_S,
)


def _is_transient(exc: Exception) -> bool:
    """A failure worth retrying: network/timeout, or a 5xx/429 response.
    A 4xx (other than 429) is the caller's fault and won't fix itself."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code == 429
    # TransportError covers ConnectError/ReadTimeout/etc.
    return isinstance(exc, httpx.TransportError)


def _backoff_delay(attempt: int) -> float:
    base = settings.CORE_CLIENT_BACKOFF_BASE_S * (2**attempt)
    return min(base, settings.CORE_CLIENT_BACKOFF_MAX_S)


class LingoCoreClient:
    def __init__(self) -> None:
        self._base = settings.LINGO_CORE_URL.rstrip("/")
        self._headers = {"Authorization": f"Bearer {settings.INTERNAL_SERVICE_TOKEN}"}

    def _request(self, label: str, send: Callable[[httpx.Client], httpx.Response]) -> dict[str, Any]:
        """Run ``send`` against a fresh client with bounded retry/backoff,
        guarded by the shared breaker. ``send`` must perform exactly one
        HTTP call and return the (un-raised) response; we call
        ``raise_for_status`` here so status handling stays uniform.

        ``label`` is for logs only (e.g. ``"list_quests"``)."""
        if not _breaker.allow():
            # Fail fast — don't even open a socket. The handler will mark
            # the event failed and SQS requeues it after the breaker's
            # cooldown, by which point a probe will have re-tested core.
            raise CircuitOpenError(f"circuit open for lingo-core ({label})")

        last_exc: Exception | None = None
        max_attempts = 1 + max(0, settings.CORE_CLIENT_MAX_RETRIES)
        for attempt in range(max_attempts):
            client = httpx.Client()
            try:
                resp = send(client)
                resp.raise_for_status()
                _breaker.record_success()
                return resp.json()
            except Exception as exc:  # noqa: BLE001 — classified below
                last_exc = exc
                if not _is_transient(exc):
                    # Non-transient (e.g. 401 bad token, 404). Not a sign
                    # core is unhealthy, so don't count it against the
                    # breaker — but also don't retry; surface immediately.
                    logger.warning("core_call_non_transient label=%s err=%s", label, exc)
                    raise
                _breaker.record_failure()
                if attempt + 1 < max_attempts:
                    delay = _backoff_delay(attempt)
                    logger.warning(
                        "core_call_retry label=%s attempt=%d/%d err=%s backoff_s=%.2f",
                        label, attempt + 1, max_attempts, exc, delay,
                    )
                    time.sleep(delay)
            finally:
                client.close()

        # Exhausted retries on a transient failure. Give up gracefully —
        # raise so the handler requeues via SQS rather than us looping.
        logger.warning("core_call_giveup label=%s attempts=%d err=%s", label, max_attempts, last_exc)
        assert last_exc is not None  # loop ran at least once
        raise last_exc

    def verify_internal_token(self) -> None:
        """Make one cheap authenticated call into core to confirm our
        INTERNAL_SERVICE_TOKEN matches what core expects.

        Hits ``/quests/_internal/list`` with a throwaway user_id — it's
        gated by ``require_internal_service`` on the core side, returns an
        empty list for an unknown user, and is the cheapest internal
        endpoint we have. A 401 means the tokens have drifted between the
        two functions; raise so the caller can log loudly.

        Bypasses ``_request`` (no breaker, no retry): this is a one-shot
        boot probe, and a 401 here is permanent, not transient.
        """
        client = httpx.Client()
        try:
            resp = client.get(
                f"{self._base}/api/core/v1/quests/_internal/list",
                params={"user_id": "__token_self_check__"},
                headers=self._headers,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        finally:
            client.close()

    def list_quests(self, user_id: str) -> dict[str, Any]:
        return self._request(
            "list_quests",
            lambda c: c.get(
                f"{self._base}/api/core/v1/quests/_internal/list",
                params={"user_id": user_id},
                headers=self._headers,
                timeout=_TIMEOUT,
            ),
        )

    def bump_progress(self, quest_id: str, *, user_id: str, delta: int) -> dict[str, Any]:
        return self._request(
            "bump_progress",
            lambda c: c.post(
                f"{self._base}/api/core/v1/quests/_internal/{quest_id}/progress",
                json={"user_id": user_id, "delta": delta},
                headers=self._headers,
                timeout=_TIMEOUT,
            ),
        )

    def add_xp(
        self,
        *,
        user_id: str,
        amount: int,
        learning_language_id: str | None,
        leaderboard_opt_in: bool,
    ) -> dict[str, Any]:
        """Credit XP + write to the leaderboard via lingo-core. Used by
        the xp_awarded handler for events that came from a *non-lingo-core*
        producer (today: synthetic admin events from lingo-ops). Real
        lesson XP is credited inline by lingo-core's progress router
        before the event publishes, so we'd be double-counting if we
        called this for those — see the source-based gate in the handler.
        """
        return self._request(
            "add_xp",
            lambda c: c.post(
                f"{self._base}/api/core/v1/users/_internal/xp/add",
                json={
                    "user_id": user_id,
                    "amount": amount,
                    "learning_language_id": learning_language_id,
                    "leaderboard_opt_in": leaderboard_opt_in,
                },
                headers=self._headers,
                timeout=_TIMEOUT,
            ),
        )

"""A minimal process-local circuit breaker for the async→core callbacks.

Why this exists (H3 operability): a transient core 5xx shouldn't escalate
into a retry storm. The ``LingoCoreClient`` does bounded in-process retries
with backoff; this breaker is the next layer up. After ``threshold``
consecutive failures it OPENS — subsequent calls fail fast for
``cooldown`` seconds instead of waiting on timeouts + backoff against a
core that is clearly down. After the cooldown it goes HALF_OPEN and lets a
single probe through; success CLOSES it, failure re-OPENS for another
cooldown window.

Scope is deliberately a single Lambda execution environment (one warm
container). We don't coordinate breaker state across concurrent Lambda
instances — that would need a shared store and isn't worth it. Each warm
container independently learns core is down and backs off, which is enough
to take the aggregate pressure off core.

Not thread-safe by design: the worker dispatch loop is single-threaded
(one boto3 call per message, no concurrency — see app/handler.py).
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger("lingo_async.http.breaker")

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the breaker is open."""


class CircuitBreaker:
    def __init__(
        self,
        *,
        name: str,
        threshold: int,
        cooldown_s: float,
        clock=time.monotonic,
    ) -> None:
        self._name = name
        self._threshold = max(1, threshold)
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._state = CLOSED
        self._opened_at = 0.0

    @property
    def state(self) -> str:
        return self._state

    def _now(self) -> float:
        return self._clock()

    def allow(self) -> bool:
        """Return True if a call may proceed. Transitions OPEN→HALF_OPEN
        once the cooldown has elapsed (lets exactly one probe through)."""
        if self._state == OPEN:
            if self._now() - self._opened_at >= self._cooldown_s:
                self._state = HALF_OPEN
                logger.info("breaker_half_open name=%s probing", self._name)
                return True
            return False
        # CLOSED or HALF_OPEN both allow the call. HALF_OPEN only ever
        # reaches here via the OPEN→HALF_OPEN transition above (one probe).
        return True

    def record_success(self) -> None:
        if self._state != CLOSED:
            logger.info("breaker_closed name=%s recovered", self._name)
        self._failures = 0
        self._state = CLOSED

    def record_failure(self) -> None:
        self._failures += 1
        # A failed half-open probe means core is still down — re-open
        # immediately for another cooldown window.
        if self._state == HALF_OPEN or self._failures >= self._threshold:
            if self._state != OPEN:
                logger.warning(
                    "breaker_open name=%s failures=%d cooldown_s=%.1f",
                    self._name,
                    self._failures,
                    self._cooldown_s,
                )
            self._state = OPEN
            self._opened_at = self._now()

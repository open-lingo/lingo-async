"""CircuitBreaker — closed → open → half-open → closed transitions."""

from app.http.circuit_breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker


class FakeClock:
    """Manually advanced monotonic clock so cooldowns are deterministic."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _breaker(clock, threshold=3, cooldown=10.0):
    return CircuitBreaker(name="test", threshold=threshold, cooldown_s=cooldown, clock=clock)


def test_starts_closed_and_allows():
    b = _breaker(FakeClock())
    assert b.state == CLOSED
    assert b.allow() is True


def test_opens_after_threshold_consecutive_failures():
    b = _breaker(FakeClock(), threshold=3)
    b.record_failure()
    b.record_failure()
    assert b.state == CLOSED  # still under threshold
    assert b.allow() is True
    b.record_failure()  # 3rd → trips
    assert b.state == OPEN
    assert b.allow() is False  # fails fast within cooldown


def test_success_resets_failure_count():
    b = _breaker(FakeClock(), threshold=3)
    b.record_failure()
    b.record_failure()
    b.record_success()
    # Counter cleared — two more failures should NOT trip it.
    b.record_failure()
    b.record_failure()
    assert b.state == CLOSED


def test_half_open_after_cooldown_then_close_on_success():
    clock = FakeClock()
    b = _breaker(clock, threshold=2, cooldown=10.0)
    b.record_failure()
    b.record_failure()
    assert b.state == OPEN
    assert b.allow() is False

    clock.advance(10.0)
    assert b.allow() is True  # probe allowed
    assert b.state == HALF_OPEN
    b.record_success()
    assert b.state == CLOSED


def test_half_open_failure_reopens_for_another_cooldown():
    clock = FakeClock()
    b = _breaker(clock, threshold=2, cooldown=10.0)
    b.record_failure()
    b.record_failure()
    clock.advance(10.0)
    assert b.allow() is True  # half-open probe
    b.record_failure()  # probe failed → straight back to OPEN
    assert b.state == OPEN
    assert b.allow() is False  # cooldown restarts; no immediate retry

    clock.advance(9.9)
    assert b.allow() is False  # still cooling
    clock.advance(0.1)
    assert b.allow() is True  # cooldown elapsed again

"""Resolve the INTERNAL_SERVICE_TOKEN async sends on callbacks to lingo-core.

Precedence, by design:

  1. **Env var wins.** If ``settings.INTERNAL_SERVICE_TOKEN`` is non-empty
     (set via env / ``.env``, which is also what tests monkeypatch), use
     it verbatim. This keeps local dev, the local consumer, and the whole
     test suite deterministic — they never touch AWS.
  2. **SSM SecureString fallback.** Otherwise fetch
     ``/lingo/internal-service-token`` (WithDecryption) from SSM Parameter
     Store, in the configured region. This is the prod path: the token
     lives in SSM, not baked into the Lambda env.

The resolved value is cached for the life of the warm container — SSM
parameters are looked up once at first use, not per callback. A token
rotation is picked up on the next cold start, which matches how every
other cold-start-bound config (Dynamo client, breaker) behaves here.

Failure policy: a token mismatch already broke quests once in prod, so we
are conservative — ANY SSM failure (no perms, param missing, network)
degrades gracefully to the (possibly empty) env value rather than
crashing the handler at import time. An empty result surfaces loudly in
the cold-start self-check (see app/handler.py), which is the right place
for "callbacks will 401" to be visible, not an import-time crash that
takes the whole worker down.
"""

from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger("lingo_async.http")

_SSM_PARAMETER_NAME = "/lingo/internal-service-token"

# Cached resolved token for the warm container. ``None`` means "not yet
# resolved"; a resolved-but-empty token is cached as "" so we don't retry
# SSM on every callback when it's genuinely unavailable.
_cached_token: str | None = None


def _fetch_from_ssm() -> str:
    """Best-effort SSM SecureString fetch. Returns "" on ANY failure —
    never raises, so a missing param / IAM gap can't crash the worker."""
    try:
        import boto3

        ssm = boto3.client("ssm", region_name=settings.AWS_REGION)
        resp = ssm.get_parameter(Name=_SSM_PARAMETER_NAME, WithDecryption=True)
        value = resp.get("Parameter", {}).get("Value", "")
        if value:
            logger.info("internal_service_token resolved from SSM param=%s", _SSM_PARAMETER_NAME)
        else:
            logger.warning("internal_service_token SSM param=%s returned empty", _SSM_PARAMETER_NAME)
        return value
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never crash boot
        logger.warning(
            "internal_service_token SSM fetch failed param=%s err=%s — "
            "falling back to env value (may be empty)",
            _SSM_PARAMETER_NAME,
            exc,
        )
        return ""


def resolve_internal_service_token() -> str:
    """Return the bearer token for async→core callbacks.

    Env wins; SSM is the fallback. Cached for the warm container."""
    global _cached_token

    # Env-set always wins, and bypasses the cache entirely so tests that
    # monkeypatch ``settings.INTERNAL_SERVICE_TOKEN`` per-case see their
    # value without a reset hook. Cheap — it's a string read.
    env_value = settings.INTERNAL_SERVICE_TOKEN
    if env_value:
        return env_value

    if _cached_token is None:
        _cached_token = _fetch_from_ssm()
    return _cached_token


def reset_cache() -> None:
    """Drop the cached SSM value. Test-only — lets a test exercise the SSM
    path without container-restart semantics leaking across cases."""
    global _cached_token
    _cached_token = None

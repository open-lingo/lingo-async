"""Lambda entry point for the async worker.

Wired to the ``lingo-events`` SQS queue via event source mapping
(see lingo-infra). AWS invokes us with:

    {
      "Records": [
        {"messageId": "...", "body": "<json>", ...},
        ...
      ]
    }

We:
  1. Parse each ``body`` against the ``EventMessage`` discriminated union.
  2. Save an event row to the events log (if a repo is configured).
  3. Look up the handler by ``type`` and run it.
  4. Update the event row with the final status + outcomes.
  5. On any exception, append the ``messageId`` to ``batchItemFailures``
     so SQS retries that specific message — successful peers in the
     same batch still auto-delete.

This is the partial-batch-failure pattern; the event source mapping
must have ``FunctionResponseTypes = ["ReportBatchItemFailures"]`` for
the return value to be honoured.

The handler is sync (not async). Lambda's Python runtime executes both
shapes, but the SQS dispatch loop here is one boto3 call per message
with no concurrency to exploit — async would add overhead with no
throughput benefit.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import settings
from app.contracts.messages import (
    AdWatchedMessage,
    EventMessage,
    FriendAddedMessage,
    LessonCompletedMessage,
    ReviewCompletedMessage,
    SubscriptionChangedMessage,
    XpAwardedMessage,
    parse_event,
)
from app.db.provider import get_events_repo
from app.handlers import (
    ad_watched,
    friend_added,
    lesson_completed,
    review_completed,
    subscription_changed,
    xp_awarded,
)
from app.http.internal_token import resolve_internal_service_token
from app.http.lingo_core_client import LingoCoreClient

# Configure root logger once at module load (cold start). Lambda's
# default handler writes to CloudWatch Logs.
logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger("lingo_async.handler")


def _verify_internal_token_on_boot() -> None:
    """Cold-start probe: confirm our INTERNAL_SERVICE_TOKEN matches core's.

    A token mismatch silently broke quests earlier in this project — a
    drifted token surfaced only as 401s buried in handler retries, with
    nothing at boot to point at the cause. This makes the failure loud and
    unambiguous on the first warm-up of every container.

    Failure policy:
      - 401/403  → token mismatch. Log a LOUD error. This is the case we
        exist to catch.
      - other HTTP / network error → core may just be cold or briefly
        unreachable; log at WARNING and move on (don't block the worker).
      - no token configured → ERROR (callbacks will fail).

    Never raises: a boot probe must not crash the Lambda. We only log.
    """
    # Resolve via env-wins / SSM-fallback (see app/http/internal_token.py).
    # An empty result here means neither source produced a token — the
    # callbacks will 401, which is exactly the case this probe exists to
    # surface loudly at boot.
    if not resolve_internal_service_token():
        logger.error(
            "INTERNAL_SERVICE_TOKEN unresolved (env empty + SSM "
            "unavailable) — all async→core callbacks (quests, xp) will "
            "fail with 401. Set the env var or the SSM SecureString "
            "/lingo/internal-service-token."
        )
        return
    try:
        LingoCoreClient().verify_internal_token()
        logger.info("internal_token_self_check ok core=%s", settings.LINGO_CORE_URL)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            logger.error(
                "INTERNAL_SERVICE_TOKEN MISMATCH — lingo-core rejected our "
                "token with HTTP %d. async→core callbacks (quests, xp) WILL "
                "FAIL until the token is realigned between lingo-async and "
                "lingo-core. core=%s",
                code,
                settings.LINGO_CORE_URL,
            )
        else:
            logger.warning("internal_token_self_check unexpected_status=%d", code)
    except httpx.HTTPError as exc:
        # Network/timeout — core cold or unreachable at our cold start.
        # Not necessarily a token problem; don't cry wolf.
        logger.warning("internal_token_self_check unreachable err=%s", exc)


# Run once per warm container at import time (cold start).
_verify_internal_token_on_boot()


# Dispatch table — keyed on the message ``type`` literal. We store the
# handler MODULE (not the bound ``.handle`` callable) so test code can
# monkeypatch ``module.handle`` at runtime and have the dispatch loop
# pick up the replacement. Binding the callable at module load would
# freeze it past the reach of ``patch.object``.
_DISPATCH: dict[str, tuple[type, object]] = {
    "xp_awarded": (XpAwardedMessage, xp_awarded),
    "lesson_completed": (LessonCompletedMessage, lesson_completed),
    "review_completed": (ReviewCompletedMessage, review_completed),
    "friend_added": (FriendAddedMessage, friend_added),
    "subscription_changed": (SubscriptionChangedMessage, subscription_changed),
    "ad_watched": (AdWatchedMessage, ad_watched),
}

# 30-day TTL — Dynamo prunes; sqlite ignores.
_TTL_SECONDS = 30 * 24 * 3600


def _get_events_repo():
    """Indirection so tests can monkeypatch."""
    return get_events_repo()


def _safe_save(repo, **kwargs) -> None:
    """Best-effort event-log write. The log is an append-only audit trail —
    a write failure (Dynamo throttle/outage, IAM gap) must NEVER fail the
    batch and disrupt the critical dispatch path (quest/XP callbacks). Swallow
    and log; the message still processes."""
    if repo is None:
        return
    try:
        repo.save(**kwargs)
    except Exception:  # noqa: BLE001
        logger.exception("events_log_save_failed message_id=%s", kwargs.get("id"))


def _safe_update_status(repo, *, id, status, error_msg, outcomes) -> None:
    """Best-effort event-log status update. See _safe_save — never fatal.
    Serializes ``outcomes`` inside the guard so neither the JSON encoding nor
    the Dynamo write can escape when the log is enabled, and so the encode is
    skipped entirely when there's no repo."""
    if repo is None:
        return
    try:
        repo.update_status(
            id=id,
            status=status,
            error_msg=error_msg,
            outcomes_json=json.dumps(outcomes),
        )
    except Exception:  # noqa: BLE001
        logger.exception("events_log_update_status_failed message_id=%s", id)


def lambda_handler(event: dict, context: object) -> dict:
    """SQS-triggered Lambda entry point.

    Returns ``{"batchItemFailures": [{"itemIdentifier": <id>}, ...]}``.
    Only the listed message ids are retried; the rest auto-delete from
    the queue.

    Each record is written to the event log before dispatch (status
    ``received``) and updated after (status ``ok`` or ``failed``) with
    the handler's outcomes merged in.
    """
    records = event.get("Records") or []
    failures: list[dict[str, str]] = []
    repo = _get_events_repo()

    for record in records:
        message_id = record.get("messageId", "<unknown>")
        body_str = record.get("body") or "{}"
        received_at = datetime.now(UTC).isoformat()
        ttl_epoch = int(datetime.now(UTC).timestamp() + _TTL_SECONDS)
        parsed: EventMessage | None = None
        outcomes: list[dict[str, Any]] = []
        error_msg: str | None = None
        status = "ok"

        try:
            parsed = parse_event(body_str)
        except ValidationError as exc:
            # Bad envelope. Retrying won't help — the producer is wrong.
            # Still report it so the message moves to DLQ after
            # maxReceiveCount, where we can inspect it.
            logger.warning(
                "validation_error message_id=%s errors=%s",
                message_id,
                exc.errors(),
            )
            failures.append({"itemIdentifier": message_id})
            _safe_save(
                repo,
                id=message_id,
                user_id="<unparsed>",
                event_type="<unparsed>",
                payload_json=body_str,
                received_at=received_at,
                ttl_epoch=ttl_epoch,
            )
            _safe_update_status(
                repo,
                id=message_id,
                status="failed",
                error_msg=str(exc.errors()),
                outcomes=[],
            )
            continue

        _safe_save(
            repo,
            id=message_id,
            user_id=parsed.user_id,
            event_type=parsed.type,
            payload_json=body_str,
            received_at=received_at,
            ttl_epoch=ttl_epoch,
        )

        try:
            outcomes = _dispatch(parsed) or []
        except Exception as exc:
            # Handler error — could be transient (Dynamo throttle) or
            # permanent (bug). Either way, retry; the DLQ is the safety
            # net for permanent failures.
            logger.exception(
                "handler_error message_id=%s err=%s", message_id, exc
            )
            failures.append({"itemIdentifier": message_id})
            status = "failed"
            error_msg = repr(exc)

        _safe_update_status(
            repo,
            id=message_id,
            status=status,
            error_msg=error_msg,
            outcomes=outcomes,
        )

    if failures:
        logger.info("batch_partial_failure count=%d total=%d", len(failures), len(records))

    return {"batchItemFailures": failures}


def _dispatch(event: EventMessage) -> list[dict[str, Any]]:
    """Route a parsed event to its handler. Unknown types are a producer
    bug (the union should have rejected the parse) — raise to mark the
    message failed."""
    entry = _DISPATCH.get(event.type)
    if entry is None:
        raise ValueError(f"no handler registered for type={event.type!r}")
    _, module = entry
    logger.debug("dispatch type=%s", event.type)
    return module.handle(event) or []

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
  2. Look up the handler by ``type``.
  3. Run it.
  4. On any exception, append the ``messageId`` to ``batchItemFailures``
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

import logging

from pydantic import ValidationError

from app.config import settings
from app.contracts.messages import (
    EventMessage,
    FriendAddedMessage,
    LessonCompletedMessage,
    ReviewCompletedMessage,
    SubscriptionChangedMessage,
    XpAwardedMessage,
    parse_event,
)
from app.handlers import (
    friend_added,
    lesson_completed,
    review_completed,
    subscription_changed,
    xp_awarded,
)

# Configure root logger once at module load (cold start). Lambda's
# default handler writes to CloudWatch Logs.
logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger("lingo_async.handler")


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
}


def lambda_handler(event: dict, context: object) -> dict:
    """SQS-triggered Lambda entry point.

    Returns ``{"batchItemFailures": [{"itemIdentifier": <id>}, ...]}``.
    Only the listed message ids are retried; the rest auto-delete from
    the queue.
    """
    records = event.get("Records") or []
    failures: list[dict[str, str]] = []

    for record in records:
        message_id = record.get("messageId", "<unknown>")
        try:
            parsed = parse_event(record.get("body") or "{}")
            _dispatch(parsed)
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
        except Exception as exc:
            # Handler error — could be transient (Dynamo throttle) or
            # permanent (bug). Either way, retry; the DLQ is the safety
            # net for permanent failures.
            logger.exception(
                "handler_error message_id=%s err=%s", message_id, exc
            )
            failures.append({"itemIdentifier": message_id})

    if failures:
        logger.info("batch_partial_failure count=%d total=%d", len(failures), len(records))

    return {"batchItemFailures": failures}


def _dispatch(event: EventMessage) -> None:
    """Route a parsed event to its handler. Unknown types are a producer
    bug (the union should have rejected the parse) — raise to mark the
    message failed."""
    entry = _DISPATCH.get(event.type)
    if entry is None:
        raise ValueError(f"no handler registered for type={event.type!r}")
    _, module = entry
    logger.debug("dispatch type=%s", event.type)
    module.handle(event)

"""Discriminated-union message contracts for the lingo-events SQS queue.

Every message has:
  - ``type``    — string literal, discriminator
  - ``version`` — int literal, currently always 1

When a contract changes, BUMP the version (add a new union branch) rather
than silently extending an existing one. In-flight messages from the prior
producer build will still arrive with the old version, and a silent
extension is a great way to lose data on the cut-over.

Producers (lingo-core, lingo-ops) MUST mirror these shapes. The minimum
producer wrapper is in ``../lingo-core/app/events/publisher.py``.
"""

import base64
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError


class _EnvelopeV1(BaseModel):
    """Base envelope. Every v1 message carries these fields plus its own
    discriminator and payload. Subclasses pin ``type`` and ``version`` as
    ``Literal`` so Pydantic's discriminated union can dispatch."""

    version: Literal[1] = 1


class XpAwardedMessage(_EnvelopeV1):
    """User earned XP from any source. Drives leaderboard increments and
    XP-bucket quest progress (e.g. "earn 100 XP today")."""

    type: Literal["xp_awarded"] = "xp_awarded"
    user_id: str
    amount: int = Field(ge=0)
    source: Literal["lesson", "review", "manual", "quest", "streak"]
    # Self-contained payload: the producer reads these from the user's
    # in-flight session row and includes them, so the consumer can fan
    # out to the leaderboard without a second DB roundtrip. Optional +
    # safely defaulted so in-flight messages from legacy producers parse.
    learning_language_id: str | None = None
    leaderboard_opt_in: bool = True


class LessonCompletedMessage(_EnvelopeV1):
    """A lesson attempt finished (passed or failed — ``score`` reflects).
    Drives lesson-count quests + first-perfect-lesson achievements."""

    type: Literal["lesson_completed"] = "lesson_completed"
    user_id: str
    lesson_id: str
    score: float = Field(ge=0.0, le=1.0)
    perfect: bool
    attempted_at: datetime


class ReviewCompletedMessage(_EnvelopeV1):
    """One or more SRS review cards were graded. Drives review-count +
    streak quests.

    Producers should batch a whole sync into ONE message with ``count``
    set to the number of reviews. ``card_id`` / ``modality`` / ``rating``
    describe one representative card (the last in the batch) for
    observability; the consumer uses ``count`` as the delta.
    """

    type: Literal["review_completed"] = "review_completed"
    user_id: str
    card_id: str
    modality: Literal["recognition", "production"]
    rating: Literal["again", "hard", "good", "easy"]
    # NEW (additive): how many reviews this event represents. Quest
    # evaluator uses this as the delta for ``unit="cards"`` quests.
    # Legacy producers don't set it; default = 1.
    count: int = Field(default=1, ge=1)


class FriendAddedMessage(_EnvelopeV1):
    """A friendship edge was created (reciprocal — the producer fires ONE
    event with both ids; the consumer evaluates quests for ``user_id`` only.
    If the friend's own quests also need evaluation, the producer fires a
    second event with the ids swapped). Drives social quests."""

    type: Literal["friend_added"] = "friend_added"
    user_id: str
    friend_id: str


class AdWatchedMessage(_EnvelopeV1):
    """User watched a rewarded ad and was credited lingots in lingo-core
    before publish. Currently no consumer-side side effect — captured for
    the event-store inspector so we can graph watch volume per placement
    while the FE is iterating. A handler will land when we wire ad-time
    quests or anti-abuse caps."""

    type: Literal["ad_watched"] = "ad_watched"
    user_id: str
    placement: str
    idempotency_key: str
    lingots_awarded: int = Field(ge=0)
    occurred_at: datetime


class SubscriptionChangedMessage(_EnvelopeV1):
    """Subscription tier transitioned. Stub for now — future home for
    premium-badge fan-out and churn-watch alerts."""

    type: Literal["subscription_changed"] = "subscription_changed"
    user_id: str
    tier: Literal["free", "supporter", "patron", "lifetime"]
    event: Literal["new", "upgraded", "downgraded", "churned"]


# The union producers serialize from and the consumer parses into.
# Pydantic's discriminated-union dispatch on ``type`` is exhaustive — adding
# a new message type means adding a class above, extending the Union below,
# and adding a handler entry in ``app.handler``.
EventMessage = Annotated[
    XpAwardedMessage
    | LessonCompletedMessage
    | ReviewCompletedMessage
    | FriendAddedMessage
    | AdWatchedMessage
    | SubscriptionChangedMessage,
    Field(discriminator="type"),
]


# TypeAdapter caches the validator construction — building it per-call is
# the easy perf footgun with discriminated unions.
_EVENT_ADAPTER: TypeAdapter[EventMessage] = TypeAdapter(EventMessage)


def _try_kombu_envelope(text: str) -> str | None:
    """If ``text`` is a kombu envelope JSON, return its decoded inner payload;
    otherwise None. The envelope shape is::

        {"body": "<base64 JSON>", "content-encoding": "utf-8",
         "content-type": "application/json",
         "properties": {"body_encoding": "base64", ...}}
    """
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if (
        isinstance(obj, dict)
        and "body" in obj
        and "content-type" in obj
        and isinstance(obj.get("properties"), dict)
    ):
        inner = obj["body"]
        if obj["properties"].get("body_encoding") == "base64":
            encoding = obj.get("content-encoding") or "utf-8"
            return base64.b64decode(inner).decode(encoding)
        # Non-base64 kombu bodies carry the payload as a plain string.
        return inner if isinstance(inner, str) else json.dumps(inner)
    return None


def _unwrap_kombu_envelope(body: str | bytes) -> str | bytes:
    """Unwrap kombu's SQS message envelope, if present.

    lingo-core publishes via kombu (see ``lingo-core/app/events/publisher.py``).
    kombu's SQS transport does NOT put the raw event on the queue. It wraps the
    event in an envelope, base64-encodes the real payload into the envelope's
    ``body``, and then base64-encodes the WHOLE envelope as the SQS message body.
    So the prod wire format is two base64 layers deep::

        SQS body  = base64( envelope_json )
        envelope  = {"body": base64(event_json), "properties": {"body_encoding": "base64"}, ...}

    In local dev the kombu *consumer* (``app/local_consumer.py``) decodes all of
    this for us, so ``lambda_handler`` sees the bare event. But in prod the
    Lambda is driven by the raw SQS event source mapping — kombu is never in the
    consume path — so we must unwrap here. Without this, every real event fails
    (``Invalid JSON`` on the outer base64, or ``union_tag_not_found`` on the
    envelope) and lands in the DLQ (prod outage 2026-06-13).

    Returns the inner event payload when the input is a (possibly base64-wrapped)
    kombu envelope, otherwise the input unchanged (so raw/non-enveloped producers
    and tests keep working).
    """
    text = body.decode("utf-8") if isinstance(body, bytes) else body

    # The envelope may arrive as plain JSON (single layer) ...
    inner = _try_kombu_envelope(text)
    if inner is not None:
        return inner

    # ... or base64-encoded as the whole SQS message body (the prod format).
    try:
        decoded = base64.b64decode(text, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return body  # not base64 → a raw event JSON string; let the caller validate
    inner = _try_kombu_envelope(decoded)
    if inner is not None:
        return inner
    return body


def parse_event(body: str | bytes | dict) -> EventMessage:
    """Parse an SQS message body into the appropriate concrete message type.

    Accepts a JSON string/bytes (the raw SQS Records[*].body) or an already-
    decoded dict (handy in tests). Transparently unwraps kombu's SQS envelope
    (the prod producer's wire format). Raises ``pydantic.ValidationError`` on
    mismatch — the dispatch loop catches and reports the message id back to
    SQS for retry.
    """
    if isinstance(body, (str, bytes)):
        return _EVENT_ADAPTER.validate_json(_unwrap_kombu_envelope(body))
    return _EVENT_ADAPTER.validate_python(body)


__all__ = [
    "AdWatchedMessage",
    "EventMessage",
    "FriendAddedMessage",
    "LessonCompletedMessage",
    "ReviewCompletedMessage",
    "SubscriptionChangedMessage",
    "ValidationError",
    "XpAwardedMessage",
    "parse_event",
]

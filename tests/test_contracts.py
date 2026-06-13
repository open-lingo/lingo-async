"""Discriminated-union round-trip tests for the message contracts."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.contracts.messages import (
    FriendAddedMessage,
    LessonCompletedMessage,
    ReviewCompletedMessage,
    SubscriptionChangedMessage,
    XpAwardedMessage,
    parse_event,
)


def test_xp_awarded_parses_from_json_string() -> None:
    body = json.dumps(
        {
            "type": "xp_awarded",
            "version": 1,
            "user_id": "u-1",
            "amount": 25,
            "source": "lesson",
        }
    )
    parsed = parse_event(body)
    assert isinstance(parsed, XpAwardedMessage)
    assert parsed.user_id == "u-1"
    assert parsed.amount == 25
    assert parsed.source == "lesson"


def test_lesson_completed_parses_iso_datetime() -> None:
    parsed = parse_event(
        {
            "type": "lesson_completed",
            "version": 1,
            "user_id": "u-1",
            "lesson_id": "ja-01",
            "score": 0.8,
            "perfect": False,
            "attempted_at": "2026-05-27T18:30:00+00:00",
        }
    )
    assert isinstance(parsed, LessonCompletedMessage)
    assert parsed.attempted_at == datetime(2026, 5, 27, 18, 30, tzinfo=UTC)


def test_review_completed_validates_modality_enum() -> None:
    parsed = parse_event(
        {
            "type": "review_completed",
            "version": 1,
            "user_id": "u-1",
            "card_id": "card-xyz",
            "modality": "production",
            "rating": "good",
        }
    )
    assert isinstance(parsed, ReviewCompletedMessage)


def test_review_completed_rejects_bad_rating() -> None:
    with pytest.raises(ValidationError):
        parse_event(
            {
                "type": "review_completed",
                "version": 1,
                "user_id": "u-1",
                "card_id": "card-xyz",
                "modality": "recognition",
                "rating": "AWESOME",
            }
        )


def test_friend_added_parses() -> None:
    parsed = parse_event(
        {
            "type": "friend_added",
            "version": 1,
            "user_id": "u-1",
            "friend_id": "u-2",
        }
    )
    assert isinstance(parsed, FriendAddedMessage)


def test_subscription_changed_parses() -> None:
    parsed = parse_event(
        {
            "type": "subscription_changed",
            "version": 1,
            "user_id": "u-1",
            "tier": "supporter",
            "event": "new",
        }
    )
    assert isinstance(parsed, SubscriptionChangedMessage)


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_event({"type": "wat", "version": 1, "user_id": "u-1"})


def test_wrong_version_is_rejected() -> None:
    # Discriminator on ``type`` resolves the subclass; that subclass then
    # rejects ``version != 1`` via the Literal constraint.
    with pytest.raises(ValidationError):
        parse_event(
            {
                "type": "xp_awarded",
                "version": 2,
                "user_id": "u-1",
                "amount": 1,
                "source": "manual",
            }
        )


def test_amount_must_be_nonnegative() -> None:
    with pytest.raises(ValidationError):
        parse_event(
            {
                "type": "xp_awarded",
                "version": 1,
                "user_id": "u-1",
                "amount": -1,
                "source": "manual",
            }
        )


def test_xp_awarded_carries_language_and_optin():
    from app.contracts.messages import parse_event

    body = {
        "type": "xp_awarded",
        "version": 1,
        "user_id": "u-1",
        "amount": 25,
        "source": "lesson",
        "learning_language_id": "ja",
        "leaderboard_opt_in": True,
    }
    msg = parse_event(body)
    assert msg.learning_language_id == "ja"
    assert msg.leaderboard_opt_in is True


def test_xp_awarded_defaults_when_legacy_producer():
    from app.contracts.messages import parse_event

    # Legacy producer doesn't know about the new fields yet.
    body = {
        "type": "xp_awarded",
        "version": 1,
        "user_id": "u-1",
        "amount": 25,
        "source": "lesson",
    }
    msg = parse_event(body)
    assert msg.learning_language_id is None
    assert msg.leaderboard_opt_in is True  # safe default


def _kombu_envelope_json(payload: dict) -> str:
    """The kombu envelope (JSON) with the event base64-encoded into ``body`` —
    the single-layer form."""
    inner = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return json.dumps(
        {
            "body": inner,
            "content-encoding": "utf-8",
            "content-type": "application/json",
            "headers": {},
            "properties": {
                "delivery_mode": 2,
                "delivery_info": {"exchange": "lingo-events", "routing_key": "events"},
                "priority": 0,
                "body_encoding": "base64",
                "delivery_tag": "abc-123",
            },
        }
    )


def _kombu_sqs_body(payload: dict) -> str:
    """The EXACT bytes kombu's SQS transport lands as ``Records[*].body``: the
    whole envelope base64-encoded (verified against a real prod DLQ message,
    2026-06-13). Two base64 layers: outer = envelope, inner = event."""
    return base64.b64encode(_kombu_envelope_json(payload).encode("utf-8")).decode("ascii")


def test_parse_event_unwraps_kombu_sqs_envelope() -> None:
    # The real prod wire format: kombu base64-encodes the whole envelope as the
    # SQS body, and the event is base64'd inside the envelope (two layers). The
    # prod Lambda was choking on the outer layer (outage 2026-06-13).
    event = {
        "type": "lesson_completed",
        "version": 1,
        "user_id": "u-1",
        "lesson_id": "ko-m1-intro",
        "score": 1.0,
        "perfect": True,
        "attempted_at": "2026-06-13T07:34:45.679Z",
    }
    parsed = parse_event(_kombu_sqs_body(event))
    assert isinstance(parsed, LessonCompletedMessage)
    assert parsed.user_id == "u-1"
    assert parsed.lesson_id == "ko-m1-intro"
    assert parsed.perfect is True


def test_parse_event_unwraps_single_layer_kombu_envelope() -> None:
    # Defensive: also accept the envelope as plain JSON (no outer base64), in
    # case a transport variant delivers it that way.
    parsed = parse_event(
        _kombu_envelope_json(
            {
                "type": "review_completed",
                "version": 1,
                "user_id": "u-9",
                "card_id": "ja:kdrama-1",
                "modality": "production",
                "rating": "good",
                "count": 6,
            }
        )
    )
    assert isinstance(parsed, ReviewCompletedMessage)
    assert parsed.user_id == "u-9"


def test_parse_event_still_accepts_raw_json_string() -> None:
    # Regression: non-enveloped producers (e.g. a future direct-boto3 sender
    # or a test) must keep working after the unwrap is added.
    body = json.dumps(
        {
            "type": "xp_awarded",
            "version": 1,
            "user_id": "u-2",
            "amount": 10,
            "source": "manual",
        }
    )
    parsed = parse_event(body)
    assert isinstance(parsed, XpAwardedMessage)
    assert parsed.amount == 10

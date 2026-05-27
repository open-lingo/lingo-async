"""Discriminated-union round-trip tests for the message contracts."""

from __future__ import annotations

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

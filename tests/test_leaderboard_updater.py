"""Leaderboard updater tests — stubs the Dynamo table handle to assert
on the read shape and the two UpdateItem calls."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app.contracts.messages import XpAwardedMessage
from app.leaderboard import updater


def _make_user_table(item: dict | None):
    """A MagicMock for a Dynamo Table whose get_item returns ``item``."""
    table = MagicMock()
    table.get_item.return_value = {"Item": item} if item is not None else {}
    return table


def test_skips_when_amount_zero() -> None:
    event = XpAwardedMessage(user_id="u-1", amount=0, source="manual")
    with patch.object(updater, "get_table") as gt:
        updater.update_leaderboard(event)
    gt.assert_not_called()


def test_skips_when_user_row_missing() -> None:
    event = XpAwardedMessage(user_id="u-1", amount=5, source="manual")
    user_table = _make_user_table(None)
    leaderboard_table = MagicMock()

    def fake_get_table(name: str):
        if "users" in name:
            return user_table
        return leaderboard_table

    with patch.object(updater, "get_table", side_effect=fake_get_table):
        updater.update_leaderboard(event)

    leaderboard_table.update_item.assert_not_called()


def test_skips_when_opted_out() -> None:
    event = XpAwardedMessage(user_id="u-1", amount=5, source="manual")
    user_table = _make_user_table(
        {
            "settings": {
                "learning": {"learningLanguageId": "ja"},
                "social": {"show_on_leaderboard": False},
            }
        }
    )
    leaderboard_table = MagicMock()

    def fake_get_table(name: str):
        return user_table if "users" in name else leaderboard_table

    with patch.object(updater, "get_table", side_effect=fake_get_table):
        updater.update_leaderboard(event)

    leaderboard_table.update_item.assert_not_called()


def test_skips_when_no_language_set() -> None:
    event = XpAwardedMessage(user_id="u-1", amount=5, source="manual")
    user_table = _make_user_table(
        {
            "settings": {
                "learning": {},
                "social": {"show_on_leaderboard": True},
            }
        }
    )
    leaderboard_table = MagicMock()

    def fake_get_table(name: str):
        return user_table if "users" in name else leaderboard_table

    with patch.object(updater, "get_table", side_effect=fake_get_table):
        updater.update_leaderboard(event)

    leaderboard_table.update_item.assert_not_called()


def test_writes_weekly_and_monthly_buckets() -> None:
    event = XpAwardedMessage(user_id="u-1", amount=15, source="lesson")
    user_table = _make_user_table(
        {
            "settings": {
                "learning": {"learningLanguageId": "ja"},
                "social": {"show_on_leaderboard": True},
            }
        }
    )
    leaderboard_table = MagicMock()

    def fake_get_table(name: str):
        return user_table if "users" in name else leaderboard_table

    with patch.object(updater, "get_table", side_effect=fake_get_table):
        updater.update_leaderboard(event)

    # Two UpdateItem calls — one weekly, one monthly.
    assert leaderboard_table.update_item.call_count == 2

    pks = {call.kwargs["Key"]["PK"] for call in leaderboard_table.update_item.call_args_list}
    sks = {call.kwargs["Key"]["SK"] for call in leaderboard_table.update_item.call_args_list}
    assert any(pk.startswith("BUCKET#ja#") and "W" in pk for pk in pks), pks
    assert any(pk.startswith("BUCKET#ja#") and "W" not in pk for pk in pks), pks
    assert sks == {"USER#u-1"}

    # Each call uses ADD xp to atomically increment.
    for call in leaderboard_table.update_item.call_args_list:
        assert ":amount" in call.kwargs["ExpressionAttributeValues"]
        assert call.kwargs["ExpressionAttributeValues"][":amount"] == 15
        assert "ADD xp :amount" in call.kwargs["UpdateExpression"]


def test_bucket_strings_match_terraform_scheme() -> None:
    # Use the module's internal helpers directly to lock in the format.
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)  # Wednesday, ISO week 22 of 2026
    weekly, _ = updater._weekly_bucket("ja", now)
    monthly, _ = updater._monthly_bucket("ja", now)
    assert weekly == "ja#2026-W22"
    assert monthly == "ja#2026-05"


def test_ttl_is_in_the_future() -> None:
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    _, weekly_ttl = updater._weekly_bucket("ja", now)
    _, monthly_ttl = updater._monthly_bucket("ja", now)
    assert weekly_ttl > int(now.timestamp())
    assert monthly_ttl > int(now.timestamp())

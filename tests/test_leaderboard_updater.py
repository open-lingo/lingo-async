"""Leaderboard updater — reads language + opt-in from the event, writes
two UpdateItem calls (weekly + monthly bucket), returns action records."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

from app.contracts.messages import XpAwardedMessage
from app.leaderboard import updater as lb


def test_skips_when_amount_zero(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(lb, "get_table", lambda _: fake_table)
    event = XpAwardedMessage(user_id="u-1", amount=0, source="lesson",
                             learning_language_id="ja", leaderboard_opt_in=True)
    assert lb.update_leaderboard(event) == []
    fake_table.update_item.assert_not_called()


def test_skips_when_opted_out(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(lb, "get_table", lambda _: fake_table)
    event = XpAwardedMessage(user_id="u-1", amount=10, source="lesson",
                             learning_language_id="ja", leaderboard_opt_in=False)
    assert lb.update_leaderboard(event) == []
    fake_table.update_item.assert_not_called()


def test_skips_when_language_missing(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(lb, "get_table", lambda _: fake_table)
    event = XpAwardedMessage(user_id="u-1", amount=10, source="lesson",
                             learning_language_id=None, leaderboard_opt_in=True)
    assert lb.update_leaderboard(event) == []
    fake_table.update_item.assert_not_called()


def test_writes_weekly_and_monthly_buckets(monkeypatch):
    fake_table = MagicMock()
    monkeypatch.setattr(lb, "get_table", lambda _: fake_table)
    event = XpAwardedMessage(user_id="u-1", amount=15, source="lesson",
                             learning_language_id="ja", leaderboard_opt_in=True)

    now = datetime(2026, 5, 28, tzinfo=UTC)  # Thursday of ISO 2026-W22
    monkeypatch.setattr(lb, "_now", lambda: now)

    actions = lb.update_leaderboard(event)
    assert fake_table.update_item.call_count == 2

    scopes = [a["scope"] for a in actions]
    assert "weekly" in scopes and "monthly" in scopes
    assert all(a["xp_added"] == 15 for a in actions)
    assert any(a["bucket"] == "ja#2026-W22" for a in actions)
    assert any(a["bucket"] == "ja#2026-05" for a in actions)

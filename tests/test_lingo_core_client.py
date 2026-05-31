"""LingoCoreClient — thin httpx wrapper for /quests/_internal calls."""

from unittest.mock import MagicMock, patch

from app.config import settings
from app.http.lingo_core_client import LingoCoreClient


def test_list_quests_calls_internal_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "LINGO_CORE_URL", "http://lc")
    monkeypatch.setattr(settings, "INTERNAL_SERVICE_TOKEN", "tok")

    fake = MagicMock()
    fake.get.return_value.json.return_value = {"items": []}
    fake.get.return_value.status_code = 200

    with patch("app.http.lingo_core_client.httpx.Client", return_value=fake):
        client = LingoCoreClient()
        out = client.list_quests("u-1")

    fake.get.assert_called_once_with(
        "http://lc/api/core/v1/quests/_internal/list",
        params={"user_id": "u-1"},
        headers={"Authorization": "Bearer tok"},
        timeout=5.0,
    )
    assert out == {"items": []}


def test_bump_progress_posts_delta(monkeypatch):
    monkeypatch.setattr(settings, "LINGO_CORE_URL", "http://lc")
    monkeypatch.setattr(settings, "INTERNAL_SERVICE_TOKEN", "tok")

    fake = MagicMock()
    fake.post.return_value.json.return_value = {
        "id": "q-1", "progress": {"current": 15, "target": 50, "unit": "XP"},
        "status": "active",
    }
    fake.post.return_value.status_code = 200

    with patch("app.http.lingo_core_client.httpx.Client", return_value=fake):
        client = LingoCoreClient()
        out = client.bump_progress("q-1", user_id="u-1", delta=10)

    fake.post.assert_called_once_with(
        "http://lc/api/core/v1/quests/_internal/q-1/progress",
        json={"user_id": "u-1", "delta": 10},
        headers={"Authorization": "Bearer tok"},
        timeout=5.0,
    )
    assert out["progress"]["current"] == 15

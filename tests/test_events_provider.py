
from app.config import settings
from app.db.provider import get_events_repo
from app.db.sqlite.events import SqliteEventsWriteRepository


def test_disabled_returns_none(monkeypatch):
    monkeypatch.setattr(settings, "EVENT_LOG_BACKEND", "")
    assert get_events_repo() is None


def test_sqlite_returns_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "EVENT_LOG_BACKEND", "sqlite")
    monkeypatch.setattr(settings, "EVENT_LOG_SQLITE_PATH", str(tmp_path / "ev.sqlite"))
    repo = get_events_repo()
    assert isinstance(repo, SqliteEventsWriteRepository)


def test_dynamodb_returns_real_repo(monkeypatch):
    # Was a NotImplementedError stub; now a real write-side impl. Resolution
    # builds a boto3 Table handle but makes no network call until used.
    from app.db.dynamo.events import DynamoEventsWriteRepository

    monkeypatch.setattr(settings, "EVENT_LOG_BACKEND", "dynamodb")
    repo = get_events_repo()
    assert isinstance(repo, DynamoEventsWriteRepository)
    # No method should be a NotImplementedError stub anymore.
    import inspect

    for name in ("save", "update_status"):
        src = inspect.getsource(getattr(repo, name))
        assert "raise NotImplementedError" not in src, f"{name} is still a stub"

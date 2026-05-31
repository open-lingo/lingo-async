"""Single source of `get_events_repo()`. Switch by env at process start.
Tests monkeypatch ``settings`` directly and re-invoke."""

from app.config import settings
from app.db.protocols.events import EventsWriteRepository
from app.db.sqlite.events import SqliteEventsWriteRepository


def get_events_repo() -> EventsWriteRepository | None:
    backend = (settings.EVENT_LOG_BACKEND or "").lower()
    if backend == "sqlite":
        return SqliteEventsWriteRepository(settings.EVENT_LOG_SQLITE_PATH)
    if backend == "dynamodb":
        from app.db.dynamo.events import DynamoEventsWriteRepository
        return DynamoEventsWriteRepository()
    return None

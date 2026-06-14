"""Behavior tests for DynamoEventsWriteRepository without AWS / moto.

moto isn't a dependency here, so we inject a tiny in-memory fake Table that
records put_item / query / update_item calls. This proves the repo writes
the documented key shape (PK=USER#<id>, SK=EVENT#<received_at>#<id>),
resolves update_status via the id GSI, and applies the right
UpdateExpression — i.e. it is a real impl, not a NotImplementedError stub.
"""

from datetime import UTC, datetime

import pytest

from app.db.dynamo import events as events_mod
from app.db.dynamo.events import DynamoEventsWriteRepository


class _FakeTable:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.updates: list[dict] = []
        self.queries: list[dict] = []

    def put_item(self, *, Item):  # noqa: N803 — boto3 kwarg name
        self.items.append(Item)

    def query(self, **kwargs):
        self.queries.append(kwargs)
        # Resolve by id: the KeyConditionExpression is Key("id").eq(<id>).
        cond = kwargs["KeyConditionExpression"]
        # _values is (Key, eq_value) for Key("id").eq(<id>).
        wanted = cond._values[1]
        matches = [it for it in self.items if it.get("id") == wanted]
        return {"Items": matches[: kwargs.get("Limit", len(matches))]}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)


@pytest.fixture
def fake_table(monkeypatch) -> _FakeTable:
    table = _FakeTable()
    monkeypatch.setattr(events_mod, "get_table", lambda _name: table)
    return table


def test_save_writes_documented_key_shape(fake_table):
    repo = DynamoEventsWriteRepository()
    received_at = datetime.now(UTC).isoformat()
    repo.save(
        id="evt-1",
        user_id="u-1",
        event_type="xp_awarded",
        payload_json='{"amount": 10}',
        received_at=received_at,
        ttl_epoch=123,
    )
    assert len(fake_table.items) == 1
    item = fake_table.items[0]
    assert item["PK"] == "USER#u-1"
    assert item["SK"] == f"EVENT#{received_at}#evt-1"
    assert item["id"] == "evt-1"
    assert item["status"] == "received"
    assert item["outcomes_json"] == "[]"
    assert item["ttl_epoch"] == 123


def test_update_status_resolves_via_gsi_and_updates_base_key(fake_table):
    repo = DynamoEventsWriteRepository()
    received_at = datetime.now(UTC).isoformat()
    repo.save(
        id="evt-2",
        user_id="u-2",
        event_type="lesson_completed",
        payload_json="{}",
        received_at=received_at,
        ttl_epoch=0,
    )
    repo.update_status(
        id="evt-2",
        status="ok",
        error_msg=None,
        outcomes_json='[{"kind":"leaderboard"}]',
    )
    assert len(fake_table.updates) == 1
    upd = fake_table.updates[0]
    assert upd["Key"] == {"PK": "USER#u-2", "SK": f"EVENT#{received_at}#evt-2"}
    assert upd["ExpressionAttributeValues"][":status"] == "ok"
    assert upd["ExpressionAttributeValues"][":outcomes"] == '[{"kind":"leaderboard"}]'
    assert upd["ExpressionAttributeValues"][":err"] is None


def test_update_status_noops_when_row_missing(fake_table):
    repo = DynamoEventsWriteRepository()
    # No save() first → GSI query returns nothing → no update, no raise.
    repo.update_status(id="ghost", status="failed", error_msg="x", outcomes_json="[]")
    assert fake_table.updates == []

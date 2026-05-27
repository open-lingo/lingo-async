"""Wire test: a kombu Producer publishes against the same Exchange/Queue
shape the local consumer subscribes to, using kombu's in-memory transport.

memory:// requires the producer and consumer to share a Python process,
so this test plays both roles — it's a contract check, not a runtime
test of the prod path. Proves:
  - Exchange + queue + routing key match between producer and consumer
  - JSON serialization round-trips
  - handle() synthesizes a Records-shaped event that lambda_handler accepts
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from kombu import Connection, Producer

from app.local_consumer import EVENTS_EXCHANGE, EVENTS_QUEUE, LocalConsumer


def test_local_consumer_dispatches_published_message() -> None:
    """Publish one event via kombu in-memory transport, drain one message,
    verify it reaches lambda_handler with the right body shape."""
    captured: list[dict] = []

    def fake_handler(event: dict, _context: object) -> dict:
        captured.append(event)
        return {"batchItemFailures": []}

    with Connection("memory://") as conn:
        # Wire consumer first so the queue exists when the producer publishes.
        consumer = LocalConsumer(conn)
        EVENTS_QUEUE.maybe_bind(conn)
        EVENTS_QUEUE.declare()

        producer = Producer(conn, exchange=EVENTS_EXCHANGE, routing_key="events")
        producer.publish(
            {"type": "xp_awarded", "version": 1, "user_id": "u-1", "amount": 50, "source": "lesson"},
            serializer="json",
            declare=[EVENTS_QUEUE],
        )

        # Drain one message synchronously rather than spinning the ConsumerMixin.
        with patch("app.local_consumer.lambda_handler", side_effect=fake_handler):
            with conn.Consumer(
                queues=[EVENTS_QUEUE], callbacks=[consumer.handle], accept=["json"]
            ):
                conn.drain_events(timeout=1)

    assert len(captured) == 1
    event = captured[0]
    assert "Records" in event
    assert len(event["Records"]) == 1
    record = event["Records"][0]
    assert record["eventSource"] == "lingo:local-dev"
    # Body is a JSON string holding the original message.
    import json as _json

    body = _json.loads(record["body"])
    assert body["type"] == "xp_awarded"
    assert body["user_id"] == "u-1"
    assert body["amount"] == 50


def test_local_consumer_rejects_on_handler_failure() -> None:
    """Handler raising marks the message rejected (not requeued)."""
    rejected: list[bool] = []

    class _FakeMessage:
        delivery_info = {"delivery_tag": "t-1"}

        def ack(self) -> None:
            rejected.append(False)

        def reject(self, requeue: bool = False) -> None:  # noqa: FBT001, FBT002
            rejected.append(True)
            assert requeue is False

    consumer = LocalConsumer.__new__(LocalConsumer)
    consumer._processed = 0  # type: ignore[attr-defined]

    with patch("app.local_consumer.lambda_handler", side_effect=RuntimeError("kaboom")):
        consumer.handle({"type": "xp_awarded"}, _FakeMessage())

    assert rejected == [True]


def test_local_consumer_acks_on_success() -> None:
    """Handler returning empty failures list acks the message."""
    acked: list[bool] = []

    class _FakeMessage:
        delivery_info = {"delivery_tag": "t-2"}

        def ack(self) -> None:
            acked.append(True)

        def reject(self, requeue: bool = False) -> None:  # noqa: FBT001, FBT002
            acked.append(False)

    consumer = LocalConsumer.__new__(LocalConsumer)
    consumer._processed = 0  # type: ignore[attr-defined]

    with patch(
        "app.local_consumer.lambda_handler",
        return_value={"batchItemFailures": []},
    ):
        consumer.handle({"type": "xp_awarded"}, _FakeMessage())

    assert acked == [True]


@pytest.mark.parametrize("body_kind", ["dict", "str"])
def test_local_consumer_normalizes_body(body_kind: str) -> None:
    """Bodies arrive as dict (kombu deserialized) or pre-stringified;
    handle() must always pass a JSON string in the synthetic record."""
    captured: list[str] = []

    class _FakeMessage:
        delivery_info = {"delivery_tag": "t-3"}

        def ack(self) -> None:
            pass

        def reject(self, requeue: bool = False) -> None:  # noqa: FBT001, FBT002
            pass

    consumer = LocalConsumer.__new__(LocalConsumer)
    consumer._processed = 0  # type: ignore[attr-defined]

    body: object
    if body_kind == "dict":
        body = {"type": "xp_awarded", "version": 1}
    else:
        body = '{"type": "xp_awarded", "version": 1}'

    def fake_handler(event: dict, _context: object) -> dict:
        captured.append(event["Records"][0]["body"])
        return {"batchItemFailures": []}

    with patch("app.local_consumer.lambda_handler", side_effect=fake_handler):
        consumer.handle(body, _FakeMessage())

    assert len(captured) == 1
    assert isinstance(captured[0], str)

"""Local-dev kombu consumer that drains the events broker and dispatches
through the same ``lambda_handler`` used in prod.

In production the Lambda is invoked by AWS via the SQS event source
mapping; the event is shaped ``{Records: [{messageId, body}, ...]}``.
Locally we run a long-lived process polling a Redis (or in-memory)
broker via kombu — when a message arrives we synthesize the same SQS
envelope and call ``lambda_handler`` so the dispatch + partial-batch
failure logic runs unchanged.

Run with: ``python -m app.run_local`` (uses ``EVENTS_BROKER_URL`` from
env; defaults to ``redis://localhost:6379/0``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from kombu import Connection, Exchange, Queue
from kombu.mixins import ConsumerMixin

from app.handler import lambda_handler

logger = logging.getLogger("lingo_async.local")

# Must match the producer's exchange + queue exactly (see
# ``lingo-core/app/events/publisher.py``).
EVENTS_EXCHANGE = Exchange("lingo-events", type="direct", durable=True)
EVENTS_QUEUE = Queue("lingo-events", EVENTS_EXCHANGE, routing_key="events", durable=True)


class LocalConsumer(ConsumerMixin):
    """ConsumerMixin polls the broker, hands each message to ``handle``."""

    def __init__(self, connection: Connection) -> None:
        self.connection = connection
        self._processed = 0

    def get_consumers(self, Consumer, channel):  # noqa: N803 — kombu signature
        return [
            Consumer(
                queues=[EVENTS_QUEUE],
                callbacks=[self.handle],
                prefetch_count=10,
                accept=["json"],
            )
        ]

    def handle(self, body: Any, message) -> None:
        """Wrap one kombu message as an SQS-style single-record event and
        dispatch via ``lambda_handler``. Ack on success; reject (no requeue)
        on failure so a misshapen message doesn't loop the consumer."""
        delivery_tag = str(message.delivery_info.get("delivery_tag", self._processed))
        synthetic_event = {
            "Records": [
                {
                    "messageId": f"local-{delivery_tag}",
                    "body": body if isinstance(body, str) else json.dumps(body),
                    "eventSource": "lingo:local-dev",
                }
            ]
        }
        self._processed += 1

        try:
            result = lambda_handler(synthetic_event, object())
        except Exception:
            logger.exception("local_consumer: handler raised")
            message.reject(requeue=False)
            return

        failures = result.get("batchItemFailures") or []
        if failures:
            logger.warning("local_consumer: dispatch reported failures=%s", failures)
            # Don't requeue — in local dev a stuck message is worse than a
            # lost one. Prod retries via SQS; here we surface in logs.
            message.reject(requeue=False)
            return

        message.ack()

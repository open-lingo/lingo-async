"""Entry point for the local kombu consumer.

Usage: ``python -m app.run_local`` (uses ``EVENTS_BROKER_URL`` from
env; defaults to ``redis://localhost:6379/0``).

Make sure the broker is reachable. For Redis:
    docker run -d -p 6379:6379 --name lingo-redis redis:7-alpine

Stops cleanly on SIGINT.
"""

import logging
import os
import signal
import sys

from kombu import Connection

from app.local_consumer import LocalConsumer


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("lingo_async.run_local")

    event_log_path = os.environ.get("EVENT_LOG_SQLITE_PATH", "")
    if event_log_path and os.path.exists(event_log_path):
        try:
            os.remove(event_log_path)
            log.info("event_log_truncated path=%s", event_log_path)
        except OSError as e:
            log.warning("event_log_truncate_failed path=%s err=%s", event_log_path, e)

    broker_url = os.environ.get("EVENTS_BROKER_URL", "redis://localhost:6379/0")
    log.info("starting local consumer broker=%s", broker_url)

    with Connection(broker_url) as conn:
        consumer = LocalConsumer(conn)

        def _stop(signum: int, frame: object) -> None:  # noqa: ARG001
            log.info("signal=%s shutting down", signum)
            consumer.should_stop = True

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        try:
            consumer.run()
        except KeyboardInterrupt:
            log.info("keyboard interrupt — exiting")
            sys.exit(0)


if __name__ == "__main__":
    main()

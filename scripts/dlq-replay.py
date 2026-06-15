#!/usr/bin/env python3
"""Replay messages from the lingo-events DLQ back onto the main queue.

When events exceed ``maxReceiveCount`` (3) they land in
``lingo-events-dlq``. Once you've fixed whatever caused the failure (a
core outage, a deploy ordering bug, a bad token — see H3), you replay the
parked messages so they get reprocessed.

SAFETY: dry-run is the DEFAULT. Without ``--apply`` this only PREVIEWS
what would move — it reads messages (with visibility timeout 0 so they
stay put) and prints them. Nothing is moved until you pass ``--apply``,
which additionally requires an interactive ``yes`` confirmation.

A ``--filter`` substring narrows the replay to messages whose body
contains that string (e.g. an event ``type`` or a ``user_id``).

Examples
--------
Dry-run preview of everything in the DLQ (default — moves nothing)::

    python scripts/dlq-replay.py

Dry-run, only xp_awarded messages, peek at most 50::

    python scripts/dlq-replay.py --filter xp_awarded --max 50

Actually move up to 25 friend_added messages back to the main queue::

    python scripts/dlq-replay.py --filter friend_added --max 25 --apply

Override queue names / region::

    python scripts/dlq-replay.py --dlq lingo-events-dlq --dest lingo-events --region us-west-1
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any

import boto3

DEFAULT_DLQ = "lingo-events-dlq"
DEFAULT_DEST = "lingo-events"
DEFAULT_REGION = "us-west-1"
# SQS hard caps ReceiveMessage at 10 per call; we loop until --max.
_RECEIVE_BATCH = 10


@dataclass
class ReplayResult:
    scanned: int = 0
    matched: int = 0
    moved: int = 0
    skipped_filter: int = 0
    previews: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = True


def _matches(body: str, needle: str | None) -> bool:
    return needle is None or needle in body


def replay(
    sqs,
    *,
    dlq_url: str,
    dest_url: str,
    max_messages: int,
    body_filter: str | None,
    apply: bool,
    preview_limit: int = 20,
) -> ReplayResult:
    """Core replay loop — pure w.r.t. the injected ``sqs`` client so it
    can be unit-tested with a mock.

    Dry-run (``apply=False``): receives with ``VisibilityTimeout=0`` so
    messages stay visible in the DLQ, records what WOULD move, deletes
    nothing. Apply: sends each matched message to ``dest_url`` then
    deletes it from the DLQ (send-before-delete, so a crash mid-run loses
    nothing — at worst a message is reprocessed twice, which the handlers
    already tolerate via idempotency)."""
    result = ReplayResult(dry_run=not apply)

    while result.matched < max_messages:
        remaining = max_messages - result.matched
        resp = sqs.receive_message(
            QueueUrl=dlq_url,
            MaxNumberOfMessages=min(_RECEIVE_BATCH, remaining),
            # 0 in dry-run ⇒ message reappears immediately, nothing parked.
            # In apply mode we still keep it short; we delete right after
            # the send so a small window is fine.
            VisibilityTimeout=0 if not apply else 30,
            WaitTimeSeconds=1,
            MessageAttributeNames=["All"],
        )
        messages = resp.get("Messages") or []
        if not messages:
            break  # queue drained (within our visibility window)

        for msg in messages:
            result.scanned += 1
            body = msg.get("Body", "")
            if not _matches(body, body_filter):
                result.skipped_filter += 1
                continue
            result.matched += 1
            if len(result.previews) < preview_limit:
                result.previews.append(
                    {"MessageId": msg.get("MessageId"), "Body": body}
                )

            if apply:
                sqs.send_message(QueueUrl=dest_url, MessageBody=body)
                sqs.delete_message(
                    QueueUrl=dlq_url, ReceiptHandle=msg["ReceiptHandle"]
                )
                result.moved += 1

            if result.matched >= max_messages:
                break

    return result


def _queue_url(sqs, name: str) -> str:
    return sqs.get_queue_url(QueueName=name)["QueueUrl"]


def _print_preview(result: ReplayResult, *, dest: str) -> None:
    mode = "DRY-RUN (no messages moved)" if result.dry_run else "APPLY"
    print(f"\n=== DLQ replay preview [{mode}] ===")
    print(f"scanned={result.scanned} matched={result.matched} "
          f"skipped_by_filter={result.skipped_filter}")
    if not result.previews:
        print("(no matching messages)")
        return
    print(f"\nWould move {result.matched} message(s) → {dest}. "
          f"Showing first {len(result.previews)}:")
    for i, p in enumerate(result.previews, 1):
        body = p["Body"]
        snippet = body if len(body) <= 200 else body[:200] + "…"
        print(f"  {i}. id={p['MessageId']}\n     {snippet}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay lingo-events DLQ messages.")
    parser.add_argument("--dlq", default=DEFAULT_DLQ, help="DLQ queue name")
    parser.add_argument("--dest", default=DEFAULT_DEST, help="destination (main) queue name")
    parser.add_argument("--region", default=DEFAULT_REGION, help="AWS region")
    parser.add_argument("--max", type=int, default=100, dest="max_messages",
                        help="max messages to move (default 100)")
    parser.add_argument("--filter", default=None, dest="body_filter",
                        help="only replay messages whose body contains this substring")
    parser.add_argument("--apply", action="store_true",
                        help="actually move messages (default is dry-run preview)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the interactive confirmation under --apply")
    args = parser.parse_args(argv)

    sqs = boto3.client("sqs", region_name=args.region)
    dlq_url = _queue_url(sqs, args.dlq)
    dest_url = _queue_url(sqs, args.dest)

    # Always do a dry-run pass first so the operator sees exactly what
    # would move before anything is touched.
    preview = replay(
        sqs,
        dlq_url=dlq_url,
        dest_url=dest_url,
        max_messages=args.max_messages,
        body_filter=args.body_filter,
        apply=False,
    )
    _print_preview(preview, dest=args.dest)

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to move these messages.")
        return 0

    if preview.matched == 0:
        print("\nNothing to move.")
        return 0

    if not args.yes:
        confirm = input(
            f"\nMove {preview.matched} message(s) from {args.dlq} → {args.dest}? "
            f"Type 'yes' to proceed: "
        ).strip()
        if confirm != "yes":
            print("Aborted.")
            return 1

    applied = replay(
        sqs,
        dlq_url=dlq_url,
        dest_url=dest_url,
        max_messages=args.max_messages,
        body_filter=args.body_filter,
        apply=True,
    )
    print(f"\nMoved {applied.moved} message(s) → {args.dest}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

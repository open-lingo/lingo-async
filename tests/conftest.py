"""Shared pytest fixtures for lingo-async.

Imports of ``app.db.dynamo_client`` create a boto3 resource at module
load — that's fine, the client doesn't talk to AWS until you call a
method on a ``Table`` handle. Tests that exercise leaderboard / quest
writes mock those table handles explicitly per-test rather than via a
global stub, so the failure modes stay local to each test.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def xp_event_dict() -> dict:
    """Canonical xp_awarded message body — handy starting point for tests
    that need to vary one field."""
    return {
        "type": "xp_awarded",
        "version": 1,
        "user_id": "u-test-1",
        "amount": 10,
        "source": "lesson",
    }


@pytest.fixture
def sqs_record_factory():
    """Build a single Records[*] dict the way SQS hands it to Lambda."""

    def _make(body: dict | str, message_id: str = "msg-1") -> dict:
        if isinstance(body, dict):
            import json

            body = json.dumps(body)
        return {
            "messageId": message_id,
            "body": body,
            "attributes": {},
            "messageAttributes": {},
            "md5OfBody": "",
            "eventSource": "aws:sqs",
            "eventSourceARN": "arn:aws:sqs:us-west-1:000000000000:lingo-events",
            "awsRegion": "us-west-1",
        }

    return _make

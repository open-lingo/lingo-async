"""dlq-replay.py — dry-run preview logic + apply move, with a mock SQS.

The script lives under scripts/ (not importable as a package), so we load
it by path.
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dlq-replay.py"
_spec = importlib.util.spec_from_file_location("dlq_replay", _SCRIPT)
dlq_replay = importlib.util.module_from_spec(_spec)
# Register before exec so @dataclass type resolution (which looks up the
# defining module in sys.modules) works for a path-loaded script.
sys.modules["dlq_replay"] = dlq_replay
_spec.loader.exec_module(dlq_replay)


def _msg(body: str, mid: str = "m") -> dict:
    return {"MessageId": mid, "Body": body, "ReceiptHandle": f"rh-{mid}"}


class FakeSqs:
    """Stand-in for a boto3 SQS client.

    Models SQS visibility semantics just enough for the script's two-pass
    flow (dry-run preview, then apply): a message stays available until it
    is explicitly ``delete_message``-d. Each ``replay`` call walks the
    still-present messages once (tracked via ``_cursor``), then returns
    empty so the loop terminates; the next ``replay`` starts a fresh walk.
    This mirrors VisibilityTimeout=0 (dry-run) where messages reappear."""

    def __init__(self, messages: list[dict]) -> None:
        self._all = list(messages)
        self.sent: list[dict] = []
        self.deleted: list[str] = []
        self.receive_calls: list[dict] = []
        self._cursor = 0

    def _live(self) -> list[dict]:
        return [m for m in self._all if m["ReceiptHandle"] not in self.deleted]

    def receive_message(self, **kwargs):
        self.receive_calls.append(kwargs)
        live = self._live()
        if self._cursor >= len(live):
            # Walk exhausted — reset for the next replay() pass and report
            # the queue as drained so the current loop ends.
            self._cursor = 0
            return {"Messages": []}
        n = kwargs.get("MaxNumberOfMessages", 10)
        batch = live[self._cursor:self._cursor + n]
        self._cursor += len(batch)
        return {"Messages": batch}

    def send_message(self, *, QueueUrl, MessageBody):  # noqa: N803 — boto3 kwargs
        self.sent.append({"QueueUrl": QueueUrl, "MessageBody": MessageBody})
        return {"MessageId": "new"}

    def delete_message(self, *, QueueUrl, ReceiptHandle):  # noqa: N803
        self.deleted.append(ReceiptHandle)
        return {}


def test_dry_run_moves_nothing_but_previews():
    sqs = FakeSqs([_msg('{"type":"xp_awarded"}', "m1"), _msg('{"type":"friend_added"}', "m2")])
    result = dlq_replay.replay(
        sqs, dlq_url="dlq", dest_url="dest",
        max_messages=100, body_filter=None, apply=False,
    )
    assert result.dry_run is True
    assert result.matched == 2
    assert result.moved == 0
    assert sqs.sent == []  # nothing sent to dest
    assert sqs.deleted == []  # nothing removed from DLQ
    assert len(result.previews) == 2
    # Dry-run must keep messages visible (VisibilityTimeout=0).
    assert sqs.receive_calls[0]["VisibilityTimeout"] == 0


def test_filter_skips_non_matching():
    sqs = FakeSqs([_msg('{"type":"xp_awarded"}', "m1"), _msg('{"type":"friend_added"}', "m2")])
    result = dlq_replay.replay(
        sqs, dlq_url="dlq", dest_url="dest",
        max_messages=100, body_filter="friend_added", apply=False,
    )
    assert result.matched == 1
    assert result.skipped_filter == 1
    assert result.previews[0]["MessageId"] == "m2"


def test_apply_moves_then_deletes():
    sqs = FakeSqs([_msg('{"type":"xp_awarded"}', "m1"), _msg('{"type":"friend_added"}', "m2")])
    result = dlq_replay.replay(
        sqs, dlq_url="dlq", dest_url="dest",
        max_messages=100, body_filter=None, apply=True,
    )
    assert result.dry_run is False
    assert result.moved == 2
    assert [s["MessageBody"] for s in sqs.sent] == [
        '{"type":"xp_awarded"}', '{"type":"friend_added"}',
    ]
    assert sqs.sent[0]["QueueUrl"] == "dest"
    # Send-before-delete: both must be deleted from the DLQ.
    assert sqs.deleted == ["rh-m1", "rh-m2"]


def test_max_messages_caps_matched():
    sqs = FakeSqs([_msg("{}", f"m{i}") for i in range(10)])
    result = dlq_replay.replay(
        sqs, dlq_url="dlq", dest_url="dest",
        max_messages=3, body_filter=None, apply=True,
    )
    assert result.matched == 3
    assert result.moved == 3
    assert len(sqs.sent) == 3


def test_empty_dlq_is_a_noop():
    sqs = FakeSqs([])
    result = dlq_replay.replay(
        sqs, dlq_url="dlq", dest_url="dest",
        max_messages=100, body_filter=None, apply=True,
    )
    assert result.matched == 0
    assert result.moved == 0
    assert sqs.sent == []


def test_main_dry_run_default_does_not_apply(monkeypatch, capsys):
    """Invoking with no --apply must preview and move nothing, even if the
    DLQ has matching messages."""
    sqs = FakeSqs([_msg('{"type":"xp_awarded"}', "m1")])
    sqs.get_queue_url = MagicMock(return_value={"QueueUrl": "u"})
    monkeypatch.setattr(dlq_replay.boto3, "client", lambda *a, **k: sqs)

    rc = dlq_replay.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DRY-RUN" in out
    assert sqs.sent == []
    assert sqs.deleted == []


def test_main_apply_requires_confirmation(monkeypatch, capsys):
    sqs = FakeSqs([_msg('{"type":"xp_awarded"}', "m1")])
    sqs.get_queue_url = MagicMock(return_value={"QueueUrl": "u"})
    monkeypatch.setattr(dlq_replay.boto3, "client", lambda *a, **k: sqs)
    # User declines at the prompt.
    monkeypatch.setattr("builtins.input", lambda _p: "no")

    rc = dlq_replay.main(["--apply"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "Aborted" in out
    assert sqs.sent == []  # declined → nothing moved


def test_main_apply_yes_flag_skips_prompt_and_moves(monkeypatch, capsys):
    sqs = FakeSqs([_msg('{"type":"xp_awarded"}', "m1")])
    sqs.get_queue_url = MagicMock(return_value={"QueueUrl": "u"})
    monkeypatch.setattr(dlq_replay.boto3, "client", lambda *a, **k: sqs)

    def _no_input(_p):
        raise AssertionError("should not prompt when --yes is passed")

    monkeypatch.setattr("builtins.input", _no_input)

    rc = dlq_replay.main(["--apply", "--yes"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Moved 1" in out
    assert len(sqs.sent) == 1

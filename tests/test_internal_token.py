"""Resolver for INTERNAL_SERVICE_TOKEN: env-wins, SSM fallback, graceful."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.config import settings
from app.http import internal_token


def _reset(monkeypatch, env_value: str) -> None:
    monkeypatch.setattr(settings, "INTERNAL_SERVICE_TOKEN", env_value)
    internal_token.reset_cache()


def test_env_set_wins_and_never_calls_ssm(monkeypatch) -> None:
    _reset(monkeypatch, "env-tok")

    with patch("boto3.client") as boto_client:
        out = internal_token.resolve_internal_service_token()

    assert out == "env-tok"
    boto_client.assert_not_called()


def test_env_empty_falls_back_to_ssm(monkeypatch) -> None:
    _reset(monkeypatch, "")

    ssm = MagicMock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "ssm-tok"}}

    with patch("boto3.client", return_value=ssm) as boto_client:
        out = internal_token.resolve_internal_service_token()
        # Second call must be cached — SSM hit exactly once per container.
        again = internal_token.resolve_internal_service_token()

    assert out == "ssm-tok"
    assert again == "ssm-tok"
    boto_client.assert_called_once_with("ssm", region_name=settings.AWS_REGION)
    ssm.get_parameter.assert_called_once_with(
        Name="/lingo/internal-service-token", WithDecryption=True
    )


def test_ssm_failure_degrades_to_empty(monkeypatch) -> None:
    _reset(monkeypatch, "")

    ssm = MagicMock()
    ssm.get_parameter.side_effect = RuntimeError("no perms")

    with patch("boto3.client", return_value=ssm):
        out = internal_token.resolve_internal_service_token()

    # Graceful: empty string, no raise. Callbacks would 401 but the worker
    # still constructs and runs.
    assert out == ""


def test_handler_boot_check_does_not_crash_on_ssm_failure(monkeypatch) -> None:
    """The cold-start self-check must never raise, even when both env and
    SSM yield nothing — it only logs."""
    from app import handler as handler_module

    _reset(monkeypatch, "")
    ssm = MagicMock()
    ssm.get_parameter.side_effect = RuntimeError("no perms")

    with patch("boto3.client", return_value=ssm):
        # No exception == pass.
        handler_module._verify_internal_token_on_boot()

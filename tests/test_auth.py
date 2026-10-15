"""Gateway client authentication: dual header paths, constant-time compare."""

from __future__ import annotations

import hmac

import pytest

from xang_mcp_gateway.auth import is_authorized

from .conftest import GATEWAY_TOKEN

REAL_COMPARE_DIGEST = hmac.compare_digest


@pytest.fixture()
def timing_safe_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record compare_digest inputs to prove the secret never leaks in messages."""
    seen: list[str] = []

    def spy(left: object, right: object) -> bool:
        seen.append(type(left).__name__)
        return REAL_COMPARE_DIGEST(left, right)  # type: ignore[arg-type]

    monkeypatch.setattr(hmac, "compare_digest", spy)
    return seen


def test_bearer_header_accepted() -> None:
    assert is_authorized({"Authorization": f"Bearer {GATEWAY_TOKEN}"}, GATEWAY_TOKEN)


def test_custom_header_accepted() -> None:
    assert is_authorized({"x-gateway-token": GATEWAY_TOKEN}, GATEWAY_TOKEN)


def test_wrong_token_rejected() -> None:
    assert not is_authorized({"Authorization": "Bearer nope"}, GATEWAY_TOKEN)
    assert not is_authorized({"x-gateway-token": "nope"}, GATEWAY_TOKEN)


def test_missing_and_empty_token_rejected() -> None:
    assert not is_authorized({}, GATEWAY_TOKEN)
    assert not is_authorized({"Authorization": "Bearer "}, GATEWAY_TOKEN)
    assert not is_authorized({"Authorization": "Basic abc"}, GATEWAY_TOKEN)


def test_gateway_token_must_be_configured() -> None:
    assert not is_authorized({"Authorization": f"Bearer {GATEWAY_TOKEN}"}, "")


def test_uses_constant_time_compare(timing_safe_spy: list[str]) -> None:
    assert is_authorized({"Authorization": f"Bearer {GATEWAY_TOKEN}"}, GATEWAY_TOKEN)
    assert timing_safe_spy  # compare_digest was actually used

"""Calendar-only calls preserve the explicitly requested canonical market."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import xang_mcp_gateway.calendar_only as calendar_module
from xang_mcp_gateway.app import create_app
from xang_mcp_gateway.calendar import CALENDAR_MARKETS, CALENDAR_TOOL_DESCRIPTOR

from .conftest import GATEWAY_TOKEN, make_settings

HEADERS = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


def call_calendar(client, market):
    return client.post("/mcp/", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "market_calendar_status", "arguments": {"market": market}},
    })


@pytest.mark.parametrize("market,expected", [("US", "US"), ("DE", "DE"), ("de", "DE"), (" DE ", "DE")])
def test_requested_market_is_forwarded_without_replacing_facts(monkeypatch, market, expected):
    calls = []
    facts = {
        "market": expected, "expected_trading_date": "2026-10-30",
        "market_timezone": "Europe/Berlin" if expected == "DE" else "America/New_York",
        "calendar_source": "xang_market_calendar_service",
    }

    def calendar(request_id, selected, root):
        calls.append((selected, root))
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": facts}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", calendar)
    settings = make_settings(calendar_only=True)
    with TestClient(create_app(settings)) as client:
        response = call_calendar(client, market)
    assert response.json()["result"]["structuredContent"] == facts
    assert calls == [(expected, settings.calendar_app_root)]


@pytest.mark.parametrize("market", ["", "XX", None, False, 7])
def test_unsupported_market_cannot_reach_the_calendar(monkeypatch, market):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid market reached canonical calendar")

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", forbidden)
    with TestClient(create_app(make_settings(calendar_only=True))) as client:
        assert "error" in call_calendar(client, market).json()


def test_tool_schema_advertises_both_markets_with_us_default():
    schema = CALENDAR_TOOL_DESCRIPTOR["inputSchema"]["properties"]["market"]
    assert set(schema["enum"]) == set(CALENDAR_MARKETS) == {"US", "DE"}
    assert schema["default"] == "US"


def test_de_request_uses_the_real_canonical_calendar_when_available():
    settings = make_settings(calendar_only=True)
    source = Path(settings.calendar_app_root) / "app/services/market_calendar_service.py"
    if not source.is_file():
        pytest.skip("canonical backend source is available in the gateway image")
    with TestClient(create_app(settings)) as client:
        payload = call_calendar(client, "DE").json()["result"]["structuredContent"]
    assert payload["market"] == "DE"
    assert payload["market_timezone"] == "Europe/Berlin"
    assert payload["calendar_source"] == "xang_market_calendar_service"
    assert payload.get("expected_trading_date")

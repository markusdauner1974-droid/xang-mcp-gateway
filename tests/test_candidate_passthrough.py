"""Opt-in upstream passthrough keeps local tools and authentication independent."""

from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

import xang_mcp_gateway.app as app_module
from xang_mcp_gateway import GatewayConfigError
from xang_mcp_gateway.config import GatewaySettings

from .conftest import (
    BACKEND_TOKEN,
    ENRICHED_FACTS_NVDA,
    GATEWAY_TOKEN,
    FakeFactsProvider,
    FakeTransport,
    candidate_envelope,
    candidates_result_payload,
    make_settings,
)

DSN = "postgresql://offline-test@db/stockscanner"
HEADERS = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
CALL = {
    "jsonrpc": "2.0", "id": 30, "method": "tools/call",
    "params": {"name": "find_candidates", "arguments": {"limit": 3}},
}


@pytest.mark.parametrize("raw, expected", [("true", True), ("false", False), (" FALSE ", False)])
def test_explicit_env_switch_preserves_earnings_dsn(raw: str, expected: bool) -> None:
    settings = GatewaySettings.from_env({
        "XANG_BACKEND_TOKEN": BACKEND_TOKEN, "GATEWAY_TOKEN": GATEWAY_TOKEN,
        "XANG_DB_DSN": DSN, "GATEWAY_CANDIDATE_ENRICHMENT": raw,
    })
    assert settings.enrichment_enabled is expected
    assert settings.db_dsn == DSN


def test_existing_deployments_keep_enrichment_by_default() -> None:
    settings = GatewaySettings.from_env({
        "XANG_BACKEND_TOKEN": BACKEND_TOKEN, "GATEWAY_TOKEN": GATEWAY_TOKEN, "XANG_DB_DSN": DSN,
    })
    assert settings.enrichment_enabled is True


@pytest.mark.parametrize("raw", ["", "0", "1", "disabled", "typo"])
def test_invalid_env_switch_fails_fast(raw: str) -> None:
    with pytest.raises(GatewayConfigError, match="GATEWAY_CANDIDATE_ENRICHMENT"):
        GatewaySettings.from_env({
            "XANG_BACKEND_TOKEN": BACKEND_TOKEN, "GATEWAY_TOKEN": GATEWAY_TOKEN,
            "GATEWAY_CANDIDATE_ENRICHMENT": raw,
        })


@pytest.mark.parametrize("value", [None, 0, "false"])
def test_nonboolean_constructor_switch_is_rejected(value: object) -> None:
    with pytest.raises(GatewayConfigError, match="candidate_enrichment"):
        make_settings(candidate_enrichment=value)


def test_passthrough_preserves_unknowns_ratings_and_both_formats(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_sql(*args: object, **kwargs: object) -> None:
        raise AssertionError("candidate SQL must not be constructed")

    monkeypatch.setattr(app_module, "SqlFeatureStore", unexpected_sql)
    envelope = candidate_envelope(["NVDA"])
    envelope["symbols"][0].update(ENRICHED_FACTS_NVDA)
    envelope["symbols"][0].update(minervini_rating="Pass", vcp_num_bases=None, volume_surge=None)
    payload = candidates_result_payload(envelope)
    expected = copy.deepcopy(payload)
    provider = FakeFactsProvider(facts={"NVDA": ENRICHED_FACTS_NVDA}, ping_ok=False)
    transport = FakeTransport(responses=[
        (200, payload),
        (200, {"jsonrpc": "2.0", "id": "readyz", "result": {"tools": []}}),
    ])
    app = app_module.create_app(
        make_settings(db_dsn=DSN, candidate_enrichment=False),
        transport=transport, facts_provider=provider,
    )
    with TestClient(app) as client:
        response = client.post("/mcp/", headers=HEADERS, json=CALL)
        assert response.status_code == 200
        assert response.json() == expected
        assert client.get("/healthz").json()["enrichment"] == "disabled"
        readiness = client.get("/readyz")
        assert readiness.status_code == 200
        assert "feature_store" not in readiness.json()["checks"]
    assert provider.requested == []


def test_upstream_stored_data_error_is_not_replaced_or_enriched() -> None:
    payload = {
        "jsonrpc": "2.0", "id": 30,
        "result": {"isError": True, "structuredContent": {
            "error": {"code": "invalid_stored_data", "message": "offline fixture"},
        }},
    }
    app = app_module.create_app(
        make_settings(db_dsn=DSN, candidate_enrichment=False),
        transport=FakeTransport(responses=[(200, copy.deepcopy(payload))]),
    )
    with TestClient(app) as client:
        response = client.post("/mcp/", headers=HEADERS, json=CALL)
    assert response.json() == payload


def test_earnings_and_calendar_keep_inputs_when_candidate_sql_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[object, ...]] = []

    def earnings(request_id: object, symbol: str, dsn: str, *, backend_app_root: str) -> dict:
        calls.append(("earnings", symbol, dsn, backend_app_root))
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {"available": False}}}

    def calendar(request_id: object, market: str, backend_app_root: str) -> dict:
        calls.append(("calendar", market, backend_app_root))
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {"calendar_status": "unknown"}}}

    monkeypatch.setattr(app_module, "earnings_jsonrpc_result", earnings)
    monkeypatch.setattr(app_module, "calendar_jsonrpc_result", calendar)
    settings = make_settings(db_dsn=DSN, candidate_enrichment=False)
    app = app_module.create_app(settings, transport=FakeTransport())
    with TestClient(app) as client:
        for name, arguments in (
            ("earnings_lookup", {"symbol": "NVDA"}),
            ("market_calendar_status", {"market": "US"}),
        ):
            response = client.post("/mcp/", headers=HEADERS, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
            assert response.status_code == 200
            assert "error" not in response.json()
    assert calls == [
        ("earnings", "NVDA", DSN, settings.calendar_app_root),
        ("calendar", "US", settings.calendar_app_root),
    ]


def test_auth_and_write_exclusion_remain_active_in_passthrough_mode() -> None:
    transport = FakeTransport(responses=[(200, {
        "jsonrpc": "2.0", "id": 1, "result": {"tools": [
            {"name": "find_candidates"}, {"name": "watchlist_add"},
        ]},
    })])
    app = app_module.create_app(make_settings(candidate_enrichment=False), transport=transport)
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with TestClient(app) as client:
        assert client.post("/mcp/", json=body).status_code == 401
        result = client.post("/mcp/", headers=HEADERS, json=body).json()
        names = {tool["name"] for tool in result["result"]["tools"]}
        assert names == {"find_candidates", "market_calendar_status", "earnings_lookup"}
        blocked = client.post("/mcp/", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "watchlist_add", "arguments": {}},
        })
        assert "error" in blocked.json()
    assert len(transport.calls) == 1

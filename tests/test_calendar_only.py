"""Calendar-only mode must work without backend credentials, forwarding or SQL."""

from __future__ import annotations

import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import xang_mcp_gateway.app as app_module
import xang_mcp_gateway.calendar_only as calendar_module
from xang_mcp_gateway import GatewayConfigError, GatewaySettings

from .conftest import GATEWAY_TOKEN, FakeFactsProvider, FakeTransport, make_settings

HEADERS = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


def request(client, method, params=None):
    return client.post("/mcp/", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    })


@pytest.mark.parametrize("raw,expected", [("true", True), ("false", False), (" TRUE ", True)])
def test_env_flag(raw, expected):
    settings = GatewaySettings.from_env({
        "GATEWAY_TOKEN": GATEWAY_TOKEN, "XANG_BACKEND_TOKEN": "other-test-token",
        "GATEWAY_CALENDAR_ONLY": raw,
    })
    assert settings.calendar_only is expected


@pytest.mark.parametrize("raw", ["", "0", "1", "typo"])
def test_invalid_flag_fails_startup(raw):
    with pytest.raises(GatewayConfigError, match="GATEWAY_CALENDAR_ONLY"):
        GatewaySettings.from_env({"GATEWAY_TOKEN": GATEWAY_TOKEN, "GATEWAY_CALENDAR_ONLY": raw})


@pytest.mark.parametrize("value", [None, 0, "false"])
def test_constructor_requires_boolean(value):
    with pytest.raises(GatewayConfigError, match="calendar_only"):
        make_settings(calendar_only=value)


def test_calendar_needs_only_its_own_token():
    settings = GatewaySettings.from_env({"GATEWAY_TOKEN": GATEWAY_TOKEN, "GATEWAY_CALENDAR_ONLY": "true"})
    assert settings.backend_token == ""
    assert settings.db_dsn is None
    assert settings.allowed_tools == {"market_calendar_status"}
    assert settings.backend_visible_tools == set()
    assert not settings.enrichment_enabled
    with pytest.raises(GatewayConfigError, match="gateway_token"):
        GatewaySettings.from_env({"GATEWAY_CALENDAR_ONLY": "true"})


def test_no_backend_or_sql_construction(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("calendar mode must not construct network/SQL providers")

    monkeypatch.setattr("xang_mcp_gateway.rpc.HttpxTransport", forbidden)
    monkeypatch.setattr(app_module, "SqlFeatureStore", forbidden)
    settings = make_settings(calendar_only=True, db_dsn="postgresql://unused-test")
    with TestClient(app_module.create_app(settings)) as client:
        assert client.get("/healthz").json()["tools_visible"] == 1
        assert request(client, "tools/list").json()["result"]["tools"][0]["name"] == "market_calendar_status"


def test_protocol_is_local_and_authenticated():
    transport = FakeTransport()
    provider = FakeFactsProvider()
    app = app_module.create_app(make_settings(calendar_only=True), transport=transport, facts_provider=provider)
    with TestClient(app) as client:
        assert client.post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
        init = request(client, "initialize", {"protocolVersion": "2025-06-18"}).json()["result"]
        assert init["protocolVersion"] == "2025-06-18"
        assert init["capabilities"] == {"tools": {}}
        assert client.post("/mcp/", headers=HEADERS, json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }).status_code == 202
        assert request(client, "ping").json()["result"] == {}
        tools = request(client, "tools/list").json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["market_calendar_status"]
        for name in ("find_candidates", "earnings_lookup", "watchlist_add", "unknown"):
            assert "error" in request(client, "tools/call", {"name": name}).json()
    assert transport.calls == []
    assert provider.requested == []


def test_canonical_payload_and_readiness_are_local(monkeypatch):
    calls = []
    facts = {"expected_trading_date": "2026-10-29", "calendar_source": "xang_market_calendar_service"}

    def calendar(request_id, market, root):
        calls.append((market, root))
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": facts}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", calendar)
    transport = FakeTransport()
    settings = make_settings(calendar_only=True)
    with TestClient(app_module.create_app(settings, transport=transport)) as client:
        response = request(client, "tools/call", {"name": "market_calendar_status", "arguments": {"market": "US"}})
        assert response.json()["result"]["structuredContent"] == facts
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["checks"] == {"market_calendar": {"ok": True}}
    assert calls == [("US", settings.calendar_app_root)] * 2
    assert transport.calls == []


def test_readyz_is_cached_between_probes(monkeypatch):
    """The unauthenticated probe must not run the calendar on every call."""
    calls = []

    def calendar(request_id, market, root):
        calls.append(market)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {
            "expected_trading_date": "2026-10-29"}}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", calendar)
    with TestClient(app_module.create_app(make_settings(calendar_only=True))) as client:
        for _ in range(5):
            assert client.get("/readyz").status_code == 200
    assert calls == ["US"]


def test_readyz_probes_again_after_the_ttl(monkeypatch):
    calls = []

    def calendar(request_id, market, root):
        calls.append(market)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {
            "expected_trading_date": "2026-10-29"}}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", calendar)
    monkeypatch.setattr(calendar_module, "PROBE_TTL_SECONDS", 0.0)
    with TestClient(app_module.create_app(make_settings(calendar_only=True))) as client:
        assert client.get("/readyz").status_code == 200
        assert client.get("/readyz").status_code == 200
    assert calls == ["US", "US"]


def test_a_degraded_verdict_is_cached_too(monkeypatch):
    calls = []

    def unavailable(request_id, market, root):
        calls.append(market)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {
            "calendar_status": "error", "expected_trading_date": None}}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", unavailable)
    with TestClient(app_module.create_app(make_settings(calendar_only=True))) as client:
        for _ in range(3):
            assert client.get("/readyz").status_code == 503
    assert calls == ["US"]


def test_a_hung_calendar_times_out_and_is_cached(monkeypatch):
    """A stalled evaluation must not hold the lock until it finishes."""
    calls = []

    def hung(request_id, market, root):
        calls.append(market)
        time.sleep(1.0)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {
            "expected_trading_date": "2026-10-29"}}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", hung)
    monkeypatch.setattr(calendar_module, "PROBE_TIMEOUT_SECONDS", 0.05)
    with TestClient(app_module.create_app(make_settings(calendar_only=True))) as client:
        assert client.get("/readyz").status_code == 503
        # Cached: the second probe answers without starting another evaluation.
        assert client.get("/readyz").status_code == 503
    assert calls == ["US"]


def test_a_timeout_replaces_the_stuck_worker(monkeypatch):
    """A hung evaluation must not poison readiness for the rest of the process."""
    calls = []
    threads = []
    script = {"hang": True}

    def calendar(request_id, market, root):
        calls.append(market)
        threads.append(threading.get_ident())
        if script["hang"]:
            time.sleep(0.3)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {
            "expected_trading_date": "2026-10-29"}}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", calendar)
    monkeypatch.setattr(calendar_module, "PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(calendar_module, "PROBE_TIMEOUT_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(calendar_module, "PROBE_TTL_SECONDS", 0.0)
    with TestClient(app_module.create_app(make_settings(calendar_only=True))) as client:
        assert client.get("/readyz").status_code == 503
        script["hang"] = False
        assert client.get("/readyz").status_code == 200
    # The second probe ran on a fresh worker, not on the abandoned one.
    # Thread names repeat across pools, so compare the thread identities.
    assert len(calls) == 2
    assert threads[0] != threads[1]


def test_a_timeout_backs_off_before_probing_again(monkeypatch):
    calls = []

    def hung(request_id, market, root):
        calls.append(market)
        time.sleep(0.3)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {
            "expected_trading_date": "2026-10-29"}}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", hung)
    monkeypatch.setattr(calendar_module, "PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(calendar_module, "PROBE_TTL_SECONDS", 0.0)
    with TestClient(app_module.create_app(make_settings(calendar_only=True))) as client:
        assert client.get("/readyz").status_code == 503
        # The default backoff still covers this probe: no new evaluation.
        assert client.get("/readyz").status_code == 503
    assert calls == ["US"]


def test_the_probe_runs_on_its_own_thread(monkeypatch):
    """The probe must not borrow the default executor that /mcp/ uses."""
    threads = []

    def record_thread(request_id, market, root):
        threads.append(threading.current_thread().name)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {
            "expected_trading_date": "2026-10-29"}}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", record_thread)
    with TestClient(app_module.create_app(make_settings(calendar_only=True))) as client:
        assert client.get("/readyz").status_code == 200
    assert threads and threads[0].startswith("calendar-probe")


def test_concurrent_readyz_probes_run_the_calendar_once(monkeypatch):
    """Single-flight: a burst of probes shares one calendar evaluation."""
    calls = []

    def slow_calendar(request_id, market, root):
        calls.append(market)
        time.sleep(0.05)
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {
            "expected_trading_date": "2026-10-29"}}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", slow_calendar)
    app = app_module.create_app(make_settings(calendar_only=True))

    async def burst() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            responses = await asyncio.gather(*(client.get("/readyz") for _ in range(6)))
        return [response.status_code for response in responses]

    assert asyncio.run(burst()) == [200] * 6
    assert calls == ["US"]


def test_canonical_failure_stays_unavailable(monkeypatch):
    def unavailable(request_id, market, root):
        return {"jsonrpc": "2.0", "id": request_id, "result": {"structuredContent": {
            "calendar_status": "error", "expected_trading_date": None,
        }}}

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", unavailable)
    with TestClient(app_module.create_app(make_settings(calendar_only=True))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/readyz").status_code == 503
        facts = request(client, "tools/call", {"name": "market_calendar_status"}).json()["result"]["structuredContent"]
        assert facts["expected_trading_date"] is None
        assert facts["calendar_status"] == "error"


@pytest.mark.parametrize("args", [[], None, {"market": "DE"}, {"market": ""}, {"market": 1}, {"unexpected": True}])
def test_invalid_arguments_cannot_call_calendar(monkeypatch, args):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid input reached calendar")

    monkeypatch.setattr(calendar_module, "calendar_jsonrpc_result", forbidden)
    with TestClient(app_module.create_app(make_settings(calendar_only=True))) as client:
        response = request(client, "tools/call", {"name": "market_calendar_status", "arguments": args})
        assert "error" in response.json()


def test_invalid_rpc_and_methods_are_rejected():
    with TestClient(app_module.create_app(make_settings(calendar_only=True))) as client:
        assert client.post("/mcp/", headers=HEADERS, content="{").json()["error"]["code"] == -32700
        for body in ([], {"method": "tools/list"}, {"jsonrpc": "2.0", "method": []}):
            assert client.post("/mcp/", headers=HEADERS, json=body).json()["error"]["code"] == -32600
        assert request(client, "unknown").json()["error"]["code"] == -32601
        assert client.post("/mcp/", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": [],
        }).json()["error"]["code"] == -32602

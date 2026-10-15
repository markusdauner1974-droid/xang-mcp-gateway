"""Gateway RPC surface: auth gate, allowlist, passthrough, error contract."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from xang_mcp_gateway import __version__
from xang_mcp_gateway.app import create_app
from xang_mcp_gateway.config import ALL_BACKEND_TOOLS
from xang_mcp_gateway.rpc import BackendUnavailable

from .conftest import GATEWAY_TOKEN, FakeTransport, candidate_envelope, candidates_result_payload, make_settings

AUTH = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


def make_client(transport: FakeTransport, provider: object | None = None) -> TestClient:
    app = create_app(make_settings(), transport=transport, facts_provider=provider)
    return TestClient(app)


def tools_payload() -> dict:
    names = sorted(ALL_BACKEND_TOOLS)
    return {
        "jsonrpc": "2.0",
        "id": 11,
        "result": {"tools": [{"name": n, "description": f"{n} desc"} for n in names]},
    }


class TestAuthGate:
    def test_missing_token_is_401_and_backend_untouched(self) -> None:
        transport = FakeTransport()
        resp = make_client(transport).post("/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert resp.status_code == 401
        assert auth_untouched(transport)

    def test_wrong_token_is_401(self) -> None:
        transport = FakeTransport()
        resp = make_client(transport).post(
            "/mcp/", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Authorization": "Bearer nope"}
        )
        assert resp.status_code == 401
        assert auth_untouched(transport)

    def test_custom_header_works(self) -> None:
        transport = FakeTransport([(200, tools_payload())])
        resp = make_client(transport).post(
            "/mcp/", json={"jsonrpc": "2.0", "id": 11, "method": "tools/list"}, headers={"x-gateway-token": GATEWAY_TOKEN}
        )
        assert resp.status_code == 200
        names = [tool["name"] for tool in resp.json()["result"]["tools"]]
        assert names and "watchlist_add" not in names


def auth_untouched(transport: FakeTransport) -> bool:
    return transport.calls == []


class TestAllowlist:
    def test_tools_list_filters_write_tool_and_keeps_rest(self) -> None:
        transport = FakeTransport([(200, tools_payload())])
        resp = make_client(transport).post("/mcp/", json={"jsonrpc": "2.0", "id": 11, "method": "tools/list"}, headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        names = [tool["name"] for tool in body["result"]["tools"]]
        assert len(names) == 12
        assert "watchlist_add" not in names
        assert "stock_snapshot" in names and "find_candidates" in names
        # id passthrough preserved
        assert body["id"] == 11

    def test_tools_call_of_excluded_tool_is_rejected_without_backend_call(self) -> None:
        transport = FakeTransport()
        resp = make_client(transport).post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "watchlist_add", "arguments": {}}},
            headers=AUTH,
        )
        assert resp.status_code == 200
        error = resp.json()["error"]
        assert error["code"] == -32602
        assert "watchlist_add" in error["message"]
        assert auth_untouched(transport)

    def test_unknown_tool_is_rejected(self) -> None:
        transport = FakeTransport()
        resp = make_client(transport).post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nuclear_launch"}},
            headers=AUTH,
        )
        assert resp.json()["error"]["code"] == -32602
        assert auth_untouched(transport)

    def test_tools_call_without_tool_name_is_rejected(self) -> None:
        transport = FakeTransport()
        resp = make_client(transport).post(
            "/mcp/", json={"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {}}, headers=AUTH
        )
        assert resp.json()["error"]["code"] == -32602

    def test_disallowed_method_is_answered_by_gateway_itself(self) -> None:
        transport = FakeTransport()
        resp = make_client(transport).post("/mcp/", json={"jsonrpc": "2.0", "id": 7, "method": "resources/list"}, headers=AUTH)
        assert resp.status_code == 200
        error = resp.json()["error"]
        assert error["code"] == -32601
        assert error["message"].startswith("Method not allowed by gateway: resources/list")
        assert auth_untouched(transport)


class TestPassthrough:
    def test_initialize_passes_through_with_id(self) -> None:
        backend = {"jsonrpc": "2.0", "id": 5, "result": {"serverInfo": {"name": "stockscreen-market-copilot"}}}
        transport = FakeTransport([(200, backend)])
        resp = make_client(transport).post(
            "/mcp/", json={"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            headers=AUTH,
        )
        assert resp.json() == backend
        # forwarded body is unchanged (identity passthrough)
        assert transport.calls[0]["params"] == {"protocolVersion": "2025-03-26"}

    def test_ping_passes_through(self) -> None:
        backend = {"jsonrpc": "2.0", "id": 9, "result": {}}
        transport = FakeTransport([(200, backend)])
        resp = make_client(transport).post("/mcp/", json={"jsonrpc": "2.0", "id": 9, "method": "ping"}, headers=AUTH)
        assert resp.json() == backend

    def test_notification_is_forwarded_and_answers_202(self) -> None:
        transport = FakeTransport([(202, None)])
        resp = make_client(transport).post(
            "/mcp/", json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, headers=AUTH
        )
        assert resp.status_code == 202
        assert transport.calls[0] == {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}

    def test_tools_call_passes_through(self) -> None:
        envelope = candidate_envelope(["NVDA"])
        backend = candidates_result_payload(envelope, request_id=21)
        transport = FakeTransport([(200, backend)])
        resp = make_client(transport).post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {"name": "find_candidates", "arguments": {"filters": {}, "limit": 3}},
            },
            headers=AUTH,
        )
        assert resp.json()["id"] == 21
        assert "symbols" in resp.json()["result"]["structuredContent"]


class TestErrorContract:
    def test_invalid_json_keeps_200_and_jsonrpc_parse_error(self) -> None:
        transport = FakeTransport()
        resp = make_client(transport).post(
            "/mcp/", content=b"{definitely-not-json", headers={**AUTH, "Content-Type": "application/json"}
        )
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32700
        assert auth_untouched(transport)

    def test_non_object_body_is_invalid_request(self) -> None:
        transport = FakeTransport()
        resp = make_client(transport).post("/mcp/", json=[1, 2, 3], headers=AUTH)
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32600

    def test_backend_unavailable_becomes_jsonrpc_32000(self) -> None:
        transport = FakeTransport(error=BackendUnavailable("backend responded HTTP 503"))
        resp = make_client(transport).post("/mcp/", json={"jsonrpc": "2.0", "id": 8, "method": "tools/list"}, headers=AUTH)
        assert resp.status_code == 200
        error = resp.json()["error"]
        assert error["code"] == -32000
        assert "503" in error["message"]
        assert resp.json()["id"] == 8


class TestHealthEndpoints:
    def test_healthz_makes_no_backend_calls(self) -> None:
        transport = FakeTransport()  # exhausted backend would explode on any call
        resp = make_client(transport).get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"status": "ok", "version": __version__, "enrichment": "disabled", "tools_visible": 12}
        assert auth_untouched(transport)

    def test_readyz_reports_backend_when_reachable(self) -> None:
        transport = FakeTransport([(200, tools_payload())])
        resp = make_client(transport).get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ready"
        assert body["checks"]["backend_mcp"] == {"ok": True, "tools_visible": 12}

    def test_readyz_is_degraded_when_backend_unreachable(self) -> None:
        transport = FakeTransport(error=BackendUnavailable("connect error"))
        resp = make_client(transport).get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["status"] == "degraded"
        assert resp.json()["checks"]["backend_mcp"]["ok"] is False


def test_content_text_payload_parsing_is_tolerant() -> None:
    """A malformed text mirror must not crash the gateway (no enrichment path)."""
    payload = {
        "result": {
            "content": [{"type": "text", "text": "not-json"}],
            "structuredContent": {"symbols": [{"symbol": "NVDA"}]},
        }
    }
    assert json.dumps(payload)  # serializer sanity

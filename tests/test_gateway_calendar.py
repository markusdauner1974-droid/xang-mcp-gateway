"""Phase 7 tests: market_calendar_status — local canonical calendar tool.

All weekday/holiday logic stays OUT of these tests: the fake service object is
just a scripted stand-in of the CANONICAL MarketCalendarService (dates are
injected by the test), exactly like the FakeTransport pattern in conftest.py.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from xang_mcp_gateway import app as app_module
from xang_mcp_gateway import calendar as calendar_module
from xang_mcp_gateway.calendar import CALENDAR_SOURCE, CALENDAR_TOOL_NAME, DEFAULT_CALENDAR_APP_ROOT
from xang_mcp_gateway.config import ALL_BACKEND_TOOLS, DEFAULT_EXCLUDED_TOOLS, LOCAL_TOOLS

from .conftest import GATEWAY_TOKEN, FakeTransport, make_settings

AUTH = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


def make_client(transport: FakeTransport, provider: object | None = None) -> TestClient:
    app = app_module.create_app(make_settings(), transport=transport, facts_provider=provider)
    return TestClient(app)


def backend_tools_payload(request_id: int = 11) -> dict[str, Any]:
    names = sorted(ALL_BACKEND_TOOLS)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"tools": [{"name": name, "description": f"{name} desc"} for name in names]},
    }


def call_calendar(client: TestClient, arguments: dict[str, Any] | None = None, request_id: int = 42) -> Any:
    params: dict[str, Any] = {"name": CALENDAR_TOOL_NAME}
    if arguments is not None:
        params["arguments"] = arguments
    return client.post("/mcp/", headers=AUTH, json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params})


class FakeCanonicalService:
    """Scripted stand-in of the CANONICAL MarketCalendarService — no calendar
    logic of its own; every value is injected by the test."""

    def __init__(
        self,
        market_now_dt: datetime,
        is_trading: bool,
        last_completed: date,
        tz: str = "America/New_York",
    ) -> None:
        self._now = market_now_dt
        self._is_trading = is_trading
        self._last_completed = last_completed
        self._tz = tz

    def market_now(self, market: str) -> datetime:
        return self._now

    def is_trading_day(self, market: str, day: date) -> bool:
        return self._is_trading

    def last_completed_trading_day(self, market: str) -> date:
        return self._last_completed

    def market_timezone(self, market: str) -> ZoneInfo:
        return ZoneInfo(self._tz)


def _patch_canonical(monkeypatch: pytest.MonkeyPatch, service: object) -> None:
    def _loader(backend_app_root: str = DEFAULT_CALENDAR_APP_ROOT) -> Any:
        del backend_app_root
        # The loader must return the SERVICE CLASS (callable), mirroring the
        # production contract; the instance is created inside calendar_facts.
        return lambda: service

    monkeypatch.setattr(calendar_module, "_load_market_calendar_service", _loader)


class TestLocalSurface:
    def test_allowlist_includes_local_tool(self) -> None:
        settings = make_settings()
        assert CALENDAR_TOOL_NAME in settings.allowed_tools
        assert LOCAL_TOOLS <= settings.allowed_tools
        # backend surface unchanged (13 minus watchlist_add)
        assert settings.backend_visible_tools == ALL_BACKEND_TOOLS - DEFAULT_EXCLUDED_TOOLS

    def test_healthz_reports_the_visible_tool_count(self) -> None:
        transport = FakeTransport()
        resp = make_client(transport).get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["tools_visible"] == len(ALL_BACKEND_TOOLS - DEFAULT_EXCLUDED_TOOLS) + len(LOCAL_TOOLS)

    def test_tools_list_advertises_calendar_tool(self) -> None:
        transport = FakeTransport([(200, backend_tools_payload())])
        resp = make_client(transport).post(
            "/mcp/", json={"jsonrpc": "2.0", "id": 11, "method": "tools/list"}, headers=AUTH
        )
        assert resp.status_code == 200
        names = [tool["name"] for tool in resp.json()["result"]["tools"]]
        assert len(names) == 12 + len(LOCAL_TOOLS)
        assert CALENDAR_TOOL_NAME in names
        assert "watchlist_add" not in names
        descriptor = next(t for t in resp.json()["result"]["tools"] if t["name"] == CALENDAR_TOOL_NAME)
        assert descriptor["inputSchema"]["properties"]["market"]["default"] == "US"


class TestCalendarSemantics:
    def test_trading_day_mid_session_expected_date_is_market_date(self, monkeypatch) -> None:
        # Live-verified case from 2026-10-15 (canonical backend measurement):
        # market_date=2026-10-15, is_trading_day=True, last_completed=2026-10-14.
        service = FakeCanonicalService(
            market_now_dt=datetime(2026, 10, 15, 16, 9, 43, tzinfo=ZoneInfo("America/New_York")),
            is_trading=True,
            last_completed=date(2026, 10, 14),
        )
        _patch_canonical(monkeypatch, service)
        facts = calendar_module.calendar_facts("US", DEFAULT_CALENDAR_APP_ROOT)
        assert facts["market_date"] == "2026-10-15"
        assert facts["is_trading_day"] is True
        assert facts["last_completed_trading_day"] == "2026-10-14"
        # THE rule: mid-session trading day expects TODAY, not yesterday.
        assert facts["expected_trading_date"] == "2026-10-15"

    def test_saturday_semantics_expected_date_is_last_completed(self, monkeypatch) -> None:
        # Test 4 scenario: known Saturday 2026-10-17 must NOT be a trading day.
        service = FakeCanonicalService(
            market_now_dt=datetime(2026, 10, 17, 12, 0, 0, tzinfo=ZoneInfo("America/New_York")),
            is_trading=False,
            last_completed=date(2026, 10, 16),
        )
        _patch_canonical(monkeypatch, service)
        facts = calendar_module.calendar_facts("US", DEFAULT_CALENDAR_APP_ROOT)
        assert facts["market_date"] == "2026-10-17"
        assert facts["is_trading_day"] is False
        assert facts["expected_trading_date"] == "2026-10-16"

    def test_output_schema_fields_present(self, monkeypatch) -> None:
        service = FakeCanonicalService(
            market_now_dt=datetime(2026, 10, 15, 16, 9, 43, tzinfo=ZoneInfo("America/New_York")),
            is_trading=True,
            last_completed=date(2026, 10, 14),
        )
        _patch_canonical(monkeypatch, service)
        facts = calendar_module.calendar_facts("US", DEFAULT_CALENDAR_APP_ROOT)
        assert facts["market"] == "US"
        assert facts["market_timezone"] == "America/New_York"
        assert facts["market_now"].startswith("2026-10-15T16:09:43")
        assert facts["calendar_source"] == CALENDAR_SOURCE
        assert "generated_at" in facts


class TestFailClosed:
    def test_service_unreachable_answers_error_envelope(self, monkeypatch) -> None:
        def _boom(backend_app_root: str = DEFAULT_CALENDAR_APP_ROOT) -> type:
            raise ImportError("canonical service unavailable")

        monkeypatch.setattr(calendar_module, "_load_market_calendar_service", _boom)
        result = calendar_module.calendar_tool_result("US", DEFAULT_CALENDAR_APP_ROOT)
        structured = result["structuredContent"]
        assert structured["calendar_status"] == "error"
        assert structured["expected_trading_date"] is None
        assert structured["error"]
        # no guessed dates anywhere
        assert "market_date" not in structured
        assert "is_trading_day" not in structured

    def test_error_envelope_via_mcp_surface(self, monkeypatch) -> None:
        transport = FakeTransport()  # would fail hard if the backend got called
        client = make_client(transport)

        def _boom(backend_app_root: str = DEFAULT_CALENDAR_APP_ROOT) -> type:
            raise RuntimeError("backend calendar probe failed")

        monkeypatch.setattr(calendar_module, "_load_market_calendar_service", _boom)
        resp = call_calendar(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 42  # request id preserved
        structured = body["result"]["structuredContent"]
        assert structured["calendar_status"] == "error"
        assert structured["expected_trading_date"] is None
        # nothing was forwarded to the backend
        assert transport.calls == []


class TestMcpEndToEndLocal:
    def test_calendar_call_never_touches_backend(self, monkeypatch) -> None:
        service = FakeCanonicalService(
            market_now_dt=datetime(2026, 10, 15, 16, 9, 43, tzinfo=ZoneInfo("America/New_York")),
            is_trading=True,
            last_completed=date(2026, 10, 14),
        )
        _patch_canonical(monkeypatch, service)
        transport = FakeTransport()  # any backend call would raise AssertionError
        client = make_client(transport)
        resp = call_calendar(client)
        assert resp.status_code == 200
        body = resp.json()
        result = body["result"]
        # Same shape as a backend tools/call result: text mirror + structuredContent.
        text = json.loads(result["content"][0]["text"])
        assert text == result["structuredContent"]
        assert result["structuredContent"]["expected_trading_date"] == "2026-10-15"
        assert result["structuredContent"]["calendar_source"] == CALENDAR_SOURCE
        assert transport.calls == []

    def test_default_market_is_us_when_arguments_missing(self, monkeypatch) -> None:
        service = FakeCanonicalService(
            market_now_dt=datetime(2026, 10, 15, 16, 9, 43, tzinfo=ZoneInfo("America/New_York")),
            is_trading=True,
            last_completed=date(2026, 10, 14),
        )
        _patch_canonical(monkeypatch, service)
        client = make_client(FakeTransport())
        resp = call_calendar(client, arguments=None, request_id=43)
        assert resp.status_code == 200
        assert resp.json()["result"]["structuredContent"]["market"] == "US"


class TestCanonicalIntegrationOptional:
    """Runs ONLY when the canonical backend tree is importable (deployed image
    or operator recon checkout via XANG_CALENDAR_BACKEND_APP_ROOT); otherwise
    skipped. Verifies Test-4 against the REAL canonical service: known Saturday
    2026-10-17 is not a US trading day."""

    def _try_service_module(self) -> Any:
        import importlib
        import os

        # The canonical app tree's Settings() requires DATABASE_URL (a harmless
        # dummy is enough — no DB connection is made on module import; redis is
        # optional and simply degrades to the non-Redis path).
        os.environ.setdefault("DATABASE_URL", "gateway-placeholder")
        root = os.environ.get("XANG_CALENDAR_BACKEND_APP_ROOT", DEFAULT_CALENDAR_APP_ROOT)
        saved = list(calendar_module.sys.path)
        try:
            calendar_module.sys.path.insert(0, root)
            return importlib.import_module("app.services.market_calendar_service")
        except Exception:  # noqa: BLE001 - optional integration
            return None
        finally:
            calendar_module.sys.path[:] = saved

    def test_known_saturday_is_not_trading_day(self) -> None:
        module = self._try_service_module()
        if module is None:
            pytest.skip("canonical backend tree not importable in this environment")
        cal = module.MarketCalendarService()
        assert cal.is_trading_day("US", date(2026, 10, 17)) is False

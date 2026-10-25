"""Phase 8 tests: earnings_lookup — local, read-only earnings observation.

Two kinds of double, deliberately kept apart:

* ``StubRules`` scripted stand-in for the canonical freshness module. Most tests
  use it so they run in CI, where the backend source tree does not exist.
* ``TestCanonicalReuse`` loads the REAL ``app.services.yahoo_earnings_calendar``
  when the tree is present (local run, image build) and asserts the gateway
  answers exactly what the canonical rule says.

``TestRulesAreInjected`` is the anti-duplication guard: it hands the module a
stub with a *different* window (5 days instead of 14). If anyone ever re-derives
the thresholds inside the gateway, that test fails.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from xang_mcp_gateway import app as app_module
from xang_mcp_gateway import earnings as earnings_module
from xang_mcp_gateway.calendar import CALENDAR_TOOL_NAME
from xang_mcp_gateway.config import ALL_BACKEND_TOOLS, DEFAULT_EXCLUDED_TOOLS, LOCAL_TOOLS
from xang_mcp_gateway.earnings import (
    EARNINGS_SOURCE,
    EARNINGS_TOOL_NAME,
    earnings_facts,
    normalize_symbol,
)

from .conftest import GATEWAY_TOKEN, FakeTransport, make_settings

AUTH = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}
TODAY = date(2026, 10, 24)

# An explicit XANG_TEST_BACKEND_ROOT wins (set it to a non-existent path to
# reproduce the CI environment, where the backend tree is absent).
_EXPLICIT_ROOT = os.environ.get("XANG_TEST_BACKEND_ROOT")
_CANDIDATE_ROOTS = (
    (_EXPLICIT_ROOT,)
    if _EXPLICIT_ROOT is not None
    else ("/opt/xang_backend", "/workspace/xang1234.partial/backend")
)


def canonical_root() -> str:
    for root in _CANDIDATE_ROOTS:
        if root and os.path.isfile(os.path.join(root, "app/services/yahoo_earnings_calendar.py")):
            return root
    pytest.skip("canonical backend tree not importable in this environment")


class StubRules:
    """Scripted stand-in for the canonical module (same surface, own numbers)."""

    EVENT_CALENDAR_MAX_AGE_DAYS = 14
    EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS = 7
    EVENT_CALENDAR_FUTURE_TOLERANCE_DAYS = 3

    def __init__(self, *, max_age: int | None = None, future_tolerance: int | None = None) -> None:
        self.EVENT_CALENDAR_MAX_AGE_DAYS = max_age if max_age is not None else 14
        self.EVENT_CALENDAR_FUTURE_TOLERANCE_DAYS = (
            future_tolerance if future_tolerance is not None else 3
        )

    def _age(self, observed_at: Any, reference_date: date | None) -> int:
        stamp = observed_at if isinstance(observed_at, date) else date.fromisoformat(str(observed_at)[:10])
        return ((reference_date or TODAY) - stamp).days

    def is_event_calendar_observation_fresh(self, observed_at: Any, *, reference_date: date | None = None) -> bool:
        if observed_at is None:
            return False
        try:
            age = self._age(observed_at, reference_date)
        except (TypeError, ValueError):
            return False
        return -self.EVENT_CALENDAR_FUTURE_TOLERANCE_DAYS <= age <= self.EVENT_CALENDAR_MAX_AGE_DAYS

    def persisted_next_earnings_date(self, fundamentals: Any, *, as_of_date: date | None) -> tuple[date | None, bool]:
        if not isinstance(fundamentals, dict) or as_of_date is None:
            return None, False
        if not self.is_event_calendar_observation_fresh(
            fundamentals.get("event_calendar_as_of_date"), reference_date=as_of_date
        ):
            return None, False
        raw = fundamentals.get("next_earnings_date")
        if raw is None:
            return None, True
        try:
            parsed = raw if isinstance(raw, date) else date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError):
            return None, False
        if parsed < as_of_date:
            return None, False
        return parsed, True


class FakeCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.executed.append((sql, params))

    def fetchone(self) -> Any:
        row = self._conn.row
        if row is None:
            return None
        # dict_row behaviour: the gateway asks for columns by name.
        return dict(row)


class FakeConnection:
    """Just enough of a psycopg connection for ``_fetch_row``."""

    def __init__(self, row: dict[str, Any] | None = None) -> None:
        self.row = row
        self.executed: list[tuple[str, Any]] = []
        self.read_only = False
        self.closed = False

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.closed = True
        return None

    def cursor(self, *args: Any, **kwargs: Any) -> FakeCursor:
        return FakeCursor(self)


def _store(row: dict[str, Any] | None = None, *, error: Exception | None = None) -> tuple[Any, list[FakeConnection]]:
    created: list[FakeConnection] = []

    def factory() -> FakeConnection:
        if error is not None:
            raise error
        conn = FakeConnection(row)
        created.append(conn)
        return conn

    return factory, created


def _observe(days_ago: int, next_date: str | None = None) -> dict[str, Any]:
    return {
        "symbol": "AAPL",
        "event_calendar_as_of_date": TODAY - timedelta(days=days_ago),
        "next_earnings_date": next_date,
    }


class TestSymbolValidation:
    def test_symbols_are_normalized(self) -> None:
        assert normalize_symbol("  aapl ") == "AAPL"
        assert normalize_symbol("brk.b") == "BRK.B"
        assert normalize_symbol("rds-a") == "RDS-A"

    def test_invalid_symbols_are_refused_without_touching_the_database(self) -> None:
        factory, created = _store(_observe(1, "2026-11-02"))
        for raw in ("", "   ", "TOOLONGSYMBOL", "AA PL", "AAPL;", None, 42):
            result = earnings_facts(
                raw,
                "postgresql://x@h/db",
                reference_date=TODAY,
                backend_app_root="/nonexistent",
                connect_factory=factory,
            )
            assert result["freshness_status"] == "invalid", raw
            assert result["reasons"] == ["invalid_symbol"], raw
            assert result["available"] is False
        assert created == [], "a rejected symbol must never reach the database"

    def test_sql_injection_attempt_is_refused_before_the_query(self) -> None:
        factory, created = _store(_observe(1, "2026-11-02"))
        result = earnings_facts(
            "'; DROP TABLE stock_fundamentals; --",
            "postgresql://x@h/db",
            reference_date=TODAY,
            backend_app_root=canonical_root(),
            connect_factory=factory,
        )
        assert result["freshness_status"] == "invalid"
        assert result["reasons"] == ["invalid_symbol"]
        assert created == []


def _injected(rules: Any) -> Any:
    """Temporarily replace the canonical-rules loader (see TestRulesAreInjected)."""

    class _Ctx:
        def __enter__(self) -> None:
            self._original = earnings_module._load_freshness_rules
            earnings_module._load_freshness_rules = lambda _root: rules

        def __exit__(self, *exc: Any) -> None:
            earnings_module._load_freshness_rules = self._original

    return _Ctx()


class TestLookup:
    def _facts(self, row: dict[str, Any] | None, rules: Any) -> dict[str, Any]:
        factory, _ = _store(row)
        symbol = row.get("symbol") if row else "AAPL"
        with _injected(rules):
            return earnings_facts(
                symbol,
                "postgresql://x@h/db",
                reference_date=TODAY,
                backend_app_root="/nonexistent",
                connect_factory=factory,
            )

    def test_a_fresh_future_date_is_available(self) -> None:
        result = self._facts(_observe(2, "2026-11-02"), StubRules())
        assert result["freshness_status"] == "ok"
        assert result["available"] is True
        assert result["next_earnings_date"] == "2026-11-02"
        assert result["event_calendar_as_of_date"] == "2026-10-22"
        assert result["source"] == EARNINGS_SOURCE
        assert result["observation_age_days"] == 2

    def test_a_fresh_observation_without_a_date_is_still_evidence(self) -> None:
        result = self._facts(_observe(1, None), StubRules())
        assert result["freshness_status"] == "ok"
        assert result["available"] is True
        assert result["next_earnings_date"] is None
        assert "no_upcoming_earnings_date" in result["reasons"]

    def test_confirmation_is_never_claimed(self) -> None:
        result = self._facts(_observe(2, "2026-11-02"), StubRules())
        assert result["date_confirmation"] == "unknown"
        assert "date_confirmation_unrecorded" in result["reasons"]
        assert "confirmed" not in str(result["date_confirmation"])

    def test_a_missing_row_is_unknown(self) -> None:
        result = self._facts(None, StubRules())
        assert result["freshness_status"] == "unknown"
        assert result["available"] is False
        assert result["reasons"] == ["symbol_not_found"]

    def test_a_stale_observation_is_stale(self) -> None:
        result = self._facts(_observe(20, "2026-11-02"), StubRules())
        assert result["freshness_status"] == "stale"
        assert result["available"] is False
        assert "observation_stale" in result["reasons"]
        assert result["next_earnings_date"] is None

    def test_an_unreadable_observation_date_is_invalid(self) -> None:
        row = {"symbol": "AAPL", "event_calendar_as_of_date": "not-a-date", "next_earnings_date": None}
        result = self._facts(row, StubRules())
        assert result["freshness_status"] == "invalid"
        assert result["reasons"] == ["observation_date_invalid"]

    def test_a_missing_observation_date_is_unknown(self) -> None:
        row = {"symbol": "AAPL", "event_calendar_as_of_date": None, "next_earnings_date": None}
        result = self._facts(row, StubRules())
        assert result["freshness_status"] == "unknown"
        assert result["reasons"] == ["observation_date_missing"]

    def test_a_past_earnings_date_is_refused(self) -> None:
        result = self._facts(_observe(1, "2026-10-19"), StubRules())
        assert result["freshness_status"] == "invalid"
        assert result["available"] is False
        assert "earnings_date_in_past" in result["reasons"]

    def test_an_observation_from_the_future_beyond_tolerance_is_invalid(self) -> None:
        result = self._facts(_observe(-10, "2026-11-02"), StubRules())
        assert result["freshness_status"] == "invalid"
        assert "observation_date_in_future" in result["reasons"]

    def test_a_database_error_is_unknown_not_ok(self) -> None:
        factory, _ = _store(None, error=RuntimeError("connection refused"))
        with _injected(StubRules()):
            result = earnings_facts("AAPL", "postgresql://x@h/db", reference_date=TODAY,
                                    backend_app_root="/nonexistent", connect_factory=factory)
        assert result["freshness_status"] == "unknown"
        assert result["available"] is False
        assert result["reasons"] == ["database_unavailable"]
        assert "connection refused" not in str(result), "driver text must not leak into the answer"

    def test_no_dsn_is_unknown(self) -> None:
        with _injected(StubRules()):
            result = earnings_facts("AAPL", None, reference_date=TODAY, backend_app_root="/nonexistent")
        assert result["freshness_status"] == "unknown"
        assert result["reasons"] == ["database_not_configured"]

    def test_missing_freshness_rules_fail_closed(self) -> None:
        factory, created = _store(_observe(1, "2026-11-02"))
        result = earnings_facts("AAPL", "postgresql://x@h/db", reference_date=TODAY,
                                backend_app_root="/nonexistent", connect_factory=factory)
        assert result["freshness_status"] == "invalid"
        assert result["reasons"] == ["freshness_rules_unavailable"]
        assert created == []

    def test_the_query_is_parameterised_and_read_only(self) -> None:
        factory, created = _store(_observe(1, "2026-11-02"))
        with _injected(StubRules()):
            earnings_facts("AAPL", "postgresql://x@h/db", reference_date=TODAY,
                           backend_app_root="/nonexistent", connect_factory=factory)
        conn = created[0]
        assert conn.read_only is True
        sql, params = conn.executed[0]
        assert params == ("AAPL",)
        assert "stock_fundamentals" in sql and sql.strip().upper().startswith("SELECT")
        assert "%s" in sql and "AAPL" not in sql


class TestRulesAreInjected:
    """If the gateway ever re-derives 14/7/3 itself, these fail."""

    def test_a_shorter_injected_window_turns_a_fresh_row_stale(self) -> None:
        factory, _ = _store(_observe(6, "2026-11-02"))
        with _injected(StubRules(max_age=5)):
            result = earnings_facts("AAPL", "postgresql://x@h/db", reference_date=TODAY,
                                    backend_app_root="/nonexistent", connect_factory=factory)
        assert result["freshness_status"] == "stale"
        assert result["event_calendar_max_age_days"] == 5

    def test_a_longer_injected_window_keeps_the_same_row_fresh(self) -> None:
        factory, _ = _store(_observe(6, "2026-11-02"))
        with _injected(StubRules(max_age=30)):
            result = earnings_facts("AAPL", "postgresql://x@h/db", reference_date=TODAY,
                                    backend_app_root="/nonexistent", connect_factory=factory)
        assert result["freshness_status"] == "ok"
        assert result["event_calendar_max_age_days"] == 30


class TestCanonicalReuse:
    """Runs against the real backend module when the tree exists."""

    def test_the_published_thresholds_are_the_ones_reported(self) -> None:
        root = canonical_root()
        canonical = earnings_module._load_freshness_rules(root)
        factory, _ = _store(_observe(1, "2026-11-02"))
        result = earnings_facts("AAPL", "postgresql://x@h/db", reference_date=TODAY,
                                backend_app_root=root, connect_factory=factory)
        assert canonical.EVENT_CALENDAR_MAX_AGE_DAYS == 14
        assert canonical.EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS == 7
        assert canonical.EVENT_CALENDAR_FUTURE_TOLERANCE_DAYS == 3
        assert result["event_calendar_max_age_days"] == canonical.EVENT_CALENDAR_MAX_AGE_DAYS
        assert result["event_calendar_future_tolerance_days"] == canonical.EVENT_CALENDAR_FUTURE_TOLERANCE_DAYS

    @pytest.mark.parametrize("age,status", [(0, "ok"), (13, "ok"), (14, "ok"), (15, "stale"), (40, "stale")])
    def test_the_gateway_agrees_with_the_canonical_window(self, age: int, status: str) -> None:
        root = canonical_root()
        canonical = earnings_module._load_freshness_rules(root)
        factory, _ = _store(_observe(age, "2026-11-30"))
        result = earnings_facts("AAPL", "postgresql://x@h/db", reference_date=TODAY,
                                backend_app_root=root, connect_factory=factory)
        expected_fresh = canonical.is_event_calendar_observation_fresh(
            TODAY - timedelta(days=age), reference_date=TODAY
        )
        assert (result["freshness_status"] == "ok") is expected_fresh
        assert result["freshness_status"] == status

    def test_the_future_tolerance_matches(self) -> None:
        root = canonical_root()
        for age, expected in ((-3, "ok"), (-4, "invalid")):
            factory, _ = _store(_observe(age, "2026-11-30"))
            result = earnings_facts("AAPL", "postgresql://x@h/db", reference_date=TODAY,
                                    backend_app_root=root, connect_factory=factory)
            assert result["freshness_status"] == expected, age


class TestRoute:
    def _client(self, transport: FakeTransport | None = None, **settings_kwargs: Any) -> TestClient:
        transport = transport or FakeTransport()
        app = app_module.create_app(make_settings(**settings_kwargs), transport=transport)
        return TestClient(app)

    def test_tools_list_advertises_the_earnings_tool_and_keeps_the_calendar_tool(self) -> None:
        names = [name for name in ALL_BACKEND_TOOLS]
        payload = {
            "jsonrpc": "2.0",
            "id": 11,
            "result": {"tools": [{"name": name, "description": f"{name} desc"} for name in names]},
        }
        client = self._client(FakeTransport([(200, payload)]))
        body = client.post("/mcp/", json={"jsonrpc": "2.0", "id": 11, "method": "tools/list"}, headers=AUTH).json()
        advertised = [tool["name"] for tool in body["result"]["tools"]]
        assert EARNINGS_TOOL_NAME in advertised
        assert CALENDAR_TOOL_NAME in advertised
        assert "watchlist_add" not in advertised
        descriptor = next(t for t in body["result"]["tools"] if t["name"] == EARNINGS_TOOL_NAME)
        assert descriptor["inputSchema"]["required"] == ["symbol"]
        assert len(advertised) == len(ALL_BACKEND_TOOLS - DEFAULT_EXCLUDED_TOOLS) + len(LOCAL_TOOLS)

    def test_tools_call_runs_locally_without_a_backend_round_trip(self) -> None:
        transport = FakeTransport()  # exhausted: any passthrough would explode
        client = self._client(transport, db_dsn="")
        body = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {"name": EARNINGS_TOOL_NAME, "arguments": {"symbol": "AAPL"}},
            },
            headers=AUTH,
        ).json()
        assert body["id"] == 12
        payload = body["result"]["structuredContent"]
        assert payload["symbol"] == "AAPL"
        assert payload["freshness_status"] == "unknown"
        assert payload["reasons"] == ["database_not_configured"]
        assert transport.calls == [], "local tool must not touch the backend"

    def test_a_missing_symbol_is_a_protocol_error(self) -> None:
        client = self._client()
        body = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {"name": EARNINGS_TOOL_NAME, "arguments": {}},
            },
            headers=AUTH,
        ).json()
        assert body["error"]["code"] == -32602

    def test_an_invalid_symbol_is_answered_as_a_fact_not_an_error(self) -> None:
        client = self._client(db_dsn="")
        body = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 14,
                "method": "tools/call",
                "params": {"name": EARNINGS_TOOL_NAME, "arguments": {"symbol": "BAD SYMBOL"}},
            },
            headers=AUTH,
        ).json()
        assert "error" not in body
        assert body["result"]["structuredContent"]["reasons"] == ["invalid_symbol"]

    def test_the_tool_is_in_the_allowlist_and_the_calendar_tool_still_works(self) -> None:
        client = self._client()
        body = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 15,
                "method": "tools/call",
                "params": {"name": CALENDAR_TOOL_NAME, "arguments": {"market": "US"}},
            },
            headers=AUTH,
        ).json()
        # The canonical calendar service may be missing here; either way the
        # answer is an explicit envelope, never a crash.
        assert "result" in body and "structuredContent" in body["result"]

    def test_an_unknown_tool_is_still_rejected(self) -> None:
        client = self._client()
        body = client.post(
            "/mcp/",
            json={"jsonrpc": "2.0", "id": 16, "method": "tools/call", "params": {"name": "nope", "arguments": {}}},
            headers=AUTH,
        ).json()
        assert body["error"]["code"] == -32602

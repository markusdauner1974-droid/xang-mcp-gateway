"""Publication proof is optional, parameterised, read-only and fail-closed."""

from __future__ import annotations

import asyncio
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from xang_mcp_gateway.app import create_app
from xang_mcp_gateway.calendar_only import create_calendar_app
from xang_mcp_gateway.config import GatewayConfigError, GatewaySettings
from xang_mcp_gateway.publication import (
    SQL_PUBLICATION,
    TOOL_NAME,
    PublicationRunner,
    PublicationUnavailable,
    SqlPublicationStore,
    build_proof,
    validate_arguments,
)

from .conftest import GATEWAY_TOKEN, make_settings

SCAN_ID = "00000000-0000-4000-8000-000000000001"
HEADERS = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


def row(market="US"):
    return {
        "scan_id": SCAN_ID, "scan_status": "completed",
        "scan_universe_type": "market", "scan_market": market,
        "feature_run_id": 101 if market == "US" else 202,
        "pointer_run_id": 101 if market == "US" else 202,
        "run_status": "published", "run_market": market,
        "run_universe_market": market, "run_universe_type": "market",
        "pointer_key": "latest_published_market:" + market,
        "as_of_date": date(2020, 1, 2),
        "published_at": datetime(2020, 1, 2, 22, tzinfo=UTC),
        "completed_at": datetime(2020, 1, 2, 21, tzinfo=UTC),
    }


def settings(**kwargs):
    return make_settings(
        calendar_only=True, publication_provenance=True,
        provenance_db_dsn="postgresql://metadata-test-only", **kwargs,
    )


def call(client, args=None, name=TOOL_NAME, headers=HEADERS):
    return client.post("/mcp/", headers=headers, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args if args is not None else {"market": "US", "scan_id": SCAN_ID}},
    })


@pytest.mark.parametrize("market", ["US", "DE"])
def test_proof_preserves_explicit_market_run_and_publication(market):
    source = row(market)
    snapshot = copy.deepcopy(source)
    proof = build_proof(source, market, SCAN_ID)
    assert source == snapshot
    assert proof["proof_status"] == "ok"
    assert proof["market"] == market
    assert proof["run"]["id"] == source["feature_run_id"]
    assert proof["run"]["published_at"] == source["published_at"].isoformat()
    assert proof["run"]["pointer_key"] == source["pointer_key"]
    assert "symbols" not in proof
    assert "is_current" not in proof  # Calendar freshness belongs to the caller.


@pytest.mark.parametrize("field,value", [
    ("scan_id", "00000000-0000-4000-8000-000000000002"),
    ("scan_status", "running"), ("scan_status", None),
    ("scan_universe_type", "custom"), ("scan_market", "DE"),
    ("run_status", "completed"), ("run_status", None),
    ("run_market", "DE"), ("run_universe_market", None),
    ("run_universe_type", "custom"), ("pointer_key", "latest_published"),
    ("feature_run_id", True), ("feature_run_id", 0),
    ("feature_run_id", None), ("pointer_run_id", True),
    ("pointer_run_id", 999), ("pointer_run_id", None),
    ("as_of_date", None), ("as_of_date", "2020-01-02"),
    ("published_at", None), ("published_at", datetime(2020, 1, 2)),
    ("completed_at", "2020-01-02"),
])
def test_incomplete_or_conflicting_proof_is_rejected(field, value):
    source = row()
    source[field] = value
    with pytest.raises(PublicationUnavailable):
        build_proof(source, "US", SCAN_ID)


@pytest.mark.parametrize("args", [
    None, [], {}, {"market": "US"}, {"scan_id": SCAN_ID},
    {"market": "XX", "scan_id": SCAN_ID},
    {"market": None, "scan_id": SCAN_ID},
    {"market": "US", "scan_id": "' OR 1=1 --"},
    {"market": "US", "scan_id": "x" * 36},
    {"market": "US", "scan_id": SCAN_ID, "sql": "SELECT 1"},
])
def test_invalid_arguments_rejected_before_database(args):
    with pytest.raises(ValueError):
        validate_arguments(args)


def test_market_normalization_does_not_infer_market_from_symbol():
    assert validate_arguments({"market": " de ", "scan_id": SCAN_ID}) == ("DE", SCAN_ID)


class Connection:
    def __init__(self, records):
        self.records = records
        self.read_only = False
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self, **kwargs):
        return self

    def execute(self, sql, parameters):
        assert self.read_only is True
        self.statements.append((sql, parameters))

    def fetchall(self):
        return self.records


def test_store_uses_one_bounded_read_only_parameterised_select():
    connection = Connection([row()])
    connects = []

    def connect(*args, **kwargs):
        connects.append((args, kwargs))
        return connection

    result = SqlPublicationStore("not-a-real-dsn", connect).lookup("US", SCAN_ID)
    assert result["run"]["id"] == 101
    assert connection.statements == [(SQL_PUBLICATION, ("latest_published_market:US", SCAN_ID))]
    assert connects[0][1]["connect_timeout"] == 5
    assert "default_transaction_read_only=on" in connects[0][1]["options"]
    assert "statement_timeout=5000" in connects[0][1]["options"]
    assert "lock_timeout=1000" in connects[0][1]["options"]
    assert "stock_feature_daily" not in SQL_PUBLICATION


@pytest.mark.parametrize("records", [[], [row(), row()]])
def test_missing_or_ambiguous_scan_never_gets_a_fallback(records):
    with pytest.raises(PublicationUnavailable):
        SqlPublicationStore("test-only", lambda *a, **k: Connection(records)).lookup("US", SCAN_ID)


class Provider:
    def __init__(self, failure=None, delay=0):
        self.calls = []
        self.failure = failure
        self.delay = delay

    def lookup(self, market, scan_id):
        self.calls.append((market, scan_id))
        if self.delay:
            time.sleep(self.delay)
        if self.failure:
            raise self.failure
        return build_proof(row(market), market, scan_id)


def test_enabled_surface_is_calendar_and_metadata_only():
    provider = Provider()
    with TestClient(create_calendar_app(settings(), provider)) as client:
        result = client.post("/mcp/", headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list",
        }).json()["result"]
        assert [tool["name"] for tool in result["tools"]] == ["market_calendar_status", TOOL_NAME]
        health = client.get("/healthz").json()
        assert health["mode"] == "calendar_with_provenance"
        assert health["enrichment"] == "disabled"
        assert health["tools_visible"] == 2
        assert call(client).json()["result"]["structuredContent"]["run"]["id"] == 101
        for name in ("find_candidates", "earnings_lookup", "watchlist_add"):
            assert "error" in call(client, name=name).json()
        assert call(client, headers={}).status_code == 401
    assert provider.calls == [("US", SCAN_ID)]


def test_driver_error_is_sanitized_and_fail_closed():
    marker = "SYNTHETIC_DRIVER_ERROR_MUST_NOT_BE_EXPOSED"
    provider = Provider(RuntimeError(marker))
    with TestClient(create_calendar_app(settings(), provider)) as client:
        response = call(client)
        result = response.json()["result"]
        assert result["isError"] is True
        assert "structuredContent" not in result
        assert marker not in response.text


def test_timeout_is_fail_closed():
    with TestClient(create_calendar_app(settings(request_timeout_seconds=0.001), Provider(delay=0.02))) as client:
        assert call(client).json()["result"]["isError"] is True


def test_bad_tool_arguments_do_not_call_provider():
    provider = Provider()
    with TestClient(create_calendar_app(settings(), provider)) as client:
        assert "error" in call(client, args={"market": "US"}).json()
    assert provider.calls == []


def test_disabled_mode_never_constructs_a_database_provider(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Disabled feature must not construct SQL")
    monkeypatch.setattr("xang_mcp_gateway.calendar_only.SqlPublicationStore", forbidden)
    with TestClient(create_app(make_settings(calendar_only=True, provenance_db_dsn="ignored"))) as client:
        assert "error" in call(client).json()
        assert client.get("/healthz").json()["tools_visible"] == 1


@pytest.mark.parametrize("env", [
    {"GATEWAY_PUBLICATION_PROVENANCE": "yes"},
    {"GATEWAY_PUBLICATION_PROVENANCE": "true"},
    {"GATEWAY_PUBLICATION_PROVENANCE": "true", "GATEWAY_CALENDAR_ONLY": "true"},
])
def test_invalid_configuration_fails_startup(env):
    with pytest.raises(GatewayConfigError):
        GatewaySettings.from_env({"GATEWAY_TOKEN": GATEWAY_TOKEN, **env})


def test_provenance_requires_only_dedicated_dsn_not_backend_or_enrichment_credentials():
    configured = GatewaySettings.from_env({
        "GATEWAY_TOKEN": GATEWAY_TOKEN, "GATEWAY_CALENDAR_ONLY": "true",
        "GATEWAY_PUBLICATION_PROVENANCE": "true",
        "XANG_PROVENANCE_DB_DSN": "postgresql://metadata-only-test",
    })
    assert configured.allowed_tools == {"market_calendar_status", TOOL_NAME}
    assert configured.backend_token == ""
    assert configured.db_dsn is None
    assert configured.backend_visible_tools == set()
    assert not configured.enrichment_enabled


def test_sql_scan_identity_is_text_even_for_uuid_database_columns():
    assert "CAST(s.scan_id AS text) AS scan_id" in SQL_PUBLICATION


def test_slow_metadata_is_bounded_and_does_not_occupy_calendar_pool():
    release = threading.Event()
    both_started = threading.Event()
    lock = threading.Lock()
    calls = []

    class SlowProvider:
        def lookup(self, market, scan_id):
            with lock:
                calls.append(threading.current_thread().name)
                if len(calls) == 2:
                    both_started.set()
            release.wait(3)
            return build_proof(row(market), market, scan_id)

    async def scenario():
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        runner = PublicationRunner(SlowProvider())
        tasks = []
        try:
            tasks = [asyncio.create_task(runner.lookup("US", SCAN_ID, 0.2)) for _ in range(2)]
            deadline = loop.time() + 1
            while not both_started.is_set() and loop.time() < deadline:
                await asyncio.sleep(0.001)
            assert both_started.is_set()
            with pytest.raises(PublicationUnavailable, match="busy"):
                await runner.lookup("US", SCAN_ID, 0.2)
            assert await asyncio.wait_for(asyncio.to_thread(lambda: "calendar-pool-free"), 0.5) == "calendar-pool-free"
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(outcome, TimeoutError) for outcome in outcomes)
            # Timed-out queries must not free slots while still running.
            with pytest.raises(PublicationUnavailable, match="busy"):
                await runner.lookup("US", SCAN_ID, 0.2)
            assert len(calls) == 2
            assert all(name.startswith("publication-metadata") for name in calls)
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            runner.close()

    asyncio.run(scenario())

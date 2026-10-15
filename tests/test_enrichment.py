"""Enrichment: pure fact mapping, SQL store, and the response-merge behavior."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from xang_mcp_gateway.app import create_app
from xang_mcp_gateway.enrichment import (
    ENRICHMENT_FIELD_COUNT,
    SETUP_ENGINE_PREFIXED,
    TOP_LEVEL_FIELDS,
    VCP_NUM_BASES_PATH,
    SqlFeatureStore,
    build_facts,
)
from xang_mcp_gateway.rpc import BackendUnavailable, HttpxTransport

from .conftest import (
    ENRICHED_FACTS_NVDA,
    GATEWAY_TOKEN,
    FakeFactsProvider,
    FakeTransport,
    candidate_envelope,
    candidates_result_payload,
    make_settings,
)

AUTH = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}


def make_client(transport: FakeTransport, provider: object | None = None) -> TestClient:
    return TestClient(create_app(make_settings(), transport=transport, facts_provider=provider))


def find_candidates_call(candidates: list[str]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 30,
        "method": "tools/call",
        "params": {"name": "find_candidates", "arguments": {"filters": {}, "limit": len(candidates)}},
    }


def facts_sql_row() -> dict[str, Any]:
    return {
        "symbol": "NVDA",
        "minervini_rating": "Buy",
        "canslim_rating": None,
        "volume_breakthrough_rating": None,
        "data_status": "complete",
        "is_scannable": True,
        "action_state": None,
        "opportunity_state": None,
        "vcp_detected": True,
        "volume_surge": None,
        "vcp_num_bases_raw": 3,
        "setup_engine": {
            "readiness_score": 72.0,
            "quality_score": 64.5,
            "pattern_primary": "flat_base",
            "pivot_price": 148.5,
            "pivot_type": "flat",
            "pivot_date": "2026-10-10",
            "distance_to_pivot_pct": 4.2,
            "in_early_zone": True,
            "extended_from_pivot": False,
            "atr14_pct": 2.8,
            "volume_vs_50d": 1.35,
        },
    }


class TestBuildFactsPureMapping:
    def test_full_row_maps_to_exactly_21_fields(self) -> None:
        facts = build_facts(facts_sql_row())
        assert len(facts) == ENRICHMENT_FIELD_COUNT == 21
        for field_name, expected in ENRICHED_FACTS_NVDA.items():
            assert facts[field_name] == expected, field_name

    def test_float_integral_bases_become_int(self) -> None:
        row = facts_sql_row()
        row["vcp_num_bases_raw"] = 5.0
        assert build_facts(row)["vcp_num_bases"] == 5

    def test_bool_and_fractional_and_negative_bases_are_rejected(self) -> None:
        for raw in (True, 2.5, -1, "3", None):
            row = facts_sql_row()
            row["vcp_num_bases_raw"] = raw
            assert build_facts(row)["vcp_num_bases"] is None

    def test_missing_setup_engine_yields_none_se_fields(self) -> None:
        row = facts_sql_row()
        row["setup_engine"] = None
        facts = build_facts(row)
        assert all(facts[gateway_field] is None for _, gateway_field in SETUP_ENGINE_PREFIXED)

    def test_missing_top_level_keys_are_none(self) -> None:
        facts = build_facts({"symbol": "X", "setup_engine": {}})
        assert all(facts[field] is None for field in TOP_LEVEL_FIELDS)


class TestSqlFeatureStore:
    def test_facts_for_queries_the_requested_run(self) -> None:
        store_conn = FakeConnection([[facts_sql_row()]])
        store = SqlFeatureStore("postgresql://ro@db/stockscanner", connect_factory=lambda: store_conn)
        facts = store.facts_for(["NVDA"], 20)
        assert facts["NVDA"]["minervini_rating"] == "Buy"
        assert facts["NVDA"]["vcp_num_bases"] == 3
        assert facts["NVDA"]["se_readiness_score"] == 72.0
        # exact-run contract: no separate "newest published" lookup, run_id passed through
        sql, params = store_conn.cursor_obj.executed[0]
        assert "stock_feature_daily" in sql and "run_id = %s" in sql
        assert params == (VCP_NUM_BASES_PATH, 20, ["NVDA"])

    def test_empty_symbol_list_short_circuits(self) -> None:
        store = SqlFeatureStore("dsn", connect_factory=lambda: (_ for _ in ()).throw(AssertionError("no connect")))
        assert store.facts_for([], 20) == {}


class FakeCursor:
    def __init__(self, result_sets: list[list[dict[str, Any]]]) -> None:
        self._result_sets = result_sets
        self.executed: list[tuple[str, tuple | list]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple | list | None = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> dict[str, Any] | None:
        current = self._result_sets.pop(0)
        return current[0] if current else None

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._result_sets.pop(0) if self._result_sets else [])


class FakeConnection:
    def __init__(self, result_sets: list[list[dict[str, Any]]]) -> None:
        self.cursor_obj = FakeCursor(result_sets)

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self, row_factory: object = None) -> FakeCursor:
        return self.cursor_obj

    def ping(self) -> bool:
        """Not part of psycopg API — used by the provider's ping through cursor."""
        return True


class TestResponseEnrichment:
    def test_find_candidates_is_enriched_in_both_copies_with_marker(self, sample_facts: dict[str, dict[str, Any]]) -> None:
        provider = FakeFactsProvider(facts=sample_facts)
        transport = FakeTransport([(200, candidates_result_payload(candidate_envelope(["NVDA", "AMD"])))])
        client = make_client(transport, provider=provider)
        resp = client.post("/mcp/", json=find_candidates_call(["NVDA", "AMD"]), headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert provider.requested == [(["NVDA", "AMD"], 20)]

        structured = body["result"]["structuredContent"]
        nvda = structured["symbols"][0]
        assert nvda["minervini_rating"] == "Buy"
        assert nvda["vcp_num_bases"] == 3
        assert nvda["se_readiness_score"] == 72.0
        assert nvda["stage"] == 2  # base record untouched otherwise
        marker = structured["gateway"]
        assert marker["enrichment"] == "applied"
        assert marker["fields"] == 21
        assert marker["symbols_enriched"] == 1
        assert marker["symbols_missing"] == 1  # AMD not in the store

        text_mirror = json.loads(body["result"]["content"][0]["text"])
        assert text_mirror["symbols"][0]["minervini_rating"] == "Buy"
        assert text_mirror["gateway"]["enrichment"] == "applied"

    def test_provider_failure_keeps_base_data_and_marks_unavailable(self) -> None:
        provider = FakeFactsProvider(error=RuntimeError("db down"))
        envelope = candidate_envelope(["NVDA"])
        original_deep = copy.deepcopy(envelope)
        transport = FakeTransport([(200, candidates_result_payload(envelope))])
        client = make_client(transport, provider=provider)
        resp = client.post("/mcp/", json=find_candidates_call(["NVDA"]), headers=AUTH)
        structured = resp.json()["result"]["structuredContent"]
        assert structured["gateway"]["enrichment"] == "unavailable"
        assert structured["gateway"]["schema"] == "pr537-candidate-facts"
        assert "minervini_rating" not in structured["symbols"][0]
        # base envelope content preserved
        assert structured["symbols"][0]["symbol"] == original_deep["symbols"][0]["symbol"]
        assert structured["run"] == original_deep["run"]

    def test_non_enrichable_tool_is_not_touched(self) -> None:
        provider = FakeFactsProvider(facts={"NVDA": dict(ENRICHED_FACTS_NVDA)})
        backend = {"result": {"content": [{"type": "text", "text": "plain"}], "structuredContent": {"stock": {}}}}
        transport = FakeTransport([(200, backend)])
        client = make_client(transport, provider=provider)
        resp = client.post(
            "/mcp/",
            json={
                "jsonrpc": "2.0",
                "id": 40,
                "method": "tools/call",
                "params": {"name": "stock_snapshot", "arguments": {"symbol": "NVDA"}},
            },
            headers=AUTH,
        )
        body = resp.json()
        assert "gateway" not in body["result"]["structuredContent"]
        assert provider.requested == []

    def test_missing_run_id_skips_enrichment_with_marker(self, sample_facts: dict[str, dict[str, Any]]) -> None:
        provider = FakeFactsProvider(facts=sample_facts)
        envelope = candidate_envelope(["NVDA"])
        envelope.pop("run")  # malformed response: no run block at all
        transport = FakeTransport([(200, candidates_result_payload(envelope))])
        client = make_client(transport, provider=provider)
        resp = client.post("/mcp/", json=find_candidates_call(["NVDA"]), headers=AUTH)
        structured = resp.json()["result"]["structuredContent"]
        assert structured["gateway"]["enrichment"] == "skipped"
        assert "run id missing" in structured["gateway"]["reason"]
        assert provider.requested == []  # no facts fetched for a mismatched run
        assert "minervini_rating" not in structured["symbols"][0]  # base data intact

    @pytest.mark.parametrize("bad_run_id", [True, "20", 20.5, None])
    def test_invalid_run_id_types_skip_enrichment(self, sample_facts: dict[str, dict[str, Any]], bad_run_id: Any) -> None:
        provider = FakeFactsProvider(facts=sample_facts)
        envelope = candidate_envelope(["NVDA"])
        envelope["run"] = {"id": bad_run_id}
        transport = FakeTransport([(200, candidates_result_payload(envelope))])
        client = make_client(transport, provider=provider)
        resp = client.post("/mcp/", json=find_candidates_call(["NVDA"]), headers=AUTH)
        structured = resp.json()["result"]["structuredContent"]
        assert structured["gateway"]["enrichment"] == "skipped"
        assert provider.requested == []

    def test_int_run_id_of_newer_run_is_used_verbatim(self, sample_facts: dict[str, dict[str, Any]]) -> None:
        provider = FakeFactsProvider(facts=sample_facts)
        envelope = candidate_envelope(["NVDA"])
        envelope["run"] = {"id": 21, "as_of_date": "2026-10-15"}
        transport = FakeTransport([(200, candidates_result_payload(envelope))])
        client = make_client(transport, provider=provider)
        assert client.post("/mcp/", json=find_candidates_call(["NVDA"]), headers=AUTH).status_code == 200
        assert provider.requested == [(["NVDA"], 21)]

    def test_enrichment_disabled_leaves_response_absolutely_untouched(self) -> None:
        envelope = candidate_envelope(["NVDA"])
        backend = candidates_result_payload(envelope)
        original = copy.deepcopy(backend)
        transport = FakeTransport([(200, backend)])
        client = make_client(transport, provider=None)
        resp = client.post("/mcp/", json=find_candidates_call(["NVDA"]), headers=AUTH)
        assert resp.json() == original

    def test_non_dict_structured_content_with_unparseable_text_is_untouched(self) -> None:
        provider = FakeFactsProvider(facts={"NVDA": dict(ENRICHED_FACTS_NVDA)})
        backend = {"result": {"content": [{"type": "text", "text": "not-json"}]}}
        original = copy.deepcopy(backend)
        transport = FakeTransport([(200, backend)])
        client = make_client(transport, provider=provider)
        resp = client.post("/mcp/", json=find_candidates_call(["NVDA"]), headers=AUTH)
        assert resp.json() == original
        # provider never consulted because no structuredContent AND no parsable text
        assert provider.requested == []


class TestHttpxTransportHardening:
    def test_non_object_backend_payload_is_downstream_error(self) -> None:
        """CodeRabbit finding: HTTP-200 JSON lists/scalars must never reach clients."""
        transport = HttpxTransport(make_settings())
        transport._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[1, 2, 3]))
        )
        with pytest.raises(BackendUnavailable, match="non-object"):
            asyncio.run(transport.post_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))

    def test_object_payload_passes_unchanged(self) -> None:
        transport = HttpxTransport(make_settings())
        payload = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        transport._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        )
        status, body = asyncio.run(transport.post_jsonrpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        assert status == 200 and body == payload

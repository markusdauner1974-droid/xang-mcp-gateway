"""Shared test fixtures: fake backend transport, settings, payload builders."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from xang_mcp_gateway import GatewaySettings

GATEWAY_TOKEN = "gateway-test-token"
BACKEND_TOKEN = "backend-test-secret"


def make_settings(**overrides: Any) -> GatewaySettings:
    base = dict(
        backend_mcp_url="http://backend-test:8000/mcp",
        backend_token=BACKEND_TOKEN,
        gateway_token=GATEWAY_TOKEN,
    )
    base.update(overrides)
    return GatewaySettings(**base)


class FakeTransport:
    """Scripted Transport: responses consumed in order, calls recorded."""

    def __init__(
        self,
        responses: list[tuple[int, dict[str, Any] | None]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def post_jsonrpc(self, body: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        self.calls.append(copy.deepcopy(body))
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise AssertionError("FakeTransport exhausted: unexpected extra backend call")
        return self._responses.pop(0)


class FakeFactsProvider:
    """Scripted FactsProvider with optional failure injection."""

    def __init__(
        self,
        facts: dict[str, dict[str, Any]] | None = None,
        error: Exception | None = None,
        ping_ok: bool = True,
    ) -> None:
        self._facts = facts or {}
        self._error = error
        self._ping_ok = ping_ok
        self.requested: list[list[str]] = []

    def facts_for(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        self.requested.append(list(symbols))
        if self._error is not None:
            raise self._error
        return {s: dict(self._facts[s]) for s in symbols if s in self._facts}

    def ping(self) -> bool:
        return self._ping_ok


def candidate_record(symbol: str) -> dict[str, Any]:
    """Mirror of a live find_candidates symbol record (verified 2026-10-15)."""
    return {
        "symbol": symbol,
        "company_name": f"{symbol} Inc",
        "composite_score": 88.5,
        "current_price": 140.25,
        "stage": 2,
        "rating": "Pass",
        "rs_rating": 92,
        "market_cap": 3_000_000_000_000,
        "volume": 25_000_000,
        "eps_growth_qq": 34.0,
        "sales_growth_qq": 28.0,
        "gics_sector": "Information Technology",
        "ibd_industry_group": "Semiconductors",
        "se_setup_score": 61.0,
        "se_setup_ready": False,
    }


def candidate_envelope(symbols: list[str]) -> dict[str, Any]:
    """Mirror of the live find_candidates structuredContent shape."""
    return {
        "summary": "3 candidates",
        "run": {"id": 20, "as_of_date": "2026-10-14"},
        "scope": {"universe": "US"},
        "citations": [
            {"label": "feature run", "source": "feature_runs", "reference": "20", "as_of": "2026-10-14"}
        ],
        "facts": [
            {"key": "candidate_count", "value": len(symbols), "source": "feature_runs", "as_of": "2026-10-14"}
        ],
        "freshness": {"feature_run": "2026-10-14T21:25:32Z"},
        "next_actions": [],
        "symbols": [candidate_record(s) for s in symbols],
    }


def candidates_result_payload(envelope: dict[str, Any], request_id: int = 30) -> dict[str, Any]:
    """Backend tools/call result: content[0].text mirrors structuredContent."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {
            "content": [{"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}],
            "structuredContent": copy.deepcopy(envelope),
        },
    }


ENRICHED_FACTS_NVDA: dict[str, Any] = {
    "minervini_rating": "Buy",
    "canslim_rating": None,
    "volume_breakthrough_rating": None,
    "data_status": "complete",
    "is_scannable": True,
    "action_state": None,
    "opportunity_state": None,
    "vcp_detected": True,
    "volume_surge": None,
    "vcp_num_bases": 3,
    "se_readiness_score": 72.0,
    "se_quality_score": 64.5,
    "se_pattern_primary": "flat_base",
    "se_pivot_price": 148.5,
    "se_pivot_type": "flat",
    "se_pivot_date": "2026-10-10",
    "se_distance_to_pivot_pct": 4.2,
    "se_in_early_zone": True,
    "se_extended_from_pivot": False,
    "se_atr14_pct": 2.8,
    "se_volume_vs_50d": 1.35,
}


@pytest.fixture()
def sample_facts() -> dict[str, dict[str, Any]]:
    """One full 21-field facts bucket as the SQL store would produce it."""
    return {"NVDA": dict(ENRICHED_FACTS_NVDA)}

"""Read-only feature-store enrichment of gateway-proxied candidate responses.

Implements upstream PR #537
("Expose canonical setup and screening facts via MCP", open since 2026-10-05)
as a gateway-side measure: the 21 fields are read from the stack's Postgres
`stock_feature_daily.details_json` and merged into `find_candidates` symbol
records. Nothing is recomputed; the SQL paths were verified live on 2026-10-15
against run 20:

- 11 fields on details_json top level,
- `vcp_num_bases` nested at details.screeners.minervini.details.full_analysis.vcp.num_bases,
- 10 setup-engine fields inside the nested `setup_engine` object, stored
  WITHOUT the `se_` prefix (the prefix exists only in the projection).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger("xang_mcp_gateway.enrichment")

# details_json top-level fields projected 1:1 (JSON column, not jsonb — casts
# are only needed for existence checks, not for `->` extraction).
TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "minervini_rating",
    "canslim_rating",
    "volume_breakthrough_rating",
    "data_status",
    "is_scannable",
    "action_state",
    "opportunity_state",
    "vcp_detected",
    "volume_surge",
)

# setup_engine.<key> -> gateway field (se_ prefix, matching PR #537 naming).
SETUP_ENGINE_PREFIXED: tuple[tuple[str, str], ...] = (
    ("readiness_score", "se_readiness_score"),
    ("quality_score", "se_quality_score"),
    ("pattern_primary", "se_pattern_primary"),
    ("pivot_price", "se_pivot_price"),
    ("pivot_type", "se_pivot_type"),
    ("pivot_date", "se_pivot_date"),
    ("distance_to_pivot_pct", "se_distance_to_pivot_pct"),
    ("in_early_zone", "se_in_early_zone"),
    ("extended_from_pivot", "se_extended_from_pivot"),
    ("atr14_pct", "se_atr14_pct"),
    ("volume_vs_50d", "se_volume_vs_50d"),
)

# Persisted VCP base count: stored detector output, walked never recomputed
# (same contract as the PR's _vcp_num_bases_from_details helper).
VCP_NUM_BASES_PATH: list[str] = [
    "details",
    "screeners",
    "minervini",
    "details",
    "full_analysis",
    "vcp",
    "num_bases",
]

ENRICHMENT_FIELD_COUNT = len(TOP_LEVEL_FIELDS) + 1 + len(SETUP_ENGINE_PREFIXED)  # == 21

SQL_CURRENT_RUN_ID = (
    "SELECT id FROM feature_runs WHERE status = 'published' ORDER BY id DESC LIMIT 1"
)
SQL_FETCH_FACTS = """
SELECT
    symbol,
    details_json -> 'minervini_rating'          AS minervini_rating,
    details_json -> 'canslim_rating'            AS canslim_rating,
    details_json -> 'volume_breakthrough_rating' AS volume_breakthrough_rating,
    details_json -> 'data_status'               AS data_status,
    details_json -> 'is_scannable'              AS is_scannable,
    details_json -> 'action_state'              AS action_state,
    details_json -> 'opportunity_state'         AS opportunity_state,
    details_json -> 'vcp_detected'              AS vcp_detected,
    details_json -> 'volume_surge'              AS volume_surge,
    details_json #> %s                       AS vcp_num_bases_raw,
    details_json -> 'setup_engine'              AS setup_engine
FROM stock_feature_daily
WHERE run_id = %s AND symbol = ANY(%s)
""".strip()


def build_facts(row: dict[str, Any]) -> dict[str, Any]:
    """Pure mapping of one fetched SQL row to the 21 gateway facts."""
    facts: dict[str, Any] = {field: row.get(field) for field in TOP_LEVEL_FIELDS}

    raw_bases = row.get("vcp_num_bases_raw")
    if isinstance(raw_bases, bool) or not isinstance(raw_bases, (int, float)):
        bases = None
    elif isinstance(raw_bases, float) and not raw_bases.is_integer():
        bases = None
    else:
        bases = int(raw_bases)
    if bases is not None and bases < 0:
        bases = None
    facts["vcp_num_bases"] = bases

    setup_engine = row.get("setup_engine")
    if not isinstance(setup_engine, dict):
        setup_engine = {}
    for source_key, gateway_field in SETUP_ENGINE_PREFIXED:
        facts[gateway_field] = setup_engine.get(source_key)
    return facts


class FactsProvider(Protocol):
    """Source of per-symbol feature facts (SQL store in production, fakes in tests)."""

    def facts_for(self, symbols: list[str]) -> dict[str, dict[str, Any]]: ...


class SqlFeatureStore:
    """Read-only facts provider against the stack's Postgres feature store."""

    def __init__(self, dsn: str, connect_factory: Any = None) -> None:
        # Lazy psycopg import keeps the gateway core importable without the
        # database driver installed (unit-test environments).
        self._dsn = dsn
        self._connect_factory = connect_factory

    def _connect(self) -> Any:
        if self._connect_factory is not None:
            return self._connect_factory()
        import psycopg

        return psycopg.connect(self._dsn)

    def ping(self) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                return list(cursor.fetchone())[0] == 1

    def facts_for(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        try:
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - installed via dependencies
            raise RuntimeError("psycopg is required for SQL enrichment") from exc

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute(SQL_CURRENT_RUN_ID)
                run_row = cursor.fetchone()
                if run_row is None:
                    logger.warning("no published feature run found; enrichment skipped")
                    return {}
                cursor.execute(SQL_FETCH_FACTS, (VCP_NUM_BASES_PATH, run_row["id"], list(symbols)))
                rows = cursor.fetchall()
        return {row["symbol"]: build_facts(row) for row in rows}


def enrich_target(target: dict[str, Any], facts_by_symbol: dict[str, dict[str, Any]]) -> int:
    """Merge facts into the `symbols` records of a candidate envelope in place.

    Returns the number of records enriched. Records whose symbol has no store
    facts are left untouched (missing counts are reported by the caller).
    """
    symbols = target.get("symbols")
    if not isinstance(symbols, list):
        return 0
    enriched = 0
    for record in symbols:
        if not isinstance(record, dict):
            continue
        facts = facts_by_symbol.get(record.get("symbol"))
        if not facts:
            continue
        for gateway_field, value in facts.items():
            record[gateway_field] = value
        enriched += 1
    return enriched

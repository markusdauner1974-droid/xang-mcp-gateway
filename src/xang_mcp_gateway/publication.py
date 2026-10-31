"""Optional, read-only proof linking a REST scan to its published market run.

No candidate rows, ratings, filters, calendars or trading rules are read or
computed here. The caller fetches candidates from the official REST API.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Protocol
from uuid import UUID

TOOL_NAME = "scan_publication_status"
TOOL_DESCRIPTOR = {
    "name": TOOL_NAME,
    "description": (
        "Prove that one completed REST market scan references the currently "
        "published feature run of the requested US or DE market. No candidate data."
    ),
    "inputSchema": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "market": {"type": "string", "enum": ["US", "DE"]},
            "scan_id": {"type": "string", "format": "uuid"},
        },
        "required": ["market", "scan_id"],
    },
}

# One parameterised SELECT, one transaction/snapshot. The pointer and the scan
# must name the SAME run; there is deliberately no last-run fallback.
SQL_PUBLICATION = """
SELECT s.scan_id, s.status AS scan_status,
       s.universe_type AS scan_universe_type, s.universe_market AS scan_market,
       s.feature_run_id, r.status AS run_status, r.as_of_date,
       r.published_at, r.completed_at,
       r.config_json ->> 'market' AS run_market,
       r.config_json ->> 'universe_type' AS run_universe_type,
       r.config_json #>> '{universe,market}' AS run_universe_market,
       p.key AS pointer_key, p.run_id AS pointer_run_id
FROM public.scans AS s
LEFT JOIN public.feature_runs AS r ON r.id = s.feature_run_id
LEFT JOIN public.feature_run_pointers AS p ON p.key = %s
WHERE s.scan_id = %s
""".strip()


class PublicationUnavailable(RuntimeError):
    """No complete, consistent publication proof is available."""


class PublicationProvider(Protocol):
    def lookup(self, market: str, scan_id: str) -> dict[str, Any]: ...


def validate_arguments(arguments: Any) -> tuple[str, str]:
    if not isinstance(arguments, dict) or set(arguments) != {"market", "scan_id"}:
        raise ValueError("Exactly market and scan_id are required")
    market, scan_id = arguments["market"], arguments["scan_id"]
    if not isinstance(market, str) or market.strip().upper() not in {"US", "DE"}:
        raise ValueError("Supported markets are US and DE")
    if not isinstance(scan_id, str) or len(scan_id) != 36:
        raise ValueError("scan_id must be a UUID")
    try:
        canonical_id = str(UUID(scan_id))
    except ValueError as exc:
        raise ValueError("scan_id must be a UUID") from exc
    return market.strip().upper(), canonical_id


def build_proof(row: dict[str, Any], market: str, scan_id: str) -> dict[str, Any]:
    """Reject incomplete metadata instead of inferring identities or status."""
    market, scan_id = validate_arguments({"market": market, "scan_id": scan_id})
    expected = {
        "scan_id": scan_id, "scan_status": "completed",
        "scan_universe_type": "market", "scan_market": market,
        "run_status": "published", "run_market": market,
        "run_universe_market": market, "run_universe_type": "market",
        "pointer_key": f"latest_published_market:{market}",
    }
    if not isinstance(row, dict) or any(row.get(k) != v for k, v in expected.items()):
        raise PublicationUnavailable("Publication metadata missing or inconsistent")
    run_id = row.get("feature_run_id")
    if (
        type(run_id) is not int or run_id <= 0
        or type(row.get("pointer_run_id")) is not int
        or row["pointer_run_id"] != run_id
    ):
        raise PublicationUnavailable("Scan does not reference the current market publication")
    as_of = row.get("as_of_date")
    published_at, completed_at = row.get("published_at"), row.get("completed_at")
    if type(as_of) is not date:
        raise PublicationUnavailable("Published run date missing")
    if not isinstance(published_at, datetime) or published_at.utcoffset() is None:
        raise PublicationUnavailable("Published run timestamp missing")
    if completed_at is not None and (
        not isinstance(completed_at, datetime) or completed_at.utcoffset() is None
    ):
        raise PublicationUnavailable("Invalid completion timestamp")
    return {
        "source": "xang_publication_metadata", "proof_status": "ok",
        "market": market, "scan_id": scan_id, "scan_status": "completed",
        "checked_at": datetime.now(UTC).isoformat(),
        "run": {
            "id": run_id, "status": "published", "market": market,
            "universe_type": "market", "pointer_key": expected["pointer_key"],
            "as_of_date": as_of.isoformat(), "published_at": published_at.isoformat(),
            "completed_at": completed_at.isoformat() if completed_at else None,
        },
    }


class SqlPublicationStore:
    """Bounded read-only transactions using a dedicated metadata-only DSN."""

    def __init__(self, dsn: str, connect_factory: Any = None) -> None:
        self._dsn = dsn
        self._connect_factory = connect_factory

    def lookup(self, market: str, scan_id: str) -> dict[str, Any]:
        market, scan_id = validate_arguments({"market": market, "scan_id": scan_id})
        import psycopg
        from psycopg.rows import dict_row

        connect = self._connect_factory or psycopg.connect
        with connect(
            self._dsn, connect_timeout=5,
            options="-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000",
        ) as connection:
            connection.read_only = True
            with connection.cursor(row_factory=dict_row) as cursor:
                cursor.execute(SQL_PUBLICATION, (f"latest_published_market:{market}", scan_id))
                rows = cursor.fetchall()
        if len(rows) != 1:
            raise PublicationUnavailable("Scan publication is not unique or does not exist")
        return build_proof(dict(rows[0]), market, scan_id)


async def publication_result(request_id: Any, arguments: Any, provider: PublicationProvider, timeout: float) -> dict[str, Any]:
    """Keep driver errors and credentials out of tool responses."""
    import asyncio

    from .rpc import INVALID_PARAMS, error_response

    try:
        market, scan_id = validate_arguments(arguments)
    except ValueError as exc:
        return error_response(request_id, INVALID_PARAMS, str(exc))
    try:
        facts = await asyncio.wait_for(asyncio.to_thread(provider.lookup, market, scan_id), timeout=timeout)
    except Exception:
        return {
            "jsonrpc": "2.0", "id": request_id, "result": {
                "isError": True,
                "content": [{"type": "text", "text": "Publication proof unavailable; no fallback was used."}],
            },
        }
    import json

    return {
        "jsonrpc": "2.0", "id": request_id, "result": {
            "structuredContent": facts,
            "content": [{"type": "text", "text": json.dumps(facts, ensure_ascii=False)}],
            "isError": False,
        },
    }

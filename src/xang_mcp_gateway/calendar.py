"""Canonical XANG market-calendar facts bridge (read-only, no calendar logic).

Phase 7: ``market_calendar_status`` exposes the single fact Hermes needs to
evaluate a published XANG run's freshness deterministically:

    expected_trading_date = market_date  if is_trading_day  else  last_completed_trading_day

The weekend/holiday judgment ALWAYS comes from the canonical
``app.services.market_calendar_service.MarketCalendarService`` that ships with
the xang1234 backend image (copied verbatim into this image at build time, see
the Dockerfile's ``xang-backend`` stage). This module must never grow weekday,
holiday, ``date.today()`` or SQL calendar logic. If the canonical service
cannot be loaded or questioned, the tool answers with an explicit error
envelope (fail closed) — Hermes treats UNKNOWN as not buyable. Trading
decisions (fresh/stale) are NOT made here; that stays in Hermes.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from typing import Any

CALENDAR_SOURCE = "xang_market_calendar_service"
CALENDAR_TOOL_NAME = "market_calendar_status"
DEFAULT_CALENDAR_APP_ROOT = "/opt/xang_backend"

CALENDAR_TOOL_DESCRIPTOR: dict[str, Any] = {
    "name": CALENDAR_TOOL_NAME,
    "description": (
        "Canonical XANG market calendar facts (market_now, market_date, "
        "is_trading_day, last_completed_trading_day, expected_trading_date) "
        "for run-freshness checks. Read-only; no trading decisions."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "market": {
                "type": "string",
                "default": "US",
                "description": "Market code; US supported.",
            }
        },
        "additionalProperties": False,
    },
}


def _load_market_calendar_service(backend_app_root: str) -> type:
    """Load the CANONICAL ``MarketCalendarService`` from the backend code tree."""
    root = backend_app_root.rstrip("/")
    if not root:
        raise ImportError("empty calendar backend app root")
    if root not in sys.path:
        sys.path.insert(0, root)
    # The canonical backend tree's module-level Settings() loader requires
    # DATABASE_URL to be PRESENT. Nothing in the calendar path ever connects
    # to a database and redis stays optional (backend import guard →
    # non-Redis path), so a harmless placeholder satisfies only the loader.
    # This is the gateway-side import shim; it contains NO calendar logic.
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql://xang-mcp-gateway@127.0.0.1/xang_calendar_placeholder",
    )
    from app.services.market_calendar_service import MarketCalendarService

    return MarketCalendarService


def calendar_facts(
    market: str = "US", backend_app_root: str = DEFAULT_CALENDAR_APP_ROOT
) -> dict[str, Any]:
    """Return canonical calendar facts for one market (mirror of backend logic).

    Semantics agreed for Phase 7 (matches the live-verified backend behavior):

    - ``market_now`` / ``market_date`` come from the service (market timezone).
    - ``last_completed_trading_day`` is the service's own notion.
    - ``expected_trading_date`` is ``market_date`` when the market is open /
      trading today (even mid-session), else ``last_completed_trading_day``.
    - No ratings, no runs, no fresh/stale verdicts — only the calendar fact.
    """
    market = (market or "US").upper()
    MarketCalendarService = _load_market_calendar_service(backend_app_root)
    cal = MarketCalendarService()
    market_now = cal.market_now(market)
    market_date = market_now.date()
    is_trading_day = bool(cal.is_trading_day(market, market_date))
    last_completed = cal.last_completed_trading_day(market)
    expected = market_date if is_trading_day else last_completed
    return {
        "market": market,
        "market_timezone": str(cal.market_timezone(market)),
        "market_now": market_now.isoformat(),
        "market_date": market_date.isoformat(),
        "is_trading_day": is_trading_day,
        "last_completed_trading_day": last_completed.isoformat(),
        "expected_trading_date": expected.isoformat(),
        "calendar_source": CALENDAR_SOURCE,
        "generated_at": datetime.now(UTC).isoformat(),
    }


def calendar_tool_result(
    market: str, backend_app_root: str = DEFAULT_CALENDAR_APP_ROOT
) -> dict[str, Any]:
    """MCP ``result`` object for tools/call (text mirror + structuredContent)."""
    try:
        payload: dict[str, Any] = calendar_facts(market, backend_app_root)
    except Exception as exc:  # noqa: BLE001 - fail closed, never guess
        payload = {
            "market": market or "US",
            "calendar_status": "error",
            "expected_trading_date": None,
            "calendar_source": CALENDAR_SOURCE,
            "error": str(exc) or type(exc).__name__,
        }
    return {
        "content": [{"type": "text", "text": _json_text(payload)}],
        "structuredContent": payload,
    }


def calendar_jsonrpc_result(
    request_id: Any, market: str, backend_app_root: str = DEFAULT_CALENDAR_APP_ROOT
) -> dict[str, Any]:
    """Full JSON-RPC response for the local calendar tool."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": calendar_tool_result(market, backend_app_root),
    }


def _json_text(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False)

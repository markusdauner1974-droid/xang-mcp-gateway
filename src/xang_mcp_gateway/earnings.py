"""Read-only earnings-calendar lookup served by the gateway itself.

Phase 8: ``earnings_lookup`` answers one question Hermes asks before it sizes a
position: *is the stored earnings observation still evidence?*

The gateway owns NO freshness rule of its own. The canonical XANG rules live in
``app.services.yahoo_earnings_calendar`` (14-day observation window, 7-day
producer refresh, 3-day future tolerance) and that module is copied verbatim
into this image from the backend image (Dockerfile stage ``xang-backend``). This
file imports it exactly the way ``calendar.py`` imports the market-calendar
service: same source tree, no second implementation, no re-derived thresholds.

Three things this module refuses to do:

* fill a gap. A missing row, a missing observation date or an unreadable date
  stays missing and is named in ``reasons``.
* guess a confirmation. ``stock_fundamentals`` carries no confirmation proof, so
  ``date_confirmation`` is ``unknown`` - never ``confirmed``, never ``estimated``
  from thin air.
* write. One parameterised ``SELECT``; the connection is switched to read-only
  when the driver supports it, and every database problem answers fail-closed
  with ``unknown`` rather than a plausible-looking ``ok``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, date, datetime
from typing import Any

logger = logging.getLogger("xang_mcp_gateway.earnings")

EARNINGS_TOOL_NAME = "earnings_lookup"
EARNINGS_SOURCE = "xang_yahoo_calendar"
DEFAULT_BACKEND_APP_ROOT = "/opt/xang_backend"

# Symbols accepted by the lookup. XANG trades US listings: letters, digits, and
# the two separators that occur in real tickers (BRK.B, RDS-A). The dot and the
# hyphen are escaped inside the class; anchors make a partial match impossible.
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")

# Deliberately one statement, one parameter, no string building. The column list
# is the exact three the xang_ro grant covers.
_SQL_LOOKUP = (
    "SELECT symbol, next_earnings_date, event_calendar_as_of_date "
    "FROM public.stock_fundamentals WHERE symbol = %s LIMIT 1"
)

EARNINGS_TOOL_DESCRIPTOR: dict[str, Any] = {
    "name": EARNINGS_TOOL_NAME,
    "description": (
        "Read-only earnings-calendar observation for one symbol from "
        "public.stock_fundamentals: next_earnings_date, "
        "event_calendar_as_of_date, freshness_status, date_confirmation and "
        "machine-readable reasons. Missing or stale facts are named, never "
        "filled in; no trading decision is made here."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": "US ticker symbol, e.g. AAPL or BRK.B.",
            }
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
}


def normalize_symbol(raw: Any) -> str | None:
    """Return the canonical symbol, or ``None`` when it is not a valid ticker."""
    if not isinstance(raw, str):
        return None
    symbol = raw.strip().upper()
    return symbol if _SYMBOL_RE.match(symbol) else None


def _load_freshness_rules(backend_app_root: str) -> Any:
    """Import the CANONICAL freshness module from the backend source tree.

    Same mechanism as ``calendar._load_market_calendar_service``: put the
    backend root on ``sys.path`` and satisfy the backend settings loader with a
    placeholder ``DATABASE_URL`` (this path never connects). If the tree is
    missing the caller gets an ImportError and the tool answers ``unknown`` -
    it must never fall back to locally re-derived thresholds.
    """
    root = backend_app_root.rstrip("/")
    if not root:
        raise ImportError("empty earnings backend app root")
    if root not in sys.path:
        sys.path.insert(0, root)
    os.environ.setdefault(
        "DATABASE_URL",
        "postgresql://xang-mcp-gateway@127.0.0.1/xang_earnings_placeholder",
    )
    from app.services import yahoo_earnings_calendar

    return yahoo_earnings_calendar


def _as_date(value: Any) -> date | None:
    """Strict date parsing: ``None`` on anything that is not a real date."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _fetch_row(symbol: str, dsn: str, connect_factory: Any = None) -> dict[str, Any] | None:
    """One read-only, parameterised lookup. Raises on any database problem."""
    if connect_factory is not None:
        conn = connect_factory()
    else:
        import psycopg

        conn = psycopg.connect(dsn)
    with conn:
        # Belt and suspenders: a SELECT cannot write, but a read-only
        # transaction also stops a compromised DSN from doing anything else.
        try:
            conn.read_only = True
        except Exception:  # noqa: BLE001 - driver/older psycopg: SELECT is still read-only
            pass
        try:
            from psycopg.rows import dict_row
        except ImportError:  # pragma: no cover - installed via dependencies
            dict_row = None
        if dict_row is not None:
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute(_SQL_LOOKUP, (symbol,))
                return cursor.fetchone()
        with conn.cursor() as cursor:
            cursor.execute(_SQL_LOOKUP, (symbol,))
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(zip(("symbol", "next_earnings_date", "event_calendar_as_of_date"), row, strict=False))


def _base_envelope(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "next_earnings_date": None,
        "event_calendar_as_of_date": None,
        "source": EARNINGS_SOURCE,
        "freshness_status": "unknown",
        "date_confirmation": "unknown",
        "available": False,
        "reasons": [],
        "generated_at": datetime.now(UTC).isoformat(),
    }


def earnings_facts(
    symbol: Any,
    dsn: str | None,
    *,
    reference_date: date | None = None,
    backend_app_root: str = DEFAULT_BACKEND_APP_ROOT,
    connect_factory: Any = None,
) -> dict[str, Any]:
    """Assess the stored earnings observation for one symbol.

    ``reference_date`` is the day freshness is measured against. Production
    passes nothing and the canonical module uses today (UTC); tests pass a
    literal so the answer is reproducible.
    """
    normalized = normalize_symbol(symbol)
    if normalized is None:
        envelope = _base_envelope(str(symbol) if symbol is not None else "")
        envelope["freshness_status"] = "invalid"
        envelope["reasons"].append("invalid_symbol")
        return envelope

    envelope = _base_envelope(normalized)

    # No DSN means no observation at all. Checked before the rules import: the
    # answer "not configured" does not depend on a backend source tree.
    if not dsn:
        envelope["reasons"].append("database_not_configured")
        return envelope

    try:
        rules = _load_freshness_rules(backend_app_root)
    except Exception as exc:  # noqa: BLE001 - fail closed, never guess a window
        logger.warning("earnings freshness rules unavailable: %s", type(exc).__name__)
        envelope["freshness_status"] = "invalid"
        envelope["reasons"].append("freshness_rules_unavailable")
        return envelope

    envelope["event_calendar_max_age_days"] = rules.EVENT_CALENDAR_MAX_AGE_DAYS
    envelope["event_calendar_producer_refresh_after_days"] = (
        rules.EVENT_CALENDAR_PRODUCER_REFRESH_AFTER_DAYS
    )
    envelope["event_calendar_future_tolerance_days"] = (
        rules.EVENT_CALENDAR_FUTURE_TOLERANCE_DAYS
    )

    try:
        row = _fetch_row(normalized, dsn, connect_factory)
    except Exception as exc:  # noqa: BLE001 - fail closed on every driver error
        logger.warning("earnings lookup failed for %s: %s", normalized, type(exc).__name__)
        envelope["reasons"].append("database_unavailable")
        return envelope

    if row is None:
        envelope["reasons"].append("symbol_not_found")
        return envelope

    observed_raw = row.get("event_calendar_as_of_date")
    observed = _as_date(observed_raw)
    if observed_raw is None:
        envelope["reasons"].append("observation_date_missing")
        return envelope
    if observed is None:
        envelope["freshness_status"] = "invalid"
        envelope["reasons"].append("observation_date_invalid")
        return envelope

    envelope["event_calendar_as_of_date"] = observed.isoformat()
    reference = reference_date or datetime.now(UTC).date()
    age_days = (reference - observed).days
    envelope["observation_age_days"] = age_days

    # The canonical rule decides; the constants only explain WHICH side of the
    # window was missed. No threshold is re-derived here.
    fresh = rules.is_event_calendar_observation_fresh(observed, reference_date=reference)
    if not fresh:
        if age_days > rules.EVENT_CALENDAR_MAX_AGE_DAYS:
            envelope["freshness_status"] = "stale"
            envelope["reasons"].append("observation_stale")
        else:
            envelope["freshness_status"] = "invalid"
            envelope["reasons"].append("observation_date_in_future")
        return envelope

    envelope["freshness_status"] = "ok"

    stored_next, is_evidence = rules.persisted_next_earnings_date(
        {
            "event_calendar_as_of_date": observed,
            "next_earnings_date": row.get("next_earnings_date"),
        },
        as_of_date=reference,
    )

    if not is_evidence:
        # A fresh observation whose stored date is unusable. The canonical
        # helper folds "unreadable" and "in the past" into one False; the two
        # are different answers for a consumer, so name them apart.
        raw_next = row.get("next_earnings_date")
        parsed_next = _as_date(raw_next)
        envelope["freshness_status"] = "invalid"
        if raw_next is None:
            envelope["reasons"].append("earnings_date_missing")
        elif parsed_next is None:
            envelope["reasons"].append("earnings_date_unreadable")
        elif parsed_next < reference:
            envelope["reasons"].append("earnings_date_in_past")
        else:  # pragma: no cover - the canonical helper would have accepted it
            envelope["reasons"].append("earnings_date_unusable")
        return envelope

    if stored_next is None:
        # A fresh observation of "nothing upcoming" IS evidence - it says the
        # calendar knows of no date inside the window. It is not a date.
        envelope["available"] = True
        envelope["reasons"].append("no_upcoming_earnings_date")
        envelope["reasons"].append("date_confirmation_unrecorded")
        return envelope

    envelope["next_earnings_date"] = stored_next.isoformat()
    envelope["available"] = True
    # The table records no confirmation proof. "estimated" would be an
    # assertion about the provider; "unknown" is what we can actually say.
    envelope["date_confirmation"] = "unknown"
    envelope["reasons"].append("date_confirmation_unrecorded")
    return envelope


def earnings_tool_result(
    symbol: Any,
    dsn: str | None,
    *,
    reference_date: date | None = None,
    backend_app_root: str = DEFAULT_BACKEND_APP_ROOT,
    connect_factory: Any = None,
) -> dict[str, Any]:
    """MCP ``result`` object for tools/call (text mirror + structuredContent)."""
    payload = earnings_facts(
        symbol,
        dsn,
        reference_date=reference_date,
        backend_app_root=backend_app_root,
        connect_factory=connect_factory,
    )
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
    }


def earnings_jsonrpc_result(
    request_id: Any,
    symbol: Any,
    dsn: str | None,
    *,
    reference_date: date | None = None,
    backend_app_root: str = DEFAULT_BACKEND_APP_ROOT,
    connect_factory: Any = None,
) -> dict[str, Any]:
    """Full JSON-RPC response for the local earnings tool."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": earnings_tool_result(
            symbol,
            dsn,
            reference_date=reference_date,
            backend_app_root=backend_app_root,
            connect_factory=connect_factory,
        ),
    }

"""Configuration and tool surface for the XANG MCP gateway."""

from __future__ import annotations

import hmac
import math
import os
from dataclasses import dataclass

DEFAULT_BACKEND_MCP_URL = "http://xang1234-backend-1:8000/mcp/"


class GatewayConfigError(ValueError):
    """Raised when required settings are missing or contradictory."""


# The 13 tools the backend MCP server exposes (live-verified 2026-10-15).
ALL_BACKEND_TOOLS: frozenset[str] = frozenset(
    {
        "breadth_snapshot",
        "compare_feature_runs",
        "daily_digest",
        "explain_symbol",
        "find_candidates",
        "group_rankings",
        "market_overview",
        "stock_lookup",
        "stock_snapshot",
        "task_status",
        "theme_state",
        "watchlist_add",
        "watchlist_snapshot",
    }
)

# Write tool stays behind the gateway by default (MCP_WATCHLIST_WRITES_ENABLED
# is already false upstream; belt and suspenders).
DEFAULT_EXCLUDED_TOOLS: frozenset[str] = frozenset({"watchlist_add"})

# Method-level passthrough surface: everything else is answered with
# JSON-RPC -32601 without touching the backend.
ALLOWED_METHODS: frozenset[str] = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "ping",
        "tools/list",
        "tools/call",
    }
)

# Tools whose responses may be enriched with feature-store facts.
ENRICHABLE_TOOLS: frozenset[str] = frozenset({"find_candidates"})

# Phase 7: local (non-passthrough) tools served by the gateway itself.
# market_calendar_status exposes CANONICAL xang calendar facts (read-only);
# the weekend/holiday judgment never lives in this codebase — see calendar.py.
LOCAL_TOOLS: frozenset[str] = frozenset({"market_calendar_status"})

DEFAULT_CALENDAR_APP_ROOT = "/opt/xang_backend"


@dataclass(frozen=True)
class GatewaySettings:
    """Runtime settings of the gateway (fail-fast validated)."""

    backend_mcp_url: str
    backend_token: str
    gateway_token: str
    excluded_tools: frozenset[str] = DEFAULT_EXCLUDED_TOOLS
    request_timeout_seconds: float = 120.0
    db_dsn: str | None = None
    calendar_app_root: str = DEFAULT_CALENDAR_APP_ROOT

    def __post_init__(self) -> None:
        if not math.isfinite(self.request_timeout_seconds) or self.request_timeout_seconds <= 0:
            raise GatewayConfigError("request_timeout_seconds must be a finite positive number")
        url = self.backend_mcp_url.strip()
        if not url:
            raise GatewayConfigError("backend_mcp_url must not be empty")
        # The backend router is mounted under /mcp and answers /mcp only with a
        # 307 redirect (verified live): always normalize onto the trailing slash.
        normalized = url.rstrip("/")
        if not normalized.rstrip("/").endswith("/mcp"):
            normalized = normalized.rstrip("/") + "/mcp"
        object.__setattr__(self, "backend_mcp_url", normalized + "/")

        if not self.backend_token:
            raise GatewayConfigError("backend_token must not be empty")
        if not self.gateway_token:
            raise GatewayConfigError("gateway_token must not be empty")
        if hmac_equal(self.gateway_token, self.backend_token):
            raise GatewayConfigError("gateway_token must differ from backend_token")

    @property
    def backend_visible_tools(self) -> frozenset[str]:
        return ALL_BACKEND_TOOLS - self.excluded_tools

    @property
    def allowed_tools(self) -> frozenset[str]:
        # Local tools are served by the gateway itself and participate in the
        # same allowlist surface (tools/list + tools/call gating).
        return (ALL_BACKEND_TOOLS - self.excluded_tools) | LOCAL_TOOLS

    @property
    def enrichment_enabled(self) -> bool:
        return bool(self.db_dsn)

    @property
    def enrichable_tools(self) -> frozenset[str]:
        return ENRICHABLE_TOOLS & self.allowed_tools

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> GatewaySettings:
        source = os.environ if env is None else env
        excluded = DEFAULT_EXCLUDED_TOOLS
        raw_excluded = source.get("GATEWAY_EXCLUDED_TOOLS")
        if raw_excluded is not None:
            excluded = frozenset(t.strip() for t in raw_excluded.split(",") if t.strip())
        return cls(
            backend_mcp_url=source.get("XANG_MCP_URL", DEFAULT_BACKEND_MCP_URL),
            backend_token=source.get("XANG_BACKEND_TOKEN", ""),
            gateway_token=source.get("GATEWAY_TOKEN", ""),
            excluded_tools=excluded,
            request_timeout_seconds=_parse_timeout(source.get("GATEWAY_REQUEST_TIMEOUT", "120")),
            db_dsn=source.get("XANG_DB_DSN") or None,
            calendar_app_root=source.get(
                "XANG_CALENDAR_BACKEND_APP_ROOT", DEFAULT_CALENDAR_APP_ROOT
            ),
        )


def _parse_timeout(raw: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise GatewayConfigError("GATEWAY_REQUEST_TIMEOUT must be a number") from exc


def hmac_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))

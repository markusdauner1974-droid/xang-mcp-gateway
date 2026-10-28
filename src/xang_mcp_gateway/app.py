"""FastAPI application: MCP passthrough gateway with allowlist and enrichment.

Endpoint contract:
- POST /mcp/ — authenticated JSON-RPC (Streamable HTTP). Auth failures are
  HTTP 401; malformed JSON-RPC keeps HTTP 200 with a JSON-RPC error envelope
  (mirrors the backend's transport behavior). Backend outages answer with
  JSON-RPC -32000 instead of tearing down the protocol surface.
- GET /healthz — local liveness only, NO backend call (a coupled healthcheck
  would mark the container unhealthy whenever the screener backend restarts;
  same rationale as the paperbroker phase-5 judge decision).
- GET /readyz — active readiness: backend tools/list probe plus, when SQL
  enrichment is configured, a database ping.

Phase 7 addition: `market_calendar_status` is a LOCAL tool (no backend round
trip) that surfaces canonical XANG calendar facts read-only; see calendar.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

import xang_mcp_gateway as _pkg

from . import auth as auth_module
from .calendar import (
    CALENDAR_TOOL_DESCRIPTOR,
    CALENDAR_TOOL_NAME,
    calendar_jsonrpc_result,
)
from .config import ALLOWED_METHODS, GatewaySettings
from .earnings import (
    EARNINGS_TOOL_DESCRIPTOR,
    EARNINGS_TOOL_NAME,
    earnings_jsonrpc_result,
)
from .enrichment import ENRICHMENT_FIELD_COUNT, FactsProvider, SqlFeatureStore, enrich_target
from .rpc import (
    GATEWAY_DOWNSTREAM_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    BackendUnavailable,
    Transport,
    error_response,
)

logger = logging.getLogger("xang_mcp_gateway")


def _marker(status: str, **extra: Any) -> dict[str, Any]:
    marker = {"enrichment": status, "schema": "pr537-candidate-facts"}
    marker.update(extra)
    return marker


def _apply_marker(target: dict[str, Any], marker: dict[str, Any]) -> None:
    target["gateway"] = marker


async def _enrich_result(result: dict[str, Any], provider: FactsProvider) -> None:
    """Enrich a tools/call result in place; never raise (fail-closed markers).

    Blocking provider calls run in a worker thread (asyncio.to_thread) so a slow
    or unreachable Postgres cannot stall the event loop — keeping /healthz live.
    """
    structured = result.get("structuredContent")
    text_entry: dict[str, Any] | None = None
    text_target: dict[str, Any] | None = None
    contents = result.get("content")
    if isinstance(contents, list):
        for entry in contents:
            if isinstance(entry, dict) and entry.get("type") == "text" and isinstance(
                entry.get("text"), str
            ):
                text_entry = entry
                try:
                    text_target = json.loads(entry["text"])
                except json.JSONDecodeError:
                    logger.warning("content[0].text is not JSON; enrichment skipped")
                    return
                break

    base_target = structured if isinstance(structured, dict) else text_target
    if base_target is None:
        return
    symbols = base_target.get("symbols")
    if not isinstance(symbols, list):
        return  # not a candidates-shaped response: pass through untouched
    requested = [item.get("symbol") for item in symbols if isinstance(item, dict) and item.get("symbol")]
    if not requested:
        return

    run = base_target.get("run")
    run_id = run.get("id") if isinstance(run, dict) else None
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        # Facts of a different (newer) published run could mismatch this
        # response; skip instead of risking a run-id mismatch.
        logger.warning("response run id missing or invalid; enrichment skipped")
        skip = _marker("skipped", reason="response run id missing or invalid")
        if isinstance(structured, dict):
            _apply_marker(structured, skip)
        if isinstance(text_target, dict) and text_entry is not None:
            _apply_marker(text_target, skip)
            text_entry["text"] = json.dumps(text_target, ensure_ascii=False)
        return

    try:
        facts_by_symbol = await asyncio.to_thread(provider.facts_for, requested, run_id)
    except Exception as exc:  # noqa: BLE001 - enrichment must never break passthrough
        logger.warning("fact enrichment failed: %s", exc)
        failure = _marker("unavailable", reason="feature-store read failed")
        if isinstance(structured, dict):
            _apply_marker(structured, failure)
        if isinstance(text_target, dict) and text_entry is not None:
            _apply_marker(text_target, failure)
            text_entry["text"] = json.dumps(text_target, ensure_ascii=False)
        return

    if isinstance(structured, dict):
        enriched_count = enrich_target(structured, facts_by_symbol)
        _apply_marker(structured, _marker("applied", fields=ENRICHMENT_FIELD_COUNT,
                                          symbols_enriched=enriched_count,
                                          symbols_missing=len(requested) - enriched_count))
    if isinstance(text_target, dict) and text_entry is not None:
        text_enriched = enrich_target(text_target, facts_by_symbol)
        _apply_marker(text_target, _marker("applied", fields=ENRICHMENT_FIELD_COUNT,
                                           symbols_enriched=text_enriched,
                                           symbols_missing=len(requested) - text_enriched))
        text_entry["text"] = json.dumps(text_target, ensure_ascii=False)


def create_app(
    settings: GatewaySettings,
    transport: Transport | None = None,
    facts_provider: FactsProvider | None = None,
) -> FastAPI:
    if transport is None:
        from .rpc import HttpxTransport

        transport = HttpxTransport(settings)
    if not settings.candidate_enrichment:
        facts_provider = None
    elif facts_provider is None and settings.enrichment_enabled:
        facts_provider = SqlFeatureStore(settings.db_dsn)  # type: ignore[arg-type]

    app = FastAPI(title="XANG MCP Gateway", version=_pkg.__version__, docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Deliberately no backend/database access here.
        return JSONResponse(
            {
                "status": "ok",
                "version": _pkg.__version__,
                "enrichment": "enabled" if facts_provider else "disabled",
                "tools_visible": len(settings.allowed_tools),
            }
        )

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        checks: dict[str, Any] = {}
        try:
            status, payload = await transport.post_jsonrpc(
                {"jsonrpc": "2.0", "id": "readyz", "method": "tools/list"}
            )
            jsonrpc_result = payload.get("result") if isinstance(payload, dict) else None
            tools = jsonrpc_result.get("tools") if isinstance(jsonrpc_result, dict) else None
            backend_tools = [
                tool.get("name")
                for tool in (tools if isinstance(tools, list) else [])
                if isinstance(tool, dict)
            ]
            visible = [name for name in backend_tools if name in settings.allowed_tools]
            # A JSON-RPC error body arrives with HTTP 200: only a dictionary result
            # counts as a successful probe (readiness, not status-code guessing).
            # A tools/list probe without a list-shaped tools block is also broken.
            checks["backend_mcp"] = {
                "ok": status == 200 and isinstance(jsonrpc_result, dict) and isinstance(tools, list),
                "tools_visible": len(visible),
            }
        except BackendUnavailable as exc:
            checks["backend_mcp"] = {"ok": False, "error": str(exc)}
        if facts_provider is not None:
            try:
                db_ok = await asyncio.to_thread(facts_provider.ping)
                checks["feature_store"] = {"ok": bool(db_ok)}
            except Exception as exc:  # noqa: BLE001 - readiness must stay robust
                checks["feature_store"] = {"ok": False, "error": str(exc)}
        ok = all(check.get("ok", False) for check in checks.values())
        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ready" if ok else "degraded", "checks": checks},
        )

    @app.post("/mcp/")
    async def mcp(request: Request) -> Response:
        if not auth_module.is_authorized(request.headers, settings.gateway_token):
            return JSONResponse(status_code=401, content={"detail": "Gateway authentication required"})

        try:
            raw = await request.body()
            body = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse(
                content=error_response(None, PARSE_ERROR, "Parse error: invalid JSON"),
                status_code=200,
            )
        if not isinstance(body, dict):
            return JSONResponse(
                content=error_response(None, INVALID_REQUEST, "Invalid Request: expected a JSON object"),
                status_code=200,
            )

        request_id = body.get("id")
        method = body.get("method")
        if not isinstance(method, str) or method not in ALLOWED_METHODS:
            return JSONResponse(
                content=error_response(request_id, METHOD_NOT_FOUND, f"Method not allowed by gateway: {method}")
            )

        tool_name: str | None = None
        params = body.get("params")
        if method == "tools/call":
            if isinstance(params, dict) and isinstance(params.get("name"), str):
                tool_name = params["name"]
            if tool_name is None or tool_name not in settings.allowed_tools:
                return JSONResponse(
                    content=error_response(
                        request_id,
                        INVALID_PARAMS,
                        f"Tool not allowed by gateway allowlist: {tool_name}",
                    )
                )

        if method == "tools/call" and tool_name == CALENDAR_TOOL_NAME:
            # Phase 7: local canonical-calendar tool — answered here, WITHOUT a
            # backend round trip. All failure modes answer explicit error
            # envelopes (fail closed); see calendar.py for the contract.
            args = params.get("arguments") if isinstance(params, dict) else None
            market = args.get("market", "US") if isinstance(args, dict) else "US"
            if not isinstance(market, str) or not market.strip():
                return JSONResponse(
                    content=error_response(
                        request_id, INVALID_PARAMS, "Invalid params: market must be a non-empty string"
                    )
                )
            return JSONResponse(
                content=calendar_jsonrpc_result(request_id, market.strip(), settings.calendar_app_root)
            )

        if method == "tools/call" and tool_name == EARNINGS_TOOL_NAME:
            # Phase 8: local earnings tool - one read-only SELECT, no backend
            # round trip. Contract-level failures (missing symbol argument)
            # are JSON-RPC errors; data-level ones are answered as facts.
            args = params.get("arguments") if isinstance(params, dict) else None
            symbol = args.get("symbol") if isinstance(args, dict) else None
            if not isinstance(symbol, str) or not symbol.strip():
                return JSONResponse(
                    content=error_response(
                        request_id, INVALID_PARAMS, "Invalid params: symbol must be a non-empty string"
                    )
                )
            # The lookup is synchronous by nature (psycopg). Running it on the
            # event loop would freeze every other request - including /healthz -
            # for the duration of a TCP timeout, so it goes to a worker thread
            # (same pattern as the enrichment and calendar calls).
            return JSONResponse(
                content=await asyncio.to_thread(
                    earnings_jsonrpc_result,
                    request_id,
                    symbol,
                    settings.db_dsn,
                    backend_app_root=settings.calendar_app_root,
                )
            )

        try:
            status, payload = await transport.post_jsonrpc(body)
        except BackendUnavailable as exc:
            logger.warning("backend unavailable for %s: %s", method, exc)
            return JSONResponse(
                content=error_response(request_id, GATEWAY_DOWNSTREAM_ERROR, f"Backend unavailable: {exc}")
            )
        if status == 202 or payload is None:
            return Response(status_code=202)

        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict) and method == "tools/list":
            tools = result.get("tools")
            if isinstance(tools, list):
                filtered = [
                    tool
                    for tool in tools
                    if isinstance(tool, dict) and tool.get("name") in settings.allowed_tools
                ]
                # Phase 7: advertise the local calendar tool alongside the
                # backend passthrough tools.
                filtered.append(CALENDAR_TOOL_DESCRIPTOR)
                # Phase 8: and the local earnings tool.
                filtered.append(EARNINGS_TOOL_DESCRIPTOR)
                result["tools"] = filtered
        if (
            facts_provider is not None
            and method == "tools/call"
            and tool_name in settings.enrichable_tools
            and isinstance(result, dict)
            and "error" not in payload
        ):
            # Enrichment mutates payload["result"] in place (markers + text mirror);
            # it never raises and never replaces the passthrough payload.
            await _enrich_result(result, facts_provider)
        return JSONResponse(content=payload)

    return app

"""Authenticated local MCP calendar service without backend or SQL transport."""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

import xang_mcp_gateway as _pkg

from .auth import is_authorized
from .calendar import CALENDAR_TOOL_DESCRIPTOR, CALENDAR_TOOL_NAME, calendar_jsonrpc_result
from .config import GatewaySettings
from .rpc import INVALID_PARAMS, INVALID_REQUEST, METHOD_NOT_FOUND, PARSE_ERROR, error_response


# The readiness probe is the one endpoint an unauthenticated caller can repeat,
# and every call runs the canonical service in a worker thread. A short cache
# plus a single-flight lock keeps that traffic off the executor that serves /mcp/.
PROBE_TTL_SECONDS = 10.0
PROBE_TIMEOUT_SECONDS = 5.0


def create_calendar_app(settings: GatewaySettings) -> FastAPI:
    probe_lock = asyncio.Lock()
    probe_cache: list[tuple[float, bool]] = []
    # Its own single-worker pool: a hung evaluation occupies this thread and
    # nothing else, so /mcp/ keeps its share of the default executor.
    probe_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="calendar-probe")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        # wait=False: a hung probe thread must not delay shutdown.
        probe_executor.shutdown(wait=False)

    app = FastAPI(
        title="XANG MCP Calendar",
        version=_pkg.__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    async def calendar_ready() -> bool:
        async with probe_lock:
            if probe_cache and time.monotonic() - probe_cache[0][0] < PROBE_TTL_SECONDS:
                return probe_cache[0][1]
            loop = asyncio.get_running_loop()
            try:
                payload = await asyncio.wait_for(
                    loop.run_in_executor(
                        probe_executor,
                        calendar_jsonrpc_result,
                        "readyz",
                        "US",
                        settings.calendar_app_root,
                    ),
                    timeout=PROBE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                # Fail closed and cache it: an unbounded wait would hold the lock
                # and queue every later probe behind it.
                probe_cache[:] = [(time.monotonic(), False)]
                return False
            facts = payload.get("result", {}).get("structuredContent", {})
            ok = isinstance(facts.get("expected_trading_date"), str) and bool(facts["expected_trading_date"])
            probe_cache[:] = [(time.monotonic(), ok)]
            return ok

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({
            "status": "ok", "version": _pkg.__version__, "mode": "calendar_only",
            "enrichment": "disabled", "tools_visible": 1,
        })

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Answer whether the canonical calendar works, refreshed at most once per TTL.

        Stays unauthenticated so an orchestrator can probe it; the cached verdict
        means repeated probes cost nothing and a slow canonical service cannot be
        multiplied into the shared worker pool.
        """
        ok = await calendar_ready()
        return JSONResponse(status_code=200 if ok else 503, content={
            "status": "ready" if ok else "degraded",
            "checks": {"market_calendar": {"ok": ok}},
        })

    @app.post("/mcp/")
    async def mcp(request: Request) -> Response:
        if not is_authorized(request.headers, settings.gateway_token):
            return JSONResponse(status_code=401, content={"detail": "Gateway authentication required"})
        try:
            body = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(content=error_response(None, PARSE_ERROR, "Parse error: invalid JSON"))
        if not isinstance(body, dict):
            return JSONResponse(content=error_response(None, INVALID_REQUEST, "Invalid Request: expected an object"))
        request_id = body.get("id")
        method = body.get("method")
        if body.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return JSONResponse(content=error_response(request_id, INVALID_REQUEST, "Invalid JSON-RPC request"))
        params = body.get("params", {})
        if not isinstance(params, dict):
            return JSONResponse(content=error_response(request_id, INVALID_PARAMS, "Params must be an object"))
        if method == "notifications/initialized" and "id" not in body:
            return Response(status_code=202)
        if method == "initialize":
            version = params.get("protocolVersion")
            if version not in ("2025-03-26", "2025-06-18"):
                version = "2025-03-26"
            result = {
                "protocolVersion": version, "capabilities": {"tools": {}},
                "serverInfo": {"name": "xang-mcp-calendar", "version": _pkg.__version__},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [CALENDAR_TOOL_DESCRIPTOR]}
        elif method == "tools/call":
            if params.get("name") != CALENDAR_TOOL_NAME:
                return JSONResponse(content=error_response(request_id, INVALID_PARAMS, "Only market_calendar_status is available"))
            args = params.get("arguments", {})
            if not isinstance(args, dict) or set(args) - {"market"}:
                return JSONResponse(content=error_response(request_id, INVALID_PARAMS, "Invalid calendar arguments"))
            market = args.get("market", "US")
            if not isinstance(market, str) or market.strip().upper() != "US":
                return JSONResponse(content=error_response(request_id, INVALID_PARAMS, "Only market US is supported"))
            payload = await asyncio.to_thread(calendar_jsonrpc_result, request_id, "US", settings.calendar_app_root)
            return JSONResponse(content=payload)
        else:
            return JSONResponse(content=error_response(request_id, METHOD_NOT_FOUND, "Method not allowed by calendar service"))
        return JSONResponse(content={"jsonrpc": "2.0", "id": request_id, "result": result})

    return app

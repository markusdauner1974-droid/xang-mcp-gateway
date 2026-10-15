# XANG MCP Gateway

A standalone MCP gateway for the self-hosted [xang1234/stock-screener](https://github.com/xang1234/stock-screener)
stack. It sits **beside** the running backend as its own container and exposes a
controlled MCP surface for AI clients (e.g. Hermes) without touching the upstream
application.

## Why this exists

1. **Upstream bridge.** The candidate-fact extension lives in an open upstream PR
   ([xang1234/stock-screener#537](https://github.com/xang1234/stock-screener/pull/537),
   *"Expose canonical setup and screening facts via MCP"*) that has been waiting for
   maintainer attention since 2026-10-05. Until it merges, this gateway enriches the
   MCP responses with the same field set from the already-persisted feature store —
   no recomputation, no upstream changes. When the PR merges, the enrichment is
   removed and the gateway stays as the auth/proxy layer ("slimming").
2. **Separated authentication.** The backend MCP endpoint is protected by
   `SERVER_AUTH_PASSWORD` — the same single-user secret as the web UI login. This
   gateway carries that secret internally and issues its own client token, so MCP
   clients never see the UI password.

## Architecture (planned, Phase 3+ of the gateway decision)

- Own container in the stack's Docker network (`md-netzwerk`), bound LAN-only
  (host LAN IP, no router port forward).
- **Passthrough:** JSON-RPC over Streamable HTTP to the backend `/mcp/` endpoint;
  the 13 live tools are exposed 1:1, `tools/list` is allowlist-filtered
  (`watchlist_add` excluded by default).
- **Enrichment:** `find_candidates` / stock responses gain the 21 PR-#537 fields
  (`minervini_rating`, `canslim_rating`, `volume_breakthrough_rating`,
  `data_status`, `is_scannable`, `action_state`, `opportunity_state`,
  `vcp_detected`, `vcp_num_bases`, `volume_surge` and the 11 `se_*` setup-engine
  fields), read read-only from the feature store's `details_json`
  (`stock_feature_daily`; SE facts live under the nested `setup_engine` object,
  `vcp_num_bases` under `details.screeners.minervini.details.full_analysis.vcp.num_bases`).
- Own client token via environment (`${VAR}` style, no plaintext secrets in config),
  health endpoint, request audit log.

## Security posture

- LAN-only binding; nothing exposed beyond the home network.
- Backend secret only inside the gateway container; clients authenticate with the
  gateway token.
- Database access is read-only and limited to the feature-store tables.
- Stack deployment happens manually (Arcane), as with the other stacks on this QNAP.

## Status

Phase-3 prototype (decision 2026-10-15): FastAPI gateway with authenticated
passthrough, method/tool allowlist, health/readiness endpoints, and read-only
feature-store enrichment of `find_candidates` (the 21 PR-#537 fields). Not yet
deployed; Docker/Arcane packaging and the Hermes-side configuration follow.

Design decision and verified data paths are documented in the operator's
decision log (`mcp_gateway` section) and on the shared knowledge base.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /mcp/` | Authenticated JSON-RPC passthrough (Streamable HTTP). `tools/list` is allowlist-filtered, `tools/call` guarded, `watchlist_add` excluded by default. Backend outages answer with JSON-RPC `-32000` (protocol stays intact). |
| `GET /healthz` | Liveness only — deliberately **no** backend/database call, so a restarted screener backend cannot flip the container unhealthy. |
| `GET /readyz` | Readiness with real probes: backend `tools/list` +, when SQL enrichment is configured, a database ping. Returns 503 + reasons when degraded. |

## Configuration (environment)

| Variable | Meaning |
|---|---|
| `GATEWAY_TOKEN` | Token clients must present (`Authorization: Bearer ...` or `x-gateway-token`). Fail fast when missing. |
| `XANG_BACKEND_TOKEN` | The backend MCP bearer secret (`SERVER_AUTH_PASSWORD` value). Stays inside the gateway process. |
| `XANG_MCP_URL` | Backend endpoint; default `http://xang1234-backend-1:8000/mcp/` — always normalised to a trailing slash (the bare path only redirects). |
| `XANG_DB_DSN` | Read-only Postgres DSN for the feature store; unset = enrichment disabled (fail-closed markers in responses). |
| `GATEWAY_EXCLUDED_TOOLS` | Comma-separated tool exclusions; default `watchlist_add`. |
| `GATEWAY_REQUEST_TIMEOUT` | Per-request backend timeout in seconds (default 120). |
| `GATEWAY_HOST` / `GATEWAY_PORT` | Bind address for `python -m xang_mcp_gateway` (default `127.0.0.1:9160`). |

## Development

```bash
python -m pytest tests -q --tb=short   # test suite
ruff check .                            # lint (pyproject.toml config)
```

CI runs on every push/PR (ruff 0.16.10 + pytest on Python 3.13, same conventions
as the operator's `paperbroker` repo).

English is the language of this repository (issues, PRs, commits, docs); the
operator's working language is German — keep chat communication in German.
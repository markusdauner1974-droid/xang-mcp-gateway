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

## Deployment (Arcane stack template)

`docker-compose.yml` in this repository is the Arcane import template, following
the operator's paperbroker-screener pattern: image from GHCR (built by CI on every
`main` push as `ghcr.io/markusdauner1974-droid/xang-mcp-gateway:latest` +
`:sha-<commit>`, arm64+amd64), LAN-only host binding, external `md-netzwerk`
attachment (backend and Postgres reachable by container name), and **mandatory**
secrets (`${VAR:?...}`) so the stack never deploys with missing credentials.

Suggested Arcane env block:

```dotenv
GATEWAY_TOKEN=<own client token for AI clients>
XANG_BACKEND_TOKEN=<value of SERVER_AUTH_PASSWORD of xang1234-backend-1>
XANG_DB_DSN=postgresql://xang_ro:<password>@xang1234-postgres-1:5432/stockscanner
HOST_PORT=9160            # verified free on the QNAP 2026-10-15
CONTAINER_NAME=xang-mcp-gateway
```

The container port is hard-fixed to 9160; the module default binds `127.0.0.1`
but the image/compose set `GATEWAY_HOST=0.0.0.0` (reachability comes only from
the LAN-only host binding — never publish a router port forward).

### Read-only database user (operator step)

The production DSN should be a DB user that can only read the feature store:

```sql
CREATE ROLE xang_ro LOGIN PASSWORD '<password>';
GRANT USAGE ON SCHEMA public TO xang_ro;
GRANT SELECT ON stock_feature_daily, feature_runs TO xang_ro;
```

The gateway only ever issues SELECTs against `feature_runs`/`stock_feature_daily`.
Run this in the stack's Postgres (`xang1234-postgres-1`) at deploy time.

### Phase 7: refreshing the backend code pin

`market_calendar_status` mirrors the canonical calendar implementation of the
backend image the production stack runs. The Dockerfile pins that image by
digest. After every backend stack update:

```bash
docker images --digests --format '{{.Repository}}@{{.Digest}}' | grep stockscreenclaude-backend
# -> update the FROM ...@sha256:... line in the Dockerfile, rebuild, redeploy
#    (Arcane), then rerun the test suite + live smoke.
```

Rationale, measured live on 2026-10-15: exchange_calendars 4.13.x changes the
post-close semantics of `last_completed_trading_day` compared to the 4.11.1 the
backend runs. The digest pin keeps the gateway on the SAME canonical
implementation as the live backend. The suite covers the Saturday case
(2026-10-17 is not a US trading day).

## Status

Deployed and live since 2026-10-15 (Arcane, container `xang-mcp-gateway`,
LAN `192.168.178.10:9160`). Phase 5+6 (packaging + Hermes-side registration,
13+12 MCP tools) are complete; Phase 7 adds the local `market_calendar_status`
tool.

Design decision and verified data paths are documented in the operator's
decision log (`mcp_gateway` section) and on the shared knowledge base.

## Endpoints

| Endpoint | Purpose |
|---|---|
| `POST /mcp/` | Authenticated JSON-RPC (Streamable HTTP). `tools/list` is allowlist-filtered, `tools/call` guarded, `watchlist_add` excluded by default; the local calendar tool is answered by the gateway itself. Backend outages answer with JSON-RPC `-32000` (protocol stays intact). |
| `GET /healthz` | Liveness only — deliberately **no** backend/database call, so a restarted screener backend cannot flip the container unhealthy. |
| `GET /readyz` | Readiness with real probes: backend `tools/list` +, when SQL enrichment is configured, a database ping. Returns 503 + reasons when degraded. |

## Tools

- **Backend passthrough (12 of 13, allowlist-filtered):** `breadth_snapshot`,
  `compare_feature_runs`, `daily_digest`, `explain_symbol`, `find_candidates`,
  `group_rankings`, `market_overview`, `stock_lookup`, `stock_snapshot`,
  `task_status`, `theme_state`, `watchlist_snapshot` (the write tool
  `watchlist_add` is excluded).
- **Local (Phase 7):** `market_calendar_status` — canonical xang calendar facts
  (`market`, `market_timezone`, `market_now`, `market_date`, `is_trading_day`,
  `last_completed_trading_day`, `expected_trading_date`, `calendar_source`,
  `generated_at`). Agreed semantics: `expected_trading_date = market_date if
  is_trading_day else last_completed_trading_day` — a mid-session trading day
  expects TODAY, never yesterday. On any failure the response is an explicit
  error envelope (`calendar_status: "error"`, `expected_trading_date: null`);
  no dates are ever guessed. Hermes decides fresh/stale deterministically; the
  gateway does not.

## Configuration (environment)

| Variable | Meaning |
|---|---|
| `GATEWAY_TOKEN` | Token clients must present (`Authorization: Bearer ***` or `x-gateway-token`). Fail fast when missing. |
| `XANG_BACKEND_TOKEN` | The backend MCP bearer secret (`SERVER_AUTH_PASSWORD` value). Stays inside the gateway process. |
| `XANG_MCP_URL` | Backend endpoint; default `http://xang1234-backend-1:8000/mcp/` — always normalised to a trailing slash (the bare path only redirects). |
| `XANG_DB_DSN` | Read-only Postgres DSN for the feature store; unset = enrichment disabled (responses pass through unchanged, no markers). |
| `GATEWAY_EXCLUDED_TOOLS` | Comma-separated tool exclusions; default `watchlist_add`. |
| `GATEWAY_REQUEST_TIMEOUT` | Per-request backend timeout in seconds (default 120). |
| `XANG_CALENDAR_BACKEND_APP_ROOT` | Root of the canonical backend source tree used by the calendar tool. Image default `/opt/xang_backend` (copied from the pinned backend image at build time). |
| `GATEWAY_HOST` / `GATEWAY_PORT` | Bind address for `python -m xang_mcp_gateway` (default `127.0.0.1:9160`). |

## Development

```bash
python -m pytest tests -q --tb=short   # test suite
ruff check .                            # lint (pyproject.toml config)
```

CI runs on every push/PR (ruff 0.16.10 + pytest on Python 3.13 + Docker build
smoke, same conventions as the operator's `paperbroker` repo).

English is the language of this repository (issues, PRs, commits, docs); the
operator's working language is German — keep chat communication in German.
# XANG MCP Gateway

Optional REST publication provenance: see
[`docs/REST_PUBLICATION_PROVENANCE.md`](docs/REST_PUBLICATION_PROVENANCE.md).
This opt-in local tool supplies only the scan-to-published-run metadata link;
candidate data continues to come from the official REST API.

A standalone MCP gateway for the self-hosted [xang1234/stock-screener](https://github.com/xang1234/stock-screener)
stack. It sits **beside** the running backend as its own container and exposes a
controlled MCP surface for AI clients (e.g. Hermes) without touching the upstream
application.

## Why this exists

1. **Upstream compatibility.** The candidate-fact extension was merged upstream in PR
   ([xang1234/stock-screener#537](https://github.com/xang1234/stock-screener/pull/537),
   *"Expose canonical setup and screening facts via MCP"*). After deploying that
   backend version, clients can consume the canonical fields directly. Set
   GATEWAY_CANDIDATE_ENRICHMENT=false to stop the candidate SQL reads and pass the
   upstream payload through unchanged. The default remains true for existing
   clients that require the legacy enrichment marker. The gateway continues as
   the auth/proxy layer and serves its independent local tools.
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
tool, Phase 8 the local `earnings_lookup` tool.

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
- **Local (Phase 8):** `earnings_lookup` — one read-only, parameterised
  `SELECT` on `public.stock_fundamentals` (`symbol`, `next_earnings_date`,
  `event_calendar_as_of_date`) answered by the gateway itself. Returns
  `freshness_status` (`ok`/`stale`/`unknown`/`invalid`), `available`,
  `date_confirmation` and machine-readable `reasons`. The freshness window is
  **not** reimplemented: the canonical
  `app.services.yahoo_earnings_calendar` from the backend tree decides
  (14-day observation age, 7-day producer refresh, 3-day future tolerance) and
  the answer repeats the thresholds it actually used. A missing row, a missing
  observation date or a database error stays missing and blocks `available`. `date_confirmation` is
  `unknown` — the table records no confirmation proof, so `confirmed` is never
  claimed. `XANG_DB_DSN` is required; without it the tool answers
  `database_not_configured`.

## Configuration (environment)

| Variable | Meaning |
|---|---|
| `GATEWAY_TOKEN` | Token clients must present (`Authorization: Bearer ***` or `x-gateway-token`). Fail fast when missing. |
| `XANG_BACKEND_TOKEN` | The backend MCP bearer secret (`SERVER_AUTH_PASSWORD` value). Stays inside the gateway process. |
| `XANG_MCP_URL` | Backend endpoint; default `http://xang1234-backend-1:8000/mcp/` — always normalised to a trailing slash (the bare path only redirects). |
| `XANG_DB_DSN` | Read-only Postgres DSN for the feature store; unset = enrichment disabled (responses pass through unchanged, no markers) **and** `earnings_lookup` answers `database_not_configured`. |
| `GATEWAY_CANDIDATE_ENRICHMENT` | Strict boolean `true`/`false`, default `true`. Set `false` after upstream/client migration to disable candidate enrichment while retaining `XANG_DB_DSN` for earnings. Invalid values fail at startup. |
| `GATEWAY_EXCLUDED_TOOLS` | Comma-separated tool exclusions; default `watchlist_add`. |
| `GATEWAY_REQUEST_TIMEOUT` | Per-request backend timeout in seconds (default 120). |
| `XANG_CALENDAR_BACKEND_APP_ROOT` | Root of the canonical backend source tree used by the calendar tool. Image default `/opt/xang_backend` (copied from the pinned backend image at build time). |
| `GATEWAY_HOST` / `GATEWAY_PORT` | Bind address for `python -m xang_mcp_gateway` (default `127.0.0.1:9160`). |

## Migrating to official candidate facts

1. Deploy a backend containing PR #537 and compare its candidate facts with the
   current gateway for the same published run. Missing fields and null values
   must remain distinguishable; no rating translation is part of this migration.
2. Prepare clients to validate the official candidate schema without requiring
   the legacy gateway enrichment marker.
3. Set GATEWAY_CANDIDATE_ENRICHMENT=false in the Arcane environment and redeploy
   the gateway. Keep XANG_DB_DSN configured when earnings_lookup is used.
4. Verify candidate passthrough, local calendar/earnings tools, authentication,
   and the write-tool exclusion before resuming client jobs.

With candidate enrichment disabled, no candidate FactsProvider is constructed or
called, including providers supplied directly to create_app. Candidate fields,
nulls, ratings and upstream tool errors pass through unchanged; no gateway marker
is added. Health reports enrichment=disabled, and readiness skips the candidate
feature-store ping. Earnings still uses its own DSN and reports its own failures.
The calendar implementation and backend authentication are unchanged.

The earnings loader also verifies that its requested rule file exists and that
the imported module belongs to that file. An already cached module from another
backend tree cannot substitute for a missing or misconfigured rule source.
Failures remain invalid/unavailable; no freshness thresholds are reimplemented.

## Calendar-only operation after direct-client migration

Once clients fetch candidates directly from the official backend MCP, set
`GATEWAY_CALENDAR_ONLY=true` and redeploy. The default is `false`, preserving
existing deployments. This is a strict boolean; invalid values fail startup.

In calendar-only mode:

- MCP initialization, notifications, ping, and tool discovery are answered locally.
- The only advertised and callable tool is `market_calendar_status` (US and DE).
- Candidate, earnings, and write-tool calls are rejected before any forwarding.
- No backend HTTP transport or candidate SQL provider is constructed, even if
  callers inject a provider or transport. The enrichment flag cannot override this.
- Only `GATEWAY_TOKEN` is required; remove `XANG_BACKEND_TOKEN` and `XANG_DB_DSN`
  from this container after backing up the private deployment configuration.
- `/healthz` reports `mode=calendar_only`, `enrichment=disabled`, and one tool.
  `/readyz` checks the canonical local calendar rather than backend MCP or SQL.
  Calendar errors retain the existing explicit error/null-date contract and make
  readiness HTTP 503; they never cause a guessed trading date.
- `/readyz` stays unauthenticated so an orchestrator can probe it, so its verdict
  is cached for 10 seconds and only one probe runs at a time: repeated calls cost
  nothing and a slow calendar cannot multiply. Degraded verdicts are cached the
  same way. The probe runs on its own single-worker thread with a 5-second limit,
  so a hung calendar answers 503 instead of blocking later probes or taking a
  thread from `/mcp/`. After a timeout the stuck worker is replaced and the next
  probe backs off (60 s, doubling to a 5-minute cap), so a calendar that never
  recovers cannot keep spawning threads or queueing work.

Use the standalone [calendar compose template](docker-compose.calendar.yml)
instead of the legacy proxy template, which still requires backend/SQL secrets.
Back up the existing stack configuration before replacing it. Preserve the
service/container name, network, gateway token, and port binding so existing
calendar consumers do not need another address change. Retain the source-tree
and calendar-data paths provided by the Docker image.

Move client candidate requests to the official backend endpoint and its own
credential first; keep calendar requests on this service. A direct client now
holds a backend credential rather than relying on gateway credential isolation.
Audit other consumers before removing proxy/earnings support from a deployment.
No scanner strategy or earnings-collection behavior is changed by this flag.

When the upstream calendar MCP tool is deployed, validate its contract and
switch the client calendar endpoint and credential. Stop this service only after
all remaining consumers have migrated. To roll back, restore the private stack
configuration and set `GATEWAY_CALENDAR_ONLY=false` with the required backend/SQL
settings; client routing must be restored separately if needed.

### US and German calendar calls

Calendar-only clients can request either `{"market":"US"}` or
`{"market":"DE"}`. The default stays US. Codes are trimmed and normalized;
other market codes and malformed values are rejected before calendar access.
The tool descriptor advertises both codes.

Both paths call the existing canonical XANG calendar bridge with the requested
market. The gateway adds no holiday, timezone, session-date or freshness rules.
There is no other-market fallback. Clients must match each candidate run to its
own market calendar and keep an unavailable calendar fail-closed.

The readiness endpoint keeps its existing cached US baseline probe; it does not
attest to every market's current coverage. Validate the DE request separately
when deploying or checking DE consumers. No credential or database setting is
added; calendar-only mode still requires only the gateway client token.

## Development

```bash
python -m pytest tests -q --tb=short   # test suite
ruff check .                            # lint (pyproject.toml config)
```

CI runs on every push/PR (ruff 0.16.10 + pytest on Python 3.13 + Docker build
smoke, same conventions as the operator's `paperbroker` repo).

English is the language of this repository (issues, PRs, commits, docs); the
operator's working language is German — keep chat communication in German.

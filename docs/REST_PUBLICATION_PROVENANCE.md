# Temporary publication proof for official REST candidate reads

The official REST scan API supports market selection, pagination and canonical
candidate fields. Some completed market scans expose `source=feature_store` but
do not expose their feature-run identity (`published_source` is null). The
optional `scan_publication_status` tool supplies only this missing link.

It does **not** collect candidates, enrich facts, calculate scores, make trading
decisions, invoke the backend MCP transport or start scans. The canonical US/DE
calendar bridge is unchanged. The database remains an explicitly temporary
dependency until the REST API exposes equivalent publication provenance.

## Explicit opt-in

Keep `GATEWAY_CALENDAR_ONLY=true` and `GATEWAY_CANDIDATE_ENRICHMENT=false`.
Set `GATEWAY_PUBLICATION_PROVENANCE=true` and supply a separate
`XANG_PROVENANCE_DB_DSN` through the deployment's secret mechanism.

The flag defaults to false. Enabling it without a dedicated DSN, or outside
local calendar mode, fails startup. `XANG_DB_DSN` is not used for this feature;
`XANG_BACKEND_TOKEN` remains unnecessary. Do not restore enrichment or backend
passthrough credentials. No Compose secret or production configuration is
changed by installing this code.

Without the flag, the existing one-tool calendar service is unchanged and no
publication SQL provider is constructed. With the flag, `tools/list` exposes
exactly `market_calendar_status` and `scan_publication_status`. Both require
the existing gateway authentication. All other tools remain blocked.

## Database permissions

Provision a **dedicated** least-privilege database login, with no elevated
attributes or inherited write roles. Give it database CONNECT and public-schema
USAGE plus column-level SELECT only on:

- `public.scans`: `scan_id`, `status`, `universe_type`, `universe_market`,
  `feature_run_id`.
- `public.feature_runs`: `id`, `status`, `as_of_date`, `published_at`,
  `completed_at`, `config_json`.
- `public.feature_run_pointers`: `key`, `run_id`.

Do not grant access to `stock_feature_daily`, candidate facts or write
operations. Verify the login's effective grants before deployment, including
PUBLIC and inherited privileges; application settings are not a replacement
for database authorization. Keep passwords and DSNs out of command output and
the repository.

Every lookup additionally forces a read-only transaction and issues one fixed,
parameterised SELECT joining only the three metadata tables. Connection timeout
is 5 seconds, statement timeout 5 seconds and lock timeout 1 second. The MCP
handler also applies `GATEWAY_REQUEST_TIMEOUT`. Driver exceptions are never
returned to clients because they may contain connection details.

Metadata requests use their own two-worker executor. Additional requests fail
closed immediately when both workers are occupied; there is no unbounded
waiting queue. A timed-out running query keeps its slot until it actually
finishes. Slow metadata queries therefore cannot occupy the calendar's default
executor. The SQL projection casts the scan identity to text explicitly.

## Request and response

The tool requires exactly `market` (`US` or `DE`) and `scan_id` (UUID). It proves:

1. The scan exists uniquely, is completed and represents the requested market.
2. It names a positive feature-run ID and that run is published.
3. The run's root and nested market provenance agree with the request.
4. The market pointer `latest_published_market:<market>` still names that run.
5. The published date and timezone-aware publication timestamp are present.

On success it returns `source=xang_publication_metadata`, `proof_status=ok`,
the scan identity, a check timestamp and the original run identity, dates,
market and pointer key. It does not certify calendar freshness; Hermes must
still compare the run's as-of date to the canonical calendar for that market.

Missing metadata, pointer changes, wrong markets, database failures and
timeouts produce an error. There is no fallback to another run or source,
no partial success, and no conversion of missing evidence into an OK result.

## Consumer consistency contract

Select a completed `source=feature_store` market scan through official REST.
Request a publication proof for that exact market and scan ID. Pin all REST
pages to that scan ID and check totals, page numbers, query identity, duplicate
symbols and field types. Then re-request the proof after collection and require
the **run identity** (ID, market, status, as-of date, publication timestamp and
pointer) to agree; `checked_at` naturally differs. A changed publication aborts
the collection. Never mix markets' metadata or infer market from a ticker
suffix. Save the proofs and REST response provenance alongside the raw facts.

## Readiness, validation and rollback

`/healthz` remains liveness-only and shows two visible tools when opted in.
`/readyz` retains its existing cached canonical-calendar check; it does **not**
probe this optional database connection or attest metadata availability.
Before routing Hermes through the feature, perform authenticated live lookups
for both markets using the dedicated login and verify an error for a wrong
market or stale scan. Test the database permissions independently. Run the
full regression suite in the deployment's Python/container environment.

Installing the optional tool alone does not change Hermes routing. Validate
the REST adapter and both proofs in a test copy before production cutover;
keep the scanner cron job paused throughout the migration.

To roll back, disable `GATEWAY_PUBLICATION_PROVENANCE`, remove the dedicated
DSN and redeploy. The existing calendar-only service remains. A consumer still
requiring this proof must fail closed until explicitly rerouted to a validated
replacement. Revoke the temporary database login when no longer needed.

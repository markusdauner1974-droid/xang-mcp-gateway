# XANG MCP Gateway container image.
# Non-root runtime (uid 10001, operator convention / CWE-250), python:3.13-slim base.
#
# Phase 7 (market_calendar_status): the CANONICAL calendar service ships with
# the xang1234 backend image and is copied verbatim into /opt/xang_backend at
# build time — NO calendar logic is reimplemented here. The source image is
# referenced with an explicit linux/amd64 platform hint because only the Python
# source tree is copied (architecture-independent), not binaries.
#
# PINNED BY DIGEST to the image the production stack currently runs
# (verified 2026-10-15, docker images --digests). When the operator updates
# the backend stack, this pin should be refreshed in the same change so the
# gateway keeps mirroring the LIVE canonical implementation (documented in
# README "Phase 7 pin refresh").
FROM --platform=linux/amd64 ghcr.io/xang1234/stockscreenclaude-backend@sha256:8b48b3fb2eca1691d1492a69e834b9a91565cd7f8af0581fd8f418eaef48c336 AS xang-backend

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 gateway \
 && useradd --uid 10001 --gid gateway --no-create-home --shell /usr/sbin/nologin gateway

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# Canonical XANG backend source tree (calendar service + domain modules).
COPY --from=xang-backend /app/app /opt/xang_backend/app
# Canonical calendar manifests (backend Dockerfile copies these to
# /opt/stockscanner/market-calendars and sets MARKET_CALENDAR_DATA_ROOT —
# mirrored 1:1 here so the gateway loads the SAME canonical data).
COPY --from=xang-backend /opt/stockscanner/market-calendars /opt/xang_backend/market-calendars
RUN pip install --no-cache-dir .

# Root of the imported canonical backend package (see calendar.py).
ENV XANG_CALENDAR_BACKEND_APP_ROOT=/opt/xang_backend
# Mirror of the backend image's own calendar-data env (live-verified 2026-10-15).
ENV MARKET_CALENDAR_DATA_ROOT=/opt/xang_backend/market-calendars

# Container-internal listen port (fixed: compose publishing + healthcheck expect 9160).
ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=9160
EXPOSE 9160

# Liveness only: /healthz deliberately makes NO external backend/database calls,
# so a restarted xang backend can never flip this container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if 200 <= urllib.request.urlopen('http://127.0.0.1:9160/healthz', timeout=4).status < 500 else 1)"]

USER gateway
CMD ["python", "-m", "xang_mcp_gateway"]
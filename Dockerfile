# XANG MCP Gateway container image.
# Non-root runtime (uid 10001, operator convention / CWE-250), python:3.13-slim base.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN groupadd --gid 10001 gateway \
 && useradd --uid 10001 --gid gateway --no-create-home --shell /usr/sbin/nologin gateway

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

# Container-internal listen port (fixed: compose publishing + healthcheck expect 9160).
ENV GATEWAY_HOST=0.0.0.0 \
    GATEWAY_PORT=9160
EXPOSE 9160

# Liveness only: /healthz deliberately makes_NO external backend/database calls,
# so a restarted xang backend can never flip this container unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if 200 <= urllib.request.urlopen('http://127.0.0.1:9160/healthz', timeout=4).status < 500 else 1)"]

USER gateway
CMD ["python", "-m", "xang_mcp_gateway"]
"""Run the gateway: python -m xang_mcp_gateway."""

from __future__ import annotations

import os

from .app import create_app
from .config import GatewayConfigError, GatewaySettings


def main() -> None:
    try:
        settings = GatewaySettings.from_env()
    except GatewayConfigError as exc:
        raise SystemExit(f"configuration error: {exc}") from exc
    import uvicorn

    uvicorn.run(
        create_app(settings),
        host=os.getenv("GATEWAY_HOST", "127.0.0.1"),
        port=int(os.getenv("GATEWAY_PORT", "9160")),
    )


if __name__ == "__main__":
    main()

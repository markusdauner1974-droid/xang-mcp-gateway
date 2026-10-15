"""JSON-RPC error envelopes and the backend transport."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from .config import GatewaySettings

# JSON-RPC 2.0 standard codes plus the gateway's downstream error code.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
GATEWAY_DOWNSTREAM_ERROR = -32000


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class BackendUnavailable(RuntimeError):
    """Raised when the backend cannot be reached or answers non-conformantly."""


class Transport(Protocol):
    """Pluggable backend transport (real HTTP in production, fakes in tests)."""

    async def post_jsonrpc(self, body: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        """Forward a JSON-RPC body; return (http_status, payload_or_none)."""


class HttpxTransport:
    """Forwards JSON-RPC POSTs to the backend /mcp/ endpoint."""

    def __init__(self, settings: GatewaySettings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=settings.request_timeout_seconds)

    async def post_jsonrpc(self, body: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        try:
            response = await self._client.post(
                self._settings.backend_mcp_url,
                json=body,
                headers={"Authorization": f"Bearer {self._settings.backend_token}"},
            )
        except httpx.HTTPError as exc:  # timeout, connect, protocol errors
            raise BackendUnavailable(f"backend connection failed: {type(exc).__name__}") from exc
        if response.status_code == 202:
            return 202, None
        if response.status_code != 200:
            raise BackendUnavailable(f"backend responded HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendUnavailable("backend returned a non-JSON body") from exc
        if not isinstance(payload, dict):
            raise BackendUnavailable("backend returned a non-object JSON body")
        return 200, payload

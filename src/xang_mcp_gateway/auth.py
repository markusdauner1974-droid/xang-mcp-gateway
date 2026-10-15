"""Gateway client authentication.

Clients authenticate with the gateway's own token; the backend secret never
leaves the gateway process. Accepts either an ``Authorization: Bearer <token>``
header or ``x-gateway-token: <token>`` (mirrors the backend's dual header
paths, verified via its server_auth module).
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-tolerant lookup: FastAPI Headers are case-insensitive, plain dicts are not."""
    direct = headers.get(name)
    if direct:
        return direct
    for key, value in headers.items():
        if key.lower() == name.lower() and value:
            return value
    return ""


def is_authorized(headers: Mapping[str, str], gateway_token: str) -> bool:
    if not gateway_token:
        return False
    candidates: list[str] = []
    authorization = _header(headers, "authorization")
    if authorization.lower().startswith("bearer "):
        candidates.append(authorization.split(" ", 1)[1].strip())
    candidates.append(_header(headers, "x-gateway-token").strip())
    return any(
        candidate and hmac.compare_digest(candidate, gateway_token) for candidate in candidates
    )

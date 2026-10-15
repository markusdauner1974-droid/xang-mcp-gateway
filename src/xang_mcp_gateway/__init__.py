"""XANG MCP Gateway: standalone MCP gateway for the xang1234 stock-screener stack.

Phase 3 (prototype) starts here: passthrough of the backend's 13 MCP tools over
Streamable HTTP, later plus candidate-fact enrichment (PR #537 field set) read
read-only from the feature store. See README for architecture and security posture.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
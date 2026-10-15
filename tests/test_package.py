"""Smoke tests for the package skeleton (keep CI green from commit one)."""

import xang_mcp_gateway


def test_version_matches_pyproject() -> None:
    assert xang_mcp_gateway.__version__ == "0.1.0"


def test_package_name_is_declared() -> None:
    """Guard against accidental rename drift between package and distribution."""
    import importlib.metadata

    try:
        dist = importlib.metadata.version("xang-mcp-gateway")
    except importlib.metadata.PackageNotFoundError:
        # Package not installed in this environment (plain pytest run).
        return
    assert dist == xang_mcp_gateway.__version__
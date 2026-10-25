"""GatewaySettings: normalization, fail-fast validation, env parsing."""

from __future__ import annotations

import pytest

from xang_mcp_gateway import GatewayConfigError
from xang_mcp_gateway.config import (
    ALL_BACKEND_TOOLS,
    DEFAULT_EXCLUDED_TOOLS,
    LOCAL_TOOLS,
    GatewaySettings,
)

from .conftest import BACKEND_TOKEN, GATEWAY_TOKEN, make_settings


def test_backend_url_normalized_to_trailing_slash() -> None:
    settings = make_settings(backend_mcp_url="http://backend:8000/mcp")
    assert settings.backend_mcp_url == "http://backend:8000/mcp/"


def test_backend_url_without_mcp_path_gets_path_appended() -> None:
    settings = make_settings(backend_mcp_url="http://backend:8000")
    assert settings.backend_mcp_url == "http://backend:8000/mcp/"


def test_empty_url_rejected() -> None:
    with pytest.raises(GatewayConfigError):
        make_settings(backend_mcp_url="   ")


def test_empty_tokens_rejected() -> None:
    with pytest.raises(GatewayConfigError):
        make_settings(backend_token="")
    with pytest.raises(GatewayConfigError):
        make_settings(gateway_token="")


def test_identical_tokens_rejected() -> None:
    with pytest.raises(GatewayConfigError):
        make_settings(backend_token="same", gateway_token="same")


def test_allowed_tools_excludes_default_write_tool() -> None:
    settings = make_settings()
    # Phase 7: surface = backend passthrough tools (minus write tool) + the
    # local canonical calendar tool (see calendar.py).
    assert settings.allowed_tools == (
        (ALL_BACKEND_TOOLS - DEFAULT_EXCLUDED_TOOLS) | LOCAL_TOOLS
    )
    assert "watchlist_add" not in settings.allowed_tools
    assert len(settings.allowed_tools) == 12 + len(LOCAL_TOOLS)


def test_custom_exclusion_list() -> None:
    settings = make_settings(excluded_tools=frozenset({"watchlist_add", "explain_symbol"}))
    assert "explain_symbol" not in settings.allowed_tools
    assert "stock_snapshot" in settings.allowed_tools


def test_enrichment_disabled_without_db_dsn() -> None:
    settings = make_settings()
    assert settings.enrichment_enabled is False
    # Capability set is independent of the DSN: the provider decides whether it runs.
    assert settings.enrichable_tools == frozenset({"find_candidates"})


class TestConfigTimeoutValidation:
    def test_from_env_invalid_number_is_config_error(self) -> None:
        with pytest.raises(GatewayConfigError, match="GATEWAY_REQUEST_TIMEOUT"):
            GatewaySettings.from_env(
                {"XANG_BACKEND_TOKEN": BACKEND_TOKEN, "GATEWAY_TOKEN": GATEWAY_TOKEN, "GATEWAY_REQUEST_TIMEOUT": "abc"}
            )

    @pytest.mark.parametrize("bad", [0, -5.0, float("nan"), float("inf")])
    def test_nonfinite_and_nonpositive_rejected_at_boundary(self, bad: float) -> None:
        with pytest.raises(GatewayConfigError, match="finite positive"):
            make_settings(request_timeout_seconds=bad)

    def test_valid_timeout_accepted(self) -> None:
        assert make_settings(request_timeout_seconds=42.5).request_timeout_seconds == 42.5


def test_main_exits_with_configuration_error_on_bad_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from xang_mcp_gateway.__main__ import main

    monkeypatch.setenv("XANG_BACKEND_TOKEN", BACKEND_TOKEN)
    monkeypatch.setenv("GATEWAY_TOKEN", GATEWAY_TOKEN)
    monkeypatch.setenv("GATEWAY_PORT", "not-a-port")
    with pytest.raises(SystemExit, match="configuration error"):
        main()


def test_from_env_defaults() -> None:
    settings = GatewaySettings.from_env(
        {"XANG_BACKEND_TOKEN": BACKEND_TOKEN, "GATEWAY_TOKEN": GATEWAY_TOKEN}
    )
    assert settings.backend_mcp_url.endswith("/mcp/")
    assert settings.excluded_tools == DEFAULT_EXCLUDED_TOOLS
    assert settings.db_dsn is None


def test_from_env_overrides_and_excluded_list() -> None:
    settings = GatewaySettings.from_env(
        {
            "XANG_MCP_URL": "http://x:9/mcp/",
            "XANG_BACKEND_TOKEN": BACKEND_TOKEN,
            "GATEWAY_TOKEN": GATEWAY_TOKEN,
            "GATEWAY_EXCLUDED_TOOLS": "watchlist_add, explain_symbol ,",
            "XANG_DB_DSN": "postgresql://ro@db/stockscanner",
            "GATEWAY_REQUEST_TIMEOUT": "42.5",
        }
    )
    assert settings.backend_mcp_url == "http://x:9/mcp/"
    assert settings.excluded_tools == frozenset({"watchlist_add", "explain_symbol"})
    assert settings.db_dsn == "postgresql://ro@db/stockscanner"
    assert settings.enrichment_enabled is True
    assert settings.request_timeout_seconds == 42.5

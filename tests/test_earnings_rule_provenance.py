"""The requested backend tree remains authoritative when Python has cached rules."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

from xang_mcp_gateway import earnings as earnings_module


def rule_file(root: Path) -> Path:
    path = root / "app/services/yahoo_earnings_calendar.py"
    path.parent.mkdir(parents=True)
    path.write_text("# Isolated provenance fixture; never executed.\n")
    return path


def cached_rules(monkeypatch: pytest.MonkeyPatch, path: Path) -> ModuleType:
    app = ModuleType("app")
    app.__path__ = [str(path.parent.parent)]
    services = ModuleType("app.services")
    services.__path__ = [str(path.parent)]
    rules = ModuleType("app.services.yahoo_earnings_calendar")
    rules.__file__ = str(path)
    app.services = services
    services.yahoo_earnings_calendar = rules
    for name, module in (
        ("app", app), ("app.services", services),
        ("app.services.yahoo_earnings_calendar", rules),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return rules


def test_missing_requested_tree_cannot_reuse_cached_rules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cached_rules(monkeypatch, rule_file(tmp_path / "cached"))
    before = list(sys.path)
    with pytest.raises(ImportError, match="missing from requested backend tree"):
        earnings_module._load_freshness_rules(str(tmp_path / "missing"))
    assert sys.path == before


def test_existing_requested_tree_cannot_reuse_another_trees_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cached_rules(monkeypatch, rule_file(tmp_path / "cached"))
    requested = tmp_path / "requested"
    rule_file(requested)
    monkeypatch.syspath_prepend(str(requested))
    with pytest.raises(ImportError, match="different backend tree"):
        earnings_module._load_freshness_rules(str(requested))


def test_matching_requested_tree_keeps_the_canonical_cached_module(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    requested = tmp_path / "requested"
    rules = cached_rules(monkeypatch, rule_file(requested))
    monkeypatch.syspath_prepend(str(requested))
    assert earnings_module._load_freshness_rules(str(requested)) is rules

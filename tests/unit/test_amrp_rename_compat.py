"""Compatibility shims for the AERP/financial_intelligence -> AMRP rename."""

from __future__ import annotations

from pathlib import Path

import pytest

from amrp.control.secrets_store import default_config_dir
from amrp.envcompat import env


def test_env_prefers_new_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMRP_API_URL", "http://new")
    monkeypatch.setenv("AERP_API_URL", "http://old")
    assert env("AMRP_API_URL") == "http://new"


def test_env_falls_back_to_legacy_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMRP_CONTROL_TOKEN", raising=False)
    monkeypatch.setenv("EQUITY_RESEARCH_CONTROL_TOKEN", "legacy-token-value")
    with pytest.warns(DeprecationWarning):
        assert env("AMRP_CONTROL_TOKEN") == "legacy-token-value"


def test_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMRP_RUN_TIMEOUT", raising=False)
    monkeypatch.delenv("AERP_RUN_TIMEOUT", raising=False)
    assert env("AMRP_RUN_TIMEOUT", "120") == "120"


def test_config_dir_copies_legacy_and_keeps_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    old = tmp_path / "equity-research"
    old.mkdir()
    (old / "dashboard_settings.json").write_text("{}", encoding="utf-8")
    new = default_config_dir("amrp")
    assert new == tmp_path / "amrp"
    assert (new / "dashboard_settings.json").read_text(encoding="utf-8") == "{}"
    assert (old / "dashboard_settings.json").exists()


def test_codec_decodes_legacy_type_names() -> None:
    from amrp.infrastructure.sqlite import codec

    resolved = codec._resolve(
        "financial_intelligence.domain.orchestration.objectives:ResearchObjective"
    )
    assert resolved.__module__ == "amrp.domain.orchestration.objectives"

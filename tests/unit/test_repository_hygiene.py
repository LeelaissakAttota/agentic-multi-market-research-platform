"""Repository hygiene contracts: root layout, links, packaging, CI and test client."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLOWED_ROOT_MARKDOWN = {
    "README.md",
    "ARCHITECTURE.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY_GUIDELINES.md",
    "TESTING_STRATEGY.md",
    "DATA_SOURCES.md",
    "EVIDENCE_MODEL.md",
    "MODEL_POLICY.md",
}
_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


def test_root_markdown_is_limited_to_control_documents() -> None:
    present = {path.name for path in ROOT.glob("*.md")}
    assert present == ALLOWED_ROOT_MARKDOWN


def test_history_reports_live_outside_repository_root() -> None:
    # Phase history and detailed phase and defect reports are archived outside the repository.
    archived = [
        path.name for path in ROOT.glob("*.md") if path.name.startswith(("F0", "F1", "PHASE_"))
    ]
    assert archived == []


def test_root_markdown_relative_links_resolve() -> None:
    broken: list[str] = []
    for document in sorted(ROOT.glob("*.md")):
        for target in _LINK.findall(document.read_text(encoding="utf-8")):
            if re.match(r"[a-z]+:", target):
                continue
            if not (ROOT / target).exists():
                broken.append(f"{document.name} -> {target}")
    assert broken == []


def test_no_empty_placeholder_source_packages() -> None:
    package = ROOT / "src" / "amrp"
    assert list(package.rglob(".gitkeep")) == []
    for directory in (path for path in package.rglob("*") if path.is_dir()):
        if directory.name == "__pycache__":
            continue
        assert any(directory.iterdir()), directory


def test_project_metadata_author_and_python_support() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert project["authors"] == [
        {"name": "attota leelaissak", "email": "leelaissak@users.noreply.github.com"}
    ]
    assert project["requires-python"] == ">=3.12,<3.14"
    assert "Programming Language :: Python :: 3.13" in project["classifiers"]


def test_ci_matrix_covers_supported_pythons() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert 'python-version: ["3.12", "3.13"]' in workflow


def test_tests_do_not_use_deprecated_starlette_test_client() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "tests").rglob("*.py")
        if re.search(
            r"from (fastapi|starlette)\.testclient import", path.read_text(encoding="utf-8")
        )
    ]
    assert offenders == []
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pytest_options = tomllib.load(handle)["tool"]["pytest"]["ini_options"]
    assert "filterwarnings" not in pytest_options

"""Navigation loading pill: fixed-position, no st.spinner block on page loads."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "dashboard"))

import loading as ld


class Slot:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def markdown(self, body: str, **_: object) -> None:
        self.calls.append(body)

    def empty(self) -> None:
        self.calls.append("")


def test_pill_is_fixed_and_compact() -> None:
    h = ld.pill_html("Loading…")
    assert "position:fixed" in h and "height:30px" in h and "Loading…" in h
    assert "<b" not in ld.pill_html("<b>x")


def test_loading_restores_then_clears() -> None:
    slot = Slot()
    ld.attach(slot)
    with ld.loading("Agents reviewing", restore="Loading…"):
        pass
    assert "Agents reviewing" in slot.calls[0] and "Loading…" in slot.calls[1]
    with ld.loading():
        pass
    assert slot.calls[-1] == ""
    ld.attach(None)


def test_debate_spinner_no_longer_uses_st_spinner() -> None:
    src = (Path(__file__).resolve().parents[2] / "dashboard" / "agents_debate.py").read_text(
        encoding="utf-8"
    )
    assert "st.spinner(" not in src

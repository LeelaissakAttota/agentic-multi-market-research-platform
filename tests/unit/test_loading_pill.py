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


def test_overlay_is_centered_spinner_with_loading_label() -> None:
    h = ld.pill_html("Bull, Bear, Judge")
    assert "position:fixed" in h and "top:50%" in h and "left:50%" in h
    assert "animation:amrpspin" in h and ld.LABEL in h
    assert "Bull" not in h and "<b>" not in ld.pill_html("<b>x")


def test_loading_shows_then_clears() -> None:
    slot = Slot()
    ld.attach(slot)
    with ld.loading("Agents reviewing", restore="x"):
        pass
    assert "Agents reviewing" not in slot.calls[0] and ld.LABEL in slot.calls[0]
    with ld.loading():
        pass
    assert slot.calls[-1] == ""
    ld.attach(None)


def test_no_st_spinner_anywhere_in_dashboard() -> None:
    d = Path(__file__).resolve().parents[2] / "dashboard"
    for f in d.glob("*.py"):
        assert "st.spinner(" not in f.read_text(encoding="utf-8"), f.name


def test_debate_spinner_no_longer_uses_st_spinner() -> None:
    src = (Path(__file__).resolve().parents[2] / "dashboard" / "agents_debate.py").read_text(
        encoding="utf-8"
    )
    assert "st.spinner(" not in src

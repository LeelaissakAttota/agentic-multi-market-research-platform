"""AERP v3 look: colours sampled from the reference design, plus small HTML components.

Palette (sampled with PIL from the reference mock-up):
page #001632, sidebar #000F21, card #011733 / border #0D2B55, active nav #00317F,
primary #0E4BEC, approve #00A581, reject #D01A3A, completed pill #003438/#22D3A0,
pending pill #331E1F/#F0911F, icon tiles blue #2458F5 green #049F7E orange #F0911F
red #DD1138, step done #0A3A7A, step current #D19326, step todo #10325E.
"""

from __future__ import annotations

import base64
import html
from collections.abc import Iterable, Sequence
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"


def asset_uri(name: str) -> str:
    try:
        data = (ASSETS / name).read_bytes()
    except FileNotFoundError:
        return ""
    return "data:image/png;base64," + base64.b64encode(data).decode()


CSS = "<style>" + (ASSETS / "aerp.css").read_text(encoding="utf-8") + "</style>"


def esc(value: object) -> str:
    return html.escape("" if value is None else str(value))


def pill(text: str, tone: str) -> str:
    return f'<span class="pill p-{tone}">{esc(text)}</span>'


STATUS_TONE = {
    "Completed": "green",
    "Partial": "blue",
    "Pending approval": "amber",
    "Created": "gray",
    "Failed": "red",
    "Rejected": "red",
    "Cancelled": "red",
}


def status_pill(label: str) -> str:
    return pill(label, STATUS_TONE.get(label, "gray"))


def stat_card(label: str, value: str, sub: str, tone: str, icon: str) -> str:
    tile = {"blue": "t-blue", "green": "t-green", "amber": "t-orange", "red": "t-red"}[tone]
    sub_cls = {"blue": "s-green", "green": "s-green", "amber": "s-amber", "red": "s-red"}[tone]
    box = "" if tone == "blue" else tone
    return (
        f'<div class="stat {box}"><div class="tile {tile}">{icon}</div><div>'
        f'<div class="l">{esc(label)}</div><div class="v">{esc(value)}</div>'
        f'<div class="s {sub_cls}">{esc(sub)}</div></div></div>'
    )


def table(
    rows: Sequence[dict[str, object]], cols: Sequence[str], html_cols: Iterable[str] = ()
) -> str:
    """HTML table; columns in ``html_cols`` are trusted pre-built HTML (pills)."""

    raw = set(html_cols)
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = "".join(
        "<tr>"
        + "".join(f"<td>{r.get(c, '') if c in raw else esc(r.get(c, ''))}</td>" for c in cols)
        + "</tr>"
        for r in rows
    )
    return f'<table class="t"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def initials(name: str) -> str:
    words = [w for w in name.replace("(", " ").split() if w[:1].isalpha()]
    return "".join(w[0] for w in words[:2]).upper() or "?"


def logo_html() -> str:
    return (
        '<div class="logo"><div class="mk"><i style="height:11px"></i><i style="height:18px"></i>'
        '<i style="height:26px"></i></div><div><b>AERP</b><small>Agentic Equity Research'
        "</small></div></div>"
    )


SIDE_FOOT = (
    '<div class="side-foot"><b>Evidence-First Research</b><br>Real data. Clear sources.<br>'
    "Greater confidence.<br><br>by Leela Issak</div>"
)

TOPBAR = (
    '<div class="topbar"><span class="dark"><i></i>Dark</span><span class="avatar">'
    '<span class="c">LI</span>Leela ▾</span></div>'
)

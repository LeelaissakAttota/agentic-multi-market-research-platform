"""Turn a stored Markdown research report into downloadable documents (PDF, HTML).

The API stores reports as Markdown. A raw ``.md`` file is hard to read for most
users, so the dashboard also offers a formatted PDF and a self-contained HTML
document built from the same Markdown. No network access, no secrets.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

DISCLAIMER = (
    "Disclaimer: This report is research context generated from stored workflow "
    "evidence and may include LLM-written summaries and fixture/demo data (labelled by "
    "data origin). It is not investment advice. Verify all figures before use."
)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_REPLACE = {
    "\u2014": "-",
    "\u2013": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2026": "...",
    "\u2022": "-",
    "\u00a0": " ",
    "\u2192": "->",
    "\u2b07": "",
    "\u2264": "<=",
    "\u2265": ">=",
    "\u2212": "-",
    "\u00b7": "-",
}


def report_filename(company: str, ext: str, when: datetime | None = None) -> str:
    """Safe, dated file name such as ``AMRP-Apple-2026-10-29.pdf``."""

    stamp = (when or datetime.now(UTC)).strftime("%Y-%m-%d")
    base = _SAFE_NAME.sub("_", company.split(" (")[0].strip()).strip("_") or "report"
    return f"AMRP-{base}-{stamp}.{ext.lstrip('.')}"


@dataclass
class Block:
    kind: str  # h1 h2 h3 p quote bullet table
    text: str = ""
    rows: list[list[str]] = field(default_factory=list)


def _unescape(text: str) -> str:
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>])", r"\1", text)


def _cells(line: str) -> list[str]:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    parts = re.split(r"(?<!\\)\|", body)
    return [_unescape(p.strip()) for p in parts]


def parse_markdown(markdown: str) -> list[Block]:
    """Very small Markdown reader for the report format the API produces."""

    blocks: list[Block] = []
    para: list[str] = []
    table: list[list[str]] | None = None

    def flush() -> None:
        nonlocal table
        if para:
            blocks.append(Block("p", _unescape(" ".join(para))))
            para.clear()
        if table:
            blocks.append(Block("table", rows=table))
        table = None

    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            continue  # invisible markers (e.g. the agents section boundary)
        if stripped.startswith("|"):
            if para:
                blocks.append(Block("p", _unescape(" ".join(para))))
                para.clear()
            if _TABLE_SEP.match(stripped):
                continue
            table = table or []
            table.append(_cells(stripped))
            continue
        if table is not None:
            flush()
        if not stripped:
            flush()
        elif m := re.match(r"^(#{1,6})\s+(.*)$", stripped):
            flush()
            level = min(len(m.group(1)), 3)
            blocks.append(Block(f"h{level}", _unescape(m.group(2))))
        elif stripped.startswith(">"):
            flush()
            blocks.append(Block("quote", _unescape(stripped.lstrip("> ").strip())))
        elif re.match(r"^[-*+]\s+", stripped):
            flush()
            blocks.append(Block("bullet", _unescape(re.sub(r"^[-*+]\s+", "", stripped))))
        else:
            para.append(stripped)
    flush()
    return blocks


def _plain(text: str) -> str:
    text = _LINK.sub(r"\1 (\2)", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<![A-Za-z0-9])_(.+?)_(?![A-Za-z0-9])", r"\1", text)
    text = text.replace("`", "")
    return text


def _latin1(text: str) -> str:
    for src, dst in _REPLACE.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", "replace").decode("latin-1")


def _inline_html(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>',
        out,
    )
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![A-Za-z0-9])_(.+?)_(?![A-Za-z0-9])", r"<i>\1</i>", out)
    return out


def markdown_to_html(markdown: str, title: str) -> str:
    """Self-contained, printable HTML document (open in a browser, Print -> PDF)."""

    parts: list[str] = []
    bullets: list[str] = []

    def close_list() -> None:
        if bullets:
            parts.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")
            bullets.clear()

    for b in parse_markdown(markdown):
        if b.kind == "bullet":
            bullets.append(_inline_html(b.text))
            continue
        close_list()
        if b.kind in ("h1", "h2", "h3"):
            parts.append(f"<{b.kind}>{_inline_html(b.text)}</{b.kind}>")
        elif b.kind == "quote":
            parts.append(f"<blockquote>{_inline_html(b.text)}</blockquote>")
        elif b.kind == "p":
            parts.append(f"<p>{_inline_html(b.text)}</p>")
        elif b.kind == "table" and b.rows:
            head, *body = b.rows
            th = "".join(f"<th>{_inline_html(c)}</th>" for c in head)
            trs = "".join(
                "<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in r) + "</tr>" for r in body
            )
            parts.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>")
    close_list()
    style = (
        "body{font-family:Segoe UI,Arial,sans-serif;max-width:960px;margin:32px auto;"
        "padding:0 20px;color:#1b2333;line-height:1.5}h1{color:#0b3d91;border-bottom:3px "
        "solid #0b3d91;padding-bottom:6px}h2{color:#0b3d91;margin-top:28px}"
        "table{border-collapse:collapse;width:100%;margin:10px 0;font-size:.88rem}"
        "th,td{border:1px solid #c9d3e6;padding:5px 8px;text-align:left;vertical-align:top;"
        "word-break:break-word}th{background:#e8eefb}blockquote{border-left:4px solid #9fb2d6;"
        "margin:10px 0;padding:6px 12px;background:#f4f7fd;color:#3a4660}"
        ".disc{margin-top:30px;font-size:.82rem;color:#5a6680;border-top:1px solid #c9d3e6;"
        "padding-top:10px}@media print{body{margin:0}}"
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>'
        f"{html.escape(title)}</title><style>{style}</style></head><body>"
        + "".join(parts)
        + f'<p class="disc">{html.escape(DISCLAIMER)}</p></body></html>'
    )


def pdf_available() -> bool:
    try:
        import fpdf  # noqa: F401
    except ImportError:
        return False
    return True


def markdown_to_pdf(markdown: str, title: str) -> bytes:
    """Formatted A4 PDF of the report. Requires the optional ``fpdf2`` package."""

    from fpdf import FPDF
    from fpdf.enums import XPos, YPos

    class _Pdf(FPDF):
        def header(self) -> None:
            self.set_font("Helvetica", "B", 8)
            self.set_text_color(110, 120, 140)
            self.cell(0, 6, _latin1(f"AMRP - {title}"), align="L")
            self.ln(8)

        def footer(self) -> None:
            self.set_y(-12)
            self.set_font("Helvetica", "I", 7)
            self.set_text_color(110, 120, 140)
            self.cell(0, 6, f"Not investment advice  |  Page {self.page_no()}/{{nb}}", align="C")

    pdf = _Pdf(format="A4")
    pdf.set_title(_latin1(title))
    pdf.set_author("AMRP - Agentic Multi-Market Research Platform")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(14, 14, 14)
    pdf.add_page()
    nl = {"new_x": XPos.LMARGIN, "new_y": YPos.NEXT}
    sizes = {"h1": 17, "h2": 13, "h3": 11}
    for b in parse_markdown(markdown):
        pdf.set_text_color(27, 35, 51)
        if b.kind in sizes:
            pdf.ln(3 if b.kind != "h1" else 0)
            pdf.set_font("Helvetica", "B", sizes[b.kind])
            pdf.set_text_color(11, 61, 145)
            pdf.multi_cell(0, 7, _latin1(_plain(b.text)), **nl)
            if b.kind == "h1":
                pdf.set_draw_color(11, 61, 145)
                pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(2)
        elif b.kind == "quote":
            pdf.set_font("Helvetica", "I", 9)
            pdf.set_text_color(70, 80, 100)
            pdf.multi_cell(0, 5, _latin1(_plain(b.text)), **nl)
            pdf.ln(2)
        elif b.kind == "p":
            pdf.set_font("Helvetica", "", 9.5)
            pdf.multi_cell(0, 5, _latin1(_plain(b.text)), **nl)
            pdf.ln(2)
        elif b.kind == "bullet":
            pdf.set_font("Helvetica", "", 9.5)
            pdf.multi_cell(0, 5, _latin1("- " + _plain(b.text)), **nl)
        elif b.kind == "table" and b.rows:
            width = max(len(r) for r in b.rows)
            rows = [[_latin1(_plain(c)) for c in r] + [""] * (width - len(r)) for r in b.rows]
            pdf.set_font("Helvetica", "", 7.5 if width > 4 else 8.5)
            pdf.set_draw_color(201, 211, 230)
            with pdf.table(
                line_height=4.2,
                headings_style=_heading_style(),
                text_align="LEFT",
            ) as tbl:
                for r in rows:
                    row = tbl.row()
                    for c in r:
                        row.cell(c)
            pdf.ln(3)
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(90, 102, 128)
    pdf.multi_cell(0, 4.5, _latin1(DISCLAIMER), **nl)
    pdf.set_font("Helvetica", "", 7)
    pdf.multi_cell(
        0, 4, f"Generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC by AMRP dashboard.", **nl
    )
    return bytes(pdf.output())


def _heading_style():  # type: ignore[no-untyped-def]
    from fpdf.fonts import FontFace

    return FontFace(emphasis="BOLD", color=(11, 61, 145), fill_color=(232, 238, 251))

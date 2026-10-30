"""Centered fixed-position loading spinner overlay that never shifts the page layout.

The slot lives at the end of the sidebar and its pill is ``position: fixed``, so
showing or clearing it does not move any content in the main area (unlike
``st.spinner``, which inserts a block at the top of the page).
"""

from __future__ import annotations

import html
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_STYLE = (
    "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:100000;"
    "display:flex;flex-direction:column;align-items:center;gap:12px;padding:22px 30px;"
    "border-radius:16px;background:rgba(17,28,51,.88);color:#C9D6EE;font-size:1rem;"
    "box-shadow:0 4px 18px rgba(0,0,0,.45);pointer-events:none"
)
_SPIN = (
    '<span style="display:block;width:56px;height:56px;border:5px solid #2A3B5F;'
    'border-top-color:#5B8DEF;border-radius:50%;animation:amrpspin .9s linear infinite"></span>'
)
_KEYFRAMES = "<style>@keyframes amrpspin{to{transform:rotate(360deg)}}</style>"

LABEL = "Loading…"  # single label everywhere; status text is never shown

_slot: Any = None


def pill_html(text: str) -> str:
    return (
        f'{_KEYFRAMES}<div class="amrp-loading" style="{_STYLE}">{_SPIN}<span>{html.escape(LABEL)}</span></div>'
    )


def attach(slot: Any) -> None:
    """Register the placeholder (an ``st.empty()``) the pill is drawn into."""

    global _slot
    _slot = slot


def show(text: str = "Loading…") -> None:
    if _slot is not None:
        _slot.markdown(pill_html(text), unsafe_allow_html=True)


def clear() -> None:
    if _slot is not None:
        _slot.empty()


@contextmanager
def loading(text: str = "Loading…", restore: str | None = None) -> Iterator[None]:
    """Show the pill for the block; afterwards restore another text or clear it."""

    show(text)
    try:
        yield
    finally:
        if restore:
            show(restore)
        else:
            clear()

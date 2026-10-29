"""Compact fixed-position loading pill that never shifts the page layout.

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
    "position:fixed;top:64px;right:24px;z-index:1000;height:30px;line-height:30px;"
    "padding:0 12px;border-radius:15px;background:rgba(17,28,51,.92);color:#C9D6EE;"
    "font-size:.78rem;border:1px solid #2A3B5F;box-shadow:0 2px 8px rgba(0,0,0,.35);"
    "white-space:nowrap;pointer-events:none"
)
_SPIN = (
    '<span style="display:inline-block;width:10px;height:10px;margin-right:7px;'
    "border:2px solid #5B8DEF;border-top-color:transparent;border-radius:50%;"
    'animation:amrpspin .8s linear infinite;vertical-align:-1px"></span>'
)
_KEYFRAMES = "<style>@keyframes amrpspin{to{transform:rotate(360deg)}}</style>"

_slot: Any = None


def pill_html(text: str) -> str:
    return (
        f'{_KEYFRAMES}<div class="amrp-loading" style="{_STYLE}">{_SPIN}{html.escape(text)}</div>'
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

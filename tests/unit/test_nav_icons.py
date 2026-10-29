"""Every sidebar page slug must have a matching nav icon."""

import re
import sys
from pathlib import Path


def test_every_nav_page_has_icon():
    root = Path(__file__).resolve()
    while not (root / "dashboard" / "app.py").exists():
        root = root.parent
    sys.path.insert(0, str(root / "dashboard"))
    import theme

    app = (root / "dashboard" / "app.py").read_text(encoding="utf8")
    start = app.index("PAGES = {")
    block = app[start : app.index("}", start)]
    slugs = re.findall(r'\("(\w+)",\s*\w+_page\)', block)
    assert {"memory", "backtest"} <= set(slugs)
    missing = [s for s in slugs if s not in theme._NAV_SVG]
    assert not missing, missing
    assert ".st-key-nav_memory" in theme.NAV_ICON_CSS
    assert ".st-key-nav_backtest" in theme.NAV_ICON_CSS

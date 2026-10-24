"""Environment variable lookup with deprecated pre-rename fallbacks.

AMRP_* names are canonical. The old AERP_* names (and EQUITY_RESEARCH_CONTROL_TOKEN)
are still read so existing setups keep working, with a DeprecationWarning.
"""

from __future__ import annotations

import os
import warnings

LEGACY_NAMES: dict[str, tuple[str, ...]] = {
    "AMRP_API_URL": ("AERP_API_URL",),
    "AMRP_CONTROL_URL": ("AERP_CONTROL_URL",),
    "AMRP_RUN_TIMEOUT": ("AERP_RUN_TIMEOUT",),
    "AMRP_DEMO_API_KEY": ("AERP_DEMO_API_KEY",),
    "AMRP_CONTROL_TOKEN": ("EQUITY_RESEARCH_CONTROL_TOKEN",),
}


def env(name: str, default: str = "") -> str:
    """Return ``name`` from the environment, falling back to its deprecated alias."""
    value = os.environ.get(name)
    if value:
        return value
    for old in LEGACY_NAMES.get(name, ()):
        legacy = os.environ.get(old)
        if legacy:
            warnings.warn(
                f"{old} is deprecated; rename it to {name}", DeprecationWarning, stacklevel=2
            )
            return legacy
    return value if value is not None else default

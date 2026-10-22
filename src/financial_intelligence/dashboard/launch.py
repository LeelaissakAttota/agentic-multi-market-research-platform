"""Start the REST API or control API with the dashboard's saved settings applied.

Usage: python -m financial_intelligence.dashboard.launch api|control [--port N]

* live-data toggles from dashboard_settings.json are applied only if that file exists
  (otherwise your own .env / environment stays in charge);
* keys saved on the Settings page (local key store) are passed to the process
  environment in memory; nothing is printed or written.
"""

from __future__ import annotations

import argparse
import os

from financial_intelligence.control.secrets_store import SecretStore, default_config_dir
from financial_intelligence.control.service import KEY_FIELDS
from financial_intelligence.dashboard.prefs import env_from_prefs, load_prefs, prefs_path


def apply_saved_settings(environ: dict[str, str] | None = None) -> list[str]:
    """Update environ in place; return the names (never values) that were set."""

    env = os.environ if environ is None else environ
    changed: list[str] = []
    if prefs_path().exists():
        for name, value in env_from_prefs(load_prefs()).items():
            env[name] = value
            changed.append(name)
    store = SecretStore(default_config_dir("equity-research") / "secrets.json", tuple(KEY_FIELDS))
    for name in KEY_FIELDS:
        secret = store.get(name)
        if secret and not env.get(name):
            env[name] = secret
            changed.append(name)
    return changed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=["api", "control"])
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    names = apply_saved_settings()
    if names:
        print("applied saved dashboard settings: " + ", ".join(sorted(names)))
    if args.target == "control":
        import sys

        from financial_intelligence.control import server

        sys.argv = ["control", "--port", str(args.port or 8712)]
        server.main()
        return
    import uvicorn

    uvicorn.run(
        "financial_intelligence.main:app",
        host="127.0.0.1",
        port=args.port or 8000,
        log_level="info",
    )


if __name__ == "__main__":
    main()

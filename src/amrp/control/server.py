"""Run the AMRP control API on 127.0.0.1 (never a public interface)."""

from __future__ import annotations

import argparse

import uvicorn

from amrp.control.api import build_control_app
from amrp.control.service import EquityResearchControl, admin_token_from_env

#: Service name reported by ``/control/health``.
SERVICE_NAME = "amrp"


def main() -> None:
    parser = argparse.ArgumentParser(description="AMRP supervised-control API")
    parser.add_argument("--port", type=int, default=8712)
    args = parser.parse_args()
    control = EquityResearchControl()
    app = build_control_app(
        control.manager,
        control.secrets,
        admin_token=admin_token_from_env(),
        title=SERVICE_NAME,
        analyzer=control.analyze_market,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()

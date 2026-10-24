"""ASGI entrypoint for local and container launches."""

from amrp.api import create_app

app = create_app()

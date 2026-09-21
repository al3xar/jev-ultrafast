"""Local startup for the Jev service: `uv run jev-service`."""

import os

import uvicorn

from ..demo import load_environment
from .app import create_app


def run():
    load_environment()
    port = int(os.environ.get("JUV_SERVICE_PORT", "8765"))
    uvicorn.run(create_app(), host="127.0.0.1", port=port)

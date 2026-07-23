"""ASGI entrypoint.

Run with:  uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

from api.app import create_app

app = create_app()

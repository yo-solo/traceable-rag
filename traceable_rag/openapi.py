from __future__ import annotations

from .web_app import app


def build_openapi_spec() -> dict:
    return app.openapi()

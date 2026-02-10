"""WSGI entrypoint wrapper.

Exposes the Flask app as `LightApp.app_light:app` while keeping the main
implementation in the repository root `app_light.py` (used by local scripts).
"""

from app_light import app  # noqa: F401

__all__ = ["app"]

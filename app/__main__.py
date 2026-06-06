"""Entrypoint for `python -m app` (used by the Docker image's uvicorn fallback).

Mirrors `app/main.py` so PyInstaller can find a single module to bundle.
"""

from app.main import app  # noqa: F401

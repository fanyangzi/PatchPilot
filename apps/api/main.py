"""ASGI entrypoint: uvicorn apps.api.main:app --reload"""
from patchpilot.api import app
__all__ = ["app"]

"""Command registration and public entry point."""

# Import order preserves the command index shown by Typer.
# ruff: noqa: F401, I001
from .core import app, main
from . import maintenance as _maintenance
from . import browse as _browse
from . import images as _images
from . import doctor as _doctor
from . import schema as _schema

__all__ = ["app", "main"]

"""Compatibility exports for document rendering operations."""

from .rendering import (
    cached_render_pages,
    export_pdf,
    register_annotated_pdf,
    render_page,
    render_pages,
)

__all__ = [
    "cached_render_pages",
    "export_pdf",
    "register_annotated_pdf",
    "render_page",
    "render_pages",
]

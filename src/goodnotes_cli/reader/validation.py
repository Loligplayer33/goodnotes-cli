"""Exact scalar and projection-derived identity checks for stored text."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import GoodnotesError
from ..models import PageRef


def required_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise GoodnotesError(f"Search index has an invalid `{field}`; expected an integer.")
    return value


def matches_pdf_identity(row: Mapping[str, Any], page_ref: PageRef) -> bool:
    """Match a stored positive integer page to the projection's PDF identity."""
    if not page_ref.attachment_id or page_ref.pdf_page_no is None:
        return False
    content_page = row.get("pdf_page_no")
    return (
        isinstance(content_page, int)
        and not isinstance(content_page, bool)
        and content_page > 0
        and row["document_id"] == page_ref.document_id
        and row["attachment_id"] == page_ref.attachment_id
        and content_page == page_ref.pdf_page_no
    )

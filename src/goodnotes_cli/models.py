"""Values shared by the storage, document, and command modules."""

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    kind: str
    path: str
    parent_id: str | None = None
    page_count: int = 0
    updated_at: float = 0
    root_folder_id: str | None = None


@dataclass(frozen=True)
class Page:
    id: str
    document_id: str
    number: int
    position: str
    attachment_path: str | None = None
    pdf_page: int | None = None
    width: float | None = None
    height: float | None = None
    annotation_count: int | None = 0
    rotation: int = 0


@dataclass(frozen=True)
class PageRef:
    """Projection-derived identity for one active page and its PDF background."""

    document_id: str
    page_id: str
    attachment_id: str | None = None
    pdf_page_no: int | None = None


@dataclass(frozen=True)
class TextPart:
    source: str
    text: str
    engine: str | None = None
    priority: str | None = None
    truncated: bool = False


@dataclass(frozen=True)
class PageText:
    page_id: str
    document_id: str
    page: int
    parts: list[TextPart] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class RecognitionStatus:
    """Stored handwriting-index metadata for one active page."""

    source: str
    text_rows: int | None
    text_chars: int | None
    availability: Literal["stored-text", "no-stored-text", "unavailable"]
    index_state: Literal["pending", "not-pending", "unknown"]


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    path: str
    page: int
    page_id: str
    source: str
    text: str

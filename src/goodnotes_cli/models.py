"""Values shared by the storage, document, and command modules."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    kind: str
    path: str
    parent_id: str | None = None
    page_count: int = 0
    updated_at: float = 0


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
    annotation_count: int = 0
    rotation: int = 0


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
class SearchHit:
    document_id: str
    path: str
    page: int
    page_id: str
    source: str
    text: str

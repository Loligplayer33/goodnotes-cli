"""Bundle evidence, manifest and response construction."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

from ..errors import GoodnotesError
from ..presentation.json import serializable
from ..presentation.selection import bound_page_text


class _BundlePage(TypedDict):
    page: int
    image: str | None
    text: dict[str, Any]
    render: NotRequired[dict[str, Any]]
    render_error: NotRequired[dict[str, Any]]
    warnings: NotRequired[list[str]]


def _read_bundle_text(
    read_page: Callable[[int, int], object],
    document_id: str,
    selected: list[int],
    max_chars: int,
) -> list[dict[str, Any]]:
    """Read and verify evidence before any managed image is published."""
    texts: list[dict[str, Any]] = []
    remaining = max_chars
    for number in selected:
        text = serializable(read_page(number, remaining))
        if not isinstance(text, dict) or text.get("document_id") != document_id:
            raise GoodnotesError("Bundle page text has missing or inconsistent document identity.")
        texts.append(text)
        remaining -= sum(len(part["text"]) for part in text["parts"])
    return texts


def _bundle_content(
    read_page: Callable[[int, int], object],
    document_id: str,
    selected: list[int],
    rendered_pages: list[dict[str, object]],
    max_chars: int,
) -> tuple[list[_BundlePage], bool]:
    manifest: list[_BundlePage] = []
    remaining = max_chars
    for number, rendered in zip(selected, rendered_pages, strict=True):
        page_data = read_page(number, remaining)
        text = serializable(page_data)
        if not isinstance(text, dict) or text.get("document_id") != document_id:
            raise GoodnotesError("Bundle page text has missing or inconsistent document identity.")
        if "render_error" not in rendered and rendered.get("document_id") != document_id:
            raise GoodnotesError(
                "Bundle page render has missing or inconsistent document identity."
            )
        remaining -= sum(len(part["text"]) for part in text["parts"])
        render_error = rendered.get("render_error")
        if isinstance(render_error, dict):
            manifest.append(
                {
                    "page": number,
                    "image": None,
                    "render_error": dict(render_error),
                    "warnings": [
                        f"Native image rendering failed for page {number}; no image is available."
                    ],
                    "text": text,
                }
            )
        else:
            manifest.append(
                {
                    "page": number,
                    "image": f"page-{number:04d}.png",
                    "render": dict(rendered),
                    "text": text,
                }
            )
    return manifest, bound_page_text([entry["text"] for entry in manifest], max_chars)


def _write_bundle_manifest(
    temporary: Path,
    manifest: list[_BundlePage],
    truncated: bool,
    snapshot_id: str | None,
    captured_at: str | None,
    source: str,
) -> None:
    content = {
        "snapshot": snapshot_id,
        "captured_at": captured_at,
        "pages": manifest,
        "truncated": truncated,
        "text_source": source,
    }
    failed_pages = [
        entry["page"] for entry in manifest if isinstance(entry.get("render_error"), dict)
    ]
    if failed_pages:
        content.update(
            {
                "status": "partial",
                "failed_pages": failed_pages,
                "warnings": [_bundle_partial_warning(failed_pages)],
            }
        )
    (temporary / "text.json").write_text(json.dumps(content, ensure_ascii=False))


def _bundle_response(
    destination: Path,
    manifest: list[_BundlePage],
    selected: list[int],
    layer: str,
    truncated: bool,
    source: str,
) -> dict[str, Any]:
    response = {
        "directory": str(destination),
        "manifest": str(destination / "text.json"),
        "pages": selected,
        "layer": layer,
        "truncated": truncated,
        "captured_at": next(
            (
                entry["render"].get("captured_at")
                for entry in manifest
                if isinstance(entry.get("render"), dict)
            ),
            None,
        ),
        "text_source": source,
        "page_evidence": [
            {
                **entry,
                "image": (
                    str(destination / cast(str, entry["image"]))
                    if isinstance(entry.get("image"), str)
                    else None
                ),
            }
            for entry in manifest
        ],
    }
    failed_pages = [
        entry["page"] for entry in manifest if isinstance(entry.get("render_error"), dict)
    ]
    if failed_pages:
        response.update(
            {
                "status": "partial",
                "failed_pages": failed_pages,
                "warnings": [_bundle_partial_warning(failed_pages)],
            }
        )
    return response


def _bundle_partial_warning(failed_pages: list[int]) -> str:
    pages = ", ".join(str(page) for page in failed_pages)
    page_label = "page" if len(failed_pages) == 1 else "pages"
    image_notice = "no image was produced" if len(failed_pages) == 1 else "no images were produced"
    return (
        f"Native rendering failed for {page_label} {pages}; source-labelled text evidence "
        f"remains available, but {image_notice}."
    )

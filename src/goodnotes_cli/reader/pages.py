"""Page identities, hydration, and bounded scans for a snapshot."""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..errors import GoodnotesError
from ..models import Item, Page, PageRef
from . import index
from .attachments import AttachmentResolver
from .session import ReaderSession


class PageReader:
    def __init__(self, session: ReaderSession, attachments: AttachmentResolver) -> None:
        self._session = session
        self.attachments = attachments
        self.clear()

    def clear(self) -> None:
        self._page_numbers: dict[tuple[str, str], int] | None = None
        self._page_cache: dict[tuple[str, str], Page] = {}
        self._pages_by_notebook: dict[str, list[Page]] = {}
        self._attachment_state_by_page: dict[tuple[str, str], tuple[str, str | None]] = {}
        self._page_refs_cache: dict[
            tuple[tuple[str, ...], tuple[str, ...] | None], list[PageRef]
        ] = {}

    @property
    def attachment_state_by_page(self) -> dict[tuple[str, str], tuple[str, str | None]]:
        return self._attachment_state_by_page

    def pages(self, notebook_id: str) -> list[Page]:
        cached = self._pages_by_notebook.get(notebook_id)
        if cached is not None:
            return list(cached)
        assert self._session.projection is not None
        index.sql_batch_size(self._session.projection)
        rows = self._session.projection.execute(
            """SELECT id, document_id, template_id, position, group_id FROM pages
               WHERE document_id = ? AND COALESCE(deleted, 0) = 0 ORDER BY position, id""",
            (notebook_id,),
        ).fetchall()
        pages = self.hydrate_page_rows(rows)
        self._pages_by_notebook[notebook_id] = pages
        return list(pages)

    def page_refs_for_notebooks(
        self, notebook_ids: set[str], *, page_ids: set[str] | None = None
    ) -> list[PageRef]:
        """Read active page identities and current PDF mappings from the projection."""
        if not notebook_ids:
            return []
        cache_key = (
            tuple(sorted(notebook_ids)),
            tuple(sorted(page_ids)) if page_ids is not None else None,
        )
        cached = self._page_refs_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        assert self._session.projection is not None
        page_columns = self._session.table_columns(self._session.projection, "pages")
        template_columns = (
            self._session.table_columns(self._session.projection, "templates")
            if self._session.table_exists(self._session.projection, "templates")
            else set()
        )
        has_projection_mapping = {"template_id", "id", "document_id"} <= page_columns and {
            "id",
            "document_id",
            "attachment_id",
            "page_number",
        } <= template_columns
        if has_projection_mapping:
            identity = "t.attachment_id AS attachment_id, t.page_number AS page_number"
            template_join = (
                "LEFT JOIN templates AS t ON t.id = p.template_id AND t.document_id = p.document_id"
            )
        else:
            identity = "NULL AS attachment_id, NULL AS page_number"
            template_join = ""
        has_position = "position" in page_columns
        order = "p.document_id, p.position, p.id" if has_position else "p.document_id, p.id"
        position = "p.position" if has_position else "NULL"
        requested_pages = sorted(page_ids) if page_ids is not None else []
        bind_limit = min(
            index.SQL_BIND_LIMIT,
            self._session.projection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER),
        )
        if page_ids is not None and bind_limit < 2:
            raise GoodnotesError("SQLite parameter limit is too low for page scope queries.")

        # Preserve the original logical 400-notebook / 300-page batch order even when
        # a statement must be divided into smaller queries to fit the bind limit.
        notebook_groups = index.batches(sorted(notebook_ids), index.SQL_BIND_LIMIT)
        page_groups = list(index.batches(requested_pages, 300)) if page_ids is not None else [None]
        result: list[PageRef] = []
        for notebook_group in notebook_groups:
            for page_group in page_groups:
                notebook_size = (
                    bind_limit
                    if page_group is None
                    else max(1, bind_limit - min(300, len(page_group), bind_limit - 1))
                )
                group_rows: list[sqlite3.Row] = []
                query_count = 0

                def query_rows(
                    notebooks: list[str] | None, pages: list[str] | None
                ) -> list[sqlite3.Row]:
                    filters = ["COALESCE(p.deleted, 0) = 0"]
                    parameters: list[str] = []
                    for column, values in (("p.document_id", notebooks), ("p.id", pages)):
                        if values is not None:
                            filters.append(f"{column} IN ({', '.join('?' for _ in values)})")
                            parameters.extend(values)
                    connection = self._session.projection
                    assert connection is not None
                    return list(
                        connection.execute(
                            f"""
                        SELECT p.id AS page_id, p.document_id, {position} AS sort_position,
                               {identity}
                        FROM pages AS p
                        {template_join}
                        WHERE {" AND ".join(filters)}
                        ORDER BY {order}
                        """,
                            parameters,
                        )
                    )

                for notebook_batch in index.batches(notebook_group, notebook_size):
                    page_size = bind_limit - len(notebook_batch)
                    page_batches = (
                        index.batches(page_group, page_size) if page_group is not None else [None]
                    )
                    for page_batch in page_batches:
                        query_count += 1
                        group_rows.extend(query_rows(notebook_batch, page_batch))
                if query_count > 1:
                    counts = Counter(tuple(row) for row in group_rows)
                    if page_group is not None and len(page_group) <= bind_limit:
                        ordered = query_rows(None, page_group)
                    elif len(notebook_group) <= bind_limit:
                        ordered = query_rows(notebook_group, None)
                    else:
                        ordered = query_rows(None, None)
                    group_rows = []
                    for row in ordered:
                        key = tuple(row)
                        if counts[key]:
                            group_rows.append(row)
                            counts[key] -= 1
                for row in group_rows:
                    attachment_id = row["attachment_id"]
                    if not isinstance(attachment_id, str) or not attachment_id:
                        attachment_id = None
                    page_number = row["page_number"]
                    pdf_page_no = (
                        page_number
                        if isinstance(page_number, int)
                        and not isinstance(page_number, bool)
                        and page_number > 0
                        else None
                    )
                    result.append(
                        PageRef(
                            document_id=self._required_text(
                                row["document_id"], "pages.document_id"
                            ),
                            page_id=self._required_text(row["page_id"], "pages.id"),
                            attachment_id=attachment_id,
                            pdf_page_no=pdf_page_no,
                        )
                    )
        self._page_refs_cache[cache_key] = result
        return list(result)

    def iter_page_window(
        self, notebooks: list[Item], *, offset: int, count: int
    ) -> Iterator[tuple[Item, Page]]:
        """Hydrate only pages in a bounded scan window, skipping by catalog counts."""
        if count <= 0:
            return
        assert self._session.projection is not None
        skipped = offset
        remaining = count
        for notebook in notebooks:
            if remaining <= 0:
                break
            if skipped >= notebook.page_count:
                skipped -= notebook.page_count
                continue
            local_offset = skipped
            skipped = 0
            fetch_count = min(remaining, notebook.page_count - local_offset)
            index.sql_batch_size(self._session.projection, fixed=2)
            rows = self._session.projection.execute(
                """
                SELECT id, document_id, template_id, position, group_id
                FROM pages
                WHERE document_id = ? AND COALESCE(deleted, 0) = 0
                ORDER BY position, id
                LIMIT ? OFFSET ?
                """,
                (notebook.id, fetch_count, local_offset),
            ).fetchall()
            pages = self.hydrate_page_rows(
                rows,
                numbers=list(range(local_offset + 1, local_offset + len(rows) + 1)),
            )
            for page in pages:
                yield notebook, page
            remaining -= len(rows)

    def hydrate_page_rows(
        self, rows: list[sqlite3.Row], *, numbers: list[int] | None = None
    ) -> list[Page]:
        """Hydrate a bounded page set with batched template, note, and attachment reads."""
        if not rows:
            return []
        assert self._session.projection is not None
        if numbers is None:
            numbers = list(range(1, len(rows) + 1))
        if len(numbers) != len(rows):
            raise GoodnotesError("Page hydration received inconsistent page numbers.")

        missing: list[sqlite3.Row] = []
        number_by_page: dict[tuple[str, str], int] = {}
        for row, number in zip(rows, numbers, strict=True):
            document_id = self._required_text(row["document_id"], "pages.document_id")
            page_id = self._required_text(row["id"], "pages.id")
            number_by_page[(document_id, page_id)] = number
            if (document_id, page_id) not in self._page_cache:
                missing.append(row)

        templates: dict[tuple[str, str], sqlite3.Row] = {}
        template_ids = sorted(
            {
                row["template_id"]
                for row in missing
                if isinstance(row["template_id"], str) and row["template_id"]
            }
        )
        if template_ids and self._session.table_exists(self._session.projection, "templates"):
            for batch in index.id_batches(
                template_ids, index.sql_batch_size(self._session.projection)
            ):
                placeholders = ", ".join("?" for _ in batch)
                for template in self._session.projection.execute(
                    f"""
                    SELECT id, document_id, attachment_id, page_number, size_width, size_height
                    FROM templates WHERE id IN ({placeholders}) ORDER BY id
                    """,
                    batch,
                ):
                    key = (template["document_id"], template["id"])
                    templates.setdefault(key, template)

        attachment_ids = sorted(
            {
                template["attachment_id"]
                for template in templates.values()
                if isinstance(template["attachment_id"], str) and template["attachment_id"]
            }
        )
        attachments: dict[str, sqlite3.Row] = {}
        if attachment_ids and self._session.table_exists(self._session.projection, "attachments"):
            for batch in index.id_batches(
                attachment_ids, index.sql_batch_size(self._session.projection)
            ):
                placeholders = ", ".join("?" for _ in batch)
                for attachment in self._session.projection.execute(
                    f"SELECT id, path FROM attachments WHERE id IN ({placeholders}) ORDER BY id",
                    batch,
                ):
                    attachments.setdefault(attachment["id"], attachment)

        group_ids = sorted(
            {
                row["group_id"]
                for row in missing
                if isinstance(row["group_id"], str) and row["group_id"]
            }
        )
        annotation_counts: dict[str, int] = {}
        notes_items_available = self._session.table_exists(
            self._session.projection, "notes_items"
        ) and {
            "group_id",
            "deleted",
        } <= self._session.table_columns(self._session.projection, "notes_items")
        if group_ids and notes_items_available:
            for batch in index.id_batches(
                group_ids, index.sql_batch_size(self._session.projection)
            ):
                placeholders = ", ".join("?" for _ in batch)
                annotation_counts.update(
                    {
                        result["group_id"]: int(result["annotation_count"])
                        for result in self._session.projection.execute(
                            f"""
                            SELECT group_id, count(*) AS annotation_count
                            FROM notes_items
                            WHERE group_id IN ({placeholders})
                              AND COALESCE(deleted, 0) = 0
                            GROUP BY group_id
                            """,
                            batch,
                        )
                    }
                )

        for row in missing:
            page_id = self._required_text(row["id"], "pages.id")
            document_id = self._required_text(row["document_id"], "pages.document_id")
            position = self._required_text(row["position"], "pages.position", allow_empty=True)
            template_id = row["template_id"]
            template = (
                templates.get((document_id, template_id))
                if isinstance(template_id, str) and template_id
                else None
            )
            attachment_id = template["attachment_id"] if template is not None else None
            attachment_row = (
                attachments.get(attachment_id)
                if isinstance(attachment_id, str) and attachment_id
                else None
            )
            if not attachment_id:
                attachment_path, attachment_state = None, "unmapped"
            elif attachment_row is None:
                attachment_path, attachment_state = None, "missing"
            else:
                raw_attachment_path = attachment_row["path"]
                attachment_path, attachment_state = self.attachments.resolve(raw_attachment_path)
                if (
                    attachment_path is None
                    and attachment_state == "missing"
                    and isinstance(raw_attachment_path, str)
                    and raw_attachment_path
                    and not Path(raw_attachment_path).is_absolute()
                    and ".." not in Path(raw_attachment_path).parts
                ):
                    # Keep a safe relative reference so document rendering can report that
                    # its mapped file is missing, while re-validating it at the read boundary.
                    attachment_path = Path(raw_attachment_path)
            self._attachment_state_by_page[(document_id, page_id)] = (
                attachment_state,
                attachment_id if isinstance(attachment_id, str) else None,
            )

            raw_page = template["page_number"] if template is not None else None
            pdf_page = (
                raw_page if isinstance(raw_page, int) and not isinstance(raw_page, bool) else None
            )
            if pdf_page is not None and pdf_page < 1:
                pdf_page = None
            width = self._optional_dimension(
                template["size_width"] if template is not None else None
            )
            height = self._optional_dimension(
                template["size_height"] if template is not None else None
            )
            group_id = row["group_id"]
            if group_id is not None and not isinstance(group_id, str):
                raise GoodnotesError("Projection database has an invalid pages.group_id value.")
            annotation_count = (
                (annotation_counts.get(group_id, 0) if group_id else 0)
                if notes_items_available
                else None
            )
            page = Page(
                id=page_id,
                document_id=document_id,
                number=number_by_page[(document_id, page_id)],
                position=position,
                attachment_path=str(attachment_path) if attachment_path is not None else None,
                pdf_page=pdf_page,
                width=width,
                height=height,
                annotation_count=annotation_count,
            )
            self._page_cache[(document_id, page_id)] = page

        result: list[Page] = []
        for row, number in zip(rows, numbers, strict=True):
            document_id = self._required_text(row["document_id"], "pages.document_id")
            page_id = self._required_text(row["id"], "pages.id")
            page = self._page_cache[(document_id, page_id)]
            if page.number != number:
                page = Page(
                    id=page.id,
                    document_id=page.document_id,
                    number=number,
                    position=page.position,
                    attachment_path=page.attachment_path,
                    pdf_page=page.pdf_page,
                    width=page.width,
                    height=page.height,
                    annotation_count=page.annotation_count,
                    rotation=page.rotation,
                )
                self._page_cache[(document_id, page_id)] = page
            result.append(page)
        return result

    @staticmethod
    def _required_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str) or (not allow_empty and not value):
            suffix = "text" if allow_empty else "non-empty text"
            raise GoodnotesError(f"Projection database has an invalid {field}; expected {suffix}.")
        return value

    @staticmethod
    def _optional_dimension(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        result = float(value)
        return result if math.isfinite(result) else None

    def page_number_for_id(
        self, document_id: str, page_id: str, notebook_ids: set[str]
    ) -> int | None:
        self._ensure_page_numbers(notebook_ids)
        assert self._page_numbers is not None
        return self._page_numbers.get((document_id, page_id))

    def page_numbers_by_id(self, notebook_ids: set[str]) -> dict[str, int]:
        """Return active page ids for checking page-index metadata consistency."""
        self._ensure_page_numbers(notebook_ids)
        assert self._page_numbers is not None
        return {page_id: number for (_, page_id), number in self._page_numbers.items()}

    def _ensure_page_numbers(self, notebook_ids: set[str]) -> None:
        if self._page_numbers is not None:
            return
        assert self._session.projection is not None
        page_numbers: dict[tuple[str, str], int] = {}
        counts: dict[str, int] = {}
        for row in self._session.projection.execute(
            """
            SELECT id, document_id
            FROM pages
            WHERE COALESCE(deleted, 0) = 0
            ORDER BY document_id, position, id
            """
        ):
            page_id = self._required_text(row["id"], "pages.id")
            document_id = self._required_text(row["document_id"], "pages.document_id")
            if document_id not in notebook_ids:
                continue
            number = counts.get(document_id, 0) + 1
            counts[document_id] = number
            page_numbers[(document_id, page_id)] = number
        self._page_numbers = page_numbers

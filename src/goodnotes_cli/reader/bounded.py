"""Shared validation and materialization for bounded indexed-text rows."""

from __future__ import annotations

import sqlite3

from ..errors import GoodnotesError
from ..models import TextPart


def validate_metadata(
    row: sqlite3.Row, table: str, *, pdf: bool = False, handwriting: bool = False
) -> tuple[int, str | None, str | None]:
    text_id = row["text_id"]
    if (
        not isinstance(text_id, int)
        or isinstance(text_id, bool)
        or (pdf and row["text_id_type"] != "integer")
    ):
        raise GoodnotesError(f"Search index has an invalid `{table}.text_id`; expected an integer.")
    if row["text_type"] != "text":
        raise GoodnotesError(f"Search index has an invalid `{table}.text` value; expected text.")
    text_length = row["text_length"]
    if not isinstance(text_length, int) or isinstance(text_length, bool) or text_length < 0:
        raise GoodnotesError(
            f"Search index has an invalid `{table}.text` length; expected a non-negative integer."
        )
    engine = priority = None
    if handwriting:
        engine, priority = row["engine_type"], row["priority"]
        if engine is not None and not isinstance(engine, str):
            raise GoodnotesError(
                f"Search index has an invalid `{table}.engine_type`; expected text or NULL."
            )
        if priority is not None and not isinstance(priority, str):
            raise GoodnotesError(
                f"Search index has an invalid `{table}.priority`; expected text or NULL."
            )
    return text_length, engine, priority


def materialize(
    row: sqlite3.Row, table: str, source: str, budget: int | None
) -> tuple[TextPart | None, int | None, bool]:
    text_length, engine, priority = validate_metadata(
        row, table, pdf=source == "pdf", handwriting=source == "handwriting"
    )
    if budget is not None and row["contains_nul"]:
        raise GoodnotesError(
            f"Search index has embedded NUL in `{table}.text`; "
            "a bounded read cannot preserve this value."
        )
    text = row["bounded_text"]
    if not isinstance(text, str):
        raise GoodnotesError(f"Search index has an invalid `{table}.text` value; expected text.")
    truncated = budget is not None and (text_length > budget or len(text) > budget)
    if truncated:
        assert budget is not None
        text = text[:budget]
    part = (
        TextPart(source=source, text=text, engine=engine, priority=priority, truncated=truncated)
        if text
        else None
    )
    if budget is not None:
        budget = max(0, budget - len(text))
    return part, budget, truncated or (budget == 0 and row["row_number"] < row["row_count"])

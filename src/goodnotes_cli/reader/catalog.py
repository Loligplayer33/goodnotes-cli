"""Catalog graph and item navigation for a single read-only reader session."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..errors import GoodnotesError
from ..models import Item
from .session import ReaderSession

_FOLDER = 0
_NOTEBOOK = 1


class _DifferentKindError(GoodnotesError):
    """A selector resolves to an item of the wrong kind."""


@dataclass(frozen=True)
class _Entry:
    kind: str
    id: str
    name: str
    parent_id: str | None
    is_root: bool
    position: str
    updated_at: float
    page_count: int = 0


class Catalog:
    """Own the graph and navigation state for one open reader session."""

    def __init__(self, session: ReaderSession, root_folder_id: str | None = None) -> None:
        self._session = session
        self.root_folder_id = root_folder_id
        self.root_item_ids: dict[str, set[str]] | None = None
        self.active_items: dict[tuple[str, str], str] = {}
        self.entries: dict[tuple[str, str], _Entry] = {}
        self.links: dict[tuple[str, str], list[sqlite3.Row]] = {}
        self.children: dict[tuple[str, str] | None, list[tuple[str, str]]] = {}
        self.paths: dict[tuple[str, str], str | None] = {}
        self.duplicate_parent_count = 0
        self.cycle_count = 0
        self.orphan_count = 0
        self.broken_link_count = 0
        self.links_to_deleted_count = 0
        self.active_folder_count = 0
        self.active_notebook_count = 0

    def list_items(self, folder: str | Item = "/", kind: str | None = None) -> list[Item]:
        """List direct children of a folder, or direct children of the library root."""
        self._session.require_open()
        self.ensure_consistent_graph()
        normalized_kind = self._validate_kind(kind)
        parent_key = self.folder_key(folder, root_allowed=True)
        keys = self.children.get(parent_key, [])
        return [
            self._item_for_key(key)
            for key in keys
            if normalized_kind is None or key[0] == normalized_kind
        ]

    def tree(
        self,
        folder: str | Item = "/",
        max_depth: int | None = None,
    ) -> list[Item]:
        """Return descendant items in stored sibling order, in pre-order."""
        self._session.require_open()
        self.ensure_consistent_graph()
        if max_depth is not None and (
            isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0
        ):
            raise GoodnotesError("max_depth must be a non-negative integer or omitted.")
        parent_key = self.folder_key(folder, root_allowed=True)
        result: list[Item] = []

        pending = [(child, 1) for child in reversed(self.children.get(parent_key, []))]
        while pending:
            child_key, depth = pending.pop()
            if max_depth is not None and depth > max_depth:
                continue
            result.append(self._item_for_key(child_key))
            if child_key[0] == "folder":
                pending.extend(
                    (child, depth + 1) for child in reversed(self.children.get(child_key, []))
                )
        return result

    def resolve(self, selector: str | Item, kind: str | None = None) -> Item:
        """Resolve an exact id, exact full path, or unique basename without guessing."""
        self._session.require_open()
        self.ensure_consistent_graph()
        normalized_kind = self._validate_kind(kind)
        if isinstance(selector, Item):
            selector = selector.id
        if not isinstance(selector, str) or selector == "":
            raise GoodnotesError("An item selector is required.")

        id_matches = [key for key in self.active_items if key[1].casefold() == selector.casefold()]
        if id_matches:
            if len(id_matches) > 1:
                raise GoodnotesError("That UUID is ambiguous in the active library data.")
            if normalized_kind is not None and id_matches[0][0] != normalized_kind:
                raise _DifferentKindError("That selector identifies an item of a different kind.")
            return self._resolved_item(id_matches)

        path_selector = selector.startswith("/") or "/" in selector
        if path_selector:
            path = self._normalize_path_selector(selector)
            if not path:
                raise GoodnotesError("The library root has no item UUID to resolve.")
            matches = [
                key
                for key, candidate in self.paths.items()
                if candidate == path and (normalized_kind is None or key[0] == normalized_kind)
            ]
        else:
            matches = [
                key
                for key, name in self.active_items.items()
                if name == selector and (normalized_kind is None or key[0] == normalized_kind)
            ]
        if not matches:
            raise GoodnotesError("No active, reachable item matches that selector.")
        return self._resolved_item(matches)

    def load(self) -> None:
        assert self._session.projection is not None
        projection = self._session.projection
        required = ("folders", "documents", "folder_to_folder_items", "pages")
        if any(not self._session.table_exists(projection, name) for name in required):
            raise GoodnotesError(
                "The projection database does not contain the expected library tables."
            )

        required_columns = {
            "folders": {"id", "name", "updated_at", "deleted", "position"},
            "documents": {"id", "name", "updated_at", "deleted", "position"},
            "folder_to_folder_items": {
                "id",
                "item_type",
                "item_id",
                "parent_folder_id",
                "root_folder_id",
                "item_position",
                "deleted",
            },
            "pages": {"id", "document_id", "template_id", "position", "group_id", "deleted"},
        }
        for table, expected in required_columns.items():
            missing = expected - self._session.table_columns(projection, table)
            if missing:
                raise GoodnotesError(
                    f"Projection database table `{table}` is missing required columns: "
                    f"{', '.join(sorted(missing))}."
                )

        self.root_item_ids = None
        if self.root_folder_id is not None:
            if not isinstance(self.root_folder_id, str) or not self.root_folder_id.strip():
                raise GoodnotesError("Library root selection must be a nonempty root ID.")
            root_links = projection.execute(
                "SELECT item_type, item_id FROM folder_to_folder_items WHERE root_folder_id = ?",
                (self.root_folder_id,),
            ).fetchall()
            if not root_links:
                raise GoodnotesError("The selected library root is not present in this copy.")
            self.root_item_ids = {
                table: {row["item_id"] for row in root_links if row["item_type"] == item_type}
                for table, item_type in (("folders", _FOLDER), ("documents", _NOTEBOOK))
            }

        folder_rows = projection.execute(
            "SELECT id, name, updated_at, deleted, position FROM folders"
        ).fetchall()
        document_rows = projection.execute(
            "SELECT id, name, updated_at, deleted, position FROM documents"
        ).fetchall()
        folder_ids: set[str] = set()
        document_ids: set[str] = set()
        live_folders: dict[str, sqlite3.Row] = {}
        live_documents: dict[str, sqlite3.Row] = {}
        for kind, rows, identifiers, active in (
            ("folders", folder_rows, folder_ids, live_folders),
            ("documents", document_rows, document_ids, live_documents),
        ):
            for row in rows:
                if self.root_item_ids is not None and row["id"] not in self.root_item_ids[kind]:
                    continue
                item_id = self._required_text(row["id"], f"{kind}.id")
                identifiers.add(item_id)
                if self._deleted_flag(row["deleted"], f"{kind}.deleted"):
                    continue
                self._required_text(row["name"], f"{kind}.name")
                self._finite_number(row["updated_at"], f"{kind}.updated_at")
                self._required_text(row["position"], f"{kind}.position", allow_empty=True)
                active[item_id] = row
        self.active_items = {
            **{("folder", item_id): row["name"] for item_id, row in live_folders.items()},
            **{("notebook", item_id): row["name"] for item_id, row in live_documents.items()},
        }
        page_counts = {
            self._required_text(row["document_id"], "pages.document_id"): int(row["page_count"])
            for row in projection.execute(
                """
                SELECT document_id, count(*) AS page_count
                FROM pages
                WHERE COALESCE(deleted, 0) = 0
                GROUP BY document_id
                """
            )
            if row["document_id"] in live_documents
        }

        root_filter = " AND root_folder_id = ?" if self.root_folder_id is not None else ""
        active_rows = projection.execute(
            """
            SELECT item_type, item_id, parent_folder_id, root_folder_id, item_position
            FROM folder_to_folder_items
            WHERE COALESCE(deleted, 0) = 0
            """
            + root_filter
            + " ORDER BY item_position, id",
            (self.root_folder_id,) if self.root_folder_id is not None else (),
        ).fetchall()
        self.links = {}
        self.children = {}
        self.entries = {}
        self.duplicate_parent_count = 0
        self.broken_link_count = 0
        self.links_to_deleted_count = 0
        self.active_folder_count = len(live_folders)
        self.active_notebook_count = len(live_documents)
        for row in active_rows:
            if row["item_type"] == _FOLDER:
                kind, live_rows = "folder", live_folders
            elif row["item_type"] == _NOTEBOOK:
                kind, live_rows = "notebook", live_documents
            else:
                self.broken_link_count += 1
                continue
            item_id = row["item_id"]
            if not isinstance(item_id, str) or not item_id or item_id not in live_rows:
                identifiers = folder_ids if kind == "folder" else document_ids
                if item_id in identifiers:
                    self.links_to_deleted_count += 1
                else:
                    self.broken_link_count += 1
                continue
            key = (kind, item_id)
            self.links.setdefault(key, []).append(row)

        self.duplicate_parent_count = sum(1 for rows in self.links.values() if len(rows) > 1)
        self.entries = {}
        for key, rows in self.links.items():
            if len(rows) != 1:
                # Keep the item in the diagnostic catalog, but never select a parent by order.
                row = rows[0]
                parent_id, is_root = None, False
            else:
                row = rows[0]
                raw_parent = row["parent_folder_id"]
                root_id = row["root_folder_id"]
                if raw_parent is not None and not isinstance(raw_parent, str):
                    raise GoodnotesError(
                        "Projection database has an invalid folder_to_folder_items."
                        "parent_folder_id value; expected text or NULL."
                    )
                if root_id is not None and not isinstance(root_id, str):
                    raise GoodnotesError(
                        "Projection database has an invalid folder_to_folder_items."
                        "root_folder_id value; expected text or NULL."
                    )
                parent_is_root = (
                    raw_parent is None
                    or raw_parent == ""
                    or (raw_parent == root_id and raw_parent not in live_folders)
                )
                if parent_is_root:
                    parent_id, is_root = None, True
                elif raw_parent in live_folders:
                    parent_id, is_root = raw_parent, False
                else:
                    parent_id, is_root = None, False
                    self.broken_link_count += 1
            kind, item_id = key
            record = live_folders[item_id] if kind == "folder" else live_documents[item_id]
            link_position = row["item_position"]
            if link_position is not None and not isinstance(link_position, str):
                raise GoodnotesError(
                    "Projection database has an invalid folder_to_folder_items.item_position "
                    "value; expected text or NULL."
                )
            position = link_position or record["position"]
            self.entries[key] = _Entry(
                kind=kind,
                id=item_id,
                name=record["name"],
                parent_id=parent_id,
                is_root=is_root,
                position=position,
                updated_at=self._finite_number(record["updated_at"], f"{kind}.updated_at"),
                page_count=page_counts.get(item_id, 0) if kind == "notebook" else 0,
            )
            if len(rows) == 1 and (is_root or parent_id is not None):
                self.children.setdefault(parent_id_key(parent_id, is_root), []).append(key)

        for children in self.children.values():
            children.sort(key=lambda key: (self.entries[key].position, key[1]))

        self.paths = {}
        self.cycle_count = self._count_folder_cycles()
        self.orphan_count = len(live_folders) + len(live_documents) - len(self.entries)
        for key in self.entries:
            path = self._path_for_key(key)
            self.paths[key] = path
            if path is None:
                self.orphan_count += 1

    def _count_folder_cycles(self) -> int:
        completed: set[str] = set()
        cycle_members: set[str] = set()
        for kind, folder_id in self.entries:
            if kind != "folder" or folder_id in completed:
                continue
            chain: list[str] = []
            positions: dict[str, int] = {}
            current: str | None = folder_id
            while current is not None and current not in completed:
                if current in positions:
                    cycle_members.update(chain[positions[current] :])
                    break
                positions[current] = len(chain)
                chain.append(current)
                entry = self.entries.get(("folder", current))
                current = entry.parent_id if entry is not None else None
            completed.update(chain)
        return len(cycle_members)

    def _path_for_key(self, key: tuple[str, str]) -> str | None:
        chain: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        current = key
        path: str | None = None
        while current not in self.paths:
            if current in seen:
                break
            seen.add(current)
            entry = self.entries.get(current)
            if entry is None or len(self.links.get(current, [])) != 1:
                break
            chain.append(current)
            if entry.is_root:
                path = ""
                break
            if entry.parent_id is None:
                break
            current = ("folder", entry.parent_id)
        else:
            path = self.paths[current]
        for item in reversed(chain):
            if path is not None:
                path = _join_path(path, self.entries[item].name)
            self.paths[item] = path
        return self.paths.get(key)

    def ensure_consistent_graph(self) -> None:
        if self.duplicate_parent_count:
            raise GoodnotesError(
                "The library has items with multiple parent links; use `stats()` to inspect it."
            )
        if self.cycle_count:
            raise GoodnotesError("The library has a folder cycle; use `stats()` to inspect it.")

    def _validate_kind(self, kind: str | None) -> str | None:
        if kind is None:
            return None
        if not isinstance(kind, str):
            raise GoodnotesError("Item kind must be `folder` or `notebook`.")
        normalized = kind.casefold()
        if normalized not in {"folder", "notebook"}:
            raise GoodnotesError("Item kind must be `folder` or `notebook`.")
        return normalized

    def _resolved_item(self, matches: list[tuple[str, str]]) -> Item:
        if len(matches) > 1:
            raise GoodnotesError("That selector is ambiguous; use a full path or exact UUID.")
        key = matches[0]
        if self.paths.get(key) is None:
            raise GoodnotesError("That active item has no resolvable path in the library tree.")
        return self._item_for_key(key)

    def folder_key(
        self,
        folder: str | Item,
        root_allowed: bool,
    ) -> tuple[str, str] | None:
        if isinstance(folder, Item):
            folder = folder.id
        if isinstance(folder, str) and folder in {"", "/"}:
            if root_allowed:
                return None
            raise GoodnotesError("The library root is not an item.")
        item = self.resolve(folder, kind="folder")
        return ("folder", item.id)

    def _item_for_key(self, key: tuple[str, str]) -> Item:
        entry = self.entries[key]
        path = self.paths.get(key)
        if path is None:
            raise GoodnotesError("That active item has no resolvable path in the library tree.")
        return Item(
            id=entry.id,
            name=entry.name,
            kind=entry.kind,
            path=path,
            parent_id=entry.parent_id,
            page_count=entry.page_count,
            updated_at=entry.updated_at,
            root_folder_id=self.links[key][0]["root_folder_id"],
        )

    @staticmethod
    def _normalize_path_selector(selector: str) -> str:
        parts = selector.split("/")
        if any(part in {".", ".."} for part in parts):
            raise GoodnotesError(
                "Relative `.` and `..` path segments are not valid item selectors."
            )
        return selector.strip("/")

    @staticmethod
    def _required_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str) or (not allow_empty and not value):
            suffix = "text" if allow_empty else "non-empty text"
            raise GoodnotesError(f"Projection database has an invalid {field}; expected {suffix}.")
        return value

    @staticmethod
    def _finite_number(value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GoodnotesError(
                f"Projection database has an invalid {field}; expected a finite number."
            )
        result = float(value)
        if not math.isfinite(result):
            raise GoodnotesError(
                f"Projection database has an invalid {field}; expected a finite number."
            )
        return result

    @staticmethod
    def _deleted_flag(value: Any, field: str) -> bool:
        if value is None:
            return False
        if not isinstance(value, int) or isinstance(value, bool):
            raise GoodnotesError(
                f"Projection database has an invalid {field}; expected an integer flag or NULL."
            )
        return value != 0

    def notebook_ids_under(self, parent_key: tuple[str, str] | None) -> set[str]:
        ids: set[str] = set()
        pending = list(self.children.get(parent_key, []))
        while pending:
            child_key = pending.pop()
            if child_key[0] == "notebook":
                if self.paths.get(child_key) is not None:
                    ids.add(child_key[1])
            elif child_key[0] == "folder":
                pending.extend(self.children.get(child_key, []))
        return ids

    def active_items_by_kind(self, kind: str) -> set[str]:
        return {item_id for item_kind, item_id in self.active_items if item_kind == kind}


def parent_id_key(parent_id: str | None, is_root: bool) -> tuple[str, str] | None:
    return None if is_root or parent_id is None else ("folder", parent_id)


def _join_path(parent: str, child: str) -> str:
    if not parent:
        return child
    if not child:
        return parent
    return f"{parent}/{child}"

"""Scope selection and caching for a single reader session."""

from __future__ import annotations

from typing import Any

from ..errors import GoodnotesError
from ..models import Item
from .catalog import Catalog, _DifferentKindError


class ScopeResolver:
    """Resolve mutually exclusive folder and notebook scopes."""

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog
        self._scope_cache: dict[
            tuple[tuple[str, str] | str, tuple[str, str] | str | None],
            tuple[dict[str, Any], list[Item], set[str]],
        ] = {}

    def clear(self) -> None:
        self._scope_cache.clear()

    @staticmethod
    def _selector_cache_key(selector: str | Item | None) -> tuple[str, str] | str | None:
        if isinstance(selector, Item):
            return (selector.kind, selector.id)
        return selector

    def scope(
        self, folder: str | Item, notebook: str | Item | None
    ) -> tuple[dict[str, Any], list[Item], set[str]]:
        folder_key = self._selector_cache_key(folder)
        assert folder_key is not None
        cache_key = (folder_key, self._selector_cache_key(notebook))
        cached = self._scope_cache.get(cache_key)
        if cached is not None:
            scope, notebooks, notebook_ids = cached
            return dict(scope), list(notebooks), set(notebook_ids)
        if notebook is not None:
            try:
                parent_key = self._catalog.folder_key(folder, root_allowed=True)
            except GoodnotesError as exc:
                if isinstance(exc, _DifferentKindError):
                    raise GoodnotesError(
                        "Use --notebook for a notebook selector and --folder for a folder "
                        "selector; do not pass both scopes."
                    ) from None
                raise
            if parent_key is not None:
                raise GoodnotesError("Use either --notebook or a non-root --folder, not both.")
            item = self.resolve_notebook(notebook, option="--notebook")
            result = (
                {
                    "type": "notebook",
                    "notebook_id": item.id,
                    "path": item.path,
                },
                [item],
                {item.id},
            )
            self._scope_cache[cache_key] = result
            return dict(result[0]), list(result[1]), set(result[2])

        try:
            parent_key = self._catalog.folder_key(folder, root_allowed=True)
        except GoodnotesError as exc:
            is_notebook = False
            try:
                self._catalog.resolve(folder, kind="notebook")
                is_notebook = True
            except GoodnotesError:
                pass
            if is_notebook or isinstance(exc, _DifferentKindError):
                raise GoodnotesError(
                    "The selector identifies a notebook; use --notebook for a notebook or "
                    "--folder for a folder."
                ) from None
            raise
        notebooks = [item for item in self._catalog.tree(folder) if item.kind == "notebook"]
        if parent_key is None:
            scope = {"type": "folder", "folder_id": None, "path": "/"}
        else:
            item = self._catalog.resolve(folder, kind="folder")
            scope = {"type": "folder", "folder_id": item.id, "path": item.path}
        result = (scope, notebooks, {item.id for item in notebooks})
        self._scope_cache[cache_key] = result
        return dict(scope), list(notebooks), set(result[2])

    def resolve_notebook(self, selector: str | Item, *, option: str) -> Item:
        try:
            return self._catalog.resolve(selector, kind="notebook")
        except GoodnotesError as exc:
            is_folder = False
            try:
                self._catalog.resolve(selector, kind="folder")
                is_folder = True
            except GoodnotesError:
                pass
            if is_folder or isinstance(exc, _DifferentKindError):
                raise GoodnotesError(
                    f"{option} identifies a folder; use --folder for a folder scope and "
                    "--notebook for a notebook."
                ) from None
            raise

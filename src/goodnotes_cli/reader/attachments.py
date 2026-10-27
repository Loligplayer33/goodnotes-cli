"""Confined attachment path resolution scoped to a reader session."""

from pathlib import Path
from typing import Any

from ..errors import GoodnotesError
from ..storage.paths import resolve_confined_file


class AttachmentResolver:
    def __init__(self, attachments_dir: Path) -> None:
        self.attachments_dir = attachments_dir
        self.clear()

    def clear(self) -> None:
        self._attachment_resolution_cache: dict[str, tuple[Path | None, str]] = {}
        self._attachment_basename_cache: dict[str, list[Path]] | None = None

    def begin_read_cache(self) -> None:
        self._attachment_basename_cache = None

    def resolve(self, raw_path: Any, *, for_read: bool = False) -> tuple[Path | None, str]:
        """Resolve one stored reference, memoized within its pinned snapshot view.

        Metadata hydration reuses results for repeated attachment references. A PDF
        read revalidates the path before opening and using the PDF.
        """
        cache = None if for_read else self._attachment_resolution_cache
        if isinstance(raw_path, str) and cache is not None:
            cached = cache.get(raw_path)
            if cached is not None:
                return cached

        result = self._resolve_uncached(raw_path)
        if isinstance(raw_path, str) and cache is not None:
            cache[raw_path] = result
        return result

    def _resolve_uncached(self, raw_path: Any) -> tuple[Path | None, str]:
        """Resolve a stored attachment without losing missing or unsafe diagnostics."""
        if not isinstance(raw_path, str) or not raw_path:
            return None, "missing"
        try:
            root = Path(self.attachments_dir).resolve()
            stored_path = Path(raw_path)
        except (OSError, RuntimeError, ValueError):
            return None, "unavailable"
        if not stored_path.is_absolute() and ".." in stored_path.parts:
            return None, "unsafe"
        candidate = stored_path if stored_path.is_absolute() else root / stored_path
        try:
            resolved = resolve_confined_file(root, candidate)
        except GoodnotesError:
            return None, "unsafe"
        except OSError:
            return None, "unavailable"
        if resolved is not None:
            return resolved, "available"
        if stored_path.is_absolute() or not stored_path.name:
            return None, "missing"
        if len(stored_path.parts) > 1 and stored_path.parts[0] == "Attachments":
            try:
                resolved = resolve_confined_file(root, root.joinpath(*stored_path.parts[1:]))
            except GoodnotesError:
                return None, "unsafe"
            except OSError:
                return None, "unavailable"
            if resolved is not None:
                return resolved, "available"
        matches = self._attachment_paths_by_basename().get(stored_path.name, [])
        if len(matches) == 1:
            try:
                resolved = resolve_confined_file(root, matches[0])
            except GoodnotesError:
                return None, "unsafe"
            except OSError:
                return None, "unavailable"
            return (resolved, "available") if resolved is not None else (None, "missing")
        return None, "ambiguous" if matches else "missing"

    def _attachment_paths_by_basename(self) -> dict[str, list[Path]]:
        if self._attachment_basename_cache is not None:
            return self._attachment_basename_cache
        root = Path(self.attachments_dir).resolve()
        paths: dict[str, list[Path]] = {}
        for candidate in root.rglob("*"):
            try:
                resolved = resolve_confined_file(root, candidate)
            except (GoodnotesError, OSError, RuntimeError, ValueError):
                continue
            if resolved is not None:
                paths.setdefault(candidate.name, []).append(resolved)
        self._attachment_basename_cache = paths
        return paths

"""Generated tests for bounded, stable cache metadata and error chains."""

import errno
import os
from pathlib import Path

import pytest

from goodnotes_cli.errors import CacheAccessError, _os_cause
from goodnotes_cli.storage import metadata


def test_error_chain_uses_cause_before_context_and_honors_suppression() -> None:
    denied = PermissionError(errno.EACCES, "denied")
    missing = FileNotFoundError(errno.ENOENT, "missing")
    wrapper = RuntimeError("changed wording")
    wrapper.__context__ = denied
    wrapper.__cause__ = missing
    assert _os_cause(wrapper) is missing
    assert (
        CacheAccessError.from_exception(
            wrapper, "failure", operation="read", path="cache", missing_is_uninitialized=True
        ).code
        == "cache_not_initialized"
    )
    wrapper.__cause__ = None
    wrapper.__suppress_context__ = True
    assert _os_cause(wrapper) is None
    wrapper.__suppress_context__ = False
    assert _os_cause(wrapper) is denied
    wrapper.__context__ = wrapper
    assert _os_cause(wrapper) is None


def test_metadata_rejects_large_special_and_replaced_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "metadata.json"
    path.write_text('{"ok":true}', encoding="utf-8")
    assert metadata.read_json(path, limit=64) == {"ok": True}
    with pytest.raises(ValueError):
        metadata.read_json(path, limit=2)
    alternate = tmp_path / "replacement.json"
    alternate.write_text('{"ok":false}', encoding="utf-8")
    original_open = os.open

    def replace_before_open(
        name: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(name) == path:
            alternate.replace(path)
        if dir_fd is None:
            return original_open(name, flags, mode)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(metadata.os, "open", replace_before_open)
    with pytest.raises(ValueError, match="changed"):
        metadata.read_json(path, limit=64)


def test_metadata_rejects_symlink_and_fifo(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        metadata.read_json(link, limit=64)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError):
        metadata.read_json(fifo, limit=64)


def test_cleanup_rejects_changed_owned_directory(tmp_path: Path) -> None:
    from goodnotes_cli.errors import GoodnotesError
    from goodnotes_cli.storage.paths import _remove_entry_at

    owned = tmp_path / "owned"
    owned.mkdir()
    expected = owned.stat()
    owned.rename(tmp_path / "original")
    owned.mkdir()
    (owned / "keep").write_text("keep", encoding="utf-8")
    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(GoodnotesError, match="changed during cleanup"):
            _remove_entry_at(
                parent_fd, owned.name, owned, expected_identity=(expected.st_dev, expected.st_ino)
            )
    finally:
        os.close(parent_fd)
    assert (owned / "keep").read_text(encoding="utf-8") == "keep"

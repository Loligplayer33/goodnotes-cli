"""Generated tests for managed cache selection and pointer classification."""

from pathlib import Path

import pytest

from goodnotes_cli.config import Settings
from goodnotes_cli.errors import CacheAccessError


def test_pointer_classification_uses_type_not_wording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goodnotes_cli.storage.cache import state

    def invalid(*args: object, **kwargs: object) -> None:
        raise state.PointerEntryError("new wording without pointer keywords")

    monkeypatch.setattr(state, "_read_pointer", invalid)
    with pytest.raises(CacheAccessError) as error:
        Settings(data_dir=tmp_path, cache_dir=tmp_path).load_sandbox()
    assert error.value.code == "cache_invalid"
    assert error.value.details["operation"] == "read_active_pointer"


def test_library_selection_rejects_replaced_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "library-root.json"
    path.write_text('{"root_folder_id":"generated"}')
    original = Path.lstat
    calls = 0

    def replaced(self: Path):
        nonlocal calls
        if self == path:
            calls += 1
            if calls == 2:
                replacement = tmp_path / "replacement"
                replacement.write_text('{"root_folder_id":"generated"}')
                replacement.replace(path)
        return original(self)

    monkeypatch.setattr(Path, "lstat", replaced)
    with pytest.raises(CacheAccessError, match="library root selection is invalid"):
        Settings(cache_dir=tmp_path).load_library_root()

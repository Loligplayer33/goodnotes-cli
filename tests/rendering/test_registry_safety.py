from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.errors import FilesystemAccessError, GoodnotesError
from goodnotes_cli.rendering.registered import _read_registry
from goodnotes_cli.storage.metadata import CACHE_METADATA_LIMIT


@pytest.mark.parametrize(
    "content",
    [
        b"[" * 10000 + b"]" * 10000,
        b" " * (CACHE_METADATA_LIMIT + 1),
        b"\xff",
        b"\xff\xfe{\x00}\x00",
    ],
    ids=["nested", "oversized", "invalid-utf8", "utf16"],
)
def test_invalid_registry_is_classified(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "record.json"
    path.write_bytes(content)
    with pytest.raises(GoodnotesError, match="Could not read the annotated-PDF record"):
        _read_registry(path, "generated-notebook")


def test_missing_registry_and_lookup_race_are_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"
    assert _read_registry(path, "generated-notebook") is None
    path.write_text("{}")
    original = Path.lstat
    calls = 0

    def raced(target: Path) -> os.stat_result:
        nonlocal calls
        if target == path:
            calls += 1
            if calls == 2:
                raise FileNotFoundError(2, "generated race", str(path))
        return original(target)

    monkeypatch.setattr(Path, "lstat", raced)
    with pytest.raises(FilesystemAccessError) as captured:
        _read_registry(path, "generated-notebook")
    assert captured.value.details["operation"] == "read_annotated_pdf_record"


def test_registry_replacement_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "record.json"
    path.write_text("{}")
    outside = tmp_path / "outside.json"
    outside.write_text('{"version": 1, "document_id": "generated-notebook"}')
    original = os.open

    def replaced(target: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if target == path:
            path.unlink()
            path.symlink_to(outside)
        return original(target, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", replaced)
    with pytest.raises((FilesystemAccessError, GoodnotesError)):
        _read_registry(path, "generated-notebook")


@pytest.mark.parametrize("managed,verbose", [(True, False), (False, True)])
@pytest.mark.parametrize(
    "content",
    [b"[" * 10000 + b"]" * 10000, b" " * (CACHE_METADATA_LIMIT + 1), b"\xff"],
    ids=["nested", "oversized", "invalid-utf8"],
)
def test_public_render_registry_errors_publish_no_result(
    tmp_path: Path, managed: bool, verbose: bool, content: bytes
) -> None:
    import json

    from goodnotes_cli.rendering.registered import _registry_path
    from tests.support.cli import invoke
    from tests.support.library import _create_snapshot

    sandbox = _create_snapshot(tmp_path / "snapshot")
    registry = _registry_path(sandbox, "doc-hostile", create=True)
    registry.write_bytes(content)
    output = tmp_path / "output.png"
    arguments = ["render", "doc-hostile", "--page", "1", "--layer", "annotated"]
    if not managed:
        arguments += ["--output", str(output)]
    result = invoke(arguments, tmp_path, sandbox=sandbox.root, verbose=verbose)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "annotated-PDF record" in json.loads(result.stderr)["error"]["message"]
    assert not output.exists()
    assert not list((tmp_path / "state").rglob("*.png"))

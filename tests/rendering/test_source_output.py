from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.errors import FilesystemAccessError, GoodnotesError, _permission_denied
from goodnotes_cli.rendering import registered
from goodnotes_cli.rendering.registered import register_annotated_pdf
from goodnotes_cli.storage.sandbox import Sandbox
from tests.rendering.test_documents import _make_pdf, _make_real_snapshot
from tests.support.cli import _tree_manifest


def _snapshot(root: Path, source: object = ...) -> Sandbox:
    _make_real_snapshot(root, {"generated-notebook": 1})
    return _with_source(Sandbox.load(root), source)


def _with_source(sandbox: Sandbox, source: object) -> Sandbox:
    marker: dict[str, object] = {"format_version": 1}
    if source is not ...:
        marker["source_path"] = source
    (sandbox.root / "goodnotes-snapshot.json").write_text(json.dumps(marker), encoding="utf-8")
    return Sandbox.load(sandbox.root)


@pytest.mark.parametrize("location", ["parent", "equal", "missing-descendant", "registry-file"])
def test_sidecar_directory_is_admitted_before_creation_including_missing_registry_source(
    tmp_path: Path, location: str
) -> None:
    sandbox = _snapshot(tmp_path / "snapshots" / "snapshot")
    registry = registered._registry_path(sandbox, "generated-notebook", create=False)
    assert not registry.parent.exists()
    if location == "parent":
        source = registry.parent.parent
    elif location == "equal":
        source = registry.parent
    elif location == "missing-descendant":
        source = registry.parent / "missing" / "source"
    else:
        source = registry
        assert source.name == hashlib.sha256(b"generated-notebook").hexdigest() + ".json"
    sandbox = _with_source(sandbox, str(source))
    before = _tree_manifest(tmp_path)

    with pytest.raises(GoodnotesError, match="source"):
        registered._registry_path(sandbox, "generated-notebook", create=True)

    assert not registry.parent.parent.exists()
    assert not source.exists()
    assert _tree_manifest(tmp_path) == before


@pytest.mark.parametrize("warm", [False, True])
def test_registration_cannot_create_or_rewrite_sidecars_in_captured_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm: bool
) -> None:
    sandbox = _snapshot(tmp_path / "snapshot")
    pdf = tmp_path / "generated-annotated.pdf"
    _make_pdf(pdf, ["generated annotated page"])
    if warm:
        register_annotated_pdf(sandbox, "generated-notebook", pdf, expected_pages=1)
    registry = registered._registry_path(sandbox, "generated-notebook", create=False)
    sandbox = _with_source(sandbox, str(registry.parent))
    before = _tree_manifest(tmp_path)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Protected sidecar reached registry publication")

    monkeypatch.setattr(registered, "_write_registry", forbidden)
    with pytest.raises(GoodnotesError, match="source"):
        register_annotated_pdf(sandbox, "generated-notebook", pdf, expected_pages=1)

    assert _tree_manifest(tmp_path) == before
    assert registry.exists() is warm


@pytest.mark.parametrize(
    "source", [None, False, 7, [], {}, "", "relative/source", "/bad\x00source"]
)
def test_malformed_provenance_denies_sidecar_writes_but_preserves_read_only_lookup(
    tmp_path: Path, source: object
) -> None:
    sandbox = _snapshot(tmp_path / "snapshot", source)
    before = _tree_manifest(tmp_path)

    registry = registered._registry_path(sandbox, "generated-notebook", create=False)
    assert registered._read_registry(registry, "generated-notebook") is None
    with pytest.raises(GoodnotesError, match="source"):
        registered._registry_path(sandbox, "generated-notebook", create=True)

    assert not registry.parent.parent.exists()
    assert _tree_manifest(tmp_path) == before


@pytest.mark.parametrize("recorded", [False, True])
def test_disjoint_sidecar_reregistration_and_create_false_reads_remain_usable(
    tmp_path: Path, recorded: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "generated.txt").write_bytes(b"generated source")
    sandbox = _snapshot(tmp_path / "snapshot", str(source) if recorded else ...)
    pdf = tmp_path / "generated-annotated.pdf"
    _make_pdf(pdf, ["first generated annotation"])
    source_before = _tree_manifest(source), _tree_manifest(sandbox.root)

    first = register_annotated_pdf(sandbox, "generated-notebook", pdf, expected_pages=1)
    registry = registered._registry_path(sandbox, "generated-notebook", create=False)
    first_record = registered._read_registry(registry, "generated-notebook")
    assert first_record is not None
    first_digest = first_record["sha256"]
    _make_pdf(pdf, ["second generated annotation"])
    second = register_annotated_pdf(sandbox, "generated-notebook", pdf, expected_pages=1)
    second_record = registered._read_registry(registry, "generated-notebook")
    assert second_record is not None
    assert second_record["sha256"] != first_digest
    assert first["registered"] is second["registered"] is True
    assert (_tree_manifest(source), _tree_manifest(sandbox.root)) == source_before

    protected = _with_source(sandbox, str(registry.parent))
    before = _tree_manifest(tmp_path)
    assert registered._registry_path(protected, "generated-notebook", create=False) == registry
    opened = registered._open_registered_pdf(protected, "generated-notebook")
    try:
        assert "second generated annotation" in opened.document[0].get_text()
    finally:
        opened.close()
    assert _tree_manifest(tmp_path) == before


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM])
def test_source_identity_permission_denial_is_an_expected_sidecar_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    sandbox = _snapshot(tmp_path / "snapshot", str(source))
    registry = registered._registry_path(sandbox, "generated-notebook", create=False)
    before = _tree_manifest(tmp_path)
    original = Path.stat

    def denied(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == source:
            raise PermissionError(error_number, "generated source identity denial", str(path))
        return original(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "stat", denied)
        with pytest.raises(FilesystemAccessError) as captured:
            registered._registry_path(sandbox, "generated-notebook", create=True)

    assert _permission_denied(captured.value)
    assert captured.value.code == "output_permission_denied"
    assert captured.value.details["operation"] == "create_annotated_pdf_sidecar"
    assert not registry.parent.parent.exists()
    assert _tree_manifest(tmp_path) == before


def test_source_symlink_loop_is_an_expected_sidecar_failure_before_creation(tmp_path: Path) -> None:
    source = tmp_path / "source-loop"
    source.symlink_to(source)
    sandbox = _snapshot(tmp_path / "snapshot", str(source))
    registry = registered._registry_path(sandbox, "generated-notebook", create=False)
    before = _tree_manifest(tmp_path)

    with pytest.raises(GoodnotesError):
        registered._registry_path(sandbox, "generated-notebook", create=True)

    assert not registry.parent.parent.exists()
    assert source.is_symlink()
    assert _tree_manifest(tmp_path) == before

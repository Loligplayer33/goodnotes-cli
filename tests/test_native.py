from __future__ import annotations

import errno
import json
from pathlib import Path

import pytest

from goodnotes_cli import native
from goodnotes_cli.native import (
    NativeError,
    NativeRuntime,
    _inventory,
    _scratch_store,
    runtime_status,
)


def store(tmp_path: Path) -> Path:
    source = tmp_path / "snapshot" / "Library" / "Databases" / "notes_main.db"
    source.mkdir(parents=True)
    (source / "CURRENT").write_bytes(b"MANIFEST-000001\n")
    (source / "MANIFEST-000001").write_bytes(b"synthetic-manifest")
    (source / "000002.log").write_bytes(b"synthetic-wal")
    (source / "000003.sst").write_bytes(b"synthetic-table")
    (source / "OPTIONS-000004").write_text(
        "comparator=leveldb.BytewiseComparator\nmerge_operator=nullptr\nprefix_extractor=nullptr\n"
    )
    return source


def test_private_scratch_writes_never_reach_snapshot_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)
    scratch_parent = tmp_path / "temporary"
    scratch_parent.mkdir()
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(scratch_parent))
    before = _inventory(source)
    content = {path.name: path.read_bytes() for path in source.iterdir()}
    with _scratch_store(source, source.parents[2]) as scratch:
        assert scratch.is_relative_to(scratch_parent)
        assert (scratch.parent.stat().st_mode & 0o777) == 0o700
        assert not list(scratch.glob("OPTIONS-*"))
        for name in content:
            if name.startswith("OPTIONS-"):
                continue
            assert (scratch / name).read_bytes() == content[name]
            assert (scratch / name).stat().st_ino != (source / name).stat().st_ino
        (scratch / "000002.log").write_bytes(b"engine-created-content")
        (scratch / "LOCK").write_text("scratch only")
        (scratch.parent / "LOG").write_text("scratch log only")
    assert not scratch.exists()
    assert not list(scratch_parent.iterdir())
    assert _inventory(source) == before
    assert {path.name: path.read_bytes() for path in source.iterdir()} == content


def test_scratch_cleanup_after_engine_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="synthetic engine failure"):
        with _scratch_store(source, source.parents[2]) as scratch:
            raise ValueError("synthetic engine failure")
    assert not scratch.parent.exists()


@pytest.mark.parametrize("entry_kind", ["symlink", "directory", "fifo"])
def test_nonregular_store_entries_are_rejected_before_copy(tmp_path: Path, entry_kind: str) -> None:
    source = store(tmp_path)
    unsafe = source / "unsafe"
    if entry_kind == "symlink":
        unsafe.symlink_to(source / "CURRENT")
    elif entry_kind == "directory":
        unsafe.mkdir()
    else:
        import os

        os.mkfifo(unsafe)
    with pytest.raises(NativeError, match="nonregular"):
        with _scratch_store(source, source.parents[2]):
            pytest.fail("unsafe entry was copied")


@pytest.mark.parametrize(
    "line", ["comparator=custom", "merge_operator=custom", "prefix_extractor=custom"]
)
def test_unsupported_options_are_not_loaded(tmp_path: Path, line: str) -> None:
    source = store(tmp_path)
    (source / "OPTIONS-000004").write_text(line)
    with pytest.raises(NativeError, match="Unsupported native"):
        with _scratch_store(source, source.parents[2]):
            pytest.fail("unsupported config reached scratch")


def test_external_options_paths_never_reach_engine_configuration(tmp_path: Path) -> None:
    source = store(tmp_path)
    (source / "OPTIONS-000004").write_text("wal_dir=/do/not/open\ndb_log_dir=/do/not/open\n")
    with _scratch_store(source, source.parents[2]) as scratch:
        assert not list(scratch.glob("OPTIONS-*"))


def test_snapshot_change_is_detected_and_scratch_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(tmp_path))
    with pytest.raises(NativeError, match="changed while reading"):
        with _scratch_store(source, source.parents[2]) as scratch:
            (source / "000002.log").write_text("a concurrent actor changed this copied snapshot")
    assert not scratch.parent.exists()


def test_fallback_refuses_large_byte_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = store(tmp_path)
    monkeypatch.setattr(native, "_clone_file_from_fd", lambda *_: False)
    monkeypatch.setattr(native, "_MAX_COPY_FALLBACK", 1)
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(tmp_path))
    with pytest.raises(NativeError, match="copy-on-write"):
        with _scratch_store(source, source.parents[2]):
            pytest.fail("large copy was allowed")
    assert not list(tmp_path.glob("goodnotes-native-*"))


def test_scratch_inside_snapshot_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)
    root = source.parents[2]
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(root))
    with pytest.raises(NativeError) as result:
        with _scratch_store(source, root):
            pytest.fail("scratch was created in snapshot")
    assert result.value.details["operation"] == "create_native_scratch"
    assert "GOODNOTES_SCRATCH_DIR" in str(result.value.details["remedy"])


def test_scratch_permission_failure_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(tmp_path))
    monkeypatch.setattr(
        native.tempfile,
        "TemporaryDirectory",
        lambda **_: (_ for _ in ()).throw(PermissionError("synthetic denied")),
    )
    with pytest.raises(NativeError) as result:
        with _scratch_store(source, source.parents[2]):
            pytest.fail("denied scratch was used")
    assert result.value.details["path"] == str(tmp_path)
    assert "write/search access" in str(result.value.details["remedy"])


def test_runtime_missing_library_is_actionable_and_creates_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native, "_library_candidate", lambda: None)
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(tmp_path))
    result = runtime_status()
    assert result["available"] is False
    assert result["code"] == "native_runtime_unavailable"
    assert "brew install rocksdb" in str(result["remedy"])
    assert not list(tmp_path.iterdir())


def test_runtime_relative_or_missing_path_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOODNOTES_ROCKSDB_LIBRARY", "relative.so")
    with pytest.raises(NativeError, match="absolute"):
        NativeRuntime()


def test_runtime_missing_c_api_symbols_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native, "_library_candidate", lambda: "/synthetic/library")
    monkeypatch.setattr(native.C, "CDLL", lambda *_: object())
    result = runtime_status()
    assert result["available"] is False
    assert result["code"] == "native_runtime_unavailable"


def test_read_rejects_unmarked_copy_before_loading_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    source = store(tmp_path)
    monkeypatch.setattr(native, "NativeRuntime", lambda: pytest.fail("runtime was loaded"))
    with pytest.raises(Exception, match="snapshot"):
        native.read_page_records(
            SimpleNamespace(root=source.parents[2], library_dir=source.parents[1]), "synthetic-page"
        )


def test_read_rejects_mismatched_library_before_loading_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    source = store(tmp_path)
    root = source.parents[2]
    (root / "Library" / "Attachments").mkdir()
    (root / "Library" / "Databases" / "projection.sqlite").touch()
    (root / "goodnotes-snapshot.json").write_text(json.dumps({"format_version": 1}))
    monkeypatch.setattr(native, "NativeRuntime", lambda: pytest.fail("runtime was loaded"))
    with pytest.raises(NativeError, match="does not match"):
        native.read_page_records(SimpleNamespace(root=root, library_dir=tmp_path), "synthetic-page")


@pytest.mark.parametrize("error_number", [errno.EXDEV, errno.ENOTSUP])
def test_clone_cross_volume_error_falls_back_to_bounded_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    source = store(tmp_path)

    def failed_clone(*_: object) -> bool:
        raise OSError(error_number, "synthetic clone unavailable")

    monkeypatch.setattr(native, "_clone_file_from_fd", failed_clone)
    with _scratch_store(source, source.parents[2]) as scratch:
        assert (scratch / "CURRENT").read_bytes() == (source / "CURRENT").read_bytes()


def test_clone_permission_error_does_not_silently_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)

    def failed_clone(*_: object) -> bool:
        raise PermissionError(errno.EACCES, "synthetic denied")

    monkeypatch.setattr(native, "_clone_file_from_fd", failed_clone)
    with pytest.raises(NativeError, match="Could not clone"):
        with _scratch_store(source, source.parents[2]):
            pytest.fail("permission error used fallback")


def test_runtime_candidate_permission_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied() -> str:
        raise PermissionError(errno.EACCES, "synthetic runtime denied", "/synthetic/library")

    monkeypatch.setattr(native, "_library_candidate", denied)
    result = runtime_status()
    assert result["available"] is False
    assert result["code"] == "native_runtime_unavailable"
    assert result["path"] == "/synthetic/library"


@pytest.mark.parametrize(
    "current",
    [
        b"/outside/MANIFEST-1\n",
        b"../MANIFEST-1\n",
        b"MANIFEST-000001/../foreign\n",
        b"MANIFEST-999999\n",
    ],
)
def test_current_cannot_reference_a_foreign_or_missing_manifest(
    tmp_path: Path, current: bytes
) -> None:
    source = store(tmp_path)
    (source / "CURRENT").write_bytes(current)
    with pytest.raises(NativeError, match="local manifest"):
        with _scratch_store(source, source.parents[2]):
            pytest.fail("foreign manifest reached scratch")

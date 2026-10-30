from __future__ import annotations

import ctypes as C
import errno
import json
import os
import shutil
from bisect import bisect_left
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.native import (
    NativeError,
    NativeRuntime,
    NativeStoreSession,
    native_store_fingerprint,
    runtime_status,
)
from goodnotes_cli.native import runtime as native_runtime
from goodnotes_cli.native import store as native
from goodnotes_cli.native.store import _inventory, _scratch_store
from goodnotes_cli.storage.paths import OutputProtection


@pytest.mark.parametrize(
    "operation",
    ["map_native_page", "decode_native_page", "render_native_page", "inspect_native_options"],
)
def test_unsupported_native_operation_has_targeted_remedy(operation: str) -> None:
    result = NativeError(
        "native_data_unavailable", "Generated unsupported data.", operation=operation
    )
    assert "does not add" in str(result.details["remedy"])
    assert "refresh it with sync" not in str(result.details["remedy"])


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


def valid_sandbox(tmp_path: Path) -> tuple[native.Sandbox, Path]:
    source = store(tmp_path)
    root = source.parents[2]
    (root / "Library" / "Attachments").mkdir()
    (source.parent / "projection.sqlite").touch()
    (root / "goodnotes-snapshot.json").write_text(json.dumps({"format_version": 1}))
    return native.Sandbox.load(root), source


def test_native_store_fingerprint_validates_metadata_without_runtime_or_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox, source = valid_sandbox(tmp_path)
    source_files = {path.name: path.read_bytes() for path in source.iterdir()}
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    before = _inventory(source)

    monkeypatch.setattr(native, "NativeRuntime", lambda: pytest.fail("runtime was loaded"))
    monkeypatch.setattr(
        native.tempfile,
        "TemporaryDirectory",
        lambda **_: pytest.fail("scratch was created"),
    )

    fingerprint = native_store_fingerprint(sandbox)

    assert fingerprint == before
    assert set(fingerprint) == set(source_files)
    assert {path.name: path.read_bytes() for path in source.iterdir()} == source_files
    assert not list(scratch_parent.iterdir())


def test_native_store_fingerprint_tracks_every_file(tmp_path: Path) -> None:
    sandbox, source = valid_sandbox(tmp_path)
    before = native_store_fingerprint(sandbox)

    added = source / "new-generated-table.sst"
    added.write_bytes(b"new generated store file")
    after = native_store_fingerprint(sandbox)

    assert added.name not in before
    assert after[added.name] == native.stat_key(added.stat())
    assert after != before


def test_native_store_fingerprint_rejects_missing_store(tmp_path: Path) -> None:
    sandbox, source = valid_sandbox(tmp_path)
    shutil.rmtree(source)

    with pytest.raises(NativeError, match="native store is missing") as result:
        native_store_fingerprint(sandbox)

    assert result.value.details["operation"] == "inspect_native_store"


@pytest.mark.parametrize(
    ("filename", "contents", "message"),
    [
        ("CURRENT", b"MANIFEST-999999\n", "local manifest"),
        ("OPTIONS-000004", b"comparator=custom\n", "Unsupported native"),
    ],
)
def test_native_store_fingerprint_rejects_invalid_control_metadata(
    tmp_path: Path, filename: str, contents: bytes, message: str
) -> None:
    sandbox, source = valid_sandbox(tmp_path)
    (source / filename).write_bytes(contents)

    with pytest.raises(NativeError, match=message):
        native_store_fingerprint(sandbox)


@pytest.mark.parametrize("entry_kind", ["symlink", "directory", "fifo"])
def test_native_store_fingerprint_rejects_nonregular_entries(
    tmp_path: Path, entry_kind: str
) -> None:
    sandbox, source = valid_sandbox(tmp_path)
    unsafe = source / "unsafe"
    if entry_kind == "symlink":
        unsafe.symlink_to(source / "CURRENT")
    elif entry_kind == "directory":
        unsafe.mkdir()
    else:
        os.mkfifo(unsafe)

    with pytest.raises(NativeError, match="nonregular"):
        native_store_fingerprint(sandbox)


class FakeNativeRuntime:
    def __init__(self, *, null_iterator: bool = False) -> None:
        self.null_iterator = null_iterator
        self.open_paths: list[Path] = []
        self.log_dirs: list[Path] = []
        self.closed: list[int] = []
        self.destroyed_iterators: list[int] = []
        self.destroyed_read_options: list[int] = []
        self.destroyed_options: list[int] = []
        self.records = [
            (b"PAGE-A.1", b"first"),
            (b"PAGE-A.2", b"second"),
            (b"PAGE-B.1", b"third"),
        ]
        self.position = 0
        self.key_buffer: object | None = None
        self.value_buffer: object | None = None

    def options_create(self) -> int:
        return 1

    def options_set_create_if_missing(self, *_: object) -> None:
        return None

    def options_set_paranoid_checks(self, *_: object) -> None:
        return None

    def options_set_max_open_files(self, *_: object) -> None:
        return None

    def options_set_db_log_dir(self, _options: int, path: bytes) -> None:
        log_dir = Path(os.fsdecode(path))
        self.log_dirs.append(log_dir)
        (log_dir / "fake-engine.log").write_text("scratch log")

    def options_set_wal_dir(self, *_: object) -> None:
        return None

    def open_for_read_only(
        self, _options: int, path: bytes, _error_if_log_file_exists: int, error: Any
    ) -> int:
        opened = Path(os.fsdecode(path))
        self.open_paths.append(opened)
        (opened / "fake-engine-write").write_text("scratch only")
        C.cast(error, C.POINTER(C.c_void_p))[0] = None
        return 2

    def check(self, error: C.c_void_p) -> None:
        if error.value:
            raise NativeError(
                "native_data_unavailable", "fake RocksDB error", operation="read_native_copy"
            )

    def readoptions_create(self) -> int:
        return 3

    def readoptions_set_fill_cache(self, *_: object) -> None:
        return None

    def readoptions_set_verify_checksums(self, *_: object) -> None:
        return None

    def create_iterator(self, *_: object) -> int | None:
        return None if self.null_iterator else 4

    def iter_seek(self, _iterator: int, prefix: bytes, size: int) -> None:
        self.position = bisect_left([key for key, _value in self.records], prefix[:size])

    def iter_valid(self, _iterator: int) -> int:
        return int(self.position < len(self.records))

    def iter_key(self, _iterator: int, size: Any) -> C.c_void_p:
        key = self.records[self.position][0]
        C.cast(size, C.POINTER(C.c_size_t))[0] = len(key)
        self.key_buffer = C.create_string_buffer(key)
        return C.cast(self.key_buffer, C.c_void_p)

    def iter_value(self, _iterator: int, size: Any) -> C.c_void_p:
        value = self.records[self.position][1]
        C.cast(size, C.POINTER(C.c_size_t))[0] = len(value)
        self.value_buffer = C.create_string_buffer(value)
        return C.cast(self.value_buffer, C.c_void_p)

    def iter_next(self, _iterator: int) -> None:
        self.position += 1

    def iter_get_error(self, *_: object) -> None:
        return None

    def iter_destroy(self, iterator: int) -> None:
        self.destroyed_iterators.append(iterator)

    def readoptions_destroy(self, options: int) -> None:
        self.destroyed_read_options.append(options)

    def close(self, db: int) -> None:
        self.closed.append(db)

    def options_destroy(self, options: int) -> None:
        self.destroyed_options.append(options)

    def free(self, _value: object) -> None:
        return None


def test_private_scratch_writes_never_reach_snapshot_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)
    scratch_parent = tmp_path / "temporary"
    scratch_parent.mkdir()
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(scratch_parent))
    before = _inventory(source)
    content = {path.name: path.read_bytes() for path in source.iterdir()}
    with _scratch_store(source, OutputProtection(source.parents[2])) as scratch:
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
        with _scratch_store(source, OutputProtection(source.parents[2])) as scratch:
            raise ValueError("synthetic engine failure")
    assert not scratch.parent.exists()


def test_native_store_session_reuses_one_clone_and_engine_for_multiple_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox, source = valid_sandbox(tmp_path)
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    before = _inventory(source)
    source_bytes = {path.name: path.read_bytes() for path in source.iterdir()}
    runtime = FakeNativeRuntime()
    runtime_instances: list[FakeNativeRuntime] = []

    def create_runtime() -> FakeNativeRuntime:
        runtime_instances.append(runtime)
        return runtime

    monkeypatch.setattr(native, "NativeRuntime", create_runtime)
    sandbox_load = native.Sandbox.load
    validation_count = 0

    def load_sandbox(path: Path) -> native.Sandbox:
        nonlocal validation_count
        validation_count += 1
        return sandbox_load(path)

    monkeypatch.setattr(native.Sandbox, "load", staticmethod(load_sandbox))
    clone_names: list[str] = []
    clone = native._clone_file_from_fd

    def tracked_clone(fd: int, destination: Path) -> bool:
        clone_names.append(destination.name)
        return clone(fd, destination)

    monkeypatch.setattr(native, "_clone_file_from_fd", tracked_clone)

    with NativeStoreSession(sandbox, scratch_dir=scratch_parent) as session:
        assert session.read_page_records("PAGE-A") == [
            (b"PAGE-A.1", b"first"),
            (b"PAGE-A.2", b"second"),
        ]
        assert session.read_page_records("PAGE-B") == [(b"PAGE-B.1", b"third")]

    assert len(runtime.open_paths) == 1
    assert len(runtime_instances) == 1
    assert validation_count == 1
    assert runtime.open_paths[0] != source
    assert runtime.open_paths[0].is_relative_to(scratch_parent)
    assert len(clone_names) == len([name for name in before if not name.startswith("OPTIONS-")])
    assert "OPTIONS-000004" not in clone_names
    assert len(runtime.destroyed_iterators) == 1
    assert len(runtime.destroyed_read_options) == 1
    assert runtime.closed == [2]
    assert runtime.destroyed_options == [1]
    assert not list(scratch_parent.iterdir())
    assert _inventory(source) == before
    assert {path.name: path.read_bytes() for path in source.iterdir()} == source_bytes


def test_native_store_session_cleans_engine_and_scratch_after_body_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox, _source = valid_sandbox(tmp_path)
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    runtime = FakeNativeRuntime()
    monkeypatch.setattr(native, "NativeRuntime", lambda: runtime)

    with pytest.raises(RuntimeError, match="synthetic operation failure"):
        with NativeStoreSession(sandbox, scratch_dir=scratch_parent):
            raise RuntimeError("synthetic operation failure")

    assert runtime.destroyed_iterators == [4]
    assert runtime.destroyed_read_options == [3]
    assert runtime.closed == [2]
    assert runtime.destroyed_options == [1]
    assert not list(scratch_parent.iterdir())


def test_native_store_session_rejects_null_iterator_and_cleans_partial_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox, _source = valid_sandbox(tmp_path)
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    runtime = FakeNativeRuntime(null_iterator=True)
    monkeypatch.setattr(native, "NativeRuntime", lambda: runtime)

    with pytest.raises(NativeError, match="iterator"):
        with NativeStoreSession(sandbox, scratch_dir=scratch_parent):
            pytest.fail("a NULL iterator was accepted")

    assert runtime.destroyed_read_options == [3]
    assert runtime.closed == [2]
    assert runtime.destroyed_options == [1]
    assert not list(scratch_parent.iterdir())


def test_missing_native_store_error_precedes_unavailable_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox, source = valid_sandbox(tmp_path)
    shutil.rmtree(source)
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    runtime_attempts = 0

    def unavailable_runtime() -> NativeRuntime:
        nonlocal runtime_attempts
        runtime_attempts += 1
        raise NativeError(
            "native_runtime_unavailable",
            "synthetic missing runtime",
            operation="load_native_runtime",
        )

    monkeypatch.setattr(native, "NativeRuntime", unavailable_runtime)

    with pytest.raises(NativeError, match="native store is missing") as result:
        with NativeStoreSession(sandbox, scratch_dir=scratch_parent):
            pytest.fail("missing native data reached the engine")

    assert result.value.code == "native_data_unavailable"
    assert result.value.details["operation"] == "inspect_native_store"
    assert runtime_attempts == 0
    assert not list(scratch_parent.iterdir())


def test_invalid_native_options_encoding_is_structured_and_precedes_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)
    options_path = source / "OPTIONS-000004"
    options_path.write_bytes(b"comparator=\xff\n")
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    monkeypatch.setattr(
        native,
        "_clone_file_from_fd",
        lambda *_: pytest.fail("invalid options reached scratch copying"),
    )

    with pytest.raises(NativeError, match="not valid UTF-8") as result:
        with _scratch_store(
            source, OutputProtection(source.parents[2]), scratch_dir=scratch_parent
        ):
            pytest.fail("invalid OPTIONS encoding was accepted")

    assert result.value.code == "native_data_unavailable"
    assert result.value.details["operation"] == "inspect_native_options"
    assert result.value.details["path"] == str(options_path)
    assert result.value.details["encoding_start"] == 11
    assert not list(scratch_parent.iterdir())


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
        with _scratch_store(source, OutputProtection(source.parents[2])):
            pytest.fail("unsafe entry was copied")


@pytest.mark.parametrize(
    "line", ["comparator=custom", "merge_operator=custom", "prefix_extractor=custom"]
)
def test_unsupported_options_are_not_loaded(tmp_path: Path, line: str) -> None:
    source = store(tmp_path)
    (source / "OPTIONS-000004").write_text(line)
    with pytest.raises(NativeError, match="Unsupported native"):
        with _scratch_store(source, OutputProtection(source.parents[2])):
            pytest.fail("unsupported config reached scratch")


def test_external_options_paths_never_reach_engine_configuration(tmp_path: Path) -> None:
    source = store(tmp_path)
    (source / "OPTIONS-000004").write_text("wal_dir=/do/not/open\ndb_log_dir=/do/not/open\n")
    with _scratch_store(source, OutputProtection(source.parents[2])) as scratch:
        assert not list(scratch.glob("OPTIONS-*"))


def test_snapshot_change_is_detected_and_scratch_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(tmp_path))
    with pytest.raises(NativeError, match="changed while reading"):
        with _scratch_store(source, OutputProtection(source.parents[2])) as scratch:
            (source / "000002.log").write_text("a concurrent actor changed this copied snapshot")
    assert not scratch.parent.exists()


def test_fallback_refuses_large_byte_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = store(tmp_path)
    monkeypatch.setattr(native, "_clone_file_from_fd", lambda *_: False)
    monkeypatch.setattr(native, "_MAX_COPY_FALLBACK", 1)
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(tmp_path))
    with pytest.raises(NativeError, match="copy-on-write"):
        with _scratch_store(source, OutputProtection(source.parents[2])):
            pytest.fail("large copy was allowed")
    assert not list(tmp_path.glob("goodnotes-native-*"))


def test_scratch_inside_snapshot_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)
    root = source.parents[2]
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(root))
    with pytest.raises(NativeError) as result:
        with _scratch_store(source, OutputProtection(root)):
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
        with _scratch_store(source, OutputProtection(source.parents[2])):
            pytest.fail("denied scratch was used")
    assert result.value.details["path"] == str(tmp_path)
    assert "write/search access" in str(result.value.details["remedy"])


def test_runtime_missing_library_is_actionable_and_creates_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(native_runtime, "_library_candidate", lambda: None)
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
    monkeypatch.setattr(native_runtime, "_library_candidate", lambda: "/synthetic/library")
    monkeypatch.setattr(native_runtime.C, "CDLL", lambda *_: object())
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
    with _scratch_store(source, OutputProtection(source.parents[2])) as scratch:
        assert (scratch / "CURRENT").read_bytes() == (source / "CURRENT").read_bytes()


def test_clone_permission_error_does_not_silently_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store(tmp_path)

    def failed_clone(*_: object) -> bool:
        raise PermissionError(errno.EACCES, "synthetic denied")

    monkeypatch.setattr(native, "_clone_file_from_fd", failed_clone)
    with pytest.raises(NativeError, match="Could not clone"):
        with _scratch_store(source, OutputProtection(source.parents[2])):
            pytest.fail("permission error used fallback")


def test_runtime_candidate_permission_failure_is_structured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def denied() -> str:
        raise PermissionError(errno.EACCES, "synthetic runtime denied", "/synthetic/library")

    monkeypatch.setattr(native_runtime, "_library_candidate", denied)
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
        with _scratch_store(source, OutputProtection(source.parents[2])):
            pytest.fail("foreign manifest reached scratch")

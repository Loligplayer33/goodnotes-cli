"""Copied-store failures retain their native operation and descriptor ownership."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.native import NativeError
from goodnotes_cli.native import store as native
from goodnotes_cli.storage.paths import OutputProtection, stat_key
from tests.native.test_native import store, valid_sandbox


@pytest.mark.parametrize("name", ["CURRENT", "OPTIONS-000004"])
@pytest.mark.parametrize("error_type", [FileNotFoundError, PermissionError])
def test_metadata_open_after_inventory_is_an_expected_native_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, error_type: type[OSError]
) -> None:
    _, source = valid_sandbox(tmp_path)
    target = source / name
    failure = error_type(errno.ENOENT if error_type is FileNotFoundError else errno.EACCES, name)
    original_open = os.open
    opened: list[int] = []
    closed: list[int] = []
    original_close = os.close

    def open_file(path: Any, *args: Any, **kwargs: Any) -> int:
        if path == target:
            raise failure
        fd = original_open(path, *args, **kwargs)
        opened.append(fd)
        return fd

    def close_file(fd: int) -> None:
        closed.append(fd)
        original_close(fd)

    monkeypatch.setattr(native.os, "open", open_file)
    monkeypatch.setattr(native.os, "close", close_file)
    with pytest.raises(NativeError) as captured:
        native._validated_inventory(source)
    assert captured.value.__cause__ is failure
    assert captured.value.code == "native_data_unavailable"
    assert captured.value.details["operation"] == "inspect_native_store"
    assert captured.value.details["path"] == str(target)
    assert closed == opened


@pytest.mark.parametrize("phase", ["list", "child_stat"])
def test_inventory_filesystem_error_identifies_the_failed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    source = store(tmp_path)
    target = source if phase == "list" else source / "CURRENT"
    method = "iterdir" if phase == "list" else "lstat"
    original = getattr(Path, method)
    failure = PermissionError(errno.EACCES, "generated inventory denied")

    def denied(path: Path, *args: Any, **kwargs: Any) -> Any:
        if path == target:
            raise failure
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, denied)
    with pytest.raises(NativeError) as captured:
        native._inventory(source)
    assert captured.value.__cause__ is failure
    assert captured.value.details["operation"] == "inspect_native_store"
    assert captured.value.details["path"] == str(target)


@pytest.mark.parametrize("phase", ["open", "dup", "fdopen", "read", "writer"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_acquired_descriptors_and_streams_close_once_when_construction_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, interrupted: bool
) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"generated source")
    copied = tmp_path / "copied"
    expected = stat_key(path.stat())
    failure = (
        KeyboardInterrupt() if interrupted else PermissionError("generated acquisition denied")
    )
    original_open, original_dup, original_fdopen, original_close = (
        os.open,
        os.dup,
        os.fdopen,
        os.close,
    )
    original_path_open = Path.open
    acquired: list[int] = []
    close_attempts: list[int] = []
    streams: list[Any] = []

    def tracked_open(*args: Any, **kwargs: Any) -> int:
        if phase == "open":
            raise failure
        fd = original_open(*args, **kwargs)
        acquired.append(fd)
        return fd

    def tracked_dup(fd: int) -> int:
        if phase == "dup":
            raise failure
        duplicate = original_dup(fd)
        acquired.append(duplicate)
        return duplicate

    class Reader:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> Reader:
            return self

        def read(self, *args: Any) -> bytes:
            if phase == "read":
                raise failure
            return self.stream.read(*args)

        def __exit__(self, *args: Any) -> None:
            close_attempts.append(self.stream.fileno())
            self.stream.close()

    def tracked_fdopen(fd: int, *args: Any, **kwargs: Any) -> Reader:
        if phase == "fdopen":
            raise failure
        stream = original_fdopen(fd, *args, **kwargs)
        streams.append(stream)
        return Reader(stream)

    def tracked_close(fd: int) -> None:
        close_attempts.append(fd)
        original_close(fd)

    def writer_open(target: Path, *args: Any, **kwargs: Any) -> Any:
        if target == copied:
            raise failure
        return original_path_open(target, *args, **kwargs)

    monkeypatch.setattr(native.os, "open", tracked_open)
    monkeypatch.setattr(native.os, "dup", tracked_dup)
    monkeypatch.setattr(native.os, "fdopen", tracked_fdopen)
    monkeypatch.setattr(native.os, "close", tracked_close)
    monkeypatch.setattr(Path, "open", writer_open)
    monkeypatch.setattr(native, "_clone_file_from_fd", lambda *_: False)
    expected_type = KeyboardInterrupt if interrupted else NativeError
    with pytest.raises(expected_type) as captured:
        if phase == "writer":
            native._copy_store_file(path, copied, expected, 1024)
        else:
            native._read_small_regular(path, expected, 1024)
    if interrupted:
        assert captured.value is failure
    else:
        assert captured.value.__cause__ is failure
    assert sorted(close_attempts) == sorted(acquired)
    assert len(close_attempts) == len(set(close_attempts))
    assert all(stream.closed for stream in streams)
    for fd in acquired:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)
    assert not copied.exists()


@pytest.mark.parametrize("phase", ["mkdir", "open", "fstat", "write", "chmod", "stat"])
def test_scratch_preparation_errors_are_native_and_remove_only_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    source = store(tmp_path)
    source_bytes = {path.name: path.read_bytes() for path in source.iterdir()}
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    failure = PermissionError(errno.EACCES, "generated scratch preparation denied")
    failed_path: Path | None = None
    active_source: Path | None = None
    original_open, original_fstat = os.open, os.fstat
    original_mkdir, original_chmod, original_stat = Path.mkdir, Path.chmod, Path.stat
    validated = native._validated_inventory
    copied_source_fds: set[int] = set()
    preparing = False

    def validated_inventory(path: Path) -> dict:
        nonlocal preparing
        result = validated(path)
        preparing = True
        return result

    def open_file(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal failed_path, active_source
        if preparing and Path(path).parent == source:
            active_source = Path(path)
            if phase == "open":
                failed_path = Path(path)
                raise failure
            fd = original_open(path, *args, **kwargs)
            copied_source_fds.add(fd)
            return fd
        return original_open(path, *args, **kwargs)

    def fstat(fd: int) -> os.stat_result:
        nonlocal failed_path
        if phase == "fstat" and fd in copied_source_fds:
            failed_path = active_source
            raise failure
        return original_fstat(fd)

    def mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal failed_path
        if phase == "mkdir" and path.name == "store" and path.is_relative_to(scratch_parent):
            failed_path = path
            raise failure
        original_mkdir(path, *args, **kwargs)

    def chmod(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal failed_path
        if phase == "chmod" and path.parent.name == "store":
            failed_path = path
            raise failure
        original_chmod(path, *args, **kwargs)

    def stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal failed_path
        if phase == "stat" and path.parent.name == "store":
            failed_path = path
            raise failure
        return original_stat(path, *args, **kwargs)

    def copy(reader: Any, writer: Any) -> None:
        nonlocal failed_path
        writer.write(b"partial scratch bytes")
        failed_path = Path(writer.name)
        raise failure

    monkeypatch.setattr(native, "_validated_inventory", validated_inventory)
    monkeypatch.setattr(native.os, "open", open_file)
    monkeypatch.setattr(native.os, "fstat", fstat)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "chmod", chmod)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(native, "_clone_file_from_fd", lambda *_: False)
    if phase == "write":
        monkeypatch.setattr(native.shutil, "copyfileobj", copy)
    with pytest.raises(NativeError) as captured:
        with native._scratch_store(
            source, OutputProtection(source.parents[2]), scratch_dir=scratch_parent
        ):
            pytest.fail("preparation failure reached caller")
    assert captured.value.__cause__ is failure
    assert captured.value.details["operation"] == "clone_native_store"
    assert captured.value.details["path"] == str(failed_path)
    assert not list(scratch_parent.iterdir())
    assert {path.name: path.read_bytes() for path in source.iterdir()} == source_bytes


def test_scratch_does_not_reclassify_a_caller_body_oserror(
    tmp_path: Path,
) -> None:
    source = store(tmp_path)
    scratch_parent = tmp_path / "scratch"
    scratch_parent.mkdir()
    failure = OSError("generated caller failure")
    with pytest.raises(OSError) as captured:
        with native._scratch_store(
            source, OutputProtection(source.parents[2]), scratch_dir=scratch_parent
        ):
            raise failure
    assert captured.value is failure
    assert not list(scratch_parent.iterdir())

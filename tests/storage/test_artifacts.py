from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import stat
from pathlib import Path
from typing import Any

import pytest

import goodnotes_cli.storage.artifacts.filesystem as filesystem_module
from goodnotes_cli.artifacts import ArtifactCache, ArtifactCapacityError, artifact_key
from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.storage.artifacts.blobs import ArtifactBlobs
from goodnotes_cli.storage.artifacts.filesystem import ArtifactFilesystem
from goodnotes_cli.storage.artifacts.io import ArtifactIO
from goodnotes_cli.storage.artifacts.metadata import ArtifactMetadata
from goodnotes_cli.storage.artifacts.records import _ROOT_OWNER


def _make_cache(
    tmp_path: Path,
    *,
    max_bytes: int = 100_000_000,
    ttl_seconds: float = 86_400,
    grace_seconds: float = 300,
    clock: Any = None,
) -> tuple[ArtifactCache, Path, Path]:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir(exist_ok=True)
    root = tmp_path / "state" / "artifacts"
    kwargs: dict[str, Any] = {
        "max_bytes": max_bytes,
        "ttl_seconds": ttl_seconds,
        "grace_seconds": grace_seconds,
    }
    if clock is not None:
        kwargs["clock"] = clock
    return ArtifactCache(root, snapshot, **kwargs), root, snapshot


def _publish(
    cache: ArtifactCache,
    key: str,
    *,
    image: bytes | None = None,
    text: bytes | None = None,
    result: dict[str, Any] | None = None,
) -> Path:
    with cache.staging() as stage:
        if image is not None:
            (stage / "page-1.png").write_bytes(image)
        if text is not None:
            (stage / "text.json").write_bytes(text)
        return cache.publish(key, stage, result or {"fixture": "generated"}).directory


def _concurrent_publish(
    root: str,
    snapshot: str,
    key: str,
    payload: str,
    barrier: Any,
    results: Any,
) -> None:
    try:
        barrier.wait(timeout=10)
        with ArtifactCache(root, snapshot) as cache:
            with cache.staging() as stage:
                (stage / "text.json").write_text(payload, encoding="utf-8")
                cache.publish(key, stage, {"fixture": payload})
        results.put(None)
    except BaseException as exc:
        results.put(f"{type(exc).__name__}: {exc}")


def _hold_partial_owner_marker(
    root: str,
    snapshot: str,
    marker_opened: Any,
    finish_marker: Any,
    results: Any,
) -> None:
    cache = ArtifactCache(root, snapshot)
    try:
        cache._fs._prepare_root()
        cache._fs._acquire_lock()
        assert cache._fs._root_fd is not None
        marker_fd = os.open(
            ".goodnotes-artifacts.json",
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=cache._fs._root_fd,
        )
        marker_opened.set()
        if not finish_marker.wait(timeout=10):
            raise TimeoutError("parent did not release partial marker writer")
        payload = json.dumps(_ROOT_OWNER, sort_keys=True, separators=(",", ":")).encode("utf-8")
        os.write(marker_fd, payload)
        os.fsync(marker_fd)
        os.close(marker_fd)
        cache._fs._release_lock()
        results.put(None)
    except BaseException as exc:
        cache._fs._release_lock()
        results.put(f"{type(exc).__name__}: {exc}")


def _open_after_partial_marker(
    root: str,
    snapshot: str,
    prepared: Any,
    results: Any,
) -> None:
    cache = ArtifactCache(root, snapshot)
    try:
        cache._fs._prepare_root()
        prepared.set()
        cache._fs._acquire_lock()
        cache._fs._ensure_root_marker()
        cache._fs._ensure_managed_directories()
        cache._maintain()
        cache._fs._release_lock()
        results.put(None)
    except BaseException as exc:
        cache._fs._release_lock()
        results.put(f"{type(exc).__name__}: {exc}")


def test_artifact_key_is_stable_and_rejects_non_json_values() -> None:
    assert artifact_key({"b": 2, "a": [1, "two"]}) == artifact_key({"a": [1, "two"], "b": 2})
    with pytest.raises(GoodnotesError, match="JSON-serializable"):
        artifact_key({"value": float("nan")})


def test_cache_creates_private_root_and_publishes_complete_flat_entry(tmp_path: Path) -> None:
    cache, root, _snapshot = _make_cache(tmp_path)
    key = artifact_key({"page": 1})
    image = b"generated fixture png bytes"
    text = b'{"text":"generated fixture"}'

    with cache as opened:
        directory = _publish(
            opened,
            key,
            image=image,
            text=text,
            result={"page": 1, "source": "generated"},
        )
        hit = opened.lookup(key)
        assert hit is not None
        assert hit.result == {"page": 1, "source": "generated"}
        assert (hit.directory / "page-1.png").read_bytes() == image
        assert (hit.directory / "text.json").read_bytes() == text
        assert not list(opened.staging_root.iterdir())

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "entries" / key).stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / "page-1.png").stat().st_mode) == 0o400
    assert stat.S_IMODE((directory / ".artifact.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / ".artifacts.lock").stat().st_mode) == 0o600


def test_png_blobs_are_deduplicated_across_entries(tmp_path: Path) -> None:
    cache, _root, _snapshot = _make_cache(tmp_path, clock=lambda: 1000)
    first_key = artifact_key({"page": 1})
    second_key = artifact_key({"page": 2})
    png = b"same generated fixture image"

    with cache as opened:
        first = _publish(opened, first_key, image=png)
        before = opened._retention._storage_size()
        with opened.staging() as stage:
            os.link(first / "page-1.png", stage / "page-1.png")
            second = opened.publish(second_key, stage, {"page": 2}).directory
        after = opened._retention._storage_size()

        first_image = first / "page-1.png"
        second_image = second / "page-1.png"
        blob = opened.blobs / f"{hashlib.sha256(png).hexdigest()}.png"
        assert first_image.stat().st_ino == second_image.stat().st_ino == blob.stat().st_ino
        assert blob.stat().st_nlink == 3
        assert after - before == (second / ".artifact.json").stat().st_size


def test_context_inventory_and_bundle_reuse_do_not_rehash_unrequested_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, _root, _snapshot = _make_cache(tmp_path)
    page_key = artifact_key({"page": "generated-page"})
    bundle_key = artifact_key({"bundle": "generated-page"})
    png = b"generated fixture image bytes"
    text = b'{"text":"generated fixture text"}'
    hashed: list[tuple[str, int]] = []
    original = ArtifactBlobs._hash_file

    def track_hash(
        self: ArtifactBlobs,
        path: Path,
        *,
        dir_fd: int | None = None,
    ) -> tuple[str, int, os.stat_result]:
        result = original(self, path, dir_fd=dir_fd)
        hashed.append((path.name, result[1]))
        return result

    monkeypatch.setattr(ArtifactBlobs, "_hash_file", track_hash)
    with cache as opened:
        _publish(opened, page_key, image=png)
    assert hashed == [("page-1.png", len(png))]

    hashed.clear()
    with cache as opened:
        page = opened.lookup(page_key)
        assert page is not None
        with opened.staging() as stage:
            os.link(page.directory / "page-1.png", stage / "page-1.png")
            (stage / "text.json").write_bytes(text)
            opened.publish(bundle_key, stage, {"kind": "bundle"})

    assert sorted(size for _name, size in hashed) == sorted([len(png), len(text)])
    assert sum(size for _name, size in hashed) == len(png) + len(text)


def test_lookup_extends_ttl_and_expired_entries_are_removed(tmp_path: Path) -> None:
    now = [1000.0]
    cache, _root, _snapshot = _make_cache(
        tmp_path, ttl_seconds=1000, grace_seconds=300, clock=lambda: now[0]
    )
    key = artifact_key({"resource": "background"})

    with cache as opened:
        entry = _publish(opened, key, image=b"generated fixture")
    now[0] = 1500
    with cache as opened:
        assert opened.lookup(key) is not None
    now[0] = 2300
    with cache as opened:
        assert opened.lookup(key) is not None
    now[0] = 3401
    with cache as opened:
        assert opened.lookup(key) is None
        assert not entry.exists()
        assert list(opened.blobs.iterdir()) == []


def test_grace_protects_recent_entries_and_oldest_entry_is_lru_evicted(
    tmp_path: Path,
) -> None:
    now = [1000.0]
    cache, root, snapshot = _make_cache(
        tmp_path, ttl_seconds=10_000, grace_seconds=300, clock=lambda: now[0]
    )
    first_key = artifact_key({"entry": 1})
    second_key = artifact_key({"entry": 2})

    with cache as opened:
        first = _publish(opened, first_key, image=b"first image")
        first_size = opened._retention._storage_size()
    limit = first_size + 16
    now[0] += 299
    with ArtifactCache(
        root,
        snapshot,
        max_bytes=limit,
        ttl_seconds=10_000,
        grace_seconds=300,
        clock=lambda: now[0],
    ) as opened:
        with opened.staging() as stage:
            (stage / "page-1.png").write_bytes(b"second image")
            with pytest.raises(ArtifactCapacityError):
                opened.publish(second_key, stage, {"fixture": "generated"})
        assert first.exists()
        assert opened.lookup(second_key) is None

        now[0] += 2
    with ArtifactCache(
        root,
        snapshot,
        max_bytes=limit,
        ttl_seconds=10_000,
        grace_seconds=300,
        clock=lambda: now[0],
    ) as opened:
        second = _publish(opened, second_key, image=b"second image")
        assert second.exists()
        assert not first.exists()
        assert opened.lookup(second_key) is not None


def test_open_removes_oversized_orphan_blob_before_capacity_accounting(
    tmp_path: Path,
) -> None:
    cache, root, snapshot = _make_cache(tmp_path, max_bytes=500)
    with cache:
        pass
    content = b"orphaned generated image" * 50
    blob = root / "blobs" / f"{hashlib.sha256(content).hexdigest()}.png"
    blob.write_bytes(content)
    blob.chmod(0o400)

    with ArtifactCache(root, snapshot, max_bytes=500) as opened:
        assert not blob.exists()
        assert opened._retention._storage_size() == 0
        assert opened._retention._storage_size() <= opened.max_bytes


def test_open_does_not_mask_oversized_referenced_cache_entries(tmp_path: Path) -> None:
    cache, root, snapshot = _make_cache(tmp_path, max_bytes=5_000)
    key = artifact_key({"capacity": "referenced"})
    with cache as opened:
        entry = _publish(opened, key, image=b"generated fixture image" * 50)
    assert entry.exists()

    with pytest.raises(ArtifactCapacityError):
        with ArtifactCache(root, snapshot, max_bytes=500):
            pass
    assert entry.exists()


def test_context_pins_lookups_and_renews_lease_at_return(tmp_path: Path) -> None:
    now = [1000.0]
    cache, root, snapshot = _make_cache(
        tmp_path, ttl_seconds=10_000, grace_seconds=300, clock=lambda: now[0]
    )
    first_key = artifact_key({"entry": "active"})
    second_key = artifact_key({"entry": "later"})
    with cache as opened:
        first = _publish(opened, first_key, image=b"first image")
        limit = opened._retention._storage_size() + 16

    now[0] = 1000
    active = ArtifactCache(
        root,
        snapshot,
        max_bytes=limit,
        ttl_seconds=10_000,
        grace_seconds=300,
        clock=lambda: now[0],
    )
    with active as opened:
        assert opened.lookup(first_key) is not None
        now[0] = 1301
        with opened.staging() as stage:
            (stage / "page-1.png").write_bytes(b"second image")
            with pytest.raises(ArtifactCapacityError):
                opened.publish(second_key, stage, {"fixture": "generated"})
        assert first.exists()

    now[0] = 1600
    with ArtifactCache(
        root,
        snapshot,
        max_bytes=limit,
        ttl_seconds=10_000,
        grace_seconds=300,
        clock=lambda: now[0],
    ) as opened:
        with opened.staging() as stage:
            (stage / "page-1.png").write_bytes(b"second image")
            with pytest.raises(ArtifactCapacityError):
                opened.publish(second_key, stage, {"fixture": "generated"})
        assert first.exists()

    now[0] = 1602
    with ArtifactCache(
        root,
        snapshot,
        max_bytes=limit,
        ttl_seconds=10_000,
        grace_seconds=300,
        clock=lambda: now[0],
    ) as opened:
        second = _publish(opened, second_key, image=b"second image")
        assert not first.exists()
        assert second.exists()


def test_lookup_touch_rejects_replaced_entry_directory_without_touching_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, _root, _snapshot = _make_cache(tmp_path, clock=lambda: 2000)
    key = artifact_key({"entry": "lookup-swap"})
    external = tmp_path / "external-entry"
    external.mkdir(mode=0o700)
    marker = external / ".artifact.json"
    original_read = ArtifactMetadata._read_entry
    swapped = False
    armed = False

    with cache as opened:
        entry = _publish(opened, key, text=b"generated fixture")
        marker.write_bytes((entry / ".artifact.json").read_bytes())
        marker.chmod(0o600)
        os.utime(marker, (1000, 1000))
        moved = entry.with_name(entry.name + "-original")

        def swap_after_read(self: ArtifactMetadata, directory: Path, **kwargs: Any) -> Any:
            nonlocal swapped
            record = original_read(self, directory, **kwargs)
            if self is opened._metadata and directory == entry and armed and not swapped:
                swapped = True
                entry.rename(moved)
                entry.symlink_to(external, target_is_directory=True)
            return record

        armed = True
        monkeypatch.setattr(ArtifactMetadata, "_read_entry", swap_after_read)
        with pytest.raises(GoodnotesError, match="changed before update artifact cache use time"):
            opened.lookup(key)
        opened._pinned_keys.discard(key)

    assert swapped
    assert marker.stat().st_mtime == 1000


def test_exit_lease_renewal_does_not_follow_replaced_entry_directory(tmp_path: Path) -> None:
    cache, _root, _snapshot = _make_cache(tmp_path, clock=lambda: 2000)
    key = artifact_key({"entry": "exit-swap"})
    external = tmp_path / "external-entry"
    external.mkdir(mode=0o700)
    marker = external / ".artifact.json"

    with pytest.raises(GoodnotesError, match="changed before renew artifact cache lease"):
        with cache as opened:
            entry = _publish(opened, key, text=b"generated fixture")
            marker.write_bytes((entry / ".artifact.json").read_bytes())
            marker.chmod(0o600)
            os.utime(marker, (1000, 1000))
            moved = entry.with_name(entry.name + "-original")
            entry.rename(moved)
            entry.symlink_to(external, target_is_directory=True)

    assert marker.stat().st_mtime == 1000


def test_mkdir_does_not_follow_staging_root_replaced_during_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, _root, _snapshot = _make_cache(tmp_path)
    external = tmp_path / "external-staging"
    external.mkdir(mode=0o700)
    sentinel = external / "caller-data"
    sentinel.write_text("generated external fixture", encoding="utf-8")
    original_mkdir = os.mkdir
    swapped = False

    with cache as opened:
        staging_root = opened.staging_root
        moved = staging_root.with_name(staging_root.name + "-original")

        def swap_then_mkdir(
            path: str | bytes | os.PathLike[Any],
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            nonlocal swapped
            if isinstance(path, str) and path.startswith(".staging-") and not swapped:
                swapped = True
                staging_root.rename(moved)
                staging_root.symlink_to(external, target_is_directory=True)
            original_mkdir(path, mode=mode, dir_fd=dir_fd)

        monkeypatch.setattr(filesystem_module.os, "mkdir", swap_then_mkdir)
        with pytest.raises(GoodnotesError):
            with opened.staging():
                pass

    assert swapped
    assert sentinel.read_text(encoding="utf-8") == "generated external fixture"
    assert stat.S_IMODE(external.stat().st_mode) == 0o700
    assert set(external.iterdir()) == {sentinel}


@pytest.mark.parametrize("entry_kind", ["symlink", "fifo"])
def test_lookup_rejects_unsafe_entry_files(tmp_path: Path, entry_kind: str) -> None:
    cache, _root, _snapshot = _make_cache(tmp_path)
    key = artifact_key({"unsafe": entry_kind})
    with cache as opened:
        directory = _publish(opened, key, text=b"generated fixture")
        target = directory / "text.json"
        target.unlink()
        if entry_kind == "symlink":
            external = tmp_path / "external.json"
            external.write_text("generated external fixture", encoding="utf-8")
            target.symlink_to(external)
        else:
            os.mkfifo(target)
        with pytest.raises(GoodnotesError):
            opened.lookup(key)


def test_publish_rejects_incomplete_nested_stage_and_external_hardlink(tmp_path: Path) -> None:
    cache, _root, _snapshot = _make_cache(tmp_path)
    nested_key = artifact_key({"stage": "nested"})
    external_key = artifact_key({"stage": "external-hardlink"})

    with cache as opened:
        with opened.staging() as stage:
            nested = stage / "nested"
            nested.mkdir()
            (nested / "text.json").write_text("generated", encoding="utf-8")
            with pytest.raises(GoodnotesError):
                opened.publish(nested_key, stage, {"fixture": "generated"})
        assert opened.lookup(nested_key) is None

        external = tmp_path / "external.json"
        external.write_text("generated external fixture", encoding="utf-8")
        with opened.staging() as stage:
            os.link(external, stage / "text.json")
            with pytest.raises(GoodnotesError, match="hardlinked"):
                opened.publish(external_key, stage, {"fixture": "generated"})
        assert external.read_text(encoding="utf-8") == "generated external fixture"
        assert opened.lookup(external_key) is None


def test_cache_rejects_unsafe_roots_and_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, root, snapshot = _make_cache(tmp_path)
    with pytest.raises(GoodnotesError, match="64 lowercase"):
        with cache as opened:
            opened.lookup("../outside")

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(GoodnotesError, match="parents must not be a symlink"):
        with ArtifactCache(linked_parent / "artifacts", snapshot):
            pass

    with pytest.raises(GoodnotesError, match="must not overlap"):
        with ArtifactCache(snapshot / "nested", snapshot):
            pass

    with pytest.raises(GoodnotesError, match="not owned"):
        monkeypatch.setattr(filesystem_module, "_OWNER_UID", os.getuid() + 1)
        with ArtifactCache(root, snapshot):
            pass


def test_unmarked_nonempty_root_is_not_adopted(tmp_path: Path) -> None:
    cache, root, snapshot = _make_cache(tmp_path)
    root.mkdir(parents=True)
    root.chmod(0o755)
    (root / "user-data").write_text("generated fixture", encoding="utf-8")
    with pytest.raises(GoodnotesError, match="non-empty unmarked"):
        with ArtifactCache(root, snapshot):
            pass
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert (root / "user-data").read_text(encoding="utf-8") == "generated fixture"


def test_invalid_marker_root_is_rejected_without_permission_or_file_changes(
    tmp_path: Path,
) -> None:
    _cache, root, snapshot = _make_cache(tmp_path)
    root.mkdir(parents=True)
    root.chmod(0o755)
    marker = root / ".goodnotes-artifacts.json"
    marker.write_text("generated invalid marker", encoding="utf-8")
    marker.chmod(0o600)

    with pytest.raises(GoodnotesError):
        with ArtifactCache(root, snapshot):
            pass

    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    assert marker.read_text(encoding="utf-8") == "generated invalid marker"
    assert not (root / ".artifacts.lock").exists()


def test_concurrent_processes_publish_independent_entries_under_one_lock(tmp_path: Path) -> None:
    cache, root, snapshot = _make_cache(tmp_path)

    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    results = context.Queue()
    items = [(artifact_key({"process": number}), f"fixture {number}") for number in range(2)]
    processes = [
        context.Process(
            target=_concurrent_publish,
            args=(str(root), str(snapshot), key, payload, barrier, results),
        )
        for key, payload in items
    ]
    for process in processes:
        process.start()
    barrier.wait(timeout=10)
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert [results.get(timeout=2) for _ in processes] == [None, None]

    with ArtifactCache(root, snapshot) as opened:
        for key, _payload in items:
            assert opened.lookup(key) is not None


def test_partial_owner_marker_is_validated_only_after_lock_release(tmp_path: Path) -> None:
    _cache, root, snapshot = _make_cache(tmp_path)
    context = multiprocessing.get_context("spawn")
    marker_opened = context.Event()
    finish_marker = context.Event()
    second_prepared = context.Event()
    results = context.Queue()
    writer = context.Process(
        target=_hold_partial_owner_marker,
        args=(str(root), str(snapshot), marker_opened, finish_marker, results),
    )
    reader = context.Process(
        target=_open_after_partial_marker,
        args=(str(root), str(snapshot), second_prepared, results),
    )
    writer.start()
    assert marker_opened.wait(timeout=10)
    reader.start()
    prepared_before_release = second_prepared.wait(timeout=10)
    finish_marker.set()
    writer.join(timeout=20)
    reader.join(timeout=20)

    assert prepared_before_release
    assert writer.exitcode == 0
    assert reader.exitcode == 0
    assert [results.get(timeout=2), results.get(timeout=2)] == [None, None]


def test_missing_marker_to_scan_race_accepts_concurrent_initializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, root, snapshot = _make_cache(tmp_path)
    original_stat = cache._fs._stat_at
    initialized = False

    def initialize_after_absence(directory_fd: int, name: str) -> os.stat_result | None:
        nonlocal initialized
        result = original_stat(directory_fd, name)
        if name == ".goodnotes-artifacts.json" and result is None and not initialized:
            initialized = True
            with ArtifactCache(root, snapshot):
                pass
        return result

    monkeypatch.setattr(cache._fs, "_stat_at", initialize_after_absence)
    with cache as opened:
        assert opened.lookup(artifact_key({"empty": True})) is None
    assert initialized


@pytest.mark.parametrize("replacement", ["root", "ancestor"])
def test_root_replacement_before_lock_never_touches_external_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    cache, root, snapshot = _make_cache(tmp_path)
    snapshot.chmod(0o755)
    sentinel = snapshot / "caller-data"
    sentinel.write_text("generated external fixture", encoding="utf-8")
    original_lock = cache._fs._acquire_lock

    if replacement == "root":
        moved = root.with_name("artifacts-original")

        def replace_root() -> None:
            root.rename(moved)
            root.symlink_to(snapshot, target_is_directory=True)
            original_lock()

        expected_external = snapshot
    else:
        parent = root.parent
        parent.mkdir(parents=True, exist_ok=True)
        external_parent = tmp_path / "external-parent"
        external_parent.mkdir()
        external = external_parent / root.name
        external.mkdir()
        external.chmod(0o755)
        sentinel = external / "caller-data"
        sentinel.write_text("generated external fixture", encoding="utf-8")
        moved = parent.with_name("state-original")

        def replace_root() -> None:
            parent.rename(moved)
            parent.symlink_to(external_parent, target_is_directory=True)
            original_lock()

        expected_external = external

    monkeypatch.setattr(cache._fs, "_acquire_lock", replace_root)
    with pytest.raises(GoodnotesError):
        with cache:
            pass

    assert stat.S_IMODE(expected_external.stat().st_mode) == 0o755
    assert sentinel.read_text(encoding="utf-8") == "generated external fixture"
    assert not (expected_external / ".artifacts.lock").exists()
    assert not (expected_external / ".goodnotes-artifacts.json").exists()
    assert not (expected_external / "entries").exists()
    assert not (expected_external / "blobs").exists()


def test_root_swap_before_fchmod_changes_only_pinned_original_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, root, snapshot = _make_cache(tmp_path)
    root.mkdir(parents=True)
    root.chmod(0o755)
    external = tmp_path / "external"
    external.mkdir()
    external.chmod(0o755)
    sentinel = external / "caller-data"
    sentinel.write_text("generated external fixture", encoding="utf-8")
    moved = root.with_name("artifacts-original")
    original_fchmod = os.fchmod
    swapped = False

    def swap_then_fchmod(descriptor: int, mode: int) -> None:
        nonlocal swapped
        if descriptor == cache._fs._root_fd and not swapped:
            swapped = True
            root.rename(moved)
            root.symlink_to(external, target_is_directory=True)
        original_fchmod(descriptor, mode)

    monkeypatch.setattr(filesystem_module.os, "fchmod", swap_then_fchmod)
    with pytest.raises(GoodnotesError):
        with cache:
            pass

    assert swapped
    assert stat.S_IMODE(external.stat().st_mode) == 0o755
    assert sentinel.read_text(encoding="utf-8") == "generated external fixture"
    assert not (external / ".artifacts.lock").exists()
    assert not (external / ".goodnotes-artifacts.json").exists()


@pytest.mark.parametrize("child", ["blobs", "staging"])
def test_managed_child_swap_cannot_touch_external_files(tmp_path: Path, child: str) -> None:
    cache, _root, _snapshot = _make_cache(tmp_path)
    external = tmp_path / "external-child"
    external.mkdir(mode=0o700)
    if child == "blobs":
        payload = b"external generated fixture image"
        protected = external / f"{hashlib.sha256(payload).hexdigest()}.png"
        protected.write_bytes(payload)
        protected.chmod(0o400)
    else:
        protected = external / "caller-data"
        protected.write_text("generated external fixture", encoding="utf-8")
    with cache as opened:
        original = opened.blobs if child == "blobs" else opened.staging_root
        moved = original.with_name(original.name + "-original")
        original.rename(moved)
        original.symlink_to(external, target_is_directory=True)
        with pytest.raises(GoodnotesError):
            with opened.staging():
                pass

    assert protected.exists()
    assert stat.S_IMODE(external.stat().st_mode) == 0o700
    if child == "blobs":
        assert protected.read_bytes() == b"external generated fixture image"
    else:
        assert protected.read_text(encoding="utf-8") == "generated external fixture"


def test_abandoned_owned_staging_directory_is_cleaned_on_next_open(tmp_path: Path) -> None:
    cache, root, _snapshot = _make_cache(tmp_path)
    with cache as opened:
        pass
    staging = root / ".staging"
    token = "a" * 32
    abandoned = staging / f".staging-{token}"
    abandoned.mkdir(mode=0o700)
    (abandoned / ".artifact-staging.json").write_text(
        (
            '{"format_version":1,"kind":"staging",'
            f'"owner":"goodnotes-cli-artifacts","token":"{token}"'
            "}"
        ),
        encoding="utf-8",
    )
    os.chmod(abandoned / ".artifact-staging.json", 0o600)
    (abandoned / "partial.json").write_text("generated partial fixture", encoding="utf-8")

    with ArtifactCache(root, _snapshot) as opened:
        assert opened.lookup(artifact_key({"missing": True})) is None
        assert list(opened.staging_root.iterdir()) == []


@pytest.mark.parametrize("kind", ["staging", "publishing"])
def test_marker_initialization_failure_removes_owned_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    cache, root, snapshot = _make_cache(tmp_path)
    original = ArtifactIO._write_json_exclusive

    def fail_marker(self: ArtifactIO, path: Path, value: Any, **kwargs: Any) -> bytes:
        if path.name == ".artifact-staging.json" and value["kind"] == kind:
            raise OSError("generated marker failure")
        return original(self, path, value, **kwargs)

    with cache as opened:
        monkeypatch.setattr(ArtifactIO, "_write_json_exclusive", fail_marker)
        with pytest.raises(OSError, match="generated marker failure"):
            with opened.staging() as stage:
                (stage / "page.txt").write_text("generated fixture")
                opened.publish(artifact_key({"test": "marker"}), stage, {})
        assert list(opened.staging_root.iterdir()) == []
    monkeypatch.setattr(ArtifactIO, "_write_json_exclusive", original)
    with ArtifactCache(root, snapshot):
        pass


def test_blob_copy_does_not_reopen_parent_after_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, _, _ = _make_cache(tmp_path)
    original_copy = ArtifactBlobs._copy_private_file
    original_open = ArtifactFilesystem._open_directory_fd
    armed = False

    def copy_then_fail_parent(
        self: ArtifactBlobs, source: Path, destination: Path, **kwargs: Any
    ) -> Any:
        nonlocal armed
        result = original_copy(self, source, destination, **kwargs)
        if destination.name.startswith(".blob-"):
            armed = True
        return result

    def fail_parent(self: ArtifactFilesystem, path: Path) -> int:
        if armed and path.name.startswith(".staging-"):
            raise OSError("generated parent-open failure")
        return original_open(self, path)

    with cache as opened:
        with opened.staging() as stage:
            (stage / "page.png").write_bytes(b"generated image")
            monkeypatch.setattr(ArtifactBlobs, "_copy_private_file", copy_then_fail_parent)
            monkeypatch.setattr(ArtifactFilesystem, "_open_directory_fd", fail_parent)
            result = opened.publish(artifact_key({"test": "blob"}), stage, {})
            assert result.directory.exists()
            assert armed
            monkeypatch.setattr(ArtifactFilesystem, "_open_directory_fd", original_open)
            assert sorted(p.name for p in stage.iterdir()) == [".artifact-staging.json", "page.png"]


def test_marker_failure_does_not_delete_replaced_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, root, snapshot = _make_cache(tmp_path)
    original_remove = ArtifactFilesystem._remove_empty_created
    replacement: Path | None = None

    def fail_marker(self: ArtifactIO, path: Path, value: Any, **kwargs: Any) -> bytes:
        if path.name == ".artifact-staging.json":
            raise OSError("generated marker failure")
        raise AssertionError("unexpected marker")

    def swap_before_remove(
        self: ArtifactFilesystem, parent_fd: int, path: Path, identity: tuple[int, int]
    ) -> None:
        nonlocal replacement
        path.rename(path.with_name(path.name + "-original"))
        path.mkdir(mode=0o700)
        replacement = path / "caller-data"
        replacement.write_text("generated fixture", encoding="utf-8")
        original_remove(self, parent_fd, path, identity)

    with cache as opened:
        monkeypatch.setattr(ArtifactIO, "_write_json_exclusive", fail_marker)
        monkeypatch.setattr(ArtifactFilesystem, "_remove_empty_created", swap_before_remove)
        with pytest.raises(GoodnotesError, match="changed during cleanup"):
            with opened.staging():
                pass
        assert replacement is not None and replacement.read_text() == "generated fixture"
    monkeypatch.undo()
    with pytest.raises(
        GoodnotesError, match="Unexpected item|Could not inspect artifact staging marker"
    ):
        with ArtifactCache(root, snapshot):
            pass


def test_blob_post_copy_failure_cleans_temporary_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import goodnotes_cli.storage.artifacts.blobs as blob_module

    cache, _, _ = _make_cache(tmp_path)
    original_open = os.open

    def fail_temporary_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if isinstance(path, str) and path.startswith(".blob-") and flags & os.O_CREAT == 0:
            raise OSError("generated post-copy failure")
        return original_open(path, flags, *args, **kwargs)

    with cache as opened:
        with opened.staging() as stage:
            (stage / "page.png").write_bytes(b"generated image")
            monkeypatch.setattr(blob_module.os, "open", fail_temporary_open)
            with pytest.raises(GoodnotesError, match="Could not publish artifact PNG blob"):
                opened.publish(artifact_key({"test": "retry"}), stage, {})
            monkeypatch.setattr(blob_module.os, "open", original_open)
            assert sorted(p.name for p in stage.iterdir()) == [".artifact-staging.json", "page.png"]
            assert opened.publish(artifact_key({"test": "retry"}), stage, {}).directory.exists()


@pytest.mark.parametrize("kind", ["staging", "publishing"])
def test_created_directory_fchmod_failure_is_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    cache, root, snapshot = _make_cache(tmp_path)
    original_fchmod = os.fchmod
    target = f".{kind}-"

    def fail_created_directory(descriptor: int, mode: int) -> None:
        info = os.fstat(descriptor)
        for path in opened.staging_root.iterdir():
            candidate = path.stat()
            if path.name.startswith(target) and (info.st_dev, info.st_ino) == (
                candidate.st_dev,
                candidate.st_ino,
            ):
                raise OSError("generated fchmod failure")
        original_fchmod(descriptor, mode)

    with cache as opened:
        monkeypatch.setattr(filesystem_module.os, "fchmod", fail_created_directory)
        with pytest.raises(GoodnotesError, match="Could not create"):
            with opened.staging() as stage:
                (stage / "page.txt").write_text("generated fixture")
                opened.publish(artifact_key({"test": "fchmod"}), stage, {})
        assert list(opened.staging_root.iterdir()) == []
    monkeypatch.setattr(filesystem_module.os, "fchmod", original_fchmod)
    with ArtifactCache(root, snapshot):
        pass


def test_capacity_changes_apply_to_blob_preparation(tmp_path: Path) -> None:
    cache, _, _ = _make_cache(tmp_path)
    with cache as opened:
        opened.max_bytes = 32
        with opened.staging() as stage:
            (stage / "page.png").write_bytes(b"generated image" * 10)
            with pytest.raises(ArtifactCapacityError):
                opened.publish(artifact_key({"test": "capacity"}), stage, {})


def test_failed_directory_initialization_preserves_unknown_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, root, snapshot = _make_cache(tmp_path)
    original_fchmod = os.fchmod
    with cache as opened:

        def fail_with_unexpected_child(descriptor: int, mode: int) -> None:
            info = os.fstat(descriptor)
            for path in opened.staging_root.iterdir():
                candidate = path.stat()
                if path.name.startswith(".staging-") and (info.st_dev, info.st_ino) == (
                    candidate.st_dev,
                    candidate.st_ino,
                ):
                    (path / "unexpected").write_text("generated fixture")
                    raise OSError("generated fchmod failure")
            original_fchmod(descriptor, mode)

        monkeypatch.setattr(filesystem_module.os, "fchmod", fail_with_unexpected_child)
        with pytest.raises(OSError):
            with opened.staging():
                pass
        residue = list(opened.staging_root.iterdir())
        assert len(residue) == 1
        assert (residue[0] / "unexpected").read_text() == "generated fixture"
    monkeypatch.setattr(filesystem_module.os, "fchmod", original_fchmod)
    with pytest.raises(GoodnotesError):
        with ArtifactCache(root, snapshot):
            pass


def test_blob_copy_failure_preserves_replaced_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import goodnotes_cli.storage.artifacts.blobs as blob_module

    cache, _, _ = _make_cache(tmp_path)
    original_fsync = os.fsync
    with cache as opened:
        with opened.staging() as stage:
            (stage / "page.png").write_bytes(b"generated image")

            def swap_then_fail(descriptor: int) -> None:
                temporary = list(stage.glob(".blob-*.tmp"))
                if temporary:
                    temporary[0].rename(stage / "saved-original")
                    temporary[0].write_text("generated replacement")
                    raise OSError("generated copy failure")
                original_fsync(descriptor)

            monkeypatch.setattr(blob_module.os, "fsync", swap_then_fail)
            with pytest.raises(GoodnotesError, match="Could not publish artifact PNG blob"):
                opened.publish(artifact_key({"test": "swapped-copy"}), stage, {})
            monkeypatch.setattr(blob_module.os, "fsync", original_fsync)
            temporary = list(stage.glob(".blob-*.tmp"))
            assert len(temporary) == 1
            assert temporary[0].read_text() == "generated replacement"


def test_macos_system_alias_allows_cache_but_not_selected_or_nested_symlinks(
    tmp_path: Path,
) -> None:
    import sys
    import tempfile

    if sys.platform != "darwin":
        pytest.skip("macOS system aliases are platform-specific")
    with tempfile.TemporaryDirectory(dir="/private/tmp") as directory:
        real = Path(directory)
        snapshot = real / "snapshot"
        snapshot.mkdir()
        alias_root = Path("/tmp") / real.name / "artifacts"
        with ArtifactCache(alias_root, snapshot) as opened:
            assert opened.root == real / "artifacts"
        with pytest.raises(GoodnotesError, match="symlink"):
            with ArtifactCache(Path("/tmp"), snapshot):
                pass
        nested = real / "nested-link"
        nested.symlink_to(real, target_is_directory=True)
        with pytest.raises(GoodnotesError, match="symlink"):
            with ArtifactCache(Path("/tmp") / real.name / "nested-link" / "artifacts", snapshot):
                pass


def test_marker_failure_preserves_unknown_directory_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache, root, snapshot = _make_cache(tmp_path)
    with cache as opened:

        def fail_with_unknown(self: ArtifactIO, path: Path, value: Any, **kwargs: Any) -> bytes:
            if path.name == ".artifact-staging.json":
                (path.parent / "unexpected").write_text("generated fixture")
                raise OSError("generated marker failure")
            raise AssertionError("unexpected marker")

        monkeypatch.setattr(ArtifactIO, "_write_json_exclusive", fail_with_unknown)
        with pytest.raises(OSError, match="generated marker failure"):
            with opened.staging():
                pass
        residue = list(opened.staging_root.iterdir())
        assert len(residue) == 1
        assert (residue[0] / "unexpected").read_text() == "generated fixture"
    monkeypatch.undo()
    with pytest.raises(GoodnotesError):
        with ArtifactCache(root, snapshot):
            pass

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path

import pytest

from goodnotes_cli.artifacts import ArtifactCache, artifact_key
from goodnotes_cli.errors import GoodnotesError


@pytest.fixture
def cache(tmp_path: Path) -> ArtifactCache:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    return ArtifactCache(tmp_path / "state" / "artifacts", snapshot)


def _blob(cache: ArtifactCache, payload: bytes) -> tuple[str, Path]:
    digest = hashlib.sha256(payload).hexdigest()
    path = cache.blobs / f"{digest}.png"
    path.write_bytes(payload)
    path.chmod(0o400)
    return digest, path


@pytest.mark.parametrize(
    "change",
    ["replacement", "private-replacement", "hardlink", "mode", "symlink", "size", "missing"],
)
def test_orphan_cleanup_revalidates_inventoried_candidate_before_unlink(
    cache: ArtifactCache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    payload = b"generated orphan payload"
    external_payload = b"r" * len(payload)
    external = tmp_path / "external.png"
    external.write_bytes(external_payload)
    external.chmod(0o400)
    with cache:
        digest, path = _blob(cache, payload)
        inventory = cache._blobs._blob_inventory
        inventoried_info: list[os.stat_result] = []
        replacement_info: list[os.stat_result] = []

        def changed_inventory() -> dict[str, os.stat_result]:
            result = inventory()
            inventoried_info.append(result[digest])
            if change == "replacement":
                path.unlink()
                os.link(external, path)
                replacement_info.append(path.stat())
            elif change == "private-replacement":
                replacement = tmp_path / "replacement.png"
                replacement.write_bytes(external_payload)
                replacement.chmod(0o400)
                os.replace(replacement, path)
                replacement_info.append(path.stat())
            elif change == "hardlink":
                os.link(path, tmp_path / "alias.png")
            elif change == "mode":
                path.chmod(0o600)
            elif change == "symlink":
                path.unlink()
                path.symlink_to(external)
            elif change == "size":
                path.chmod(0o600)
                path.write_bytes(b"generated changed size")
                path.chmod(0o400)
            else:
                path.unlink()
            return result

        observed_error = None
        with monkeypatch.context() as fault:
            fault.setattr(cache._blobs, "_blob_inventory", changed_inventory)
            try:
                cache._cleanup_orphan_blobs(cache._records)
            except GoodnotesError as error:
                observed_error = error

        assert external.read_bytes() == external_payload
        assert path.exists() == (change != "missing")
        assert observed_error is not None
        if change in {"replacement", "private-replacement"}:
            assert path.read_bytes() == external_payload
            assert path.stat().st_ino != inventoried_info[0].st_ino
            assert cache._blobs._digest_for_file(replacement_info[0]) is None
        elif change == "hardlink":
            assert path.stat().st_nlink == 2
        elif change == "symlink":
            assert path.is_symlink()
        assert cache._blobs._digest_for_file(inventoried_info[0]) == digest


def test_orphan_cleanup_defers_names_created_after_inventory(
    cache: ArtifactCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    with cache:
        _digest, original = _blob(cache, b"generated original orphan")
        inventory = cache._blobs._blob_inventory
        added: list[Path] = []

        def add_blob() -> dict[str, os.stat_result]:
            result = inventory()
            _digest, path = _blob(cache, b"generated later orphan")
            added.append(path)
            return result

        with monkeypatch.context() as fault:
            fault.setattr(cache._blobs, "_blob_inventory", add_blob)
            cache._cleanup_orphan_blobs(cache._records)
        assert not original.exists()
        assert len(added) == 1 and added[0].exists()
        cache._cleanup_orphan_blobs(cache._records)
        assert not added[0].exists()


@pytest.mark.parametrize("unlink_fails", [False, True])
def test_orphan_signature_changes_are_forgotten_only_after_successful_unlink(
    cache: ArtifactCache, monkeypatch: pytest.MonkeyPatch, unlink_fails: bool
) -> None:
    with cache:
        digest, path = _blob(cache, b"generated orphan with changed mtime")
        cache._blobs._verified_blob(digest)
        inventory = cache._blobs._blob_inventory
        signatures: list[os.stat_result] = []
        original_unlink = os.unlink
        primary = OSError(errno.EIO, "generated orphan unlink failure")

        def change_mtime() -> dict[str, os.stat_result]:
            result = inventory()
            old = result[digest]
            os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000_000))
            current = path.stat()
            assert current.st_ino == old.st_ino and current.st_size == old.st_size
            assert current.st_mtime_ns != old.st_mtime_ns
            signatures.extend([old, current])
            return result

        def unlink(name: str, *, dir_fd: int | None = None) -> None:
            if name == path.name and unlink_fails:
                raise primary
            original_unlink(name, dir_fd=dir_fd)

        with monkeypatch.context() as fault:
            fault.setattr(cache._blobs, "_blob_inventory", change_mtime)
            fault.setattr(os, "unlink", unlink)
            if unlink_fails:
                with pytest.raises(GoodnotesError) as error:
                    cache._cleanup_orphan_blobs(cache._records)
                assert error.value.__cause__ is primary
            else:
                cache._cleanup_orphan_blobs(cache._records)
        assert path.exists() is unlink_fails
        assert len(signatures) == 2
        for info in signatures:
            assert cache._blobs._digest_for_file(info) == (digest if unlink_fails else None)
        assert (digest in cache._blobs._verified_blob_stats) is unlink_fails


def test_orphan_cleanup_protects_retained_and_active_stage_references(
    cache: ArtifactCache,
) -> None:
    with cache:
        with cache.staging() as retained_stage:
            (retained_stage / "page-1.png").write_bytes(b"generated retained PNG")
            entry = cache.publish(
                artifact_key({"fixture": "retained"}), retained_stage, {"fixture": "generated"}
            )
        retained_blob = next(cache.blobs.iterdir())
        _digest, staged_blob = _blob(cache, b"generated staged PNG")
        _digest, orphan = _blob(cache, b"generated unused PNG")
        with cache.staging() as active_stage:
            os.link(staged_blob, active_stage / "page-1.png")
            cache._cleanup_orphan_blobs(cache._records)
            assert not orphan.exists()
            assert retained_blob.exists() and staged_blob.exists()
            assert (entry.directory / "page-1.png").read_bytes() == b"generated retained PNG"
            assert (active_stage / "page-1.png").read_bytes() == b"generated staged PNG"

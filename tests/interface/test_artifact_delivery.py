"""Managed outputs stay pinned until their response has been delivered."""

import fcntl
import json
import os
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.cli import images
from goodnotes_cli.config import Settings
from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.storage import artifacts
from goodnotes_cli.storage.artifacts import ArtifactCache, ArtifactCapacityError
from tests.support.cli import invoke_in_process
from tests.support.library import _create_snapshot


@pytest.mark.parametrize("command", ["render", "bundle"])
@pytest.mark.parametrize("verbose", [False, True])
def test_managed_delivery_holds_lock_and_renews_lease_after_slow_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, verbose: bool
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)
    data_dir = tmp_path / "state"
    clock = [1_000.0]
    returned: list[Path] = []
    original_emit = images.emit

    class TimedCache(ArtifactCache):
        def __init__(self, root: Path, snapshot: Path) -> None:
            super().__init__(root, snapshot, clock=lambda: clock[0])

    def slow_emit(value: dict[str, Any], settings: Settings) -> None:
        path = Path(value["path"] if command == "render" else value["manifest"])
        returned.append(path)
        descriptor = os.open(data_dir / "artifacts" / ".artifacts.lock", os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(descriptor)
        clock[0] += 301
        assert path.is_file()
        original_emit(value, settings)

    monkeypatch.setattr(artifacts, "ArtifactCache", TimedCache)
    monkeypatch.setattr(images, "emit", slow_emit)
    selection = "--page" if command == "render" else "--pages"
    result = invoke_in_process(
        [command, "doc-hostile", selection, "3", "--layer", "background"],
        tmp_path,
        sandbox=sandbox.root,
        data_dir=data_dir,
        verbose=verbose,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    json.loads(result.stdout)
    assert len(returned) == 1
    # The lease starts again after slow output, not before its elapsed 301 seconds.
    with pytest.raises(ArtifactCapacityError):
        with ArtifactCache(
            data_dir / "artifacts", sandbox.root, max_bytes=1, clock=lambda: clock[0]
        ):
            pass
    assert returned[0].is_file()
    clock[0] += 301
    with ArtifactCache(data_dir / "artifacts", sandbox.root, max_bytes=1, clock=lambda: clock[0]):
        assert not returned[0].exists()


@pytest.mark.parametrize("command", ["render", "bundle"])
def test_delivery_renewal_failure_precedes_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)

    def fail_renewal(self: ArtifactCache) -> None:
        raise GoodnotesError("Generated lease renewal failure.")

    monkeypatch.setattr(ArtifactCache, "renew_leases", fail_renewal)
    selection = "--page" if command == "render" else "--pages"
    result = invoke_in_process(
        [command, "doc-hostile", selection, "3", "--layer", "background"],
        tmp_path,
        sandbox=sandbox.root,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["message"] == "Generated lease renewal failure."


@pytest.mark.parametrize("failure", [BrokenPipeError, KeyboardInterrupt])
@pytest.mark.parametrize("command", ["render", "bundle"])
def test_interrupted_delivery_releases_artifact_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, failure: type[BaseException]
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)
    data_dir = tmp_path / "state"

    def interrupted_emit(value: Any, settings: Settings) -> None:
        raise failure()

    monkeypatch.setattr(images, "emit", interrupted_emit)
    selection = "--page" if command == "render" else "--pages"
    result = invoke_in_process(
        [command, "doc-hostile", selection, "3", "--layer", "background"],
        tmp_path,
        sandbox=sandbox.root,
        data_dir=data_dir,
    )
    assert result.returncode != 0
    descriptor = os.open(data_dir / "artifacts" / ".artifacts.lock", os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def test_post_delivery_renewal_failure_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)
    renewal = ArtifactCache.renew_leases
    calls = 0

    def fail_after_delivery(self: ArtifactCache) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GoodnotesError("Generated post-delivery renewal failure.")
        renewal(self)

    monkeypatch.setattr(ArtifactCache, "renew_leases", fail_after_delivery)
    result = invoke_in_process(
        ["render", "doc-hostile", "--page", "3", "--layer", "background"],
        tmp_path,
        sandbox=sandbox.root,
    )
    assert result.returncode == 1
    assert len(result.stdout.splitlines()) == 1
    assert json.loads(result.stdout)["page"] == 3
    assert (
        json.loads(result.stderr)["error"]["message"] == "Generated post-delivery renewal failure."
    )

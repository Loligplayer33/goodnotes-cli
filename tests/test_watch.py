from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_cache import make_library

import goodnotes_cli.watch as watch_module
from goodnotes_cli.errors import CacheAccessError, GoodnotesError
from goodnotes_cli.snapshot import _InventoryChanged


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock()
    monkeypatch.setattr(watch_module.time, "sleep", value.sleep)
    monkeypatch.setattr(watch_module.time, "monotonic", lambda: value.now)
    return value


def result(snapshot: str, *, changed: bool = False, status: str = "ready") -> dict[str, object]:
    return {
        "status": status,
        "snapshot": f"/generated/cache/{snapshot}",
        "snapshot_id": snapshot,
        "checked_at": "generated-check-time",
        "captured_at": "generated-capture-time",
        "source_changed_during_refresh": changed,
    }


@pytest.mark.parametrize(
    "options",
    [
        {"interval": 0},
        {"interval": 0.01},
        {"interval": 3601},
        {"interval": float("nan")},
        {"interval": float("inf")},
        {"interval": True},
        {"debounce": -1},
        {"debounce": 61},
        {"debounce": float("nan")},
        {"debounce": True},
        {"iterations": 0},
        {"iterations": True},
        {"iterations": 1.5},
    ],
)
def test_watch_rejects_invalid_options_without_io(
    options: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: object) -> str:
        raise AssertionError("Invalid options must fail before inspecting source")

    monkeypatch.setattr(watch_module, "_input_signature", forbidden)
    with pytest.raises(GoodnotesError):
        list(watch_module.watch_library(Path("source"), Path("cache"), **options))


def test_watch_refreshes_immediately_and_keeps_unchanged_polls_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = clock(monkeypatch)
    checks = []
    refreshes = []

    def signature(path: Path) -> str:
        checks.append(timer.now)
        return "same"

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        refreshes.append(timer.now)
        return result("one", status="unchanged")

    monkeypatch.setattr(watch_module, "_input_signature", signature)
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(watch_module.watch_library(Path("source"), Path("cache"), iterations=4))

    assert [event["event"] for event in events] == ["ready"]
    assert checks == [0, 2, 4, 6]
    assert refreshes == [0]
    assert timer.sleeps == [2, 2, 2]
    assert events[0]["captured_at"] == "generated-capture-time"


def test_watch_debounces_a_burst_and_refreshes_serially(monkeypatch: pytest.MonkeyPatch) -> None:
    timer = clock(monkeypatch)
    signatures = iter(["a", "b", "c", "c", "c"])
    calls = []
    running = False

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        nonlocal running
        assert not running
        running = True
        calls.append(timer.now)
        running = False
        return result(str(len(calls)))

    monkeypatch.setattr(watch_module, "_input_signature", lambda source: next(signatures))
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=1, debounce=2, iterations=5
        )
    )

    assert calls == [0, 4]
    assert [event["event"] for event in events] == ["ready", "updated"]
    assert events[-1]["iteration"] == 5


def test_watch_sleeps_only_for_the_remaining_debounce_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = clock(monkeypatch)
    signatures = iter(["a", "b", "b"])
    calls: list[float] = []

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        calls.append(timer.now)
        return result(str(len(calls)))

    monkeypatch.setattr(watch_module, "_input_signature", lambda source: next(signatures))
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=10, debounce=1, iterations=3
        )
    )

    assert calls == [0, 11]
    assert timer.sleeps == [10, 1]
    assert [event["event"] for event in events] == ["ready", "updated"]


def test_unstable_inventory_resumes_coalescing_without_an_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = clock(monkeypatch)
    observations: list[str | _InventoryChanged] = [
        "a",
        _InventoryChanged("Generated inventory changed mid-scan"),
        "b",
        "b",
    ]
    refreshes: list[float] = []

    def signature(source: Path) -> str:
        observation = observations.pop(0)
        if isinstance(observation, _InventoryChanged):
            raise observation
        return observation

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        refreshes.append(timer.now)
        return result(str(len(refreshes)))

    monkeypatch.setattr(watch_module, "_input_signature", signature)
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=0.05, debounce=0, iterations=4
        )
    )

    assert refreshes == [0, 0.1]
    assert [event["event"] for event in events] == ["ready", "updated"]


def test_unstable_inventory_restarts_the_positive_debounce_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = clock(monkeypatch)
    observations: list[str | _InventoryChanged] = [
        "a",
        _InventoryChanged("Generated inventory changed mid-scan"),
        "b",
        "b",
        "b",
    ]
    refreshes: list[float] = []

    def signature(source: Path) -> str:
        observation = observations.pop(0)
        if isinstance(observation, _InventoryChanged):
            raise observation
        return observation

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        refreshes.append(timer.now)
        return result(str(len(refreshes)))

    monkeypatch.setattr(watch_module, "_input_signature", signature)
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=1, debounce=2, iterations=5
        )
    )

    assert refreshes == [0, 4]
    assert timer.sleeps == [1, 1, 1, 1]
    assert [event["event"] for event in events] == ["ready", "updated"]


def test_unstable_inventory_does_not_reset_maximum_coalescing_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = clock(monkeypatch)
    observations: list[str | _InventoryChanged] = [
        "a",
        _InventoryChanged("Generated inventory changed mid-scan"),
        *list("bcdefghijk"),
    ]
    refreshes: list[float] = []

    def signature(source: Path) -> str:
        observation = observations.pop(0)
        if isinstance(observation, _InventoryChanged):
            raise observation
        return observation

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        refreshes.append(timer.now)
        return result(str(len(refreshes)))

    monkeypatch.setattr(watch_module, "_input_signature", signature)
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=1, debounce=2, iterations=12
        )
    )

    assert refreshes == [0, 11]
    assert [event["event"] for event in events] == ["ready", "updated"]
    assert events[-1]["iteration"] == 12


def test_repeated_unstable_inventories_attempt_capture_at_maximum_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = clock(monkeypatch)
    observations: list[str | _InventoryChanged] = [
        "a",
        *[_InventoryChanged("Generated inventory changed mid-scan") for _ in range(11)],
    ]
    refreshes: list[float] = []

    def signature(source: Path) -> str:
        observation = observations.pop(0)
        if isinstance(observation, _InventoryChanged):
            raise observation
        return observation

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        refreshes.append(timer.now)
        return result(str(len(refreshes)))

    monkeypatch.setattr(watch_module, "_input_signature", signature)
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=1, debounce=2, iterations=12
        )
    )

    assert refreshes == [0, 11]
    assert [event["event"] for event in events] == ["ready", "updated"]
    assert events[-1]["iteration"] == 12


def test_unstable_capture_restarts_coalescing_without_failure_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = clock(monkeypatch)
    refreshes: list[float] = []

    def signature(source: Path) -> str:
        if timer.now == 0:
            return "a"
        raise _InventoryChanged("Generated inventory changed mid-scan")

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        refreshes.append(timer.now)
        if len(refreshes) == 2:
            raise _InventoryChanged("Generated source changed during capture")
        return result(str(len(refreshes)))

    monkeypatch.setattr(watch_module, "_input_signature", signature)
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=1, debounce=2, iterations=22
        )
    )

    assert refreshes == [0, 11, 21]
    assert [event["event"] for event in events] == ["ready", "updated"]


def test_startup_unstable_inventory_waits_for_a_meaningful_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timer = clock(monkeypatch)
    source = make_library(tmp_path / "source")
    original_signature = watch_module._input_signature
    signature_calls = 0
    refreshes: list[float] = []

    def signature(source: Path) -> str:
        nonlocal signature_calls
        observed = original_signature(source)
        signature_calls += 1
        if signature_calls < 3:
            raise _InventoryChanged("Generated initial inventory changed mid-scan")
        return observed

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        refreshes.append(timer.now)
        return result("one")

    monkeypatch.setattr(watch_module, "_input_signature", signature)
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(source, Path("cache"), interval=1, debounce=2, iterations=3)
    )

    assert refreshes == [2]
    assert [event["event"] for event in events] == ["ready"]


@pytest.mark.parametrize("iterations", [None, 1])
def test_missing_library_root_is_first_watch_error_without_sleep(
    iterations: int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    timer = clock(monkeypatch)
    missing = tmp_path / "missing-library"
    watcher = watch_module.watch_library(
        missing, tmp_path / "cache", interval=1, iterations=iterations
    )

    event = next(watcher)

    assert event["event"] == "error"
    error = event["error"]
    assert isinstance(error, dict)
    assert error["code"] == "source_unavailable"
    details = error["details"]
    assert isinstance(details, dict)
    assert details["operation"] == "inspect_source_library"
    assert details["path"] == str(missing)
    assert timer.sleeps == []
    watcher.close()


def test_unsafe_inventory_failure_still_emits_error_and_backs_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = clock(monkeypatch)
    observations: list[str | GoodnotesError] = [
        "a",
        GoodnotesError("Symlinks are not allowed in snapshot inputs: generated-link"),
        "b",
    ]

    def signature(source: Path) -> str:
        observation = observations.pop(0)
        if isinstance(observation, GoodnotesError):
            raise observation
        return observation

    calls: list[float] = []

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        calls.append(timer.now)
        return result(str(len(calls)))

    monkeypatch.setattr(watch_module, "_input_signature", signature)
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=0.05, debounce=0, iterations=3
        )
    )

    assert [event["event"] for event in events] == ["ready", "error", "recovered"]
    assert timer.sleeps == [0.05, 1]
    assert calls == [0, 1.05]


def test_continuous_changes_have_a_bounded_coalescing_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = clock(monkeypatch)
    calls = []

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        calls.append(timer.now)
        return result(str(len(calls)))

    monkeypatch.setattr(watch_module, "_input_signature", lambda source: str(timer.now))
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=1, debounce=1, iterations=12
        )
    )

    assert calls == [0, 11]
    assert [event["event"] for event in events] == ["ready", "updated"]


def test_change_that_reverts_before_debounce_does_not_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock(monkeypatch)
    signatures = iter(["a", "b", "a", "a"])
    calls = []
    monkeypatch.setattr(watch_module, "_input_signature", lambda source: next(signatures))

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        calls.append(source)
        return result("one")

    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=1, debounce=1, iterations=4
        )
    )

    assert len(events) == len(calls) == 1


def test_refresh_error_backs_off_and_emits_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    timer = clock(monkeypatch)
    calls = []

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        calls.append(timer.now)
        if len(calls) == 2:
            raise CacheAccessError(
                "cache_permission_denied",
                "Generated permission failure",
                operation="prepare_cache_directory",
                path=destination,
                writer=True,
            )
        return result(str(len(calls)))

    monkeypatch.setattr(watch_module, "_input_signature", lambda source: str(timer.now))
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=0.1, debounce=0, iterations=3
        )
    )

    assert [event["event"] for event in events] == ["ready", "error", "recovered"]
    assert timer.sleeps == [0.1, 1]
    error = events[1]["error"]
    assert isinstance(error, dict)
    assert error["code"] == "cache_permission_denied"
    assert error["details"]["operation"] == "prepare_cache_directory"
    assert events[1]["retry_in_seconds"] == 1


def test_watch_redebounces_a_new_edit_after_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timer = clock(monkeypatch)
    signatures = iter(["a", "b", "b", "b", "c", "c", "c"])
    refresh_times: list[float] = []

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        refresh_times.append(timer.now)
        if len(refresh_times) == 2:
            raise GoodnotesError("Generated transient capture failure")
        return result(str(len(refresh_times)))

    monkeypatch.setattr(watch_module, "_input_signature", lambda source: next(signatures))
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=1, debounce=2, iterations=7
        )
    )

    assert refresh_times == [0, 3, 6]
    assert [event["event"] for event in events] == ["ready", "error", "recovered"]


def test_failed_inventory_retries_with_capped_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    timer = clock(monkeypatch)

    def denied(source: Path) -> str:
        raise PermissionError("Generated source denial")

    monkeypatch.setattr(watch_module, "_input_signature", denied)
    events = list(
        watch_module.watch_library(Path("source"), Path("cache"), interval=0.05, iterations=8)
    )

    assert len(events) == 8
    assert all(event["event"] == "error" for event in events)
    assert timer.sleeps == [1, 2, 4, 8, 16, 30, 30]


def test_source_permission_error_preserves_actionable_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock(monkeypatch)
    denied_path = Path("/generated/source/Databases")

    def denied(source: Path) -> str:
        try:
            raise PermissionError(13, "Generated denial", str(denied_path))
        except PermissionError as exc:
            raise GoodnotesError("Could not inspect source") from exc

    monkeypatch.setattr(watch_module, "_input_signature", denied)
    event = next(watch_module.watch_library(Path("source"), Path("cache"), iterations=1))
    error = event["error"]
    assert isinstance(error, dict)
    assert error["code"] == "source_permission_denied"
    assert error["details"]["path"] == str(denied_path)
    assert error["details"]["operation"] == "inspect_source_library"
    assert error["details"]["remedy"]


def test_startup_failure_can_recover_without_another_source_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock(monkeypatch)
    checks = 0
    refreshes = []

    def signature(source: Path) -> str:
        nonlocal checks
        checks += 1
        if checks == 1:
            raise GoodnotesError("Generated transient inventory failure")
        return "same"

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        refreshes.append(source)
        return result("one", status="unchanged")

    monkeypatch.setattr(watch_module, "_input_signature", signature)
    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(watch_module.watch_library(Path("source"), Path("cache"), iterations=3))
    assert [event["event"] for event in events] == ["error", "recovered"]
    assert len(refreshes) == 1


def test_source_change_during_refresh_forces_another_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock(monkeypatch)
    calls = []
    monkeypatch.setattr(watch_module, "_input_signature", lambda source: "same")

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        calls.append(source)
        return result(str(len(calls)), changed=len(calls) == 1)

    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(Path("source"), Path("cache"), interval=1, iterations=3)
    )

    assert len(calls) == 2
    assert [event["event"] for event in events] == ["ready", "updated"]


def test_post_capture_edit_is_not_swallowed_by_a_late_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock(monkeypatch)
    source_state = "before"
    calls = []
    monkeypatch.setattr(watch_module, "_input_signature", lambda source: source_state)

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        nonlocal source_state
        calls.append(source_state)
        source_state = "after"
        return result(str(len(calls)))

    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    events = list(
        watch_module.watch_library(
            Path("source"), Path("cache"), interval=1, debounce=0, iterations=2
        )
    )

    assert calls == ["before", "after"]
    assert [event["event"] for event in events] == ["ready", "updated"]


@pytest.mark.parametrize("location", ["sleep", "refresh"])
def test_interrupt_propagates_without_retry_or_extra_refresh(
    location: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock(monkeypatch)
    calls = []
    monkeypatch.setattr(watch_module, "_input_signature", lambda source: "same")

    def interrupt(*args: object) -> None:
        raise KeyboardInterrupt

    def refresh(source: Path, destination: Path) -> dict[str, object]:
        calls.append(source)
        if location == "refresh":
            interrupt()
        return result("one")

    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    if location == "sleep":
        monkeypatch.setattr(watch_module.time, "sleep", interrupt)
    with pytest.raises(KeyboardInterrupt):
        list(watch_module.watch_library(Path("source"), Path("cache"), iterations=2))
    assert len(calls) == 1


def test_native_only_edit_refreshes_and_transient_failure_keeps_the_old_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock(monkeypatch)
    source = make_library(tmp_path / "source")
    native = source / "Databases" / "notes_main.db"
    native.mkdir()
    wal = native / "000003.log"
    wal.write_bytes(b"generated native WAL one")
    data_dir = tmp_path / "cache"
    actual_refresh = watch_module.refresh_cache
    calls = 0

    def refresh(library: Path, cache: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GoodnotesError("Generated transient capture failure")
        return actual_refresh(library, cache)

    monkeypatch.setattr(watch_module, "refresh_cache", refresh)
    watcher = watch_module.watch_library(source, data_dir, interval=0.05, debounce=0, iterations=3)
    initial = next(watcher)
    first_snapshot = Path(str(initial["snapshot"]))
    first_pointer = (data_dir / "active.json").read_bytes()
    relative_wal = "Library/Databases/notes_main.db/000003.log"
    assert (first_snapshot / relative_wal).read_bytes().endswith(b"one")
    wal.write_bytes(b"generated native WAL two")
    source_after_edit = wal.read_bytes()
    failed = next(watcher)
    assert failed["event"] == "error"
    assert (data_dir / "active.json").read_bytes() == first_pointer
    assert first_snapshot.is_dir()
    recovered = next(watcher)
    assert recovered["event"] == "recovered"
    second_snapshot = Path(str(recovered["snapshot"]))
    assert second_snapshot != first_snapshot
    assert not first_snapshot.exists()
    assert (second_snapshot / relative_wal).read_bytes() == source_after_edit
    assert wal.read_bytes() == source_after_edit
    assert json.loads((data_dir / "active.json").read_text())["snapshot"] == str(second_snapshot)
    with pytest.raises(StopIteration):
        next(watcher)

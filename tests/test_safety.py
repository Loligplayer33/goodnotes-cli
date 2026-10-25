import errno
import json
import os
import sqlite3
import stat
from contextlib import ExitStack
from pathlib import Path

import pytest

from goodnotes_cli import safety as safety_module
from goodnotes_cli.errors import CacheAccessError, FilesystemAccessError, GoodnotesError
from goodnotes_cli.safety import (
    Sandbox,
    open_database,
    path_contains,
    paths_overlap,
    resolve_confined_file,
    safe_output,
    safe_remove_tree,
    stat_key,
)


def populate_snapshot(root: Path, *, include_fts: bool = False) -> None:
    database_dir = root / "Library" / "Databases"
    attachments_dir = root / "Library" / "Attachments"
    database_dir.mkdir(parents=True)
    attachments_dir.mkdir()
    with sqlite3.connect(database_dir / "projection.sqlite") as connection:
        connection.execute("CREATE TABLE item (name TEXT)")
        connection.execute("INSERT INTO item VALUES ('sandbox')")
    if include_fts:
        with sqlite3.connect(database_dir / "fts.sqlite") as connection:
            connection.execute("CREATE TABLE text_cache (body TEXT)")
    (root / "goodnotes-snapshot.json").write_text(
        json.dumps({"format_version": 1}), encoding="utf-8"
    )


def make_snapshot(root: Path, *, include_fts: bool = False) -> Sandbox:
    populate_snapshot(root, include_fts=include_fts)
    return Sandbox.load(root)


def test_load_requires_a_valid_snapshot_marker(tmp_path: Path) -> None:
    root = tmp_path / "unmarked"
    root.mkdir()

    with pytest.raises(GoodnotesError, match="missing goodnotes-snapshot.json"):
        Sandbox.load(root)


def test_load_supports_optional_fts_database(tmp_path: Path) -> None:
    sandbox = make_snapshot(tmp_path / "snapshot")

    assert sandbox.root == tmp_path / "snapshot"
    assert sandbox.library_dir == sandbox.root / "Library"
    assert sandbox.projection_path.is_file()
    assert sandbox.fts_path is None
    assert sandbox.attachments_dir.is_dir()


def test_load_retains_the_parsed_snapshot_marker(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    populate_snapshot(root)
    marker = root / "goodnotes-snapshot.json"
    marker.write_text(
        json.dumps(
            {
                "format_version": 1,
                "created_at_utc": "2026-10-25T12:00:00Z",
                "capture_stats": {"captured_files": 2},
            }
        ),
        encoding="utf-8",
    )

    sandbox = Sandbox.load(root)

    assert sandbox.captured_at == "2026-10-25T12:00:00Z"
    assert sandbox.marker_data["capture_stats"] == {"captured_files": 2}


def test_load_refuses_a_marked_production_container_path(tmp_path: Path) -> None:
    root = tmp_path / "Library" / "Containers" / "com.goodnotesapp.x" / "Data" / "snapshot"
    populate_snapshot(root)

    with pytest.raises(GoodnotesError, match="production Goodnotes path"):
        Sandbox.load(root)


def test_load_rejects_symlinked_database(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    sandbox = make_snapshot(root)
    projection = sandbox.projection_path
    copied = root / "outside.sqlite"
    projection.rename(copied)
    projection.symlink_to(copied)

    with pytest.raises(GoodnotesError, match="must not be a symlink"):
        Sandbox.load(root)


@pytest.mark.parametrize("relative_path", ["Library", "Library/Databases", "Library/Attachments"])
def test_load_rejects_symlinked_required_directories(tmp_path: Path, relative_path: str) -> None:
    root = tmp_path / "snapshot"
    make_snapshot(root)
    source = root / relative_path
    linked_directory = tmp_path / "linked-directory"
    linked_directory.mkdir()
    source.rename(source.with_name(source.name + "-original"))
    source.symlink_to(linked_directory, target_is_directory=True)

    with pytest.raises(GoodnotesError, match="required directory"):
        Sandbox.load(root)


def test_load_and_open_database_reject_wal_only_committed_rows(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    database_dir = root / "Library" / "Databases"
    database_dir.mkdir(parents=True)
    (root / "Library" / "Attachments").mkdir()
    database_path = database_dir / "projection.sqlite"

    writer = sqlite3.connect(database_path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE item (name TEXT)")
    writer.execute("INSERT INTO item VALUES ('checkpointed')")
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer.execute("INSERT INTO item VALUES ('wal only')")
    writer.commit()
    (root / "goodnotes-snapshot.json").write_text(
        json.dumps({"format_version": 1}), encoding="utf-8"
    )

    wal_path = Path(f"{database_path}-wal")
    assert wal_path.is_file() and wal_path.stat().st_size > 0
    # immutable=1 ignores WAL, which is why marked snapshots must reject sidecars.
    stale_uri = f"{database_path.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(stale_uri, uri=True) as stale_reader:
        names = [row[0] for row in stale_reader.execute("SELECT name FROM item ORDER BY rowid")]
    assert names == ["checkpointed"]

    with pytest.raises(GoodnotesError, match="sidecars"):
        Sandbox.load(root)
    with pytest.raises(GoodnotesError, match="sidecars"):
        with open_database(database_path):
            pass
    writer.close()


def test_open_database_is_read_only_and_query_only(tmp_path: Path) -> None:
    sandbox = make_snapshot(tmp_path / "snapshot")

    with open_database(sandbox.projection_path) as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("SELECT name FROM item").fetchone()[0] == "sandbox"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO item VALUES ('blocked')")

    with sqlite3.connect(f"file:{sandbox.projection_path}?mode=ro", uri=True) as check:
        assert check.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 1


def test_open_database_refuses_a_production_container_path(tmp_path: Path) -> None:
    path = tmp_path / "Library" / "Containers" / "com.goodnotesapp.x" / "Data" / "db.sqlite"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE item (name TEXT)")

    with pytest.raises(GoodnotesError, match="production Goodnotes path"):
        with open_database(path):
            pass


def test_safe_output_rejects_snapshot_and_symlinked_snapshot_paths(tmp_path: Path) -> None:
    sandbox = make_snapshot(tmp_path / "snapshot")
    with pytest.raises(GoodnotesError, match="outside the Goodnotes snapshot"):
        safe_output(sandbox.root / "export.pdf", sandbox)

    alias = tmp_path / "snapshot-alias"
    alias.symlink_to(sandbox.root, target_is_directory=True)
    with pytest.raises(GoodnotesError, match="outside the Goodnotes snapshot"):
        sandbox.ensure_output(alias / "export.pdf")


def test_safe_output_rejects_production_paths_and_existing_files(tmp_path: Path) -> None:
    sandbox = make_snapshot(tmp_path / "snapshot")
    live_path = tmp_path / "Library" / "Containers" / "com.goodnotesapp.x" / "Data" / "export.pdf"
    with pytest.raises(GoodnotesError, match="production Goodnotes path"):
        safe_output(live_path, sandbox)

    existing = tmp_path / "already-there.pdf"
    existing.write_bytes(b"keep")
    with pytest.raises(GoodnotesError, match="Refusing to overwrite"):
        safe_output(existing, sandbox)
    assert existing.read_bytes() == b"keep"


def test_safe_output_rejects_input_overlap(tmp_path: Path) -> None:
    sandbox = make_snapshot(tmp_path / "snapshot")
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    input_file = input_dir / "source.pdf"
    input_file.write_bytes(b"source")

    with pytest.raises(GoodnotesError, match="overlap any input"):
        safe_output(input_file, sandbox, inputs=[input_file])
    with pytest.raises(GoodnotesError, match="overlap any input"):
        safe_output(input_dir / "export.pdf", sandbox, inputs=[input_dir])


def test_snapshot_marker_permission_error_is_structured_even_for_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "snapshot"
    make_snapshot(root)
    marker = root / "goodnotes-snapshot.json"
    original_read = Path.read_text

    def denied_read(path: Path, *args: object, **kwargs: object) -> str:
        if path == marker:
            raise PermissionError(errno.EACCES, "generated marker denial", str(marker))
        return original_read(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", denied_read)
    with pytest.raises(CacheAccessError) as caught:
        Sandbox.load(root)
    assert caught.value.code == "cache_permission_denied"
    assert caught.value.details["operation"] == "resolve_read_view"
    assert caught.value.details["path"] == str(marker)


def test_database_sidecar_inspection_permission_error_is_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = make_snapshot(tmp_path / "snapshot")
    database_dir = sandbox.projection_path.parent
    original_iterdir = Path.iterdir

    def denied_iterdir(path: Path):
        if path == database_dir:
            raise PermissionError(errno.EPERM, "generated directory denial", str(database_dir))
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", denied_iterdir)
    with pytest.raises(CacheAccessError) as caught:
        with open_database(sandbox.projection_path):
            pass
    assert caught.value.code == "cache_permission_denied"
    assert caught.value.details["operation"] == "open_snapshot_database"
    assert caught.value.details["path"] == str(database_dir)


def test_output_validation_permission_error_is_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = make_snapshot(tmp_path / "snapshot")
    destination = tmp_path / "output.png"
    original_resolve = Path.resolve

    def denied_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == destination:
            raise PermissionError(errno.EACCES, "generated output denial", str(destination))
        return original_resolve(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "resolve", denied_resolve)
    with pytest.raises(FilesystemAccessError) as caught:
        safe_output(destination, sandbox)
    assert caught.value.code == "output_permission_denied"
    assert caught.value.details["operation"] == "validate_output"
    assert caught.value.details["path"] == str(destination)


@pytest.mark.parametrize("structured", [False, True])
def test_open_database_preserves_caller_output_permission_errors(
    tmp_path: Path, structured: bool
) -> None:
    sandbox = make_snapshot(tmp_path / "snapshot")
    output_path = tmp_path / "output.png"
    underlying = PermissionError(errno.EACCES, "generated output denial", str(output_path))
    error: PermissionError | FilesystemAccessError = underlying
    if structured:
        error = FilesystemAccessError.from_exception(
            underlying,
            operation="publish_output",
            path=output_path,
            kind="output",
        )
        error.__cause__ = underlying

    with pytest.raises(type(error)) as caught:
        with ExitStack() as stack:
            first = stack.enter_context(open_database(sandbox.projection_path))
            second = stack.enter_context(open_database(sandbox.projection_path))
            raise error

    assert caught.value is error
    if structured:
        assert isinstance(caught.value, FilesystemAccessError)
        assert caught.value.code == "output_permission_denied"
        assert caught.value.details["operation"] == "publish_output"
    for connection in (first, second):
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def test_path_helpers_support_missing_leaves_without_case_folding(tmp_path: Path) -> None:
    parent = tmp_path / "existing-parent"
    parent.mkdir()
    missing_descendant = parent / "not-created" / "result.pdf"
    sibling_a = parent / "missing-a" / "result.pdf"
    sibling_b = parent / "missing-b" / "result.pdf"

    assert path_contains(parent, missing_descendant)
    assert paths_overlap(parent, missing_descendant)
    assert not paths_overlap(sibling_a, sibling_b)
    assert not path_contains(tmp_path / "absent-parent", missing_descendant)


def _case_variant(path: Path) -> Path:
    name = path.name
    for index, character in enumerate(name):
        if character.isalpha():
            replacement = character.lower() if character.isupper() else character.upper()
            return path.with_name(name[:index] + replacement + name[index + 1 :])
    return path


def test_paths_overlap_recognizes_existing_case_aliases_on_insensitive_filesystems(
    tmp_path: Path,
) -> None:
    sandbox = make_snapshot(tmp_path / "MixedCaseSnapshot")
    alias = _case_variant(sandbox.root)
    if alias == sandbox.root or not alias.exists() or not alias.samefile(sandbox.root):
        pytest.skip("temporary filesystem does not provide case-insensitive path aliases")

    absent_leaf = alias / "not-created.pdf"
    assert not absent_leaf.exists()
    assert path_contains(sandbox.root, absent_leaf)
    assert paths_overlap(sandbox.root, absent_leaf)
    with pytest.raises(GoodnotesError, match="outside the Goodnotes snapshot"):
        safe_output(absent_leaf, sandbox)


def test_identity_probe_covers_differently_cased_prefix_at_another_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = make_snapshot(tmp_path / "canonical-route" / "MixedCaseSnapshot")
    case_variant_name = _case_variant(sandbox.root).name
    alias_prefix = tmp_path / "alternate-route" / "extra-level" / case_variant_name
    alias_prefix.mkdir(parents=True)
    assert alias_prefix.is_dir()
    assert len(alias_prefix.parts) != len(sandbox.root.parts)

    original_samefile = Path.samefile

    def identity_probe(path: Path, other: str | Path) -> bool:
        other_path = Path(other)
        if (path == sandbox.root and other_path == alias_prefix) or (
            path == alias_prefix and other_path == sandbox.root
        ):
            return True
        return original_samefile(path, other_path)

    missing_leaf = alias_prefix / "not-created.pdf"
    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "samefile", identity_probe)
        assert path_contains(sandbox.root, missing_leaf)
        assert paths_overlap(sandbox.root, missing_leaf)
        with pytest.raises(GoodnotesError, match="outside the Goodnotes snapshot"):
            safe_output(missing_leaf, sandbox)

    # Without filesystem identity evidence, differing routes remain distinct.
    assert not path_contains(sandbox.root, missing_leaf)
    assert not paths_overlap(sandbox.root, missing_leaf)


def test_path_helpers_do_not_fold_case_without_identity_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = make_snapshot(tmp_path / "CaseSensitiveSnapshot")
    case_variant = _case_variant(sandbox.root)
    original_samefile = Path.samefile

    def preserve_spelling(path: str | Path) -> Path:
        return Path(path).expanduser().absolute()

    def no_alias_probe(path: Path, other: str | Path) -> bool:
        other_path = Path(other)
        if (path == sandbox.root and other_path == case_variant) or (
            path == case_variant and other_path == sandbox.root
        ):
            return False
        return original_samefile(path, other_path)

    with monkeypatch.context() as patcher:
        patcher.setattr(safety_module, "_resolved", preserve_spelling)
        patcher.setattr(Path, "samefile", no_alias_probe)
        assert not path_contains(sandbox.root, case_variant / "not-created.pdf")
        assert not paths_overlap(sandbox.root, case_variant / "not-created.pdf")


def test_path_helpers_keep_case_sensitive_directories_distinct(tmp_path: Path) -> None:
    upper = tmp_path / "CaseSensitive"
    lower = _case_variant(upper)
    upper.mkdir()
    try:
        lower.mkdir()
    except FileExistsError:
        pytest.skip("temporary filesystem does not distinguish case variants")
    if upper.samefile(lower):
        pytest.skip("temporary filesystem aliases case variants")

    assert not path_contains(upper, lower / "not-created.pdf")
    assert not paths_overlap(upper / "not-created.pdf", lower / "another.pdf")


def test_resolve_confined_file_distinguishes_missing_from_unsafe(tmp_path: Path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    present = root / "page.pdf"
    present.write_bytes(b"synthetic PDF")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (root / "escape.pdf").symlink_to(outside)

    assert resolve_confined_file(root, Path("page.pdf")) == present
    assert resolve_confined_file(root, present) == present
    assert resolve_confined_file(root, Path("missing.pdf")) is None
    with pytest.raises(GoodnotesError, match="outside its allowed directory"):
        resolve_confined_file(root, Path("escape.pdf"))
    with pytest.raises(GoodnotesError, match="outside its allowed directory"):
        resolve_confined_file(root, Path("../missing-outside.pdf"))


def test_safe_remove_tree_unlinks_symlinks_without_changing_targets(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "owned.txt").write_text("generated", encoding="utf-8")
    target_file = tmp_path / "read-only-target.txt"
    target_file.write_text("keep", encoding="utf-8")
    target_file.chmod(0o444)
    target_directory = tmp_path / "read-only-target-directory"
    target_directory.mkdir()
    target_inside = target_directory / "keep.txt"
    target_inside.write_text("keep", encoding="utf-8")
    target_inside.chmod(0o444)
    target_directory.chmod(0o555)
    (root / "linked-file").symlink_to(target_file)
    (nested / "linked-directory").symlink_to(target_directory, target_is_directory=True)
    nested.chmod(0o555)
    root.chmod(0o555)

    safe_remove_tree(root)

    assert not root.exists()
    assert stat.S_IMODE(target_file.stat().st_mode) == 0o444
    assert stat.S_IMODE(target_directory.stat().st_mode) == 0o555
    assert stat.S_IMODE(target_inside.stat().st_mode) == 0o444
    assert target_file.read_text(encoding="utf-8") == "keep"
    assert target_inside.read_text(encoding="utf-8") == "keep"


def test_safe_remove_tree_removes_a_symlink_root_without_visiting_its_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target-directory"
    target.mkdir()
    protected = target / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    protected.chmod(0o444)
    link = tmp_path / "staging-link"
    link.symlink_to(target, target_is_directory=True)

    safe_remove_tree(link)

    assert not link.is_symlink()
    assert target.is_dir()
    assert protected.read_text(encoding="utf-8") == "keep"
    assert stat.S_IMODE(protected.stat().st_mode) == 0o444


@pytest.mark.parametrize("replacement_timing", ["before_open", "after_open"])
def test_safe_remove_tree_does_not_follow_a_directory_replaced_by_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_timing: str,
) -> None:
    root = tmp_path / "staging"
    child = root / "nested"
    child.mkdir(parents=True)
    (child / "generated.txt").write_text("generated", encoding="utf-8")
    target = tmp_path / "external-target"
    target.mkdir()
    protected = target / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    protected.chmod(0o444)
    target.chmod(0o555)
    moved_child = root / "nested-before-race"
    original_open = os.open
    replaced = False

    def replace_with_symlink() -> None:
        nonlocal replaced
        child.rename(moved_child)
        child.symlink_to(target, target_is_directory=True)
        replaced = True

    def racing_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "nested" and dir_fd is not None and flags & getattr(os, "O_DIRECTORY", 0):
            if replacement_timing == "before_open":
                replace_with_symlink()
                return original_open(path, flags, mode, dir_fd=dir_fd)
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            replace_with_symlink()
            return descriptor
        return original_open(path, flags, mode, dir_fd=dir_fd)

    expected_error = OSError if replacement_timing == "before_open" else GoodnotesError
    with monkeypatch.context() as patcher:
        patcher.setattr(os, "open", racing_open)
        with pytest.raises(expected_error):
            safe_remove_tree(root)

    assert replaced
    assert child.is_symlink()
    assert child.resolve() == target
    assert moved_child.is_dir()
    assert target.is_dir()
    assert protected.read_text(encoding="utf-8") == "keep"
    assert stat.S_IMODE(target.stat().st_mode) == 0o555
    assert stat.S_IMODE(protected.stat().st_mode) == 0o444

    safe_remove_tree(root)
    assert not root.exists()
    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o555
    assert stat.S_IMODE(protected.stat().st_mode) == 0o444


def test_safe_remove_tree_rejects_parent_replacement_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "managed-parent"
    managed_tree = parent / "staging"
    managed_tree.mkdir(parents=True)
    (managed_tree / "generated.txt").write_text("generated", encoding="utf-8")
    replacement_parent = tmp_path / "replacement-parent"
    external_tree = replacement_parent / "staging"
    external_tree.mkdir(parents=True)
    protected = external_tree / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    protected.chmod(0o444)
    external_tree.chmod(0o555)
    moved_parent = tmp_path / "managed-parent-before-race"
    original_open = os.open
    replaced = False

    def racing_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if Path(path) == parent and dir_fd is None:
            parent.rename(moved_parent)
            replacement_parent.rename(parent)
            replaced = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "open", racing_open)
        with pytest.raises(GoodnotesError, match="parent changed during cleanup"):
            safe_remove_tree(managed_tree)

    assert replaced
    assert moved_parent.is_dir()
    assert (moved_parent / "staging" / "generated.txt").read_text(encoding="utf-8") == "generated"
    assert parent.is_dir()
    external_protected = parent / "staging" / "protected.txt"
    assert external_protected.read_text(encoding="utf-8") == "keep"
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert stat.S_IMODE((parent / "staging").stat().st_mode) == 0o555
    assert stat.S_IMODE(external_protected.stat().st_mode) == 0o444

    safe_remove_tree(moved_parent / "staging")
    safe_remove_tree(parent / "staging")
    moved_parent.rmdir()
    parent.rmdir()


def test_safe_remove_tree_rejects_intermediate_parent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed_outer = tmp_path / "managed-route"
    managed_parent = managed_outer / "nested-parent"
    managed_tree = managed_parent / "staging"
    managed_tree.mkdir(parents=True)
    (managed_tree / "generated.txt").write_text("generated", encoding="utf-8")
    replacement_outer = tmp_path / "replacement-route"
    replacement_parent = replacement_outer / "nested-parent"
    external_tree = replacement_parent / "staging"
    external_tree.mkdir(parents=True)
    protected = external_tree / "protected.txt"
    protected.write_text("keep", encoding="utf-8")
    protected.chmod(0o444)
    external_tree.chmod(0o555)
    moved_outer = tmp_path / "managed-route-before-race"
    parent_to_open = managed_parent.resolve(strict=True)
    original_open = os.open
    replaced = False

    def racing_open(
        path: str | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if Path(path) == parent_to_open and dir_fd is None:
            managed_outer.rename(moved_outer)
            replacement_outer.rename(managed_outer)
            replaced = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "open", racing_open)
        with pytest.raises(GoodnotesError, match="parent changed during cleanup"):
            safe_remove_tree(managed_tree)

    assert replaced
    assert (moved_outer / "nested-parent" / "staging" / "generated.txt").read_text(
        encoding="utf-8"
    ) == "generated"
    assert (managed_outer / "nested-parent").is_dir()
    external_protected = managed_outer / "nested-parent" / "staging" / "protected.txt"
    assert external_protected.read_text(encoding="utf-8") == "keep"
    assert stat.S_IMODE((managed_outer / "nested-parent" / "staging").stat().st_mode) == 0o555
    assert stat.S_IMODE(external_protected.stat().st_mode) == 0o444

    safe_remove_tree(moved_outer / "nested-parent" / "staging")
    safe_remove_tree(managed_outer / "nested-parent" / "staging")
    (moved_outer / "nested-parent").rmdir()
    (managed_outer / "nested-parent").rmdir()
    moved_outer.rmdir()
    managed_outer.rmdir()


def test_stat_key_uses_shared_identity_and_freshness_fields(tmp_path: Path) -> None:
    path = tmp_path / "signature"
    path.write_text("signature", encoding="utf-8")
    info = path.stat()

    assert stat_key(info) == (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )

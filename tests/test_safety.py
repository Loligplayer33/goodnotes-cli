import json
import sqlite3
from pathlib import Path

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.safety import Sandbox, open_database, safe_output


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

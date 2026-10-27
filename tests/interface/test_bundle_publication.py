"""Explicit bundle cleanup retains ownership across parent replacement."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli import rendering
from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.rendering import publication
from tests.support.cli import invoke_in_process
from tests.support.library import _create_snapshot


@pytest.mark.parametrize("phase", ["render", "handoff", "publication"])
def test_bundle_failure_preserves_replacement_staging_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)
    parent = tmp_path / "output"
    parent.mkdir()
    moved = tmp_path / "original-output"
    stage_name: str | None = None
    original_render = rendering.render_pages
    original_publish = publication.publish_bundle
    original_link = os.link

    def replace_parent(stage: Path) -> None:
        nonlocal stage_name
        stage_name = stage.name
        parent.rename(moved)
        parent.mkdir()
        replacement = parent / stage_name
        replacement.mkdir()
        (replacement / "unrelated").write_text("generated unrelated file", encoding="utf-8")

    def failed_render(*args: Any, **kwargs: Any) -> Any:
        original_render(*args, **kwargs)
        replace_parent(args[2][0].parent)
        raise GoodnotesError("Generated staging failure.")

    def replaced_handoff(stage: Path, destination: Path, **kwargs: Any) -> None:
        replace_parent(stage)
        original_publish(stage, destination, **kwargs)

    def replaced_publication(*args: Any, **kwargs: Any) -> None:
        original_link(*args, **kwargs)
        if kwargs.get("src_dir_fd") is not None and args[0] == "text.json":
            replace_parent(next(parent.glob(".goodnotes-bundle-*")))

    if phase == "render":
        monkeypatch.setattr(rendering, "render_pages", failed_render)
    elif phase == "handoff":
        monkeypatch.setattr(publication, "publish_bundle", replaced_handoff)
    else:
        monkeypatch.setattr(publication.os, "link", replaced_publication)
    result = invoke_in_process(
        [
            "bundle",
            "doc-hostile",
            "--pages",
            "3",
            "--layer",
            "background",
            "--output",
            str(parent / "bundle"),
        ],
        tmp_path,
        sandbox=sandbox.root,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "operation_failed"
    assert stage_name is not None
    assert (parent / stage_name / "unrelated").read_text() == "generated unrelated file"
    assert not (moved / stage_name).exists()

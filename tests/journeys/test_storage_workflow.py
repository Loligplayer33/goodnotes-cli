"""Managed visual requests use generated snapshots and preserve caller-owned data."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pymupdf
import pytest

import goodnotes_cli.rendering.cache as documents
import goodnotes_cli.rendering.registered as registered
from goodnotes_cli.artifacts import ArtifactCache
from goodnotes_cli.cache import refresh_cache
from goodnotes_cli.library import Library
from goodnotes_cli.models import PageText, TextPart
from goodnotes_cli.native import NativeStoreSession
from tests.support.cli import _tree_manifest, invoke_in_process, successful
from tests.support.library import _create_snapshot
from tests.support.native_store_fixture import (
    NOTEBOOK,
    GeneratedNativeStore,
    _stroke,
)
from tests.support.native_store_fixture import generated_source as generated_source
from tests.support.native_store_fixture import native_runtime as native_runtime


@pytest.fixture
def visual_snapshot(tmp_path: Path) -> Path:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    pdf_path = tmp_path / "generated.pdf"
    with pymupdf.open() as pdf:
        for number in range(12):
            page = pdf.new_page(width=240, height=180)
            page.insert_text((15, 80), f"Synthetic page {number + 1}")
        pdf.save(pdf_path)
    for name in ("safe.pdf", "fallback.pdf"):
        shutil.copyfile(pdf_path, sandbox.attachments_dir / name)
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("UPDATE attachments SET path = 'safe.pdf' WHERE id = 'attachment-unsafe'")
    return sandbox.root


def _bundle(snapshot: Path, tmp_path: Path, *options: str) -> dict[str, Any]:
    return successful(
        invoke_in_process(
            ["bundle", "doc-hostile", "--layer", "background", *options],
            tmp_path,
            sandbox=snapshot,
        )
    )


def test_managed_bundle_reuses_images_and_has_one_self_contained_manifest(
    visual_snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = documents._render_page_with_resources
    calls = []

    def traced(*args: Any, **kwargs: Any) -> dict[str, object]:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(documents, "_render_page_with_resources", traced)
    before = _tree_manifest(visual_snapshot)
    first = _bundle(visual_snapshot, tmp_path, "--pages", "2,1")
    second = _bundle(visual_snapshot, tmp_path, "--pages", "2,1")
    assert len(calls) == 2
    assert first == second
    assert first["temporary"] is True
    assert first["pages"] == [2, 1]
    manifest = json.loads(Path(first["manifest"]).read_text())
    assert [page["page"] for page in manifest["pages"]] == [2, 1]
    for entry in manifest["pages"]:
        image = Path(first["directory"]) / entry["image"]
        assert image.is_file()
        assert entry["render"]["path"] == str(image)
        assert entry["render"]["includes_handwriting"] is False
        assert entry["render"]["warnings"]
    marker = json.loads((Path(first["directory"]) / ".artifact.json").read_text())
    assert marker["result"] == {"kind": "bundle"}
    assert _tree_manifest(visual_snapshot) == before


def test_render_and_bundle_share_generated_png_storage(
    visual_snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = documents._render_page_with_resources
    calls = []

    def traced(*args: Any, **kwargs: Any) -> dict[str, object]:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(documents, "_render_page_with_resources", traced)
    image = successful(
        invoke_in_process(
            ["render", "doc-hostile", "--layer", "background"], tmp_path, sandbox=visual_snapshot
        )
    )
    bundle = _bundle(visual_snapshot, tmp_path)
    bundle_image = Path(bundle["page_evidence"][0]["image"])
    assert len(calls) == 1
    assert os.path.samefile(image["path"], bundle_image)
    assert image["temporary"] is True


def test_bundle_uses_fresh_text_when_reusing_an_image(
    visual_snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Library.read_page
    text = ["First text"]

    def current_text(self: Library, *args: Any, **kwargs: Any) -> PageText:
        return replace(original(self, *args, **kwargs), parts=[TextPart("pdf", text[0])])

    monkeypatch.setattr(Library, "read_page", current_text)
    first = _bundle(visual_snapshot, tmp_path)
    text[0] = "Updated text"
    second = _bundle(visual_snapshot, tmp_path)
    assert first["manifest"] != second["manifest"]
    assert second["page_evidence"][0]["text"]["parts"][0]["text"] == "Updated text"
    assert os.path.samefile(first["page_evidence"][0]["image"], second["page_evidence"][0]["image"])


def test_identical_images_across_distinct_views_share_storage(
    visual_snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "other" / visual_snapshot.name
    shutil.copytree(visual_snapshot, other)
    original = documents._render_page_with_resources
    calls = []

    def traced(*args: Any, **kwargs: Any) -> dict[str, object]:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(documents, "_render_page_with_resources", traced)
    first = _bundle(visual_snapshot, tmp_path)
    second = _bundle(other, tmp_path)
    assert len(calls) == 2
    assert first["manifest"] != second["manifest"]
    assert os.path.samefile(first["page_evidence"][0]["image"], second["page_evidence"][0]["image"])


def test_renderer_upgrade_invalidates_cached_images(
    visual_snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = documents._render_page_with_resources
    calls = []

    def traced(*args: Any, **kwargs: Any) -> dict[str, object]:
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(documents, "_render_page_with_resources", traced)
    first = _bundle(visual_snapshot, tmp_path)
    monkeypatch.setattr(documents, "_RENDER_CACHE_VERSION", documents._RENDER_CACHE_VERSION + 1)
    second = _bundle(visual_snapshot, tmp_path)
    third = _bundle(visual_snapshot, tmp_path)

    assert len(calls) == 2
    assert first["manifest"] != second["manifest"]
    assert second == third


def test_render_settings_invalidate_reuse(visual_snapshot: Path, tmp_path: Path) -> None:
    first = _bundle(visual_snapshot, tmp_path, "--dpi", "72")
    second = _bundle(visual_snapshot, tmp_path, "--dpi", "150")
    first_image = Path(first["page_evidence"][0]["image"])
    second_image = Path(second["page_evidence"][0]["image"])
    assert not os.path.samefile(first_image, second_image)
    low = pymupdf.Pixmap(first_image)
    high = pymupdf.Pixmap(second_image)
    assert low.width < high.width


def test_managed_expiry_preserves_explicitly_saved_outputs(
    visual_snapshot: Path, tmp_path: Path
) -> None:
    saved = tmp_path / "saved"
    persistent = _bundle(visual_snapshot, tmp_path, "--output", str(saved))
    before = _tree_manifest(saved)
    assert "temporary" not in persistent
    temporary = _bundle(visual_snapshot, tmp_path)
    managed_root = tmp_path / "state" / "artifacts"
    with ArtifactCache(managed_root, visual_snapshot, clock=lambda: time.time() + 86_500):
        pass
    assert not Path(temporary["manifest"]).exists()
    assert _tree_manifest(saved) == before
    assert Path(persistent["manifest"]).is_file()


def test_temporary_output_fields_and_optional_output_are_in_schema(
    visual_snapshot: Path, tmp_path: Path
) -> None:
    for command in ("render", "bundle"):
        schema = successful(invoke_in_process(["schema", command], tmp_path))
        option = next(item for item in schema["options"] if item["name"] == "output")
        assert option["required"] is False
        assert "temporary" in schema["output"]["optional_fields"]
    projected = successful(
        invoke_in_process(
            ["--fields", "path,temporary", "render", "doc-hostile", "--layer", "background"],
            tmp_path,
            sandbox=visual_snapshot,
        )
    )
    assert projected["temporary"] is True
    assert Path(projected["path"]).is_file()


def test_cached_annotated_requests_still_verify_the_registered_pdf_once(
    visual_snapshot: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    annotated = tmp_path / "annotated.pdf"
    with pymupdf.open() as pdf:
        for number in range(3):
            page = pdf.new_page(width=240, height=180)
            page.insert_text((15, 80), f"Synthetic annotated page {number + 1}")
        pdf.save(annotated)
    successful(
        invoke_in_process(
            ["attach-pdf", "doc-hostile", str(annotated)], tmp_path, sandbox=visual_snapshot
        )
    )
    original_hash = registered._sha256_stable
    original_render = documents._render_page_with_resources
    hashes = []
    renders = []

    def counted_hash(path: Path) -> tuple[str, os.stat_result]:
        hashes.append(path)
        return original_hash(path)

    def counted_render(*args: Any, **kwargs: Any) -> dict[str, object]:
        renders.append(1)
        return original_render(*args, **kwargs)

    monkeypatch.setattr(registered, "_sha256_stable", counted_hash)
    monkeypatch.setattr(documents, "_render_page_with_resources", counted_render)
    first = _bundle(visual_snapshot, tmp_path, "--layer", "annotated", "--pages", "1-2")
    second = _bundle(visual_snapshot, tmp_path, "--layer", "annotated", "--pages", "1-2")
    assert first == second
    assert len(hashes) == 2
    assert len(renders) == 2
    assert first["page_evidence"][0]["render"]["includes_handwriting"] is None
    with annotated.open("r+b") as stream:
        stream.seek(-10, os.SEEK_END)
        stream.write(b"corruption")
    failed = invoke_in_process(
        ["bundle", "doc-hostile", "--layer", "annotated"], tmp_path, sandbox=visual_snapshot
    )
    assert failed.returncode == 1
    assert "changed" in failed.stderr


def test_missing_background_does_not_return_a_cached_image(
    visual_snapshot: Path, tmp_path: Path
) -> None:
    first = _bundle(visual_snapshot, tmp_path)
    (visual_snapshot / "Library" / "Attachments" / "safe.pdf").unlink()
    failed = invoke_in_process(
        ["bundle", "doc-hostile", "--layer", "background"],
        tmp_path,
        sandbox=visual_snapshot,
    )
    assert failed.returncode == 1
    assert Path(first["page_evidence"][0]["image"]).is_file()


def test_native_attachment_changes_invalidate_cached_images(
    generated_source: tuple[Path, GeneratedNativeStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _store = generated_source
    with sqlite3.connect(source / "Databases" / "projection.sqlite") as database:
        database.execute(
            "INSERT INTO attachments(id, path, document_id, available) "
            "VALUES ('auxiliary', 'auxiliary.bin', ?, 1)",
            (NOTEBOOK,),
        )
    (source / "Attachments" / "auxiliary.bin").write_bytes(b"original auxiliary asset")
    state = tmp_path / "state"
    view = refresh_cache(source, state)
    original = NativeStoreSession.__enter__
    opens = []

    def traced(self: NativeStoreSession) -> NativeStoreSession:
        opens.append(1)
        return original(self)

    monkeypatch.setattr(NativeStoreSession, "__enter__", traced)
    arguments = ["bundle", NOTEBOOK, "--pages", "1", "--dpi", "72"]
    first = successful(invoke_in_process(arguments, tmp_path, data_dir=state))
    assert successful(invoke_in_process(arguments, tmp_path, data_dir=state)) == first
    assert len(opens) == 1

    # Deliberately damage a generated fixture to exercise cache-hit validation.
    snapshot_path = view["snapshot"]
    assert isinstance(snapshot_path, str)
    snapshot = Path(snapshot_path)
    asset = snapshot / "Library" / "Attachments" / "auxiliary.bin"
    original_mode = asset.stat().st_mode
    asset.chmod(0o600)
    asset.write_bytes(b"changed auxiliary")
    asset.chmod(original_mode)
    second = successful(invoke_in_process(arguments, tmp_path, data_dir=state))
    assert len(opens) == 2
    assert second["snapshot"] == first["snapshot"]
    assert second["manifest"] != first["manifest"]


def test_native_cached_request_skips_engine_and_refresh_invalidates_images(
    generated_source: tuple[Path, GeneratedNativeStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = generated_source
    state = tmp_path / "state"
    refresh_cache(source, state)
    original = NativeStoreSession.__enter__
    opens = []

    def traced(self: NativeStoreSession) -> NativeStoreSession:
        opens.append(1)
        return original(self)

    monkeypatch.setattr(NativeStoreSession, "__enter__", traced)
    arguments = ["bundle", NOTEBOOK, "--pages", "1-2", "--dpi", "72"]
    first = successful(invoke_in_process(arguments, tmp_path, data_dir=state))
    second = successful(invoke_in_process(arguments, tmp_path, data_dir=state))
    assert first == second
    assert len(opens) == 1
    assert all(
        page["render"]["annotation_status"] == "native-reconstructed-approximate"
        for page in first["page_evidence"]
    )
    old_image = Path(first["page_evidence"][0]["image"])
    old_bytes = old_image.read_bytes()
    store.write(_stroke(0, offset_y=80))
    refresh_cache(source, state)
    current = successful(invoke_in_process(arguments, tmp_path, data_dir=state))
    assert len(opens) == 2
    assert current["snapshot"] != first["snapshot"]
    assert Path(current["page_evidence"][0]["image"]).read_bytes() != old_bytes
    assert old_image.read_bytes() == old_bytes


@pytest.mark.parametrize("bad_identity", [None, "other-document"])
def test_managed_bundle_rejects_bad_hit_before_publishing_staged_miss(
    visual_snapshot: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_identity: str | None,
) -> None:
    successful(
        invoke_in_process(
            ["render", "doc-hostile", "--page", "2", "--layer", "background"],
            tmp_path,
            sandbox=visual_snapshot,
        )
    )
    original_lookup = ArtifactCache.lookup
    original_publish = ArtifactCache.publish
    publications: list[str] = []
    hits: list[int] = []

    def malformed_hit(self: ArtifactCache, key: str) -> Any:
        hit = original_lookup(self, key)
        if hit is not None and hit.result.get("page") == 2:
            hits.append(2)
            result = dict(hit.result)
            if bad_identity is None:
                result.pop("document_id", None)
            else:
                result["document_id"] = bad_identity
            return replace(hit, result=result)
        return hit

    def track_publish(self: ArtifactCache, key: str, stage: Path, result: dict[str, Any]) -> Any:
        publications.append(key)
        return original_publish(self, key, stage, result)

    monkeypatch.setattr(ArtifactCache, "lookup", malformed_hit)
    monkeypatch.setattr(ArtifactCache, "publish", track_publish)
    response = invoke_in_process(
        ["bundle", "doc-hostile", "--pages", "1,2", "--layer", "background"],
        tmp_path,
        sandbox=visual_snapshot,
    )
    assert response.returncode != 0
    assert "missing or inconsistent document identity" in response.stderr
    assert hits == [2]
    assert not publications
    assert not list((tmp_path / "state" / "artifacts").rglob("text.json"))
    assert not list((tmp_path / "state" / "artifacts").rglob(".staging-*"))


@pytest.mark.parametrize("bad_identity", [None, "other-document"])
def test_managed_bundle_rejects_bad_fresh_render_before_publication(
    visual_snapshot: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_identity: str | None,
) -> None:
    original_render = documents._render_page_with_resources
    original_publish = ArtifactCache.publish
    publications: list[str] = []

    def malformed_render(*args: Any, **kwargs: Any) -> dict[str, object]:
        result = original_render(*args, **kwargs)
        if bad_identity is None:
            result.pop("document_id", None)
        else:
            result["document_id"] = bad_identity
        return result

    def track_publish(self: ArtifactCache, key: str, stage: Path, result: dict[str, Any]) -> Any:
        publications.append(key)
        return original_publish(self, key, stage, result)

    monkeypatch.setattr(documents, "_render_page_with_resources", malformed_render)
    monkeypatch.setattr(ArtifactCache, "publish", track_publish)
    response = invoke_in_process(
        ["bundle", "doc-hostile", "--pages", "1,2", "--layer", "background"],
        tmp_path,
        sandbox=visual_snapshot,
    )
    assert response.returncode != 0
    assert "missing or inconsistent document identity" in response.stderr
    assert not publications
    assert not list((tmp_path / "state" / "artifacts").rglob("text.json"))
    assert not list((tmp_path / "state" / "artifacts").rglob(".staging-*"))

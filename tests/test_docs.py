from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

DOCS_SCRIPT = Path(__file__).parents[1] / "scripts" / "docs.py"
spec = importlib.util.spec_from_file_location("docs_generator", DOCS_SCRIPT)
assert spec is not None and spec.loader is not None
docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docs)


def test_generated_reference_uses_cli_schema_defaults_and_stable_anchors() -> None:
    reference = docs.render_reference()

    assert "`goodnotes render [OPTIONS] NOTEBOOK`" in reference
    assert "| `--fields` | Text | Unset |" in reference
    assert "| `--json` | Flag | `False` |" in reference
    assert "Generated from the registered Typer commands" in reference
    assert "results and output](output.md)" in reference
    assert "| `--force` / `--no-force` | Flag | `False` |" in reference
    assert "goodnotes schema [OPTIONS] [COMMAND]" in reference
    assert "### render" in reference
    assert "## Artifact retention" in reference
    assert "output.md#temporary-and-saved-files" in reference


def test_parameter_rows_escape_pipes_and_flatten_multiline_help() -> None:
    row = docs._param_rows(
        [
            {
                "name": "topic",
                "type": "str",
                "required": False,
                "default": "a|b",
                "flags": ["--topic"],
                "is_flag": False,
                "help": "First | line\nsecond line",
            }
        ]
    )[0]

    assert row == r"| `--topic` | Text | `a\|b` | First \| line second line |"


def test_nav_check_requires_every_published_docs_page(tmp_path: Path) -> None:
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    (docs_root / "index.md").write_text("# Home\n")
    (docs_root / "new-guide.md").write_text("# New guide\n")
    config = tmp_path / "mkdocs.yml"
    config.write_text(
        "docs_dir: docs\nnav:\n  - Home: index.md\n  - Contributing: contributing.md\n"
    )

    assert docs.check_nav(tmp_path) == ["mkdocs.yml nav is missing docs/new-guide.md"]

    config.write_text(
        "docs_dir: docs\nnav:\n  - Home: index.md\n"
        "  - Contributing: contributing.md\n  - New guide: new-guide.md\n"
    )
    assert docs.check_nav(tmp_path) == []


def test_check_reports_stale_generated_reference(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    reference = tmp_path / "docs" / "commands.md"
    reference.write_text("# Old generated command list\n")

    errors = docs.check(tmp_path, reference)

    assert any("commands.md is stale" in error for error in errors)


def test_link_checker_reports_missing_file_and_fragment(tmp_path: Path) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    page = source / "guide.md"
    page.write_text("""# Guide

[Missing page](missing.md)
[Missing section](#unknown-section)

````markdown
```
[Example only](also-missing.md#not-real)
```
````
""")

    errors = docs.check_links(tmp_path, [page])

    assert errors == [
        "docs/guide.md: missing file link missing.md",
        "docs/guide.md: missing anchor #unknown-section",
    ]


def test_link_checker_resolves_real_fragment_and_ignores_fenced_examples(tmp_path: Path) -> None:
    source = tmp_path / "docs"
    source.mkdir()
    target = source / "reference.md"
    target.write_text("# Real heading\n")
    page = source / "guide.md"
    page.write_text("""# Guide

[Works](reference.md#real-heading)

~~~md
[Not a link](missing.md)
~~~
""")

    assert docs.check_links(tmp_path, [page, target]) == []


def test_site_hook_rewrites_root_and_source_links_only() -> None:
    page = """[Contributing](../../CONTRIBUTING.md)
[Test](../../tests/test_cli.py#example)
[Neighbor](usage.md#overview)
"""

    output = docs.on_page_markdown(
        page,
        page=SimpleNamespace(file=SimpleNamespace(src_uri="nested/usage.md")),
        config={"repo_url": "https://github.com/example/project"},
        files=None,
    )

    assert "[Contributing](../contributing.md)" in output
    assert "https://github.com/example/project/blob/main/tests/test_cli.py#example" in output
    assert "[Neighbor](usage.md#overview)" in output


def test_markdown_file_inventory_checks_nested_docs_and_feature_pointer(tmp_path: Path) -> None:
    (tmp_path / "docs" / "nested").mkdir(parents=True)
    (tmp_path / "docs" / "nested" / "page.md").write_text("# Nested\n")
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "verification.md").write_text("# Feature map\n")
    (tmp_path / "features" / "nested").mkdir()
    (tmp_path / "features" / "nested" / "private.md").write_text("# Outside inventory\n")
    (tmp_path / ".local").mkdir()
    (tmp_path / ".local" / "private.md").write_text("# Private\n")

    paths = {path.relative_to(tmp_path).as_posix() for path in docs.markdown_files(tmp_path)}

    assert paths == {"docs/nested/page.md", "features/verification.md"}


def test_generated_contributing_page_and_local_doc_links(monkeypatch) -> None:
    mkdocs = ModuleType("mkdocs")
    mkdocs.__path__ = []
    structure = ModuleType("mkdocs.structure")
    structure.__path__ = []
    files_module = ModuleType("mkdocs.structure.files")

    class FakeFile:
        @classmethod
        def generated(cls, config, src_uri, *, content):
            return SimpleNamespace(src_uri=src_uri, content=content)

    files_module.__dict__["File"] = FakeFile
    monkeypatch.setitem(sys.modules, "mkdocs", mkdocs)
    monkeypatch.setitem(sys.modules, "mkdocs.structure", structure)
    monkeypatch.setitem(sys.modules, "mkdocs.structure.files", files_module)
    generated_files = docs.on_files([], config={})

    contributing_page = next(file for file in generated_files if file.src_uri == "contributing.md")
    assert contributing_page.content == docs.CONTRIBUTING.read_text()

    output = docs.on_page_markdown(
        "[Architecture](docs/architecture.md)\n[Tests](../tests/test_cli.py)",
        page=SimpleNamespace(file=SimpleNamespace(src_uri="contributing.md")),
        config={"repo_url": "https://github.com/example/project"},
        files=generated_files,
    )

    assert "[Architecture](architecture.md)" in output
    assert "https://github.com/example/project/blob/main/tests/test_cli.py" in output

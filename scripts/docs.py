"""Generate the CLI reference and check local Markdown links without network access."""

from __future__ import annotations

import json
import posixpath
import re
import sys
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from goodnotes_cli.cli import app

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "docs" / "commands.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
ROOT_SOURCE_RE = re.compile(
    r"(?P<prefix>\]\()(?P<parent>(?:\.\./)+)?(?P<name>(?:README|CONTRIBUTING|AGENTS|CLAUDE)\.md|"
    r"(?:src|tests|scripts|features|\.github)/[^)#\s]+)(?P<suffix>(?:#[^)]*)?\))"
)
CONTRIBUTING_DOC_RE = re.compile(
    r"(?P<prefix>\]\()docs/(?P<path>[^)#\s]+)(?P<suffix>(?:#[^)]*)?\))"
)
TYPE_LABELS = {
    "boolean": "Flag",
    "float range": "Number",
    "int range": "Integer",
    "path": "Path",
    "str": "Text",
}


def schema(command: str | None = None) -> dict[str, Any]:
    """Read the CLI's own schema through its registered command implementation."""
    args = ["schema"]
    if command:
        args.append(command)
    result = CliRunner().invoke(app, args, prog_name="goodnotes")
    if result.exit_code:
        raise RuntimeError(f"Could not read schema for {command or 'CLI'}: {result.output}")
    return json.loads(result.output)


def _value_name(parameter: dict[str, Any]) -> str:
    return parameter["name"].replace("_", "-").upper()


def _syntax(command_name: str, contract: dict[str, Any]) -> str:
    return re.sub(
        r"\{([^}]+)\}",
        lambda match: match.group(1).replace("_", "-").upper(),
        contract["usage"],
    )


def _default(parameter: dict[str, Any]) -> str:
    if parameter.get("required"):
        return "Required"
    value = parameter.get("default")
    return "Unset" if value is None else f"`{value}`"


def _md_cell(value: str) -> str:
    """Keep parser-provided text on one safe Markdown table row."""
    flattened = " ".join(value.split())
    return flattened.replace("\\", "\\\\").replace("|", "\\|")


def _bounds(parameter: dict[str, Any]) -> str:
    minimum, maximum = parameter.get("min"), parameter.get("max")
    if minimum is None and maximum is None:
        return ""
    if minimum is not None and maximum is not None:
        return f"{minimum} to {maximum}"
    if minimum is not None:
        return f"at least {minimum}"
    return f"at most {maximum}"


def _type_name(parameter: dict[str, Any]) -> str:
    raw_type = parameter.get("type", "value")
    return TYPE_LABELS.get(raw_type, raw_type.replace("_", " ").capitalize())


def _param_rows(parameters: list[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    for parameter in parameters:
        if parameter["name"] == "help":
            continue
        label = " / ".join(f"`{flag}`" for flag in parameter.get("flags", []))
        if not label:
            label = f"`{_value_name(parameter)}`"
        default = _default(parameter)
        envvar = parameter.get("envvar")
        if envvar:
            default += f"; env `{envvar}`"
        details = parameter.get("help", "")
        bounds = _bounds(parameter)
        if bounds:
            details = f"{details} Bounds: {bounds}.".strip()
        values = (label, _type_name(parameter), default, details)
        rows.append("| " + " | ".join(_md_cell(value) for value in values) + " |")
    return rows


def render_reference() -> str:
    overview = schema()
    names = list(overview["commands"])
    contracts = {name: schema(name) for name in names}
    lines = [
        "# Command reference",
        "",
        "Generated from the registered Typer commands and their machine-readable schema. "
        "After changing the CLI, update this page with `uv run --locked python scripts/docs.py`.",
        "",
        "Run the CLI as `uv run goodnotes`. Put global options before the command. "
        "Use `uv run goodnotes COMMAND --help` for terminal help and "
        "`uv run goodnotes schema COMMAND` for the complete machine-readable contract.",
        "",
        "For interpreting fields, warnings, and pagination, see [results and output](output.md) "
        "and the [agent guide](agent-guide.md).",
        "",
        "## Command index",
        "",
        "| Command | Syntax |",
        "| --- | --- |",
    ]
    for name in names:
        lines.append(f"| [`{name}`](#{name}) | `{_syntax(name, contracts[name])}` |")

    lines.extend(["", "## Global options", "", "Accepted before a command:", ""])
    lines.extend(["| Option | Type | Default | Description |", "| --- | --- | --- | --- |"])
    lines.extend(_param_rows(contracts[names[0]]["global_options"]))
    lines.extend(
        [
            "",
            "`--fields` is unset by default, so omitting it uses the command's default response "
            "profile. Supplying it selects keys from the full response contract; consult "
            "`schema COMMAND` for the command-specific fields. Compact JSON is the default "
            "output format; the schema reports the `--json` flag's parser default separately.",
            "",
            "## Commands",
        ]
    )

    for name in names:
        contract = contracts[name]
        lines.extend(["", f"### {name}", "", f"```text\n{_syntax(name, contract)}\n```", ""])
        help_text = contract.get("help", "").strip()
        description, has_example, example_text = help_text.partition("\n\nExample:")
        lines.append(description.strip())
        lines.append("")
        for key, title in (("arguments", "Arguments"), ("options", "Options")):
            params = [item for item in contract[key] if item["name"] != "help"]
            if not params:
                continue
            lines.extend(
                [
                    f"**{title}**",
                    "",
                    "| Name | Type | Default | Description and bounds |",
                    "| --- | --- | --- | --- |",
                ]
            )
            lines.extend(_param_rows(params))
            lines.append("")
        if has_example:
            lines.extend(["Example:", "", f"```sh\n{example_text.strip()}\n```", ""])
        if name not in {"schema"}:
            lines.append(
                f"Output details: [`uv run goodnotes schema {name}`](#schema) reports "
                "the fields and output profile."
            )
            lines.append("")
        if name in {"render", "bundle", "export"}:
            lines.extend(
                [
                    "For temporary cache behavior and persistent output paths, see "
                    "[temporary and saved files](output.md#temporary-and-saved-files).",
                    "",
                ]
            )
    lines.extend(
        [
            "## Artifact retention",
            "",
            "Temporary render and bundle artifacts follow bounded cache retention; "
            "explicit output paths are persistent. See [temporary and saved files]"
            "(output.md#temporary-and-saved-files) for cleanup and output-path details.",
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def markdown_files(root: Path) -> list[Path]:
    """Select root docs, recursive public docs, and feature maps; ignore private trees."""
    files = (
        list(root.glob("*.md"))
        + list((root / "docs").rglob("*.md"))
        + list((root / "features").glob("*.md"))
    )
    return sorted(path for path in files if path.is_file())


def _without_fences(source: str) -> str:
    result: list[str] = []
    fence: tuple[str, int] | None = None
    for line in source.splitlines():
        match = FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = (marker[0], len(marker))
            elif marker[0] == fence[0] and len(marker) >= fence[1]:
                fence = None
            continue
        if fence is None:
            result.append(line)
    return "\n".join(result)


def _slug(heading: str) -> str:
    heading = re.sub(r"[`*_~]", "", heading).lower()
    heading = re.sub(r"[^\w\- ]", "", heading)
    return re.sub(r"\s+", "-", heading.strip())


def _anchors(source: str) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in _without_fences(source).splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if not match:
            continue
        base = _slug(match.group(1))
        count = counts.get(base, 0)
        anchors.add(base if count == 0 else f"{base}-{count}")
        counts[base] = count + 1
    return anchors


def check_links(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    anchors_by_file = {path.resolve(): _anchors(path.read_text()) for path in files}
    for source in files:
        content = _without_fences(source.read_text())
        for raw_target in LINK_RE.findall(content):
            target = raw_target.strip().split()[0].strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                if target.startswith("#") and target[1:] not in anchors_by_file[source.resolve()]:
                    errors.append(f"{source.relative_to(root)}: missing anchor {target}")
                continue
            path_text, separator, anchor = target.partition("#")
            destination = (source.parent / path_text).resolve() if path_text else source.resolve()
            if not destination.is_file():
                errors.append(f"{source.relative_to(root)}: missing file link {target}")
            elif separator and destination.suffix.lower() == ".md":
                target_anchors = anchors_by_file.get(destination)
                if target_anchors is None:
                    target_anchors = _anchors(destination.read_text())
                if anchor not in target_anchors:
                    errors.append(f"{source.relative_to(root)}: missing anchor {target}")
    return errors


def check_nav(root: Path) -> list[str]:
    config = root / "mkdocs.yml"
    if not config.exists():
        return ["mkdocs.yml is missing"]
    config_text = config.read_text()
    nav = re.search(r"(?ms)^nav:\s*\n(?P<body>.*?)(?=^[^\s#][^\n]*:|\Z)", config_text)
    nav_text = nav.group("body") if nav else ""
    nav_paths = set(re.findall(r"(?m)^\s*-\s+(?:[^:\n]+:\s*)?([A-Za-z0-9_./-]+\.md)\s*$", nav_text))
    docs_root = root / "docs"
    required = {
        path.relative_to(docs_root).as_posix() for path in docs_root.rglob("*.md") if path.is_file()
    }
    required.add("contributing.md")
    missing = sorted(required - nav_paths)
    return [f"mkdocs.yml nav is missing docs/{path}" for path in missing]


def on_files(files: Any, config: Any) -> Any:
    """Add root CONTRIBUTING.md to the site as an in-memory virtual docs page."""
    from mkdocs.structure.files import File

    if not any(file.src_uri == "contributing.md" for file in files):
        files.append(File.generated(config, "contributing.md", content=CONTRIBUTING.read_text()))
    return files


def on_page_markdown(markdown: str, page: Any, config: dict[str, Any], files: Any) -> str:
    """Keep docs links local and point unavailable source files to GitHub."""
    repo_url = config.get("repo_url", "https://github.com/Loligplayer33/goodnotes-cli").rstrip("/")
    branch = "main"
    src_uri = getattr(getattr(page, "file", None), "src_uri", None)

    if src_uri == "contributing.md":
        markdown = CONTRIBUTING_DOC_RE.sub(
            lambda match: f"{match.group('prefix')}{match.group('path')}{match.group('suffix')}",
            markdown,
        )

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if src_uri != "contributing.md" and name == "CONTRIBUTING.md":
            parent = posixpath.dirname(src_uri or "") or "."
            target = posixpath.relpath("contributing.md", parent)
            return f"{match.group('prefix')}{target}{match.group('suffix')}"
        return f"{match.group('prefix')}{repo_url}/blob/{branch}/{name}{match.group('suffix')}"

    return ROOT_SOURCE_RE.sub(replace, markdown)


def check(root: Path = ROOT, reference: Path = REFERENCE) -> list[str]:
    errors: list[str] = []
    expected = render_reference()
    if not reference.exists() or reference.read_text() != expected:
        errors.append(
            f"{reference.relative_to(root)} is stale; run uv run --locked python scripts/docs.py"
        )
    files = markdown_files(root)
    errors.extend(check_links(root, files))
    errors.extend(check_nav(root))
    return errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["--check"]:
        errors = check()
        if errors:
            print("\n".join(errors), file=sys.stderr)
            return 1
        print("Generated command reference, Markdown links, and MkDocs nav are current.")
        return 0
    if args:
        print("Usage: python scripts/docs.py [--check]", file=sys.stderr)
        return 2
    REFERENCE.write_text(render_reference())
    print(f"Updated {REFERENCE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

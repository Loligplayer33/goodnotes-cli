"""Render root guides in MkDocs and keep repository source links usable."""

import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ROOT_PAGES = {"index.md": "README.md", "contributing.md": "CONTRIBUTING.md"}


def on_files(files: Any, config: Any) -> Any:
    from mkdocs.structure.files import File

    for target, source in ROOT_PAGES.items():
        files.append(File.generated(config, target, content=(ROOT / source).read_text()))
    return files


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    root_page = page.file.src_uri in ROOT_PAGES
    repo = config["repo_url"].rstrip("/") + "/blob/main/"

    def replace(match: re.Match[str]) -> str:
        target = match[1]
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return match[0]
        if root_page and target.startswith("docs/"):
            return "](" + target[5:] + ")"
        if not root_page and not target.startswith("../"):
            return match[0]
        target = target.removeprefix("../")
        for site_path, source in ROOT_PAGES.items():
            if target == source or target.startswith(source + "#"):
                return "](" + target.replace(source, site_path, 1) + ")"
        return "](" + repo + target + ")"

    return re.sub(r"\]\(([^)]+)\)", replace, markdown)

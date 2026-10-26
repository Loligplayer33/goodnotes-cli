import re
from pathlib import Path

import pytest

import goodnotes_cli
from goodnotes_cli import build_info


def test_build_info_fingerprints_nested_and_vendored_sources_without_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "installed" / "goodnotes_cli"
    (package / "_vendor").mkdir(parents=True)
    (package / "__init__.py").write_text("# generated package\n", encoding="utf-8")
    (package / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    vendored = package / "_vendor" / "decoder.py"
    vendored.write_text("VALUE = 2\n", encoding="utf-8")
    monkeypatch.setattr(goodnotes_cli, "__file__", str(package / "__init__.py"))

    first = build_info()
    assert first == build_info()
    assert first["version"] == goodnotes_cli.__version__
    assert re.fullmatch(r"[0-9a-f]{64}", first["source_sha256"])
    assert str(tmp_path) not in str(first)

    vendored.write_text("VALUE = 3\n", encoding="utf-8")
    assert build_info()["source_sha256"] != first["source_sha256"]

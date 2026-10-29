"""Generated optional-app metadata regressions, without probing installed apps."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.support.paths import PROJECT_ROOT


def test_optional_app_metadata_fifo_returns_unavailable_without_blocking(tmp_path: Path) -> None:
    candidate = tmp_path / "generated-info.plist"
    os.mkfifo(candidate)
    script = """
import json
import sys
from pathlib import Path
from goodnotes_cli.storage.capture import lifecycle
lifecycle._goodnotes_info_plist_candidates = lambda: (Path(sys.argv[1]),)
print(json.dumps(lifecycle._read_installed_goodnotes_info()))
"""

    result = subprocess.run(
        [sys.executable, "-c", script, str(candidate)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "null"
    assert candidate.is_fifo()

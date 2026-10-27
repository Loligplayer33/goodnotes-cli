"""Run generated verification suites and write a compact verification receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verification.runner import run_verification  # noqa: E402


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Goodnotes CLI generated verification suites.")
    parser.add_argument("--cli", help="absolute installed goodnotes executable to verify")
    parser.add_argument(
        "--private-corpus", type=Path, help="explicit outside-repository corpus manifest"
    )
    parser.add_argument("--report", type=Path, help="receipt destination")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = _arguments(argv)
    receipt, code = run_verification(
        cli=options.cli,
        private_corpus=options.private_corpus,
        report=options.report,
    )
    sys.stdout.write(json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

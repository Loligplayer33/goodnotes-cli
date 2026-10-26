"""Read your local Goodnotes library."""

from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

__version__ = version("goodnotes-local-cli")


def _package_source_sha256(package_dir: Path) -> str:
    """Fingerprint packaged Python source without exposing its installation path."""

    hasher = sha256()
    for path in sorted(package_dir.rglob("*.py")):
        relative = path.relative_to(package_dir).as_posix().encode("utf-8")
        hasher.update(len(relative).to_bytes(8, "big"))
        hasher.update(relative)
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


def build_info() -> dict[str, str]:
    """Return the installed package version and content identity."""

    package_dir = Path(__file__).resolve().parent
    return {
        "version": __version__,
        "source_sha256": _package_source_sha256(package_dir),
    }

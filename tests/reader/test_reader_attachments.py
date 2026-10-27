"""Confined attachment resolution for generated files."""

from pathlib import Path

from goodnotes_cli.reader.attachments import AttachmentResolver


def test_prefix_resolution_obeys_direct_precedence_and_symlink_confinement(tmp_path: Path) -> None:
    root = tmp_path / "Attachments"
    root.mkdir()
    (root / "x").mkdir()
    (root / "y").mkdir()
    (root / "x" / "same.pdf").write_bytes(b"x")
    (root / "y" / "same.pdf").write_bytes(b"y")
    resolver = AttachmentResolver(root)
    assert resolver.resolve("Attachments/x/same.pdf") == (root / "x" / "same.pdf", "available")
    nested = root / "Attachments" / "x"
    nested.mkdir(parents=True)
    (nested / "same.pdf").write_bytes(b"nested")
    assert resolver._resolve_uncached("Attachments/x/same.pdf") == (
        nested / "same.pdf",
        "available",
    )
    assert resolver.resolve("Attachments-extra/same.pdf") == (None, "ambiguous")
    (root / "alias.pdf").symlink_to(root / "x" / "same.pdf")
    assert resolver.resolve("Attachments/alias.pdf") == (root / "x" / "same.pdf", "available")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (root / "escape.pdf").symlink_to(outside)
    assert resolver.resolve("Attachments/escape.pdf") == (None, "unsafe")

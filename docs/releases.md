# Releases

`pyproject.toml` is the version source. `uv.lock` records the local project version and must be refreshed when the project version changes. The release workflow checks that a pushed `vX.Y.Z` tag matches the project version and that `CHANGELOG.md` has a matching release heading.

## Prepare a release

1. Choose a version under [Semantic Versioning](https://semver.org/).
2. Update `[project].version` in `pyproject.toml`.
3. Add a dated section near the top of `CHANGELOG.md` and move completed entries out of `[Unreleased]`.
4. Run `uv lock`, then `uv sync --locked --all-groups` and the checks in [Contributing](../CONTRIBUTING.md).
5. Commit the version and changelog changes. The release workflow runs on pushes to `main` as well as on version tags.

## Publish a GitHub Release

After the release commit is on `main`, create and push a matching annotated tag:

```sh
git switch main
git pull --ff-only
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

The tag triggers CI. The workflow checks the tag/version/changelog match, builds a source distribution and wheel, installs each in a temporary environment, and runs `goodnotes schema` from outside the checkout. After quality, documentation, test, and package jobs pass, the workflow creates a GitHub Release with both validated distributions and GitHub-generated release notes. It does not publish to PyPI.

See the current job definitions in [CI](../.github/workflows/ci.yml). Repository branch-protection and review settings are managed outside these files and are not specified here.

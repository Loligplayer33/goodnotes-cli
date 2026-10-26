# Releases

The version comes from `[project].version` in `pyproject.toml`. Refresh `uv.lock` after changing it. A `vX.Y.Z` tag must match the version and have a matching `CHANGELOG.md` heading.

## Prepare

1. Update the project version and dated changelog section.
2. Run `uv lock`, `uv sync --locked --all-groups`, and the checks in [Contributing](../CONTRIBUTING.md).
3. Land the version and changelog update on `main` through a pull request.

## Publish

```sh
git switch main
git pull --ff-only
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

The tag runs CI. After quality, documentation, test, and package jobs pass, CI attaches the validated wheel and source distribution to a GitHub Release with generated notes. The workflow does not publish to PyPI. See [CI](../.github/workflows/ci.yml).

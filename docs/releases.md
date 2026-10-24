# Releases

The version in `[project].version` in `pyproject.toml` is the version source. `uv.lock` also records the local project version, so refresh it after each version change. The release workflow checks that a pushed `vX.Y.Z` tag matches the project version and that `CHANGELOG.md` has a section for that version.

## Prepare a release

1. Choose the next version under [Semantic Versioning](https://semver.org/).
2. Set `[project].version` in `pyproject.toml` to that version.
3. Add a dated section for it near the top of `CHANGELOG.md` and move any completed entries out of `[Unreleased]`.
4. Run `uv lock`, then `uv sync --locked --all-groups` and the local checks in `CONTRIBUTING.md`.
5. Open and merge a pull request with the version and changelog updates. The CI workflow must pass on the pull request and the resulting commit on `main`.

## Publish a GitHub Release

After the release commit reaches `main`, create and push a version tag that matches `pyproject.toml`:

```sh
git switch main
git pull --ff-only
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```

The tag starts the same CI checks. The workflow builds a source distribution and wheel, installs the wheel into a temporary environment, and runs `goodnotes schema` from outside the checkout. Only after those checks pass does it create a GitHub Release with the validated `.tar.gz` and `.whl` files attached. The release uses GitHub's generated release notes. It does not publish to PyPI.

CI uses generated test fixtures and does not require the Goodnotes app or a real Goodnotes library.

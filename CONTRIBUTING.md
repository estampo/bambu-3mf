# Contributing to bambox

## Development setup

```bash
git clone https://github.com/estampo/bambox.git
cd bambox
uv sync --extra dev
```

## Before submitting a PR

Run all four checks locally:

```bash
uv run ruff check src tests        # lint
uv run ruff format --check src tests  # formatting
uv run mypy src/bambox          # type check
uv run pytest                      # tests
```

Auto-fix formatting: `uv run ruff format src tests`

## Changelog fragments

Every PR must include a towncrier fragment file in `changes/`:

```bash
# Format: changes/<PR-number>.<type>
# Types: feature, bugfix, misc
echo "Add support for A1 Mini base profile" > changes/42.feature
```

If you don't have a PR number yet, use an orphan fragment:

```bash
echo "Fix array padding for single-filament prints" > changes/+fix-padding.bugfix
```

Do **not** edit `CHANGELOG.md` directly.

## Cutting a release

**Always use the `prepare-release` workflow** — never push a version bump directly to main.

1. Go to **Actions → Prepare Release → Run workflow** and enter the version (e.g. `0.3.0`).
2. The workflow bumps `pyproject.toml`, builds the changelog from towncrier fragments, and opens a `release/v0.3.0` PR.
3. Review the PR, then merge it.
4. Merging triggers the release pipeline automatically:
   - Builds the package and validates on TestPyPI
   - Creates the `v0.3.0` git tag
   - Creates the GitHub Release with changelog notes
   - Publishes to PyPI (last — only after everything else succeeds)

If the pipeline fails mid-way, fix the issue and re-run via **Actions → Release → Run workflow** with `tag: v0.3.0`. The steps are idempotent — existing tags and GitHub Releases are updated rather than recreated.

## Code style

- Line length: 100 characters (ruff)
- Type hints on all public functions
- `from __future__ import annotations` in every module
- No unnecessary comments or docstrings on private helpers

## Module boundaries

See `CLAUDE.md` for the module ownership table. Each module has a defined scope — don't add logic to the wrong module.

## Tests

- Tests live in `tests/` and mirror source module names
- Use `tmp_path` fixture for file I/O tests
- Reference fixtures (`tests/fixtures/`) are Bambu Connect-validated archives

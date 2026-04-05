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

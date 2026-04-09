# bambox - Claude Code Instructions

## Working with the maintainer
The project maintainer is technically experienced and understands the codebase deeply. Trust their judgement. Don't second-guess their observations or explain things they already know.

## Pre-PR Checklist (MANDATORY)
Before pushing any PR branch, always run locally:
1. `uv run ruff check src tests` — lint must pass with zero errors
2. `uv run ruff format --check src tests` — formatting must pass (run `uv run ruff format src tests` to auto-fix)
3. `uv run mypy src/bambox` — type check must pass with zero errors
4. `uv run pytest` — all tests must pass

Do NOT push a PR until all four checks pass locally.

## Cutting a Release (MANDATORY process)

**Never push a version bump directly to main.** The release pipeline detects releases by looking for a merged `release/vX.Y.Z` PR — bypassing this means nothing gets published.

1. Trigger **Actions → Prepare Release** with the target version.
2. Review and merge the generated `release/vX.Y.Z` PR.
3. The pipeline runs automatically: build → TestPyPI gate → tag → GitHub Release → PyPI.

If the pipeline needs to be re-run manually, use **Actions → Release → Run workflow** with `tag: vX.Y.Z`. All steps are idempotent.

## Changelog (MANDATORY)
Every PR must include a **towncrier fragment file** in the `changes/` directory:
1. Create a file: `changes/<PR-number>.<type>` where type is `feature`, `bugfix`, or `misc`
2. Write a single line — concise, user-facing description of the change
3. If the PR has no number yet, use `+descriptive-name.<type>` (orphan fragment)
4. Do NOT edit CHANGELOG.md directly — towncrier compiles fragments at release time

## Module Ownership (enforce strictly)

Each module has a defined scope. Do not add logic to the wrong module — even if it seems convenient.

| Module | Owns | Must NOT contain |
|--------|------|-----------------|
| `pack.py` | Core .gcode.3mf archive construction, XML metadata, MD5 checksums, Bambu Connect fixup | Settings generation, slicer logic, printer communication |
| `settings.py` | 544-key project_settings builder, profile loading, filament overlay, array broadcasting | G-code generation, archive packing, printer logic |
| `bridge.py` | Cloud printing via Docker bridge, credential loading, AMS tray mapping, printer status | Archive construction, settings generation, slicer invocation |
| `cli.py` | Typer commands (pack, print, status), argument parsing, user-facing output | Business logic — delegate to pack/bridge/settings |
| `cura.py` | CuraEngine Docker invocation, profile conversion, start/end G-code injection | OrcaSlicer logic, archive packing, printer communication |
| `templates.py` | OrcaSlicer→Jinja2 syntax conversion, template rendering | G-code generation, settings logic |
| `toolpath.py` | Synthetic toolpath generation for testing | Production G-code, slicer invocation |
| `thumbnail.py` | G-code→PNG rendering (top-down view, bounding box) | Archive packing, settings |
| `assemble.py` | G-code component assembly (start + toolpath + end) | Slicer invocation, template rendering |

## Architecture: Key Decisions

### Template-Driven Settings (544 keys)
Bambu printers require a `project_settings.config` with ~544 keys in the .gcode.3mf archive. Rather than passing slicer output through, bambox builds this from:
1. Machine base profile (e.g. `base_p1s.json` — 544 keys)
2. Filament type profiles (`filament_pla.json`, etc. — per-type overrides)
3. `_varying_keys.json` — keys that differ per filament slot
4. `_uniform_array_keys.json` — scalars that must be broadcast to arrays

Do NOT modify `fixup_project_settings()` without understanding the array padding and key fixup logic — it ensures Bambu Connect firmware acceptance.

### Docker Bridge Pattern
Cloud printing wraps `estampo/cloud-bridge:bambu-*` Docker image. Two modes:
1. **Bind-mount** (primary) — mounts .gcode.3mf via `-v`
2. **Baked fallback** — for sandboxed/DinD environments, builds temp image with COPY

### Bambu Connect Compatibility
The archive format is validated by printer firmware. Key constraints:
- MD5 checksums must match file contents
- Per-filament arrays must be padded to exactly 5 slots (P1S)
- Both OrcaSlicer 2.3.1 and BambuStudio 2.5.0.66 format versions supported

## What bambox is NOT

- **Not a slicer.** It packages G-code produced by slicers. The CuraEngine integration in `cura.py` invokes an external engine — it does not implement slicing.
- **Not a printer API client.** It wraps the Docker bridge for cloud printing. The actual protocol implementation lives in the bridge binary (C++ today, Rust planned).
- **Not estampo.** estampo is the pipeline orchestrator. bambox is the Bambu Lab packaging library that estampo depends on. Do not add pipeline, DAG, or orchestration logic here.
- **Not a profile editor.** It loads and overlays profiles. Do not build profile editing or merging UI.

## Relationship to estampo

bambox is one piece of a three-project architecture:
- **estampo** — pipeline orchestrator, slicer-agnostic
- **bambox** — BBL .gcode.3mf packaging + G-code templates + settings generation
- **bambu-cloud** (planned) — printer communication

Per estampo ADR-005, bambox will absorb printer code from estampo at v0.4.0. See `docs/bridge-migration-plan.md` for the Rust bridge migration plan.

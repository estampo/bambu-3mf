# bambox Roadmap

This is a living document updated at each release. It captures what has shipped, what is in scope for the next milestone, and what is deliberately deferred.

## Vision

bambox is the **BBL packaging layer** — the last mile between a slicer and a Bambu Lab printer archive. It is a modern CLI that:

1. **Packs** properly formed G-code into `.gcode.3mf` archives that BBL firmware accepts
2. **Fixes up** G-code and 3MF metadata so prints work correctly on BBL printers
3. **Validates** that the result is firmware-compliant before writing

bambox handles two layers of BBL-specific fixup:

**G-code level:** header block injection (M73, M991, HEADER_BLOCK), layer marker translation, tool-change rewriting (T→M620/M621 for AMS).

**3MF archive level:** 544-key `project_settings.config` generation, array padding to firmware-expected slot count, OrcaSlicer/BambuStudio XML metadata, MD5 checksums, thumbnail generation.

bambox is **not** a slicer, not a pipeline orchestrator (that's estampo), not a printer communication tool (that's [boo-cloud](https://github.com/estampo/boo-cloud)), and not a general-purpose G-code transformer. It specifically owns "make this packagable for a BBL printer."

## Tested hardware

Everything in bambox is developed and validated against a **Bambu P1S with AMS** — that's the only hardware the maintainers have in the loop. The architecture is general and additional printer models are on the roadmap below, but they're gated on external contributors with hardware access — see the "Future" section.

---

## Shipped

### v0.4.7 — 2026-04-24

Strip `;MESH:` comments from CuraEngine G-code output before packing.

### v0.4.6 and earlier — 2026-04-11 to 2026-04-23

Cloud printing, bridge daemon, and printer credentials extracted to
[boo-cloud](https://github.com/estampo/boo-cloud). See that project's
CHANGELOG for the history of those features.

### v0.2.x — 2026-04-09

**Theme: wire up the pack pipeline; harden validation.**

- Full `bambox pack` CLI wired through `templates.py` + `settings.py` (544-key auto-generation)
- `bambox repack` command for fixing up existing OrcaSlicer archives
- `bambox validate` command (11 error rules, 9 warning rules, JSON output, `--reference` comparison)
- CuraEngine `T0`/`T1` rewrite to Bambu `M620`/`M621` for multi-filament prints
- AMS slot assignment in `bambox pack -f` (`-f 3:PETG-CF`)
- Z-change layer detection fallback for unknown slicers (layer progress on any G-code source)

### v0.1.0 — 2026-03-15

Initial release. Core `.gcode.3mf` archive packaging with Bambu Connect compatibility, 544-key `project_settings.config` generation, P1S base profile with filament overlays.

---

## Current focus

- Truth-in-advertising pass on README and ROADMAP (this document)
- End-to-end quickstart walkthrough with a checked-in sample G-code

See the open issues for the live list.

---

## Future

Not scheduled. Picked up when the right constraint lifts — either external demand or contributor access to hardware we don't have.

### Multi-printer support (contributor-gated)

The architecture is general: machine profiles in `src/bambox/data/profiles/` drive everything, and adding an X1C / A1 / A1 Mini is mechanically a matter of dropping in a new base profile and validating against real hardware. The blocker is **validation** — we only have a P1S+AMS in the loop, so anything we shipped for other models would be guessing.

If you have an X1C, A1, or A1 Mini and are willing to help test, open an issue or PR.

### Multi-filament AMS tool-change rewriting for complex prints

Basic `T0`/`T1` → `M620`/`M621` rewriting shipped in v0.2.0. More elaborate multi-material scenarios (purge tower tuning, flush-volume matrix integration, per-object filament overrides) are deferred until real multi-material users show up.

### Public API freeze (v1.0)

`pack_gcode_3mf()`, `build_project_settings()`, `fixup_project_settings()` are the natural candidates for a stable contract. Not worth freezing until the surrounding modules settle.

### Profile editing / merging UI

Out of scope. bambox loads and overlays profiles; it does not edit them.

### Moonraker / non-Bambu printer support

Out of scope. bambox is the **BBL packaging layer**.

---

## Architecture north star

Three projects, each owning one concern:

```
estampo     → pipeline orchestrator, slicer-agnostic
bambox      → BBL .gcode.3mf packaging + settings generation
boo-cloud   → Bambu cloud printing + credentials + bridge daemon
```

Every feature decision should move toward this split, not away from it.

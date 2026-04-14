# bambox Roadmap

This is a living document updated at each release. It captures what has shipped, what is in scope for the next milestone, and what is deliberately deferred.

## Vision

bambox is the **BBL compatibility layer** — the last mile between a slicer and a Bambu Lab printer. It is a modern CLI that:

1. **Packs** properly formed G-code into `.gcode.3mf` archives that BBL firmware accepts
2. **Fixes up** G-code and 3MF metadata so prints work correctly on BBL printers
3. **Validates** that the result is firmware-compliant before writing
4. **Communicates** with BBL printers for status, AMS queries, and print control (via cloud; LAN mode is deferred)
5. **Manages** printer credentials

bambox handles two layers of BBL-specific fixup:

**G-code level:** header block injection (M73, M991, HEADER_BLOCK), layer marker translation, tool-change rewriting (T→M620/M621 for AMS).

**3MF archive level:** 544-key `project_settings.config` generation, array padding to firmware-expected slot count, OrcaSlicer/BambuStudio XML metadata, MD5 checksums, thumbnail generation.

bambox is **not** a slicer, not a pipeline orchestrator (that's estampo), and not a general-purpose G-code transformer. It specifically owns "make this printable on a BBL printer."

## Tested hardware

Everything in bambox is developed and validated against a **Bambu P1S with AMS** — that's the only hardware the maintainers have in the loop. The architecture is general and additional printer models are on the roadmap below, but they're gated on external contributors with hardware access — see the "Future" section.

---

## Shipped

### v0.4.1 — 2026-04-13

**Theme: daemon HTTP API, Docker on macOS, signed-app gate investigation.**

- `bambox daemon` subcommands (`start`, `stop`, `restart`, `status`); `--foreground` flag
- CLI routes `print` / `cancel` through daemon HTTP when running, with subprocess fallback
- Bridge version check on daemon connect (`/health` reports bridge, API, and plugin versions)
- macOS bridge daemon can start via Docker when no local binary is available
- Credential env var aligned: Rust bridge uses `BAMBOX_CREDENTIALS`; Python checks macOS `~/Library/Application Support/` paths
- Cloud print fix for `BAMBU_NETWORK_SIGNED_ERROR (-26)`: X-BBL headers updated to BambuStudio 02.05.00.66, stable UUID Device-ID, platform-aware OS-Type
- macOS forced to Docker-only bridge — the `.dylib` signing gate blocks `start_print` from unsigned hosts
- `libbambu_networking` signed-app gate documented (`docs/signed-app-gate.md`); CLI `cancel` subcommand disabled pending a viable workaround — the daemon `/cancel` endpoint is unchanged
- Various daemon polish: pre-subscribe configured printers at startup, 1s status updates when idle
- Bridge FFI hardening: `send_message` signature fix, symbol resolution diagnostics, MQTT cancel QoS/param fix

### v0.4.0 — 2026-04-11

**Theme: CLI parity with the legacy C++ bridge; daemon-backed `status -w`.**

- Rust bridge `print` and `cancel` CLI subcommands — CLI parity with the legacy C++ bridge
- `bambox status -w` auto-starts the Rust daemon and polls via HTTP; sub-second updates from MQTT push
- Credentials file created atomic-0o600 via `os.open()` / `mkstemp` (no brief world-readable window)
- Rust `bambox-bridge` documented as the replacement for the legacy C++ `estampo/cloud-bridge` (ADR-002)
- Docker image shrunk from 174MB to 60MB (distroless, multi-stage fetch)

### v0.3.x — 2026-04-10/11

**Theme: CLI TUI parity with estampo; Typer + Rich migration.**

- `bambox status` display parity with estampo TUI (rounded temperatures, color swatches, print stages, active tray)
- CLI migrated from argparse to Typer + Rich; shell completion support
- `bambox cancel` subcommand (later hobbled by the signed-app gate)
- Bridge binary prefers `~/.config/bambox/` for credentials (legacy `~/.config/estampo/` still supported)
- Release pipeline fix for pre-release tag false positives (e.g. `v0.3.0rc1` no longer blocks `v0.3.0`)
- Install script included in GitHub release assets

### v0.2.x — 2026-04-09

**Theme: wire up the pack pipeline; harden the Rust bridge; add `validate`.**

- Full `bambox pack` CLI wired through `templates.py` + `settings.py` (544-key auto-generation)
- `bambox repack` command for fixing up existing OrcaSlicer archives
- `bambox login` for Bambu Cloud authentication and printer configuration
- `bambox validate` command (11 error rules, 9 warning rules, JSON output, `--reference` comparison)
- CuraEngine `T0`/`T1` rewrite to Bambu `M620`/`M621` for multi-filament prints
- AMS slot assignment in `bambox pack -f` (`-f 3:PETG-CF`)
- Z-change layer detection fallback for unknown slicers (layer progress on any G-code source)
- Bridge runner: local `bambox-bridge` binary tried before Docker
- Auto-detect printers when no device ID given; `/printers` daemon endpoint
- Rust bridge hardening: agent command channel, `CString` panic removal, callback lifetime fixes
- Multi-stage Dockerfile for the Rust bridge daemon
- CI workflow for cross-compiled bridge binaries (Linux, macOS)

### v0.1.0 — 2026-03-15

Initial release. Core `.gcode.3mf` archive packaging with Bambu Connect compatibility, 544-key `project_settings.config` generation, P1S base profile with filament overlays, Docker bridge cloud printing, CLI `pack` / `print` / `status`.

---

## Current focus

No fixed theme yet. Near-term work is driven by soft-launch readiness:

- Truth-in-advertising pass on README and ROADMAP (this document)
- End-to-end quickstart walkthrough with a checked-in sample G-code
- Release workflow detection fix (v0.4.1 shipped in code but the release pipeline silently no-op'd; fixed in #198, verification pending the next release cycle)

See the open issues for the live list.

---

## Future

Not scheduled. Picked up when the right constraint lifts — either external demand or contributor access to hardware we don't have.

### Multi-printer support (contributor-gated)

The architecture is general: machine profiles in `src/bambox/data/profiles/` drive everything, and adding an X1C / A1 / A1 Mini is mechanically a matter of dropping in a new base profile and validating against real hardware. The blocker is **validation** — we only have a P1S+AMS in the loop, so anything we shipped for other models would be guessing.

If you have an X1C, A1, or A1 Mini and are willing to help test, open an issue or PR — we'll work through the profile with you. Until then, these stay in the future bucket rather than on a scheduled milestone.

### LAN printing

Direct IP + access code, no cloud dependency. Tracked as #91. Interesting both for privacy-minded users and as a possible workaround for the signed-app gate on print-class MQTT commands (LAN-mode paths may not go through the same signing check — unverified).

### Multi-filament AMS tool-change rewriting for complex prints

Basic `T0`/`T1` → `M620`/`M621` rewriting shipped in v0.2.0. More elaborate multi-material scenarios (purge tower tuning, flush-volume matrix integration, per-object filament overrides) are deferred until real multi-material users show up.

### Public API freeze (v1.0)

`pack_gcode_3mf()`, `build_project_settings()`, `fixup_project_settings()` are the natural candidates for a stable contract. Not worth freezing until the surrounding modules settle.

### Moonraker / non-Bambu printer support

Out of scope. bambox is the **BBL compatibility layer**. General-purpose printer support lives in estampo.

### Profile editing / merging UI

Out of scope. bambox loads and overlays profiles; it does not edit them.

---

## Architecture north star

Two projects, each owning one concern:

```
estampo          → pipeline orchestrator, slicer-agnostic
bambox           → BBL compatibility layer (Python lib + Rust bridge daemon)
```

Every feature decision should move toward this split, not away from it.

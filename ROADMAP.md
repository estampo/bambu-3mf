# bambox Roadmap

This is a living document updated at each release. It captures what's done, what's in scope for the next milestone, and what's explicitly deferred.

## Vision

bambox is the **BBL compatibility layer** — the last mile between a slicer and a Bambu Lab printer. It is a modern CLI that:

1. **Packs** properly formed G-code into `.gcode.3mf` archives that BBL firmware accepts
2. **Fixes up** G-code and 3MF metadata so prints work correctly on BBL printers
3. **Validates** that the result is firmware-compliant before writing
4. **Communicates** with BBL printers for status, AMS queries, and print control (LAN + cloud)
5. **Manages** printer credentials

bambox handles two layers of BBL-specific fixup:

**G-code level:** header block injection (M73, M991, HEADER_BLOCK), layer marker translation, tool-change rewriting (T→M620/M621 for AMS).

**3MF archive level:** 544-key `project_settings.config` generation, array padding to firmware-expected slot count, OrcaSlicer/BambuStudio XML metadata, MD5 checksums, thumbnail generation.

bambox is **not** a slicer, not a pipeline orchestrator (that's estampo), and not a general-purpose G-code transformer. It specifically owns "make this printable on a BBL printer."

---

## v0.1.x — Done

Core packaging library with Bambu Connect compatibility.

- Core `.gcode.3mf` archive construction with MD5 checksums
- Template-driven 544-key `project_settings.config` generation
- Machine base profiles (P1S) with filament overlays (PLA, ASA, PETG-CF)
- Automatic array padding and missing-key fixup for Bambu Connect firmware
- Cloud printing via Docker bridge with bind-mount and baked fallback
- AMS tray mapping and printer status querying
- OrcaSlicer-to-Jinja2 template conversion and rendering
- G-code component assembly (start + toolpath + end)
- CuraEngine Docker slicer backend prototype
- CLI with `pack`, `print`, and `status` commands
- G-code-to-PNG thumbnail rendering

---

## v0.2.0 — In Progress

**Theme: Wire up the pack pipeline and harden the Rust bridge.**

### Pack pipeline

Wire up the full `bambox pack` flow: G-code in → BBL-compatible `.gcode.3mf` out.

- [ ] Wire templates.py and settings.py into the CLI `pack` command (#27)
- [ ] Update CLAUDE.md module ownership and "What bambox is NOT" to reflect BBL compatibility layer role (#27)
- [ ] Z-change layer detection fallback for unknown slicers (#30)
- [ ] Evaluate whether assemble.py is still needed or if start/end injection replaces it (#27)

### Rust bridge hardening (#28)

- [ ] Replace `CString::new().unwrap()` with error propagation (~20 call sites)
- [ ] Move agent to dedicated thread with command channel (unblock HTTP during MQTT queries)
- [ ] Wire up print cancellation (`WasCancelledFn` → `AtomicBool`)
- [ ] Replace `static mut SAVED_STDOUT` with `AtomicI32`

### Infrastructure

- [ ] Dockerfile for building and running the Rust bridge (#29)
- [ ] Towncrier fragment check in CI (#19)

### Done (Rust bridge prototype)
- [x] C++ shim wrapping `libbambu_networking.so` functions via dlopen
- [x] `build.rs` compiling shim as C++17 and linking `libdl`
- [x] `BambuAgent` struct managing agent lifecycle with Drop cleanup
- [x] Thread-safe callback state (atomics + Mutex)
- [x] `status` subcommand: connect, query, print JSON, exit
- [x] `watch` subcommand: stdin-driven MQTT message streaming
- [x] `daemon` subcommand: axum HTTP server
- [x] Credential loading from `~/.config/estampo/credentials.toml` and JSON
- [x] HTTP endpoints: `/ping`, `/health`, `/status/{device}`, `/ams/{device}`, `/print`, `/cancel/{device}`, `/shutdown`
- [x] 3MF upload via multipart POST (eliminates bind-mount issues)
- [x] Cached printer state with 30s TTL
- [x] Full print pipeline: AMS mapping, color patching, config 3MF stripping
- [x] Retry logic for `-3140` enc flag failures (15s backoff, 5 retries)
- [x] Unit tests for credential parsing, callbacks, 3MF processing, HTTP handlers

### Out of scope for v0.2.0
- Multi-filament AMS tool-change rewriting
- Migrating code from estampo
- LAN printing mode

---

## v0.3.0 — Planned

**Theme: Printer communication. Absorb printer code from estampo.**

Coordinates with estampo v0.4.0 — bambox becomes the standalone BBL library.

- Migrate `cloud/bridge.py` from estampo (rewrite as HTTP client to Rust daemon)
- Migrate `cloud/ams.py`, `auth.py`, `credentials.py`, `printer.py` from estampo
- Migrate `thumbnails.py`, `bambu_connect_fixup()` from estampo
- Migrate associated tests
- `bambox credentials` CLI for managing cloud/LAN credentials
- LAN printing mode (direct IP + access code, no cloud dependency)
- WebSocket `/watch/{device}` endpoint for real-time status
- Send print completion (100%) command to printer
- estampo drops printer code, adds optional `bambox` dependency

---

## v0.4.0 — Planned

**Theme: Multi-filament support.**

- T-command → M620/M621 AMS tool-change rewriting for multi-material prints
- `bambox validate` command — check G-code + config against firmware constraints before packing
- Validation: tool count vs AMS slot count, required markers present, settings completeness
- Support for additional Bambu printer models (X1C, A1, A1 Mini)

---

## v1.0 — Sketch

**Theme: Stable public API.**

- Public API freeze for `pack_gcode_3mf()`, `build_project_settings()`, `fixup_project_settings()`
- Comprehensive API documentation
- Release automation and Docker image publishing

---

## Deferred / Backlog

- Moonraker (non-Bambu) printer support
- Profile editing or merging UI

---

## Architecture North Star

Two projects, each owning one concern:

```
estampo          → pipeline orchestrator, slicer-agnostic
bambox           → BBL compatibility layer (Python lib + Rust bridge daemon)
```

Every feature decision should move toward this split, not away from it.

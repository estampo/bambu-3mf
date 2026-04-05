# bambox Roadmap

This is a living document updated at each release. It captures what's done, what's in scope for the next milestone, and what's explicitly deferred.

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

## v0.2.0 — Planned

**Theme: Rust FFI bridge daemon replacing the C++ binary.**

See `docs/bridge-migration-plan.md` for full design.

### Phase 1: Rust CLI (status + watch)
- [ ] C++ shim wrapping ~15 `libbambu_networking.so` functions
- [ ] `build.rs` compiling shim and linking `libdl`
- [ ] `BambuAgent` struct managing agent lifecycle
- [ ] `status` subcommand: connect, query, print JSON, exit
- [ ] `watch` subcommand: stream MQTT messages to stdout
- [ ] Credential loading from `~/.config/estampo/credentials.toml`

### Phase 2: HTTP API
- [ ] Axum HTTP server on `127.0.0.1:8765`
- [ ] Endpoints: `/health`, `/status/{device}`, `/ams/{device}`, `/print`, `/cancel/{device}`, `/watch/{device}` (WebSocket)
- [ ] 3MF upload via HTTP POST (eliminates bind-mount issues)
- [ ] Persistent MQTT connection with cached printer state

### Out of scope for v0.2.0
- Migrating code from estampo (that's v0.3.0)
- LAN printing rewrite in Rust
- Moonraker support decision

---

## v0.3.0 — Planned

**Theme: Absorb printer code from estampo. Phase 1 of the split.**

Per estampo ADR-005, bambox becomes the standalone Bambu packaging + communication library.

- Migrate `cloud/bridge.py` from estampo (rewrite as HTTP client to Rust daemon)
- Migrate `cloud/ams.py`, `auth.py`, `credentials.py`, `printer.py` from estampo
- Migrate associated tests
- bambox publishes release with new modules
- estampo drops printer code, adds optional `bambox` dependency
- Docker image for Rust bridge daemon (`estampo/bambu-bridge:latest`)

---

## v1.0 — Sketch

**Theme: Stable public API.**

- Public API freeze for `pack_gcode_3mf()`, `build_project_settings()`, `fixup_project_settings()`
- Comprehensive API documentation
- Support for additional Bambu printer models beyond P1S

---

## Deferred / Backlog

- Moonraker (non-Bambu) printer support
- LAN printing rewrite in Rust
- Multi-extruder CuraEngine output packaging
- Profile editing or merging UI
- Additional machine base profiles (X1C, A1, A1 Mini)

---

## Architecture North Star

Three independent projects, each owning one concern:

```
estampo          -> pipeline orchestrator, slicer-agnostic
bambox        -> BBL packaging + G-code templates + printer communication
bambu-cloud      -> (may merge into bambox as the Rust bridge)
```

Every feature decision should move toward this split, not away from it.

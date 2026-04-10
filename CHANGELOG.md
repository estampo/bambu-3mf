# Changelog

All notable changes to bambox are documented here.
This changelog is managed by [towncrier](https://towncrier.readthedocs.io/).

<!-- towncrier release notes start -->

## 0.3.0rc1 — 2026-04-10

### Features

- Add --watch mode, --interval flag, state color mapping, and progress bar to ``bambox status``.
- Add ``bambox --version`` / ``bambox -V`` flag that reports the installed package version.
- Add validation checks E013-E014 (multi-filament tool changes), W012-W014 (temperature/matrix ranges), --reference comparison mode (C001-C005), and release-readiness tests.
- Show AMS filament slot mapping in `bambox print` output so users can verify tray assignments before printing.
- Wire WasCancelledFn through the Rust bridge so in-flight uploads can be cancelled via the /cancel endpoint

### Bugfixes

- Fix "Compatible Printer" showing repeated X1 Carbon instead of P1S in Bambu Connect by moving machine-level list keys out of per-filament varying keys.
- Fix CuraEngine bed type mapping (textured_pei_plate → Textured PEI Plate) and sync M73 remaining-time markers with purge-compensated prediction in slice_info.
- Fix release workflow race condition: detect version from merge commit message instead of relying on GitHub API that may not be indexed yet.
- Pass --user uid:gid to Docker bridge containers and pull image before each run
- Populate per-filament usage (used_m/used_g) in slice_info by tracking extrusion per extruder from G-code E positions.

### Misc

- Improve bridge.py test coverage for credentials, AMS parsing, 3MF stripping, and cloud print. ([#80](https://github.com/estampo/bambox/pull/80))
- Add THIRD-PARTY-NOTICES file with license attribution for bundled CuraEngine definitions and OrcaSlicer/BambuStudio-derived profiles. ([#109](https://github.com/estampo/bambox/pull/109))
- Harden install.sh with partial-execution protection, temp file cleanup, sudo handling, and dependency checks
- Support pre-release versions (rc, alpha, beta) in release workflows


## 0.2.2 — 2026-04-09

### Features

- Add ``bambox validate`` command to check .gcode.3mf archives for safety and firmware compatibility (11 error rules, 9 warning rules) with human-readable and JSON output

### Bugfixes

- Add extract_slice_stats test coverage and clarify ;TIME: vs TIME_ELAPSED preference.
- Add extruders_share_nozzle and extruders_share_heater to CuraEngine P1S definition so tool changes generate proper purge volumes for single-nozzle AMS.
- Fix AMS slot matching: populate tray_info_idx from filament profiles instead of defaulting all slots to generic PLA (GFL99).


## 0.2.1 — 2026-04-09

### Features

- Add e2e comparison test: CuraEngine + bambox pack vs BambuStudio reference (``pytest -m e2e``)

### Bugfixes

- Fix CuraEngine P1S packaging: feedrate conversion (#96), multi-filament detection (#97), printer_model_id (#98), slice statistics (#99), template array-as-scalar (#100)

### Misc

- Expand e2e CuraEngine vs BambuStudio tests to validate gcode safety, multi-filament metadata, and print statistics
- Fix release pipeline: reorder so PyPI publishes last (after GitHub Release), make github-release idempotent on retry, update prepare-release PR description.


## 0.2.0 — 2026-04-09

### Features

- Add CI workflow to build and publish bridge Docker image ([#17](https://github.com/estampo/bambox/pull/17))
- Add multi-stage Dockerfile for the Rust bridge daemon. ([#23](https://github.com/estampo/bambox/pull/23))
- Add Z-change layer detection fallback for unknown slicers, enabling layer progress on Bambu printers with any G-code source. ([#30](https://github.com/estampo/bambox/pull/30))
- Bridge runner tries local ``bambox-bridge`` binary before falling back to Docker. ([#37](https://github.com/estampo/bambox/pull/37))
- Default credentials to ~/.config/estampo/credentials.toml, add --credentials flag ([#41](https://github.com/estampo/bambox/pull/41))
- Auto-detect printers when no device ID given, add /printers daemon endpoint ([#42](https://github.com/estampo/bambox/pull/42))
- Rewrite CuraEngine ``T0``/``T1`` tool change commands to Bambu M620/M621 sequences for multi-filament prints. ([#52](https://github.com/estampo/bambox/pull/52))
- Support explicit AMS slot assignment in ``bambox pack -f`` flag (e.g. ``-f 3:PETG-CF``). Unslotted filaments fill remaining slots sequentially. ([#58](https://github.com/estampo/bambox/pull/58))
- Add ``bambox login`` command for Bambu Cloud authentication and printer configuration, with credential storage at ``~/.config/bambox/credentials.toml`` (estampo fallback supported). ([#89](https://github.com/estampo/bambox/pull/89))
- Add API documentation site via pdoc, deployed to GitHub Pages.
- Add CuraEngine P1S AMS printer definition with BAMBOX header contract
- Add `bambox repack` command to fix up existing OrcaSlicer .gcode.3mf archives for Bambu Connect
- Bridge: replace agent mutex with command channel to unblock HTTP handlers during long operations.
- Wire BAMBOX header parsing into `bambox pack` for auto-configuration from CuraEngine output
- Wire settings.py into pack CLI: auto-generate 544-key project_settings from machine and filament profiles

### Bugfixes

- Fix platform detection in auto-fetch: send correct X-BBL-OS-Type header to Bambu API ([#43](https://github.com/estampo/bambox/pull/43))
- Add M73 P100 R0 to end G-code so the printer transitions to 100% complete instead of staying stuck at 99%. ([#56](https://github.com/estampo/bambox/pull/56))
- Add missing ``roofing_layer_count`` and ``flooring_layer_count`` overrides to ``bambox_p1s_ams`` CuraEngine definition, fixing CuraEngine 5.12+ slicing errors. ([#57](https://github.com/estampo/bambox/pull/57))
- Wire ``BAMBOX_FILAMENT_SLOT`` headers into ``bambox pack`` auto-configuration so CuraEngine extruder slot assignments are respected. ([#59](https://github.com/estampo/bambox/pull/59))
- Log a warning when ``parse_bambox_headers`` hits the 200-line limit without a ``; BAMBOX_END`` terminator instead of silently truncating. ([#69](https://github.com/estampo/bambox/pull/69))
- Log a warning when ``flush_volumes_matrix`` is missing, malformed, or contains non-numeric values instead of silently falling back to the 280 mm³ default. ([#70](https://github.com/estampo/bambox/pull/70))
- AMS mapping and color patching now handle XML namespaces in slice_info.config.
- AMS mapping raises an error when filaments have no matching tray instead of silently using external spool.
- BAMBOX header values (BED_TEMP, NOZZLE_TEMP, FILAMENT_TYPE) now correctly override project settings and CLI flags.
- Bridge: replace CString panics with proper error propagation for user-supplied strings.
- Escape user-controlled strings in slice_info.config XML to prevent corrupt archives.
- Fix Hatchling config so built wheels include the bambox package.
- Fix credentials discovery on macOS to check ~/.config/ before ~/Library/Application Support/
- Fix macOS CI: use macos-15 runner, link libc++ instead of libstdc++
- Reject duplicate explicit filament slot assignments with a clear error instead of silently overwriting the earlier assignment.
- Use ``XDG_CACHE_HOME`` with ``tempfile.gettempdir()`` fallback for cloud token files instead of hardcoded ``~/.cache/bambox``, fixing ``PermissionError`` in sandboxed environments.
- ``strip_bambox_header`` now only removes the leading header block instead of stripping ``; BAMBOX_*`` comments from the entire file.
- fixup_project_settings() no longer mutates caller-owned dicts or shared defaults.

### Misc

- Add CI, coverage, PyPI, and Python version badges to README ([#15](https://github.com/estampo/bambox/pull/15))
- Add 10 dedicated tests for assemble.py (component ordering, empty inputs).
- Add 12 tests for bridge.py Docker invocation paths (bind-mount, baked fallback, error handling).
- Add 20 dedicated tests for thumbnail.py (PNG rendering, bounding box, edge cases).
- Add CI workflow for cross-compiling bridge binaries (Linux, macOS) and install script
- Add Rust bridge test coverage for HTTP endpoints, handle, and callbacks.
- Add ``pythonpath = ["src"]`` to pytest config so tests work without editable install.
- Add tests for toolpath module.
- Align ruff config with estampo (target-version, lint rules, import sorting)
- Bridge: fix static-mut UB, cstr_to_str lifetime, document single-agent constraint.
- Fetch libbambu_networking.so at build time, matching estampo cloud-bridge pattern
- Improve CLI test coverage.
- Improve test coverage for settings, templates, gcode_compat, and pack modules.
- Remove dead cura.py module and Docker build — slicing lives in estampo.
- Rename bridge binary to bambox-bridge, add xattr fix for macOS, trigger CI on bridge changes
- Stop running TestPyPI publish on PRs (OIDC ref mismatch)


## 0.1.0 — 2026-03-15

Initial release of bambox as a standalone library.

### Features

- Core `.gcode.3mf` archive packaging with Bambu Connect compatibility
- Template-driven 544-key `project_settings.config` generation from JSON profiles
- Machine base profile for P1S with filament overlays (PLA, ASA, PETG-CF)
- Automatic array padding and missing-key fixup for Bambu Connect firmware
- Cloud printing via Docker bridge (`estampo/cloud-bridge`) with bind-mount and baked fallback
- AMS tray mapping and printer status querying
- OrcaSlicer-to-Jinja2 template syntax conversion and rendering
- G-code component assembly (start + toolpath + end templates)
- CuraEngine Docker slicer backend prototype
- Synthetic toolpath generator for testing
- G-code-to-PNG thumbnail rendering (top-down view with bounding box)
- CLI with `pack`, `print`, and `status` commands
- MD5 checksum validation matching Bambu Connect requirements
- Support for both OrcaSlicer 2.3.1 and BambuStudio 2.5.0.66 format versions

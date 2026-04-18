# Changelog

All notable changes to bambox are documented here.
This changelog is managed by [towncrier](https://towncrier.readthedocs.io/).

<!-- towncrier release notes start -->

## 0.4.4 — 2026-04-18

### Features

- Release pipeline now automatically creates an issue on ``estampo/estampo`` when a full (non-prerelease) bambox version is published to PyPI.

### Bugfixes

- Docker bridge images now tagged with bambox version (``vX.Y.Z``) and ``latest`` in addition to the Bambu SDK version. Fixed broken release skip guard that used an unreliable GitHub API call. ([#202](https://github.com/estampo/bambox/pull/202))
- Fix false positive S002 safety check on heater-off commands after last extrusion move. ([#222](https://github.com/estampo/bambox/pull/222))


## 0.4.3 — 2026-04-18

### Features

- Add pre-packaging G-code safety validation (S001–S003) to catch dangerous Z moves, premature heater shutdown, and extrusion before homing. ([#207](https://github.com/estampo/bambox/pull/207))
- Add native single-extruder CuraEngine P1S definition (``bambox_p1s``) with complete start/end G-code — no bambox post-processing required. Remove the ``bambox_p1s_ams`` multi-extruder definition and the G-code assembly/tool-change rewriting pipeline (see ADR-003). ([#210](https://github.com/estampo/bambox/pull/210))
- Support ``BAMBOX_CREDENTIALS_TOML`` env var holding the full credentials TOML content, for CI and container deployments where writing a file is awkward. ([#215](https://github.com/estampo/bambox/pull/215))

### Bugfixes

- Fix unsafe ``max_layer_z`` default (0.4mm) that caused the nozzle to crash into tall prints at end of Cura-assembled G-code. ([#203](https://github.com/estampo/bambox/pull/203))
- Pass ``-m`` machine flag through to ``repack_3mf`` even when ``-f`` is not provided. ([#212](https://github.com/estampo/bambox/pull/212))
- ``repack`` now patches ``printer_model_id`` in ``slice_info.config`` from the ``-m`` machine flag. ([#213](https://github.com/estampo/bambox/pull/213))

### Misc

- Move G-code assembly logic from ``cli.py`` into ``cura.assemble_cura_gcode()`` to respect module ownership boundaries. ([#204](https://github.com/estampo/bambox/pull/204))
- Move P1S CuraEngine printer definition to its own repo (estampo/cura-p1s)


## 0.4.2 — 2026-04-14

### Features

- Ask for user confirmation of AMS tray mapping before sending a print; skip with ``--yes`` / ``-y``

### Bugfixes

- Release workflow now detects squash-merged release PRs by querying the PR by number, surviving auto-deleted head branches.

### Misc

- Rewrite ROADMAP.md to reflect shipped v0.2–v0.4.1 and mark multi-printer support as contributor-gated future work. Add Known limitations section to README covering cancel being disabled, macOS Docker-only, P1S-only testing, and missing LAN / Windows-native support.
- Sync uv.lock with pyproject.toml — the lockfile had bambox at 0.3.0 while the project was at 0.4.1.
- Update README: add daemon commands, fix macOS platform support, add missing modules, hide broken cancel command.


## 0.4.1 — 2026-04-13

### Features

- Route ``print`` and ``cancel`` through daemon HTTP API when running, with subprocess fallback. Add ``--foreground`` flag to ``bambox daemon start``. ([#154](https://github.com/estampo/bambox/pull/154))
- Add ``bambox daemon`` subcommands: ``start``, ``stop``, ``restart``, and ``status``.
- Bridge version check: /health endpoint now reports bridge_version, api_version, and plugin_version; Python client validates API compatibility on daemon connect.
- Support starting the bridge daemon via Docker on macOS (and when no local binary is available).

### Bugfixes

- Fix credential env var mismatch: Rust bridge now uses ``BAMBOX_CREDENTIALS`` (was ``BAMBU_CREDENTIALS``), matching the Python side. Python also checks macOS ``~/Library/Application Support/`` paths to match the bridge's search behavior. ([#156](https://github.com/estampo/bambox/pull/156))
- Fix cloud print ``BAMBU_NETWORK_SIGNED_ERROR`` (-26) by correcting X-BBL HTTP headers: update Client-Version to match BambuStudio 02.05.00.66, use stable UUID Device-ID, and make OS-Type platform-aware. ([#178](https://github.com/estampo/bambox/pull/178))
- Fix cancel/stop command: use QoS 1 and add missing ``param`` field to match BambuStudio's MQTT protocol. ([#180](https://github.com/estampo/bambox/pull/180))
- Fix ``send_message`` FFI signature: add missing ``flag`` parameter, fixing cancel and other MQTT commands returning -2. ([#181](https://github.com/estampo/bambox/pull/181))
- Daemon pre-subscribes to configured printers at startup so ``status -w`` returns instantly.
- Fix ``status -w`` updating every ~9s instead of 1s when printer is idle.
- Fix cloud printing on macOS: use Docker-only mode (the macOS .dylib signing gate blocks start_print from unsigned hosts with -26).

### Misc

- Consolidate duplicated ``MIN_SLOTS`` constant, array padding logic, and secure temp-file writing into single canonical implementations. ([#157](https://github.com/estampo/bambox/pull/157))
- Improve exception handling: add debug logging to silent except blocks, narrow broad exception types, replace ``BaseException`` catch with ``try/finally`` in credential temp-file writing. ([#158](https://github.com/estampo/bambox/pull/158))
- Replace duplicated Cura definition test fixtures with symlinks to the canonical source files in ``src/bambox/data/cura/``. ([#159](https://github.com/estampo/bambox/pull/159))
- Move hardcoded P1S toolchange geometry coordinates from ``gcode_compat.py`` into the ``base_p1s.json`` machine profile, making them configurable per-machine. ([#160](https://github.com/estampo/bambox/pull/160))
- Extract shared test constants (``MINIMAL_GCODE``, ``MINIMAL_SLICE_INFO``, ``MINIMAL_SETTINGS``) and ``build_valid_3mf()`` into ``tests/conftest.py``, removing duplication between test_validate.py and test_release_readiness.py. ([#161](https://github.com/estampo/bambox/pull/161))
- Improve FFI safety in C++ shim: track symbol resolution counts, expand null-check validation to 11 critical function pointers, and log resolution diagnostics on load. ([#162](https://github.com/estampo/bambox/pull/162))
- Consolidate BambuStudio version into single constants with format and consistency tests to prevent stale or fabricated version strings. ([#179](https://github.com/estampo/bambox/pull/179))
- Add cloud printing research and send_message investigation docs ([#183](https://github.com/estampo/bambox/pull/183))
- Document `libbambu_networking` signed-app gate and disable the CLI `cancel` subcommand. The SDK rejects `{"print":...}` MQTT commands when the hosting process is not an officially signed BambuStudio binary; see `docs/signed-app-gate.md` for the full investigation and evidence. The daemon `/cancel` endpoint is unchanged. ([#184](https://github.com/estampo/bambox/pull/184))
- Add ``scripts/install-test.sh`` to install the latest dev build from TestPyPI.
- Build bridge binaries on push to main (not just PRs and tags) so ``install-dev.sh`` always picks up the latest.
- Document Linux ARM64 platform support: native bridge unavailable, Docker bridge via QEMU emulation.


## 0.4.0 — 2026-04-11

### Features

- Add ``print`` and ``cancel`` CLI subcommands to the Rust bridge, reaching CLI parity with the legacy C++ bridge.
- ``bambox status -w`` now auto-starts the Rust daemon for fast polling via HTTP instead of spawning a new bridge process per refresh. The daemon keeps MQTT subscriptions alive and updates its cache every ~1s from printer push messages, so subsequent queries are always instant.

### Bugfixes

- Fix credentials file briefly world-readable before chmod — now created with 0o600 from the start using ``os.open()``/``mkstemp``, and parent directories use 0o700. ([#139](https://github.com/estampo/bambox/pull/139))
- Fix ``bambox status`` crash when local Rust bridge is installed by translating C++ positional args to Rust ``-c/--credentials`` flag format.

### Misc

- Add ``install-dev.sh`` script for installing the bridge binary from the latest CI build on main.
- Document Rust `bambox-bridge` as the replacement for the legacy C++ `estampo/cloud-bridge`: restore the bridge migration plan, add ADR-002, and update CLAUDE.md.
- Improve ``bambox status -w``: update display in-place instead of clearing screen, and show timestamp of last update.
- Remove all legacy C++ ``estampo/cloud-bridge`` references — Docker fallback now uses the Rust ``bambox-bridge`` image with arg translation, and the baked-image fallback has been removed.
- Shrink bridge Docker image from 174MB to 60MB using distroless base and multi-stage fetch.


## 0.3.5 — 2026-04-11

### Features

- Bring ``bambox status`` display to parity with estampo TUI: rounded temperatures, color swatches, print stages, and active tray indicator ([#131](https://github.com/estampo/bambox/pull/131))
- Add ``bambox cancel`` command to stop the current print on a Bambu printer
- Migrate CLI from argparse to Typer + Rich for consistent TUI rendering with estampo, including shell completion support

### Bugfixes

- Bridge binary now checks ``~/.config/bambox/`` for credentials (preferred over legacy ``~/.config/estampo/``)
- Fix E014 false positive on initial extruder select and W013 false positive on BBL ``-1`` disabled sentinel
- Fix E014 false positive on redundant extruder re-select after M620/M621 block
- Fix release pipeline false-positive tag detection when a pre-release tag exists (e.g. v0.3.0rc1 blocked v0.3.0)
- Include ``install.sh`` in GitHub release assets so the bridge install command works

### Misc

- Add CuraEngine + P1S AMS usage guide
- Fix PyPI badges in README pointing to wrong package name
- Update README: add experimental warning, bridge install instructions, platform support table, remove bambu-cloud references


## 0.3.0 — 2026-04-10

### Bugfixes

- Remove ``--user`` from bridge Docker calls (bridge has no host-side output files) and increase status timeout to accommodate baked fallback path
- Trigger bridge binary build from release pipeline so native binaries are attached to GitHub Releases

### Misc

- Update README with full CLI documentation for all subcommands


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

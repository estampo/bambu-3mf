# bambox

[![CI](https://github.com/estampo/bambox/actions/workflows/ci.yml/badge.svg)](https://github.com/estampo/bambox/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/estampo/bambox/branch/main/graph/badge.svg)](https://codecov.io/gh/estampo/bambox)
[![PyPI version](https://img.shields.io/pypi/v/bambox)](https://pypi.org/project/bambox/)
[![Python versions](https://img.shields.io/pypi/pyversions/bambox)](https://pypi.org/project/bambox/)

> **Experimental software — use at your own risk.**
> bambox talks directly to Bambu Lab printer firmware. Incorrect packaging,
> settings, or G-code can cause failed prints, nozzle clogs, or physical
> damage to your printer. Always review output before sending to hardware.

Package plain G-code into Bambu Lab `.gcode.3mf` files — no OrcaSlicer required.

`bambox` is a standalone Python library and CLI for creating printer-ready
Bambu Lab archives from any G-code source. It handles the BBL-specific
packaging format (metadata, checksums, settings) so that any slicer —
CuraEngine, PrusaSlicer, KiriMoto, or a custom toolpath generator — can target
Bambu printers.

## What You Can Do

**Pack G-code** (no extra dependencies) — build `.gcode.3mf` archives from
any G-code source with full 544-key settings generation. Works on Linux,
macOS, and Windows.

**Validate archives** — check `.gcode.3mf` files for errors before printing.

**Print and monitor** (requires bridge) — send archives to Bambu printers
via cloud and query printer status. Requires either the native `bambox-bridge`
binary or Docker (see [Bridge Setup](#bridge-setup) below).

## Where This Fits

`bambox` is part of the [estampo](https://github.com/estampo/estampo) project
that decouples slicer pipelines from OrcaSlicer:

```
estampo (pipeline + slicer backends)
    ↓ G-code
bambox (packaging + settings + printing)
    ↓ .gcode.3mf → printer
Bambu printer
```

**estampo** orchestrates the build pipeline — plate arrangement, profile
management, CI integration. It can invoke any slicer backend and delegates
Bambu-specific concerns to bambox.

**bambox** (this project) owns: (1) the `.gcode.3mf` archive format that
Bambu firmware requires, (2) the slicer settings blob
(`project_settings.config`) that would normally come from OrcaSlicer, and
(3) printer communication via the cloud bridge. It can generate the full
544-key settings from just a machine name and filament types.

You don't need estampo to use bambox — it works standalone.

## Installation

```bash
pip install bambox
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv pip install bambox
```

This is all you need for `pack`, `repack`, and `validate`. For `print`,
`status`, and `login` you also need the bridge — see below.

### Bridge Setup

The `print`, `status`, and `login` commands communicate with Bambu printers
via a cloud bridge. You have two options:

**Option A — Native binary (recommended, Linux and macOS):**

```bash
curl -fsSL https://github.com/estampo/bambox/releases/latest/download/install.sh | sh
```

This installs `bambox-bridge` to `~/.local/bin`.

**Option B — Docker (Linux, macOS, Windows):**

If you have Docker installed and running, bambox uses the
`estampo/cloud-bridge` image automatically — no extra setup needed.
This is the only option on Windows.

### Platform Support

| Feature | Linux | macOS | Windows |
|---------|-------|-------|---------|
| `pack`, `repack`, `validate` | Yes | Yes | Yes |
| `print`, `status`, `login` (native bridge) | Yes | Yes | No |
| `print`, `status`, `login` (Docker bridge) | Yes | Yes | Yes |

## CLI

```
bambox [-V] [-v] {pack,repack,login,print,validate,status}
```

### `bambox pack` — Package G-code

Create a `.gcode.3mf` archive from a G-code file.

```bash
# Basic packaging
bambox pack plate_1.gcode -o output.gcode.3mf

# With machine profile and filament settings
bambox pack plate_1.gcode -o output.gcode.3mf -m p1s -f PLA

# Multi-filament with AMS slot and color
bambox pack plate_1.gcode -o output.gcode.3mf -m p1s \
  -f 1:PLA:#FF0000 -f 3:PETG-CF:#2850E0
```

Options:

| Flag | Description |
|------|-------------|
| `-o, --output` | Output `.gcode.3mf` path |
| `-m, --machine` | Machine profile (e.g. `p1s`) |
| `-f, --filament` | Filament spec: `[SLOT:]TYPE[:COLOR]` (repeatable) |
| `--nozzle-diameter` | Nozzle diameter (default: 0.4) |
| `--printer-model-id` | Override printer model ID |

### `bambox repack` — Fix up existing archives

Regenerate settings in an existing `.gcode.3mf` for Bambu Connect compatibility. Modifies the file in-place.

```bash
# Patch existing settings
bambox repack my_print.gcode.3mf

# Regenerate settings with a specific machine and filament
bambox repack my_print.gcode.3mf -m p1s -f PLA
```

### `bambox validate` — Validate archives

Check a `.gcode.3mf` for errors and warnings.

```bash
# Basic validation
bambox validate my_print.gcode.3mf

# JSON output for CI pipelines
bambox validate my_print.gcode.3mf --json --strict

# Compare against a reference archive
bambox validate my_print.gcode.3mf --reference known_good.gcode.3mf
```

Options:

| Flag | Description |
|------|-------------|
| `--json` | Output results as JSON |
| `--strict` | Treat warnings as errors (non-zero exit) |
| `--reference` | Reference `.gcode.3mf` to compare against |

### `bambox login` — Configure credentials

Authenticate with Bambu Cloud and save printer credentials.

```bash
bambox login
```

Credentials are stored in `~/.config/estampo/credentials.toml`.

### `bambox print` — Send to printer

Send a `.gcode.3mf` to a Bambu printer via cloud.

```bash
# Print by device serial
bambox print output.gcode.3mf -d DEVICE_SERIAL

# Print by named printer from credentials
bambox print output.gcode.3mf -p my_printer

# Dry run — show AMS mapping without sending
bambox print output.gcode.3mf -d DEVICE_SERIAL -n

# Manual AMS tray assignment
bambox print output.gcode.3mf -d DEVICE_SERIAL \
  --ams-tray 2:PETG-CF:2850E0
```

Options:

| Flag | Description |
|------|-------------|
| `-d, --device` | Printer serial number |
| `-p, --printer` | Named printer from `credentials.toml` |
| `-c, --credentials` | Path to `credentials.toml` |
| `--project` | Project name shown in Bambu Cloud |
| `--timeout` | Upload timeout in seconds |
| `--no-ams-mapping` | Skip AMS filament mapping |
| `--ams-tray` | Manual tray spec: `SLOT:TYPE:COLOR` (repeatable) |
| `-n, --dry-run` | Show print info without sending |

### `bambox status` — Query printer

Query printer status and AMS tray info.

```bash
# One-shot status
bambox status DEVICE_SERIAL

# By named printer
bambox status -p my_printer

# Live watch mode with custom interval
bambox status DEVICE_SERIAL -w -i 5
```

Options:

| Flag | Description |
|------|-------------|
| `-p, --printer` | Named printer from `credentials.toml` |
| `-c, --credentials` | Path to `credentials.toml` |
| `-w, --watch` | Continuously refresh status display |
| `-i, --interval` | Seconds between refreshes (default: 10) |

### Global options

| Flag | Description |
|------|-------------|
| `-V, --version` | Show installed version |
| `-v, --verbose` | Enable debug logging |

## Python API

### Packaging G-code

```python
from pathlib import Path
from bambox import pack_gcode_3mf, SliceInfo, FilamentInfo

gcode = Path("plate_1.gcode").read_bytes()

info = SliceInfo(
    nozzle_diameter=0.4,
    filaments=[FilamentInfo(filament_type="PLA", color="00AE42")],
)

pack_gcode_3mf(gcode, Path("output.gcode.3mf"), slice_info=info)
```

### With generated settings (no OrcaSlicer)

```python
from bambox.settings import build_project_settings

settings = build_project_settings(
    filaments=["PETG-CF"],
    machine="p1s",
    filament_colors=["2850E0FF"],
    overrides={"layer_height": "0.2"},
)

pack_gcode_3mf(
    gcode,
    Path("output.gcode.3mf"),
    slice_info=info,
    project_settings=settings,
)
```

### Cloud printing (requires bridge)

```python
from bambox.bridge import cloud_print, query_status
```

### Archive validation

```python
from bambox.validate import validate_3mf
```

## Modules

| Module | Purpose |
|--------|---------|
| `pack` | Core `.gcode.3mf` archive construction, XML metadata, MD5 checksums |
| `settings` | 544-key `project_settings.config` builder from machine + filament profiles |
| `bridge` | Cloud printing via native or Docker bridge, AMS tray mapping |
| `validate` | Archive validation checks, warnings, and reference comparison |
| `cli` | Typer CLI commands — delegates to other modules |
| `cura` | CuraEngine Docker invocation and profile conversion |
| `templates` | OrcaSlicer-to-Jinja2 syntax conversion and template rendering |
| `assemble` | G-code component assembly (start + toolpath + end) |
| `thumbnail` | G-code-to-PNG rendering (top-down view) |
| `toolpath` | Synthetic toolpath generation for testing |
| `credentials` | Credential loading and storage (`~/.config/estampo/credentials.toml`) |
| `auth` | Bambu Cloud authentication |

## BBL `.gcode.3mf` Format

A `.gcode.3mf` is a ZIP archive containing 13-17 files:

| File | Purpose |
|------|---------|
| `Metadata/plate_1.gcode` | The actual G-code |
| `Metadata/slice_info.config` | Print metadata (time, weight, filaments) |
| `Metadata/project_settings.config` | Full slicer settings (536-544 keys) |
| `Metadata/model_settings.config` | Per-plate filament mapping |
| `Metadata/plate_1.png` | Thumbnail (required by firmware) |
| `3D/3dmodel.model` | OPC/3MF model XML |
| `[Content_Types].xml` | OPC content types |
| `_rels/.rels` | OPC relationships |

BambuStudio adds: `cut_information.xml`, `filament_sequence.json`,
`top_N.png`, `pick_N.png`.

All files include MD5 checksums validated by the printer firmware.

## Development

```bash
uv sync --extra dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/bambox
uv run pytest
```

## License

MIT

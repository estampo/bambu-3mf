# bambox

[![CI](https://github.com/estampo/bambox/actions/workflows/ci.yml/badge.svg)](https://github.com/estampo/bambox/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/estampo/bambox/branch/main/graph/badge.svg)](https://codecov.io/gh/estampo/bambox)
[![PyPI version](https://img.shields.io/pypi/v/bambox)](https://pypi.org/project/bambox/)
[![Python versions](https://img.shields.io/pypi/pyversions/bambox)](https://pypi.org/project/bambox/)

> **Experimental software — use at your own risk.**
> bambox packages G-code into Bambu Lab firmware-validated archives. Incorrect
> settings or G-code can cause failed prints, nozzle clogs, or physical damage
> to your printer. Always review output before sending to hardware.

Package plain G-code into Bambu Lab `.gcode.3mf` files — no OrcaSlicer required.

## What bambox is — and isn't

bambox is a **Bambu Lab packaging layer**. It takes G-code from any slicer and
produces the `.gcode.3mf` archive Bambu Lab printers require — with the 544-key
`project_settings.config`, metadata, MD5 checksums, and per-filament arrays the
firmware validates.

**bambox is not:**

- **A slicer.** Slicing happens upstream (estampo + CuraEngine/OrcaSlicer).
  bambox packages the result — see [Where This Fits](#where-this-fits).
- **A profile editor.** It loads and overlays bundled profiles. New printers
  are added by dropping in `src/bambox/profiles/base_<printer>.json`; there
  is no UI for editing profiles.
- **A printer-control tool.** Cloud printing lives in
  [boo-cloud](https://github.com/estampo/boo-cloud). bambox only produces
  the archive.

## Where This Fits

`bambox` is part of the [estampo](https://github.com/estampo/estampo) project
that decouples slicer pipelines from OrcaSlicer:

```
estampo (pipeline + slicer backends)
    ↓ G-code
bambox (packaging + settings + validation)
    ↓ .gcode.3mf
boo-cloud (cloud printing, optional)
    ↓ → printer
Bambu printer
```

**estampo** orchestrates the build pipeline — plate arrangement, profile
management, CI integration. It can invoke any slicer backend and delegates
Bambu-specific concerns to bambox.

**bambox** (this project) owns: (1) the `.gcode.3mf` archive format that Bambu
firmware requires, and (2) the slicer settings blob (`project_settings.config`)
that would normally come from OrcaSlicer. It generates the full 544-key settings
from just a machine name and filament types.

**[boo-cloud](https://github.com/estampo/boo-cloud)** handles cloud printing
and printer credentials separately.

You don't need estampo or boo-cloud to use bambox — it works standalone.

## Installation

```bash
pip install bambox
```

Or with [uv](https://docs.astral.sh/uv/):

```bash
uv pip install bambox
```

### Supported printers and filaments

bambox ships bundled profiles in `src/bambox/profiles/`. `bambox pack` fails
loudly at entry if the requested printer or filament is not in these tables,
rather than letting the archive fail at print time.

| Printer | Firmware model ID | Profile |
|---------|-------------------|---------|
| Bambu Lab P1S (0.4 nozzle) | `C12` | `base_p1s.json` |

| Filament | Profile |
|----------|---------|
| PLA | `filament_pla.json` |
| ASA | `filament_asa.json` |
| PETG-CF | `filament_petg_cf.json` |

Other Bambu printers (P1P, X1C, X1, X1E, A1, A1 Mini) have firmware model IDs
listed in `bambox.cura.PRINTER_MODEL_IDS` but no bundled profiles yet. Adding
one is a matter of dropping in `base_<printer>.json` — contributions welcome.

Only the **Bambu P1S with AMS** is validated on hardware. Other models can be
added by dropping in machine profiles, but require external contributor testing.
If you have one of those printers and want to help, please
[open an issue](https://github.com/estampo/bambox/issues/new).

## CLI

```
bambox [-V] [-v] {pack,repack,validate}
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

#### How packing works

`.gcode.3mf` is a ZIP archive. The packing step does three things:

1. **Rewrites the G-code** to meet Bambu firmware expectations (header
   injection, layer markers, AMS tool-change rewriting).
2. **Generates a 544-key `project_settings.config`** by layering the machine
   base profile (`base_<printer>.json`) with per-slot filament overlays
   (`filament_<type>.json`). The firmware validates this blob and rejects
   archives where keys are missing, arrays are the wrong length, or the
   embedded `printer_model_id` doesn't match.
3. **Computes MD5 checksums and writes OPC/3MF metadata** so the archive is
   a valid package.

Because the settings blob is printer-specific, a `.gcode.3mf` packed for the
wrong printer is not portable. bambox validates the requested printer at
pack entry and exits with a clear error if the profile is unknown or
malformed.

### `bambox repack` — Fix up existing archives

Regenerate settings in an existing `.gcode.3mf` for Bambu Connect compatibility.
Modifies the file in-place.

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

### Global options

| Flag | Description |
|------|-------------|
| `-V, --version` | Show installed version |
| `-v, --verbose` | Enable debug logging |

## Python API

### Packaging G-code

```python
from pathlib import Path
from bambox.pack import pack_gcode_3mf, SliceInfo, FilamentInfo

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

### Archive validation

```python
from bambox.validate import validate_3mf
```

## Modules

| Module | Purpose |
|--------|---------|
| `pack` | Core `.gcode.3mf` archive construction, XML metadata, MD5 checksums |
| `settings` | 544-key `project_settings.config` builder from machine + filament profiles |
| `validate` | Archive validation checks, warnings, and reference comparison |
| `cli` | Typer CLI commands — delegates to other modules |
| `cura` | CuraEngine Docker invocation and profile conversion |
| `templates` | OrcaSlicer-to-Jinja2 syntax conversion and template rendering |
| `assemble` | G-code component assembly (start + toolpath + end) |
| `thumbnail` | G-code-to-PNG rendering (top-down view) |
| `toolpath` | Synthetic toolpath generation for testing |
| `gcode_compat` | G-code rewriting for multi-filament compatibility |
| `ui` | Rich console formatting, color swatches, interactive prompts |

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

See [`docs/gcode-3mf-format.md`](docs/gcode-3mf-format.md) for the full format
specification.

## Development

```bash
uv sync --extra dev
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src/bambox
uv run pytest
```

## Credits and attribution

The bundled machine and filament profiles under `src/bambox/profiles/` are
derived from OrcaSlicer and BambuStudio slicer profiles (AGPL-3.0-era
sources). See
[`THIRD-PARTY-NOTICES`](THIRD-PARTY-NOTICES) for full provenance, file lists,
and license details. The file is also shipped inside the installed package
so it remains discoverable after `pip install`.

## License

bambox's own source code is **MIT**. The bundled printer/filament profiles
under `src/bambox/profiles/` are derived from BambuStudio 2.5.0.66
(AGPL-3.0); they remain under AGPL-3.0. The package as a whole is therefore
`MIT AND AGPL-3.0-only`.

Full AGPL-3.0 text: [`LICENSES/AGPL-3.0.txt`](LICENSES/AGPL-3.0.txt).
Third-party file list and provenance: [`THIRD-PARTY-NOTICES`](THIRD-PARTY-NOTICES).

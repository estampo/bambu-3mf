# bambu-3mf

Package plain G-code into Bambu Lab `.gcode.3mf` files — no OrcaSlicer required.

`bambu-3mf` is a standalone Python library for creating printer-ready Bambu Lab
archives from any G-code source. It handles the BBL-specific packaging format
(metadata, checksums, settings) so that any slicer — CuraEngine, PrusaSlicer,
KiriMoto, or a custom toolpath generator — can target Bambu printers.

## Where This Fits

`bambu-3mf` is one piece of a three-project architecture that decouples
[estampo](https://github.com/estampo/estampo) from OrcaSlicer:

```
estampo (pipeline + slicer backends)
    ↓ G-code
bambu-3mf (packaging + settings generation)
    ↓ .gcode.3mf
bambu-cloud (printer communication)
    ↓ MQTT/FTP
Bambu printer
```

**estampo** orchestrates the build pipeline — plate arrangement, profile
management, CI integration. It can invoke any slicer backend and delegates
Bambu-specific concerns downward.

**bambu-3mf** (this project) owns two things: (1) the `.gcode.3mf` archive
format that Bambu firmware requires, and (2) the slicer settings blob
(`project_settings.config`) that would normally come from OrcaSlicer. It can
generate the full 544-key settings from just a machine name and filament types.

**bambu-cloud** (currently the `bridge` module here, to be extracted) handles
printer communication via the Bambu Network Library Docker bridge.

Each project is independently useful. You don't need estampo to use bambu-3mf,
and you don't need bambu-3mf to use bambu-cloud.

## Installation

```
pip install bambu-3mf
```

## Packaging G-code

### Python API

```python
from pathlib import Path
from bambu_3mf import pack_gcode_3mf, SliceInfo, FilamentInfo

gcode = Path("plate_1.gcode").read_bytes()

info = SliceInfo(
    nozzle_diameter=0.4,
    filaments=[FilamentInfo(filament_type="PLA", color="00AE42")],
)

pack_gcode_3mf(gcode, Path("output.gcode.3mf"), slice_info=info)
```

### With generated settings (no OrcaSlicer)

```python
from bambu_3mf.settings import build_project_settings

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

### CLI

```bash
# Package G-code into a .gcode.3mf
bambu-3mf pack plate_1.gcode -o output.gcode.3mf

# Query printer status and AMS tray info
bambu-3mf status DEVICE_SERIAL

# Send a .gcode.3mf to a Bambu printer via cloud
bambu-3mf print output.gcode.3mf --device DEVICE_SERIAL
```

## Modules

### `pack` — Core Packager

Takes G-code bytes and produces a `.gcode.3mf` ZIP archive with all required
metadata files (slice_info, model_settings, project_settings, thumbnails, MD5
checksums). Supports both OrcaSlicer 2.3.1 and BambuStudio 2.5.0.66 format
variants.

Key types: `SliceInfo`, `FilamentInfo`, `ObjectInfo`, `WarningInfo`

### `settings` — Slicer-Agnostic Settings Generator

Generates the full `project_settings.config` (544 keys) from a machine base
profile and per-filament-type data files. No OrcaSlicer in the loop.

Available machines: `p1s`
Available filaments: `pla`, `asa`, `petg_cf`

```python
from bambu_3mf.settings import available_machines, available_filaments, build_project_settings
```

### `bridge` — Cloud Printing

Wraps the Bambu cloud bridge Docker image to send prints, query printer status,
and handle AMS tray mapping. Reads credentials from
`~/.config/estampo/credentials.toml`.

```python
from bambu_3mf.bridge import cloud_print, query_status
```

## BBL `.gcode.3mf` Format

A `.gcode.3mf` is a ZIP archive containing 13–17 files:

| File | Purpose |
|------|---------|
| `Metadata/plate_1.gcode` | The actual G-code |
| `Metadata/slice_info.config` | Print metadata (time, weight, filaments) |
| `Metadata/project_settings.config` | Full slicer settings (536–544 keys) |
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
uv pip install -e .
uv run ruff check src tests
uv run mypy src/bambu_3mf
uv run pytest
```

## License

MIT

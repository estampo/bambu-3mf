# ADR-001: Template-Driven Settings Generation

**Status:** Accepted
**Date:** 2026-03-10

## Context

Bambu printers require a `project_settings.config` file inside the `.gcode.3mf` archive. This file contains approximately 544 key-value pairs that describe every slicer setting used for the print. Bambu Connect firmware validates this file and will reject archives with missing or malformed keys.

The obvious approach is to pass through the settings blob from the slicer (OrcaSlicer, CuraEngine). This creates two problems:

1. **Slicer coupling.** Each slicer outputs settings in a different format. OrcaSlicer's `--min-save` output omits keys that Bambu Connect requires. CuraEngine doesn't produce Bambu-format settings at all.
2. **Incompleteness.** Even OrcaSlicer output needs post-processing: missing keys must be added, per-filament arrays must be padded to exactly 5 slots (P1S AMS + external spool), and scalar values must be broadcast to arrays for keys that the firmware expects as arrays.

## Decision

bambox generates the full 544-key settings blob from a layered profile system:

1. **Machine base profile** (`profiles/base_p1s.json`) — all 544 keys with sensible defaults for the target printer. This is the single source of truth for the key set.
2. **Filament type profiles** (`profiles/filament_pla.json`, etc.) — per-filament-type overrides for keys that vary by material (temperatures, speeds, retraction).
3. **Varying keys list** (`profiles/_varying_keys.json`) — declares which keys differ per filament slot and must be built as arrays from filament profiles.
4. **Uniform array keys** (`profiles/_uniform_array_keys.json`) — declares which keys are stored as scalars in the base but must be broadcast to 5-element arrays in the output (e.g., `"retraction_length": "0.8"` becomes `["0.8", "0.8", "0.8", "0.8", "0.8"]`).

The build pipeline in `settings.py`:
1. Load machine base (544 keys)
2. Broadcast uniform scalars to arrays of `min_slots` length
3. Build per-filament arrays from filament profiles for varying keys
4. Apply optional scalar overrides
5. Return the complete dict

`pack.py` then calls `fixup_project_settings()` as a safety net before writing to the archive — this adds any keys from `_BC_REQUIRED_KEYS` that are still missing and pads short arrays.

## Consequences

### Benefits

- **Slicer-agnostic.** Any G-code source (OrcaSlicer, CuraEngine, hand-written) can be packaged with correct settings. The caller only needs to specify filament types.
- **Firmware-safe.** The base profile guarantees all 544 keys are present. The fixup layer catches edge cases. Archives always pass Bambu Connect validation.
- **Maintainable.** Adding a new filament type means adding one JSON file. Adding a new printer means one base profile.

### Costs

- **544-key maintenance.** The base profile must track firmware expectations. If Bambu adds required keys in a firmware update, the base profile needs updating.
- **Profile accuracy.** The filament profiles are hand-extracted from OrcaSlicer defaults. They may drift from upstream OrcaSlicer profiles over time.
- **Two-layer safety.** Both `build_project_settings()` and `fixup_project_settings()` handle array padding, which could be confusing. The build function is the "right" way; fixup is the safety net for callers who construct settings manually or from slicer output.

## Alternatives considered

### Pass through slicer output
Rejected. OrcaSlicer `--min-save` omits required keys. CuraEngine doesn't produce Bambu-format settings. Every slicer would need a custom post-processor, and the post-processor would need to know the full key set anyway — which is what the base profile provides.

### Generate from OrcaSlicer profiles at runtime
Rejected. Would require bundling OrcaSlicer profile data and implementing OrcaSlicer's profile inheritance chain in Python. Too much complexity for a packaging library.

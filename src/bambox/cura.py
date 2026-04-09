"""CuraEngine integration: printer definitions and BAMBOX header parsing.

Provides bundled CuraEngine printer definitions with BAMBOX header comments
that bambox reads to auto-configure packaging. The header contract lets
CuraEngine output carry machine-readable metadata without coupling the
slicer to Bambu Lab specifics.

Header format (emitted as G-code comments by the printer definition)::

    ; BAMBOX_PRINTER=p1s
    ; BAMBOX_EXTRUDERS=4
    ; BAMBOX_BED_TEMP=60
    ; BAMBOX_NOZZLE_TEMP=220
    ; BAMBOX_FILAMENT_SLOT=0
    ; BAMBOX_FILAMENT_TYPE=PLA
    ; BAMBOX_ASSEMBLE=true
    ; BAMBOX_END
"""

from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Printer definitions
# ---------------------------------------------------------------------------

_CURA_DIR = Path(__file__).parent / "data" / "cura"


def cura_definitions_dir() -> Path:
    """Return the path to bundled CuraEngine definition files.

    Pass this to CuraEngine's ``-d`` flag so it can resolve
    ``bambox_p1s_ams`` and its extruder definitions.
    """
    return _CURA_DIR


def available_cura_printers() -> list[str]:
    """Return names of bundled CuraEngine printer definitions."""
    return [
        p.name.removesuffix(".def.json")
        for p in sorted(_CURA_DIR.glob("*.def.json"))
        if "extruder" not in p.name
    ]


# ---------------------------------------------------------------------------
# BAMBOX header parsing
# ---------------------------------------------------------------------------


def parse_bambox_headers(gcode: str) -> dict[str, str]:
    """Extract ``; BAMBOX_*`` headers from G-code.

    Returns a dict of key→value pairs. Stops at ``; BAMBOX_END`` or after
    the first 200 lines (headers are always at the top).

    Multi-value keys like ``BAMBOX_FILAMENT_TYPE`` appearing multiple times
    are collected into comma-separated values.
    """
    result: dict[str, str] = {}
    for i, line in enumerate(gcode.splitlines()):
        if i > 200:
            break
        stripped = line.strip()
        if stripped == "; BAMBOX_END":
            break
        if stripped.startswith("; BAMBOX_"):
            # "; BAMBOX_KEY=value" → ("KEY", "value")
            payload = stripped[9:]  # after "; BAMBOX_"
            if "=" in payload:
                key, _, val = payload.partition("=")
                key = key.strip()
                val = val.strip()
                if key in result:
                    result[key] = result[key] + "," + val
                else:
                    result[key] = val
    return result


def strip_bambox_header(gcode: str) -> str:
    """Remove the BAMBOX header block from G-code.

    Strips all ``; BAMBOX_*`` lines and ``; BAMBOX_END`` from the top of
    the G-code, returning the raw toolpath ready for assembly.
    """
    lines = gcode.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("; BAMBOX_") or stripped == "; BAMBOX_END":
            continue
        out.append(line)
    return "".join(out)


def build_template_context(
    headers: dict[str, str],
    project_settings: dict[str, object],
) -> dict[str, object]:
    """Build a Jinja2 template context from BAMBOX headers + project_settings.

    Combines header values (concrete temps from CuraEngine) with the full
    project_settings blob (which contains per-filament arrays). Header values
    take precedence for the keys they specify.
    """
    ctx: dict[str, object] = {}

    # Convert project_settings values: string arrays → numeric arrays where possible
    for key, val in project_settings.items():
        if isinstance(val, list):
            converted: list[object] = []
            for item in val:
                if isinstance(item, str):
                    try:
                        converted.append(int(item))
                    except ValueError:
                        try:
                            converted.append(float(item))
                        except ValueError:
                            converted.append(item)
                else:
                    converted.append(item)
            ctx[key] = converted
        elif isinstance(val, str):
            try:
                ctx[key] = int(val)
            except ValueError:
                try:
                    ctx[key] = float(val)
                except ValueError:
                    ctx[key] = val
        else:
            ctx[key] = val

    # Map BAMBOX headers to template variables
    if "BED_TEMP" in headers:
        bed = int(headers["BED_TEMP"])
        ctx["bed_temperature_initial_layer_single"] = bed
        ctx["bed_temperature"] = [bed]
        ctx["bed_temperature_initial_layer"] = [bed]

    if "NOZZLE_TEMP" in headers:
        nozzle = int(headers["NOZZLE_TEMP"])
        ctx["nozzle_temperature_initial_layer"] = [nozzle]

    if "NOZZLE_DIAMETER" in headers:
        ctx["nozzle_diameter"] = float(headers["NOZZLE_DIAMETER"])

    if "BED_TYPE" in headers:
        ctx["curr_bed_type"] = headers["BED_TYPE"]

    # Defaults needed by templates
    ctx.setdefault("initial_extruder", 0)
    ctx.setdefault("max_layer_z", 0.4)
    ctx.setdefault("first_layer_print_min", [100, 100])
    ctx.setdefault("first_layer_print_size", [20, 20])
    ctx.setdefault("outer_wall_volumetric_speed", 12)
    ctx.setdefault("filament_max_volumetric_speed", [12])
    ctx.setdefault("nozzle_temperature_range_high", [240])

    return ctx

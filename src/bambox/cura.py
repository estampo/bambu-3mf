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

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from bambox.gcode_compat import _FILAMENT_AREA

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Printer definitions
# ---------------------------------------------------------------------------

# Mapping from BAMBOX_PRINTER machine name to Bambu firmware printer_model_id.
# These IDs appear in the .gcode.3mf metadata and must match what the firmware
# expects — incorrect values may cause the printer to reject the file.
PRINTER_MODEL_IDS: dict[str, str] = {
    "p1s": "C12",
    "p1p": "C11",
    "x1c": "BL-P001",
    "x1": "BL-P002",
    "x1e": "BL-P003",
    "a1": "N2S",
    "a1_mini": "N1",
}

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

    Returns a dict of key→value pairs.  Machine-level headers (PRINTER,
    EXTRUDERS, BED_TEMP, etc.) are emitted at the top of the file by
    ``machine_start_gcode``.  Per-extruder headers (FILAMENT_SLOT,
    FILAMENT_TYPE) are emitted by ``machine_extruder_start_code`` at tool-
    change points throughout the file — so the full file must be scanned.

    Multi-value keys like ``BAMBOX_FILAMENT_TYPE`` appearing multiple times
    are collected into comma-separated values.

    CuraEngine CLI does not substitute ``{variable}`` placeholders in
    machine_start_gcode.  Values containing ``{`` are treated as
    unresolved and discarded.  For machine-level headers (BED_TEMP,
    NOZZLE_TEMP, etc.) the values are inferred from actual G-code
    commands (M140/M104) via :func:`_resolve_unsubstituted_headers`.
    """
    # Per-extruder keys that may appear anywhere in the file
    _FULL_SCAN_KEYS = {"FILAMENT_SLOT", "FILAMENT_TYPE"}

    result: dict[str, str] = {}
    # Collect per-extruder slot/type entries for deduplication.
    # CuraEngine emits paired SLOT+TYPE at each tool change throughout the file.
    # The older header format uses comma-separated values on single lines.
    raw_slots: list[str] = []
    raw_types: list[str] = []

    header_done = False
    for line in gcode.splitlines():
        stripped = line.strip()
        if stripped == "; BAMBOX_END":
            header_done = True
            continue
        if not stripped.startswith("; BAMBOX_"):
            continue
        payload = stripped[9:]  # after "; BAMBOX_"
        if "=" not in payload:
            continue
        key, _, val = payload.partition("=")
        key = key.strip()
        val = val.strip()
        is_template = "{" in val
        # After the header block, only collect per-extruder keys
        if header_done and key not in _FULL_SCAN_KEYS:
            continue
        # Accumulate slot and type entries separately
        if key == "FILAMENT_SLOT" and not is_template:
            raw_slots.append(val)
            continue
        if key == "FILAMENT_TYPE":
            if not is_template:
                raw_types.append(val)
            else:
                raw_types.append("")  # placeholder for unresolved template
            continue
        # Skip unsubstituted CuraEngine templates for regular keys
        if is_template:
            continue
        if key in result:
            result[key] = result[key] + "," + val
        else:
            result[key] = val

    # Flatten comma-separated values (old format: "FILAMENT_SLOT=0,2")
    all_slots = [s for raw in raw_slots for s in raw.split(",") if s]
    all_types = [t for raw in raw_types for t in raw.split(",")]

    # Deduplicate by slot number — keep first occurrence only
    seen: set[str] = set()
    dedup_slots: list[str] = []
    dedup_types: list[str] = []
    for i, slot in enumerate(all_slots):
        if slot not in seen:
            seen.add(slot)
            dedup_slots.append(slot)
            dedup_types.append(all_types[i] if i < len(all_types) else "")
    # If no slots were found but types were, keep types (legacy format)
    if not dedup_slots and all_types:
        result["FILAMENT_TYPE"] = ",".join(all_types)
    elif dedup_slots:
        result["FILAMENT_SLOT"] = ",".join(dedup_slots)
        if dedup_types:
            result["FILAMENT_TYPE"] = ",".join(dedup_types)

    # Only resolve unsubstituted headers if we found BAMBOX markers
    if result:
        _resolve_unsubstituted_headers(gcode, result)

    return result


def _resolve_unsubstituted_headers(gcode: str, headers: dict[str, str]) -> None:
    """Infer missing header values from actual G-code commands.

    CuraEngine CLI does not substitute ``{variable}`` in start gcode,
    so BED_TEMP, NOZZLE_TEMP, and NOZZLE_DIAMETER may be missing.
    This scans the first 50 lines for M140/M104 to fill them in.
    """
    if "BED_TEMP" not in headers:
        m = re.search(r"M140 S(\d+)", gcode[:3000])
        if m:
            headers["BED_TEMP"] = m.group(1)

    if "NOZZLE_TEMP" not in headers:
        m = re.search(r"M104 S(\d+)", gcode[:3000])
        if m:
            headers["NOZZLE_TEMP"] = m.group(1)

    if "NOZZLE_DIAMETER" not in headers:
        headers["NOZZLE_DIAMETER"] = "0.4"

    if "BED_TYPE" not in headers:
        headers["BED_TYPE"] = "Textured PEI Plate"


def strip_bambox_header(gcode: str) -> str:
    """Remove the leading BAMBOX header block from G-code.

    Strips ``; BAMBOX_*`` lines and ``; BAMBOX_END`` from the contiguous
    header section at the top of the file only.  Once a non-BAMBOX,
    non-empty line is seen, the rest of the file is kept as-is.
    """
    lines = gcode.splitlines(keepends=True)
    out: list[str] = []
    in_header = True
    for line in lines:
        stripped = line.strip()
        if in_header:
            if stripped.startswith("; BAMBOX_") or stripped == "; BAMBOX_END":
                continue
            if stripped == "" or stripped.startswith(";"):
                # Skip blank lines and other comments within the header block
                out.append(line)
                continue
            in_header = False
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
    ctx.setdefault("filament_area", _FILAMENT_AREA)

    return ctx


# ---------------------------------------------------------------------------
# Slice statistics extraction
# ---------------------------------------------------------------------------

_PLA_DENSITY_G_PER_MM3 = 0.00124  # g/mm³  (1.24 g/cm³)


@dataclass
class SliceStats:
    """Statistics extracted from slicer G-code."""

    prediction: int = 0  # estimated print time in seconds
    weight: float = 0.0  # total filament weight in grams
    filament_used_m: list[float] | None = None  # per-extruder metres


def extract_slice_stats(gcode: str) -> SliceStats:
    """Extract print time and filament usage from CuraEngine G-code.

    CuraEngine emits:
    * ``;TIME:N`` — total print time in seconds (often default 6666)
    * ``;TIME_ELAPSED:N`` — cumulative elapsed time (last value = total)
    * ``;Filament used: X.XXm, Y.YYm`` — per-extruder filament usage

    We prefer the last ``TIME_ELAPSED`` value (accurate per-layer
    cumulative time) over ``;TIME:`` which CuraEngine often sets to a
    default placeholder (6666).
    """
    stats = SliceStats()

    # Time: prefer last TIME_ELAPSED (accurate) over ;TIME: (often 6666 default)
    elapsed = re.findall(r";TIME_ELAPSED:([\d.]+)", gcode)
    if elapsed:
        stats.prediction = int(float(elapsed[-1]))
    else:
        m_time = re.search(r";TIME:(\d+)", gcode)
        if m_time:
            stats.prediction = int(m_time.group(1))

    # Filament: ";Filament used: 1.234m, 0.567m" (one entry per extruder)
    m_fil = re.search(r";Filament used:\s*(.+)", gcode)
    if m_fil:
        parts = m_fil.group(1).split(",")
        metres: list[float] = []
        total_mm = 0.0
        for part in parts:
            part = part.strip().rstrip("m")
            try:
                m_val = float(part)
            except ValueError:
                m_val = 0.0
            metres.append(m_val)
            total_mm += m_val * 1000  # convert m → mm
        stats.filament_used_m = metres
        # weight = length_mm × cross-section_mm² × density_g/mm³
        stats.weight = round(total_mm * _FILAMENT_AREA * _PLA_DENSITY_G_PER_MM3, 2)

    # CuraEngine CLI often reports "Filament used: 0m" as a placeholder.
    # Compute from max absolute E position per G92-reset segment instead.
    if stats.weight == 0.0:
        segment_max = 0.0
        total_length = 0.0
        for line in gcode.splitlines():
            stripped = line.strip()
            if stripped == "G92 E0":
                total_length += segment_max
                segment_max = 0.0
                continue
            m_e = re.match(r"G[01]\s.*E([\d.]+)", stripped)
            if m_e:
                e_val = float(m_e.group(1))
                if e_val > segment_max:
                    segment_max = e_val
        total_length += segment_max  # last segment
        if total_length > 0:
            stats.weight = round(total_length * _FILAMENT_AREA * _PLA_DENSITY_G_PER_MM3, 2)

    return stats

"""Translate generic slicer G-code to Bambu-firmware-compatible format.

Bambu Lab firmware expects specific markers in G-code that most third-party
slicers (CuraEngine, PrusaSlicer, KiriMoto, etc.) do not produce.  Without
these markers, the printer displays 0/0 layers and cannot track print progress.

Required firmware markers:

* ``; HEADER_BLOCK_START`` / ``; HEADER_BLOCK_END`` — a metadata block at the
  very start of the file containing ``; total layer number: N`` and timing info.
* ``M73 L{n}`` — layer progress update at each layer change.
* ``M991 S0 P{n}`` — firmware layer-change notification (required for the
  printer's layer counter to work).

This module detects which slicer produced the G-code and applies the necessary
translations.  OrcaSlicer / BambuStudio G-code is returned unchanged.
"""

from __future__ import annotations

import logging
import math
import re
from bisect import bisect_right

log = logging.getLogger(__name__)

_FILAMENT_DIAMETER = 1.75  # mm — standard; change to 2.85 for bowden setups
_DEFAULT_FLUSH_VOLUME = 280.0  # mm³ — BambuStudio default per-transition volume


def is_bbl_gcode(gcode: str | bytes) -> bool:
    """Return True if the G-code already has BBL header markers.

    OrcaSlicer and BambuStudio produce G-code with ``; HEADER_BLOCK_START``
    which the firmware can parse natively.  No translation needed.
    """
    text = gcode if isinstance(gcode, str) else gcode.decode(errors="replace")
    return "; HEADER_BLOCK_START" in text[:2000]


def translate_to_bbl(gcode: bytes) -> bytes:
    """Translate generic slicer G-code to Bambu-firmware-compatible format.

    If the G-code already contains BBL markers (OrcaSlicer / BambuStudio),
    it is returned unchanged.

    For other slicers, this function:

    1. Prepends a ``; HEADER_BLOCK_START`` / ``; HEADER_BLOCK_END`` header with
       metadata extracted from slicer-specific comments.
    2. Injects ``M73 L{n}`` and ``M991 S0 P{n}`` commands at each layer change.

    Currently supports:

    * **CuraEngine** — ``;LAYER_COUNT:N``, ``;LAYER:N``, ``;TIME:N``
    * **PrusaSlicer / SuperSlicer** — ``;LAYER_CHANGE``, ``; total layers``

    Returns the translated G-code as bytes.
    """
    text = gcode.decode(errors="replace")

    if is_bbl_gcode(text):
        return gcode

    if ";LAYER_COUNT:" in text:
        text = _translate_cura(text)
    elif ";LAYER_CHANGE" in text:
        text = _translate_prusa(text)
    else:
        text = _translate_zchange(text)

    return text.encode()


def _translate_cura(text: str) -> str:
    """Translate CuraEngine G-code to BBL format.

    CuraEngine emits:
    * ``;LAYER_COUNT:N`` — total layers
    * ``;LAYER:N`` — 0-based layer index
    * ``;TIME:N`` — total print time in seconds (often placeholder 6666)
    * ``;TIME_ELAPSED:N`` — cumulative time at each layer (accurate)
    * ``;Filament used: X.XXm`` — filament usage
    * ``;MAXZ:N`` — maximum Z height
    """
    m_count = re.search(r";LAYER_COUNT:(\d+)", text)
    if not m_count:
        return text
    total = int(m_count.group(1))

    # Get total time: prefer last TIME_ELAPSED (accurate) over ;TIME: (often 6666 placeholder)
    elapsed_matches = re.findall(r";TIME_ELAPSED:([\d.]+)", text)
    if elapsed_matches:
        time_secs = int(float(elapsed_matches[-1]))
    else:
        m_time = re.search(r";TIME:(\d+)", text)
        time_secs = int(m_time.group(1)) if m_time else 0

    # Build per-layer elapsed time map for accurate M73 R values
    layer_elapsed: dict[int, float] = {}
    for m in re.finditer(r";LAYER:(\d+)", text):
        layer_n = int(m.group(1))
        # Find the next TIME_ELAPSED after this layer marker
        rest = text[m.end() : m.end() + 5000]
        te = re.search(r";TIME_ELAPSED:([\d.]+)", rest)
        if te:
            layer_elapsed[layer_n] = float(te.group(1))

    m_maxz = re.search(r";MAXZ:([\d.]+)", text)
    max_z = m_maxz.group(1) if m_maxz else None
    # CuraEngine sometimes emits garbage MAXZ (e.g. -2.14748e+06). Compute
    # from layer count × layer height instead.
    if max_z is None or float(max_z) <= 0:
        m_lh = re.search(r";Layer height:\s*([\d.]+)", text)
        layer_height = float(m_lh.group(1)) if m_lh else 0.2
        max_z = f"{total * layer_height:.1f}"

    header = _build_header_block(total, time_secs, max_z)
    # Initial M73 so firmware shows correct remaining time from the start
    # (before the first ;LAYER:0 marker). Without this, printer shows <1m.
    total_minutes = round(time_secs / 60)
    header += f"M73 P0 R{total_minutes}\n"
    text = header + text

    def _layer_sub(match: re.Match[str]) -> str:
        n = int(match.group(1))
        pct = round(n * 100 / total) if total > 0 else 0
        # Use per-layer elapsed if available, otherwise linear interpolation
        if n in layer_elapsed:
            remaining = max(0, round((time_secs - layer_elapsed[n]) / 60))
        elif total > 0:
            remaining = round(time_secs * (total - n) / total / 60)
        else:
            remaining = 0
        return (
            f"; layer num/total_layer_count: {n + 1}/{total}\n"
            f"M73 P{pct} R{remaining}\n"
            f"M73 L{n + 1}\n"
            f"M991 S0 P{n} ;notify layer change\n"
            f";LAYER:{n}"
        )

    text = re.sub(r";LAYER:(\d+)", _layer_sub, text)
    return text


def _translate_prusa(text: str) -> str:
    """Translate PrusaSlicer / SuperSlicer G-code to BBL format.

    PrusaSlicer emits:
    * ``;LAYER_CHANGE`` at each layer boundary
    * ``; total layers = N`` (SuperSlicer) or counts from LAYER_CHANGE
    * ``;HEIGHT:X.XX`` after LAYER_CHANGE
    * ``; estimated printing time ...``
    """
    # Count layers
    layer_changes = [m.start() for m in re.finditer(r"^;LAYER_CHANGE$", text, re.MULTILINE)]
    total = len(layer_changes)
    if total == 0:
        return text

    # Try to extract time estimate
    m_time = re.search(r"; estimated printing time[^=]*=\s*(.+)", text)
    time_secs = _parse_prusa_time(m_time.group(1)) if m_time else 0

    m_maxz = re.search(r";MAXZ:([\d.]+)", text)
    if not m_maxz:
        # PrusaSlicer uses ;HEIGHT: — grab the last one
        heights = re.findall(r";HEIGHT:([\d.]+)", text)
        max_z = heights[-1] if heights else "0"
    else:
        max_z = m_maxz.group(1)

    header = _build_header_block(total, time_secs, max_z)
    total_minutes = round(time_secs / 60)
    header += f"M73 P0 R{total_minutes}\n"
    text = header + text

    # Replace each ;LAYER_CHANGE with BBL markers
    layer_num = 0

    def _layer_sub(match: re.Match[str]) -> str:
        nonlocal layer_num
        pct = round(layer_num * 100 / total) if total > 0 else 0
        remaining = round(time_secs * (total - layer_num) / total / 60) if total > 0 else 0
        result = (
            f"; layer num/total_layer_count: {layer_num + 1}/{total}\n"
            f"M73 P{pct} R{remaining}\n"
            f"M73 L{layer_num + 1}\n"
            f"M991 S0 P{layer_num} ;notify layer change\n"
            f";LAYER_CHANGE"
        )
        layer_num += 1
        return result

    text = re.sub(r";LAYER_CHANGE", _layer_sub, text)
    return text


def _parse_prusa_time(time_str: str) -> int:
    """Parse PrusaSlicer time string like '1h 23m 45s' to seconds."""
    total = 0
    for match in re.finditer(r"(\d+)\s*h", time_str):
        total += int(match.group(1)) * 3600
    for match in re.finditer(r"(\d+)\s*m", time_str):
        total += int(match.group(1)) * 60
    for match in re.finditer(r"(\d+)\s*s", time_str):
        total += int(match.group(1))
    return total


_Z_MOVE_RE = re.compile(r"^G[01]\s.*Z([\d.]+)", re.MULTILINE)


def _translate_zchange(text: str) -> str:
    """Fallback translator: detect layers from Z-increasing G0/G1 moves.

    When no slicer-specific comments are recognised, we scan for G0/G1 moves
    whose Z value is strictly greater than the previous one and treat each
    such move as a layer boundary.  BBL firmware markers are injected so the
    printer can display layer progress.

    Time estimation is unavailable for unknown slicers so *time_secs* is 0.
    """
    # Collect Z values and their positions
    z_positions: list[tuple[int, float]] = []
    prev_z = -1.0
    for m in _Z_MOVE_RE.finditer(text):
        z = float(m.group(1))
        if z > prev_z:
            z_positions.append((m.start(), z))
            prev_z = z

    if not z_positions:
        return text

    total = len(z_positions)
    max_z = f"{z_positions[-1][1]:.2f}"

    header = _build_header_block(total, 0, max_z)
    header += "M73 P0 R0\n"

    # Inject layer markers in reverse order so earlier offsets stay valid
    for layer_num, (pos, _z) in enumerate(reversed(z_positions)):
        idx = total - 1 - layer_num  # original index
        pct = round(idx * 100 / total) if total > 0 else 0
        marker = (
            f"; layer num/total_layer_count: {idx + 1}/{total}\n"
            f"M73 P{pct} R0\n"
            f"M73 L{idx + 1}\n"
            f"M991 S0 P{idx} ;notify layer change\n"
        )
        text = text[:pos] + marker + text[pos:]

    return header + text


_TOOL_CHANGE_RE = re.compile(r"^T(\d+)\s*$", re.MULTILINE)


def rewrite_tool_changes(
    toolpath: str,
    project_settings: dict[str, object],
    machine: str = "p1s",
) -> str:
    """Replace CuraEngine ``T\\d+`` tool change commands with M620/M621 sequences.

    CuraEngine emits bare ``T0``/``T1`` commands for multi-filament prints.
    Bambu firmware requires M620/M621 sequences with temperature management,
    AMS loading, nozzle flush, and mechanical travel.

    For each ``T\\d+`` in the toolpath (excluding T255 and T1000 which are
    special AMS/flush commands), this function renders the machine's
    toolchange template with per-transition context and replaces the bare
    command.

    Args:
        toolpath: Raw toolpath G-code from CuraEngine.
        project_settings: The 544-key settings dict (arrays indexed by slot).
        machine: Machine profile name (e.g. "p1s").

    Returns:
        Toolpath with T commands replaced by M620/M621 sequences.
    """
    from bambox.templates import render_template

    # Find all tool changes (bare T0, T1, T2, T3 lines)
    matches = list(_TOOL_CHANGE_RE.finditer(toolpath))
    if not matches:
        return toolpath

    # Helper to safely get array value or scalar
    def _arr(key: str, idx: int, default: object = 0) -> object:
        val = project_settings.get(key, default)
        if isinstance(val, list):
            return val[idx] if idx < len(val) else val[0] if val else default
        return val

    def _num(key: str, idx: int, default: float = 0.0) -> float:
        v = _arr(key, idx, default)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                return default
        if v is None:
            return default
        if isinstance(v, (int, float)):
            return float(v)
        return default

    def _int(key: str, idx: int, default: int = 0) -> int:
        return int(_num(key, idx, float(default)))

    # Track Z positions sorted by file offset for bisect lookup
    z_positions: list[int] = []
    z_values: list[float] = []
    max_z = 0.0
    for m in re.finditer(r"G[01]\s.*Z([\d.]+)", toolpath):
        z = float(m.group(1))
        z_positions.append(m.start())
        z_values.append(z)
        if z > max_z:
            max_z = z

    # Detect initial extruder: if the first T command appears before any
    # extrusion move (G1 ... E), it's an extruder select, not a tool change.
    first_extrusion = re.search(r"G1\s.*E[\d.]", toolpath)
    first_extrusion_pos = first_extrusion.start() if first_extrusion else len(toolpath)
    skip_first = False
    current_extruder = 0
    if matches:
        first_t = matches[0]
        first_t_ext = int(first_t.group(1))
        if first_t_ext < 255 and first_t.start() < first_extrusion_pos:
            current_extruder = first_t_ext
            skip_first = True
    toolchange_count = 0

    # Build replacements in reverse order to preserve positions
    replacements: list[tuple[int, int, str]] = []

    for i, match in enumerate(matches):
        next_ext = int(match.group(1))
        # Skip special T commands
        if next_ext >= 255:
            continue
        # Skip initial extruder select (not a real tool change)
        if skip_first and i == 0:
            continue

        previous_ext = current_extruder
        toolchange_count += 1

        # Find the Z height at this point using bisect
        idx = bisect_right(z_positions, match.start()) - 1
        layer_z = z_values[idx] if idx >= 0 else 0.0

        # Compute flush lengths from flush_volumes_matrix
        fl1, fl2, fl3, fl4 = _compute_flush_lengths(project_settings, previous_ext, next_ext)

        ctx: dict[str, object] = {
            "next_extruder": next_ext,
            "previous_extruder": previous_ext,
            "current_extruder": current_extruder,
            "toolchange_count": toolchange_count,
            "max_layer_z": max_z,
            "layer_z": layer_z,
            # Temperature
            "old_filament_temp": _int("nozzle_temperature", previous_ext, 220),
            "new_filament_temp": _int("nozzle_temperature", next_ext, 220),
            # Feedrates
            "old_filament_e_feedrate": _int("filament_max_volumetric_speed", previous_ext, 12),
            "new_filament_e_feedrate": _int("filament_max_volumetric_speed", next_ext, 12),
            # Retraction
            "old_retract_length_toolchange": _num("retract_length_toolchange", previous_ext, 2.0),
            "new_retract_length_toolchange": _num("retract_length_toolchange", next_ext, 2.0),
            # Flush lengths from flush_volumes_matrix
            "flush_length_1": fl1,
            "flush_length_2": fl2,
            "flush_length_3": fl3,
            "flush_length_4": fl4,
            # Arrays that the template indexes
            "z_hop_types": _coerce_array(project_settings.get("z_hop_types", [0, 0, 0, 0, 0])),
            "long_retractions_when_cut": _coerce_array(
                project_settings.get("long_retractions_when_cut", [0, 0, 0, 0, 0])
            ),
            "retraction_distances_when_cut": _coerce_array(
                project_settings.get("retraction_distances_when_cut", [18, 18, 18, 18, 18])
            ),
            "nozzle_temperature_range_high": _coerce_array(
                project_settings.get("nozzle_temperature_range_high", [240, 240, 240, 240, 240])
            ),
            "filament_type": project_settings.get(
                "filament_type", ["PLA", "PLA", "PLA", "PLA", "PLA"]
            ),
            # Acceleration
            "default_acceleration": _int("default_acceleration", 0, 10000),
            "initial_layer_acceleration": _int("initial_layer_acceleration", 0, 500),
            "initial_layer_print_height": _num("initial_layer_print_height", 0, 0.2),
            # Fallback positions (used when next_extruder >= 255)
            "x_after_toolchange": 100,
            "y_after_toolchange": 100,
            "z_after_toolchange": layer_z + 2.0,
            # Travel points (used on second tool change)
            "travel_point_1_x": 20,
            "travel_point_1_y": 50,
            "travel_point_2_x": 60,
            "travel_point_2_y": 245,
            "travel_point_3_x": 70,
            "travel_point_3_y": 265,
        }

        rendered = render_template(f"{machine}_toolchange.gcode.j2", ctx)
        replacements.append((match.start(), match.end(), rendered.rstrip("\n")))
        current_extruder = next_ext

    # Apply replacements in reverse to preserve positions
    result = toolpath
    for start, end, replacement in reversed(replacements):
        result = result[:start] + replacement + result[end:]

    return result


def _compute_flush_lengths(
    project_settings: dict[str, object],
    from_ext: int,
    to_ext: int,
) -> tuple[float, float, float, float]:
    """Compute per-stage flush lengths from ``flush_volumes_matrix``.

    The matrix is a flattened N×N array where ``matrix[from * N + to]`` gives
    the flush volume (mm³) for a filament transition.  The result is scaled by
    ``flush_multiplier``, converted to filament extrusion length (mm), and
    split across four pulsatile flush stages.
    """
    matrix_raw = project_settings.get("flush_volumes_matrix")
    if isinstance(matrix_raw, list) and matrix_raw:
        n = int(math.isqrt(len(matrix_raw)))
        if n * n == len(matrix_raw) and from_ext < n and to_ext < n:
            try:
                volume = float(matrix_raw[from_ext * n + to_ext])
            except (ValueError, TypeError):
                log.warning(
                    "flush_volumes_matrix[%d→%d] is not numeric — using default %.0f mm³",
                    from_ext,
                    to_ext,
                    _DEFAULT_FLUSH_VOLUME,
                )
                volume = _DEFAULT_FLUSH_VOLUME
        else:
            log.warning(
                "flush_volumes_matrix has invalid dimensions or extruder index "
                "out of range (from=%d, to=%d, matrix size=%d) — "
                "using default %.0f mm³",
                from_ext,
                to_ext,
                len(matrix_raw),
                _DEFAULT_FLUSH_VOLUME,
            )
            volume = _DEFAULT_FLUSH_VOLUME
    else:
        log.warning(
            "flush_volumes_matrix missing or empty in project settings — "
            "using default %.0f mm³ for all transitions. "
            "Set flush_volumes_matrix in your machine profile for accurate purging.",
            _DEFAULT_FLUSH_VOLUME,
        )
        volume = _DEFAULT_FLUSH_VOLUME

    multiplier_raw = project_settings.get("flush_multiplier", "0.3")
    try:
        multiplier = float(multiplier_raw)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        multiplier = 0.3

    total_volume = volume * multiplier  # mm³

    # Convert volume to filament extrusion length: length = volume / (π × r²)
    filament_area = math.pi * (_FILAMENT_DIAMETER / 2.0) ** 2  # ≈ 2.405 mm²
    total_length = total_volume / filament_area

    # Split across four flush stages (OrcaSlicer-compatible ratios)
    fl1 = total_length * 0.35
    fl2 = total_length * 0.35
    fl3 = total_length * 0.18
    fl4 = total_length * 0.12
    return fl1, fl2, fl3, fl4


def _coerce_array(val: object) -> list[object]:
    """Ensure a value is a list, coercing string elements to numbers."""
    if not isinstance(val, list):
        return [val] * 5
    result: list[object] = []
    for item in val:
        if isinstance(item, str):
            try:
                result.append(int(item))
            except ValueError:
                try:
                    result.append(float(item))
                except ValueError:
                    result.append(item)
        else:
            result.append(item)
    return result


def _build_header_block(total_layers: int, time_secs: int, max_z: str) -> str:
    """Build a BBL-compatible HEADER_BLOCK."""
    mins, secs = divmod(time_secs, 60)
    hrs, mins = divmod(mins, 60)
    if hrs:
        time_str = f"{hrs}h {mins}m {secs}s"
    else:
        time_str = f"{mins}m {secs}s"

    return (
        "; HEADER_BLOCK_START\n"
        "; generated by bambox (generic slicer G-code translated for BBL firmware)\n"
        f"; total estimated time: {time_str}\n"
        f"; total layer number: {total_layers}\n"
        "; filament_diameter: 1.75\n"
        f"; max_z_height: {max_z}\n"
        "; HEADER_BLOCK_END\n"
    )

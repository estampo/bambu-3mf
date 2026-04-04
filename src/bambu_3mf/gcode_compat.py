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

import re


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
    # else: unknown slicer — return as-is (best effort)

    return text.encode()


def _translate_cura(text: str) -> str:
    """Translate CuraEngine G-code to BBL format.

    CuraEngine emits:
    * ``;LAYER_COUNT:N`` — total layers
    * ``;LAYER:N`` — 0-based layer index
    * ``;TIME:N`` — total print time in seconds
    * ``;Filament used: X.XXm`` — filament usage
    * ``;MAXZ:N`` — maximum Z height
    """
    m_count = re.search(r";LAYER_COUNT:(\d+)", text)
    if not m_count:
        return text
    total = int(m_count.group(1))

    m_time = re.search(r";TIME:(\d+)", text)
    time_secs = int(m_time.group(1)) if m_time else 0

    m_maxz = re.search(r";MAXZ:([\d.]+)", text)
    max_z = m_maxz.group(1) if m_maxz else "0"

    header = _build_header_block(total, time_secs, max_z)
    text = header + text

    def _layer_sub(match: re.Match[str]) -> str:
        n = int(match.group(1))
        pct = round(n * 100 / total) if total > 0 else 0
        remaining = round(time_secs * (total - n) / total / 60) if total > 0 else 0
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
        "; generated by bambu-3mf (generic slicer G-code translated for BBL firmware)\n"
        f"; total estimated time: {time_str}\n"
        f"; total layer number: {total_layers}\n"
        "; filament_diameter: 1.75\n"
        f"; max_z_height: {max_z}\n"
        "; HEADER_BLOCK_END\n"
    )

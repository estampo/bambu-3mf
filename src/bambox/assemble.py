"""Assemble G-code from rendered templates and naked toolpath."""

from __future__ import annotations


def assemble_gcode(
    start_gcode: str,
    toolpath: str,
    end_gcode: str,
    *,
    filament_start_gcode: str = "",
    filament_end_gcode: str = "",
) -> str:
    """Assemble a complete G-code file from components.

    Args:
        start_gcode: Rendered machine start template (e.g. P1S init sequence).
        toolpath: Raw toolpath G-code from the slicer (layer moves only).
        end_gcode: Rendered machine end template (e.g. P1S shutdown sequence).
        filament_start_gcode: Rendered filament start template. Inserted
            between start_gcode and toolpath.
        filament_end_gcode: Rendered filament end template. Inserted
            between toolpath and end_gcode.

    Returns:
        Complete G-code string ready for packaging.
    """
    parts: list[str] = []

    if start_gcode:
        parts.append(start_gcode.rstrip("\n"))

    if filament_start_gcode:
        parts.append("; filament start gcode")
        parts.append(filament_start_gcode.rstrip("\n"))

    if toolpath:
        parts.append(toolpath.rstrip("\n"))

    if filament_end_gcode:
        parts.append("; filament end gcode")
        parts.append(filament_end_gcode.rstrip("\n"))

    if end_gcode:
        parts.append(end_gcode.rstrip("\n"))

    return "\n".join(parts) + "\n"

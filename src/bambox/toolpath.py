"""Simple toolpath generator for basic shapes.

Generates valid G-code toolpaths for testing. NOT a replacement for a
real slicer — produces single-perimeter walls with rectilinear infill.
"""

from __future__ import annotations

import math


def rectangular_prism(
    width: float = 10.0,
    depth: float = 10.0,
    height: float = 5.0,
    *,
    layer_height: float = 0.2,
    first_layer_height: float = 0.2,
    nozzle_diameter: float = 0.4,
    filament_diameter: float = 1.75,
    bed_center_x: float = 128.0,
    bed_center_y: float = 128.0,
    print_speed: float = 60.0,
    first_layer_speed: float = 30.0,
    travel_speed: float = 150.0,
    retract_length: float = 0.8,
    retract_speed: float = 30.0,
    infill_density: float = 0.15,
) -> str:
    """Generate G-code toolpath for a solid rectangular prism.

    Args:
        width: X dimension in mm.
        depth: Y dimension in mm.
        height: Z dimension in mm.
        layer_height: Layer height in mm.
        first_layer_height: First layer height in mm.
        nozzle_diameter: Nozzle diameter in mm.
        filament_diameter: Filament diameter in mm.
        bed_center_x: Bed center X coordinate.
        bed_center_y: Bed center Y coordinate.
        print_speed: Print speed in mm/s.
        first_layer_speed: First layer speed in mm/s.
        travel_speed: Travel speed in mm/s.
        retract_length: Retraction length in mm.
        retract_speed: Retraction speed in mm/s.
        infill_density: Infill density (0-1).

    Returns:
        G-code string (toolpath only, no machine start/end).
    """
    # Extrusion calculation: E per mm of XY travel
    # E = (layer_height * nozzle_diameter) / (pi/4 * filament_diameter^2)
    filament_area = math.pi / 4 * filament_diameter**2

    # Part origin (bottom-left corner)
    ox = bed_center_x - width / 2
    oy = bed_center_y - depth / 2

    lines: list[str] = []
    lines.append("M981 S1 P20000 ;open spaghetti detector")

    # Calculate layers
    layers: list[float] = []
    z = first_layer_height
    layers.append(z)
    while z + layer_height <= height + 0.001:
        z += layer_height
        layers.append(round(z, 4))

    e_total = 0.0
    retracted = False

    def retract() -> None:
        nonlocal e_total, retracted
        if not retracted:
            e_total -= retract_length
            lines.append(f"G1 E{e_total:.5f} F{retract_speed * 60:.0f}")
            retracted = True

    def unretract() -> None:
        nonlocal e_total, retracted
        if retracted:
            e_total += retract_length
            lines.append(f"G1 E{e_total:.5f} F{retract_speed * 60:.0f}")
            retracted = False

    def move_to(x: float, y: float) -> None:
        lines.append(f"G0 X{x:.3f} Y{y:.3f} F{travel_speed * 60:.0f}")

    def extrude_to(x: float, y: float, lh: float, speed: float) -> None:
        nonlocal e_total
        dx = x - cur_x
        dy = y - cur_y
        dist = math.sqrt(dx * dx + dy * dy)
        e_per_mm = (lh * nozzle_diameter) / filament_area
        e_total += dist * e_per_mm
        lines.append(f"G1 X{x:.3f} Y{y:.3f} E{e_total:.5f} F{speed * 60:.0f}")

    for layer_idx, z in enumerate(layers):
        lh = first_layer_height if layer_idx == 0 else layer_height
        speed = first_layer_speed if layer_idx == 0 else print_speed

        lines.append("; CHANGE_LAYER")
        lines.append(f"; Z_HEIGHT: {z}")
        lines.append(f"; LAYER_HEIGHT: {lh}")

        # Move to layer Z
        retract()
        lines.append(f"G1 Z{z:.3f} F{travel_speed * 60:.0f}")
        lines.append(f"; layer num/total_layer_count: {layer_idx + 1}/{len(layers)}")

        # Perimeter: outer wall
        lines.append("; FEATURE: Outer wall")
        # Inset by half nozzle width
        inset = nozzle_diameter / 2
        x0 = ox + inset
        y0 = oy + inset
        x1 = ox + width - inset
        y1 = oy + depth - inset

        move_to(x0, y0)
        unretract()
        cur_x, cur_y = x0, y0
        for px, py in [(x1, y0), (x1, y1), (x0, y1), (x0, y0)]:
            extrude_to(px, py, lh, speed)
            cur_x, cur_y = px, py

        # Bottom/top solid layers (first 3 and last 3)
        is_solid = layer_idx < 3 or layer_idx >= len(layers) - 3

        if is_solid:
            lines.append("; FEATURE: Solid infill")
            # Rectilinear fill: alternate X and Y direction each layer
            fill_inset = nozzle_diameter
            fx0 = ox + fill_inset
            fy0 = oy + fill_inset
            fx1 = ox + width - fill_inset
            fy1 = oy + depth - fill_inset
            line_spacing = nozzle_diameter * 0.95

            if layer_idx % 2 == 0:
                # Lines parallel to X
                y_pos = fy0
                forward = True
                while y_pos <= fy1:
                    sx, ex = (fx0, fx1) if forward else (fx1, fx0)
                    move_to(sx, y_pos)
                    unretract()
                    cur_x, cur_y = sx, y_pos
                    extrude_to(ex, y_pos, lh, speed)
                    cur_x, cur_y = ex, y_pos
                    retract()
                    y_pos += line_spacing
                    forward = not forward
            else:
                # Lines parallel to Y
                x_pos = fx0
                forward = True
                while x_pos <= fx1:
                    sy, ey = (fy0, fy1) if forward else (fy1, fy0)
                    move_to(x_pos, sy)
                    unretract()
                    cur_x, cur_y = x_pos, sy
                    extrude_to(x_pos, ey, lh, speed)
                    cur_x, cur_y = x_pos, ey
                    retract()
                    x_pos += line_spacing
                    forward = not forward
        elif infill_density > 0:
            lines.append("; FEATURE: Sparse infill")
            fill_inset = nozzle_diameter
            fx0 = ox + fill_inset
            fy0 = oy + fill_inset
            fx1 = ox + width - fill_inset
            fy1 = oy + depth - fill_inset
            line_spacing = nozzle_diameter / infill_density

            if layer_idx % 2 == 0:
                y_pos = fy0
                forward = True
                while y_pos <= fy1:
                    sx, ex = (fx0, fx1) if forward else (fx1, fx0)
                    move_to(sx, y_pos)
                    unretract()
                    cur_x, cur_y = sx, y_pos
                    extrude_to(ex, y_pos, lh, speed)
                    cur_x, cur_y = ex, y_pos
                    retract()
                    y_pos += line_spacing
                    forward = not forward
            else:
                x_pos = fx0
                forward = True
                while x_pos <= fx1:
                    sy, ey = (fy0, fy1) if forward else (fy1, fy0)
                    move_to(x_pos, sy)
                    unretract()
                    cur_x, cur_y = x_pos, sy
                    extrude_to(x_pos, ey, lh, speed)
                    cur_x, cur_y = x_pos, ey
                    retract()
                    x_pos += line_spacing
                    forward = not forward

    retract()
    lines.append("M106 S0")
    lines.append("M106 P2 S0")
    lines.append("M981 S0 P20000 ; close spaghetti detector")

    return "\n".join(lines) + "\n"

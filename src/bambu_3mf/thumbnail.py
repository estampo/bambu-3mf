"""Generate thumbnails from G-code toolpath for .gcode.3mf files."""

from __future__ import annotations

import io
import re


def gcode_thumbnail(
    gcode: str | bytes,
    width: int = 256,
    height: int = 256,
) -> bytes:
    """Render a top-down toolpath preview from G-code as a PNG.

    Parses G0/G1 moves, draws extrusion moves as colored lines on a dark
    background with a bed outline. Returns PNG bytes.
    """
    from PIL import Image, ImageDraw

    if isinstance(gcode, bytes):
        gcode = gcode.decode(errors="replace")

    # Parse G0/G1 moves
    moves: list[tuple[float, float, float, float, bool]] = []  # x0,y0,x1,y1,extrude
    x = y = 0.0
    _g_re = re.compile(
        r"^G[01]\s", re.IGNORECASE
    )
    _x_re = re.compile(r"X([-\d.]+)", re.IGNORECASE)
    _y_re = re.compile(r"Y([-\d.]+)", re.IGNORECASE)
    _e_re = re.compile(r"E([-\d.]+)", re.IGNORECASE)

    for line in gcode.splitlines():
        line = line.strip()
        if not _g_re.match(line):
            continue
        xm = _x_re.search(line)
        ym = _y_re.search(line)
        em = _e_re.search(line)
        nx = float(xm.group(1)) if xm else x
        ny = float(ym.group(1)) if ym else y
        extrude = em is not None and float(em.group(1)) > 0
        if (nx != x or ny != y):
            moves.append((x, y, nx, ny, extrude))
        x, y = nx, ny

    if not moves:
        return _placeholder(width, height)

    # Compute bounding box
    all_x = [m[0] for m in moves] + [m[2] for m in moves]
    all_y = [m[1] for m in moves] + [m[3] for m in moves]
    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)

    # Scale to fit with margin
    margin = 20
    draw_w = width - 2 * margin
    draw_h = height - 2 * margin
    extent_x = max(xmax - xmin, 1)
    extent_y = max(ymax - ymin, 1)
    scale = min(draw_w / extent_x, draw_h / extent_y)
    ox = margin + (draw_w - extent_x * scale) / 2
    oy = margin + (draw_h - extent_y * scale) / 2

    def px(mx: float, my: float) -> tuple[int, int]:
        return (int(ox + (mx - xmin) * scale), int(oy + (ymax - my) * scale))

    # Draw
    bg = (25, 25, 30)
    bed_color = (55, 58, 65)
    travel_color = (60, 60, 70)
    extrude_color = (0, 180, 160)  # teal

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # Bed outline (P1S: 256x256, centered at 128,128)
    bed_tl = px(0, 256)
    bed_br = px(256, 0)
    if bed_tl[0] < bed_br[0] and bed_tl[1] < bed_br[1]:
        draw.rectangle([bed_tl, bed_br], fill=bed_color, outline=(75, 78, 85))

    # Draw moves
    for x0, y0, x1, y1, ext in moves:
        p0 = px(x0, y0)
        p1 = px(x1, y1)
        color = extrude_color if ext else travel_color
        draw.line([p0, p1], fill=color, width=1)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _placeholder(width: int = 256, height: int = 256) -> bytes:
    """Minimal branded placeholder when no toolpath data is available."""
    from PIL import Image, ImageDraw

    bg = (25, 25, 30)
    accent = (0, 150, 136)
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # Simple bed rectangle with accent border
    m = 20
    draw.rectangle([(m, m), (width - m, height - m)], fill=(50, 52, 58), outline=accent)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

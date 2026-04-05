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

    Parses G0/G1 extrusion moves from the print body (skipping startup
    G-code) and draws them on a dark background. Returns PNG bytes.
    """
    from PIL import Image, ImageDraw

    if isinstance(gcode, bytes):
        gcode = gcode.decode(errors="replace")

    # Only render moves after the first Z_HEIGHT marker (skip startup/purge).
    # BBL slicers emit "; Z_HEIGHT: <n>" at each layer.
    z_height_idx = gcode.find("; Z_HEIGHT:")
    if z_height_idx >= 0:
        gcode = gcode[z_height_idx:]

    # Parse G0/G1 extrusion moves only
    moves: list[tuple[float, float, float, float]] = []  # x0,y0,x1,y1
    x = y = 0.0
    _g_re = re.compile(r"^G[01]\s", re.IGNORECASE)
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
        if extrude and (nx != x or ny != y):
            moves.append((x, y, nx, ny))
        x, y = nx, ny

    if not moves:
        return _placeholder(width, height)

    # Bounding box of print moves
    all_x = [m[0] for m in moves] + [m[2] for m in moves]
    all_y = [m[1] for m in moves] + [m[3] for m in moves]
    xmin, xmax = min(all_x), max(all_x)
    ymin, ymax = min(all_y), max(all_y)

    # Add padding around the print (15% of extent, minimum 2mm)
    pad_x = max((xmax - xmin) * 0.15, 2.0)
    pad_y = max((ymax - ymin) * 0.15, 2.0)
    xmin -= pad_x
    xmax += pad_x
    ymin -= pad_y
    ymax += pad_y

    # Scale to fit image
    extent_x = max(xmax - xmin, 0.1)
    extent_y = max(ymax - ymin, 0.1)
    scale = min(width / extent_x, height / extent_y)
    ox = (width - extent_x * scale) / 2
    oy = (height - extent_y * scale) / 2

    def px(mx: float, my: float) -> tuple[int, int]:
        return (int(ox + (mx - xmin) * scale), int(oy + (ymax - my) * scale))

    # Colours
    bg = (25, 25, 30)
    extrude_color = (0, 180, 160)  # teal

    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)

    # Draw extrusion moves
    for x0, y0, x1, y1 in moves:
        p0 = px(x0, y0)
        p1 = px(x1, y1)
        draw.line([p0, p1], fill=extrude_color, width=1)

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

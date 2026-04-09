"""Tests for thumbnail.py — G-code to PNG rendering."""

from __future__ import annotations

import io

from PIL import Image

from bambox.thumbnail import _placeholder, gcode_thumbnail

# -- Helpers ------------------------------------------------------------------

SIMPLE_GCODE = """\
; Z_HEIGHT: 0.2
G1 X10 Y10 E0.5
G1 X20 Y10 E0.5
G1 X20 Y20 E0.5
G1 X10 Y20 E0.5
G1 X10 Y10 E0.5
"""

MULTI_LAYER_GCODE = """\
; Z_HEIGHT: 0.2
G1 X10 Y10 E0.5
G1 X50 Y10 E0.5
G1 X50 Y50 E0.5
G1 X10 Y50 E0.5
; Z_HEIGHT: 0.4
G1 X10 Y10 E0.5
G1 X50 Y10 E0.5
G1 X50 Y50 E0.5
"""


def _open_png(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


# -- gcode_thumbnail ----------------------------------------------------------


class TestGcodeThumbnail:
    def test_returns_valid_png(self):
        """Output must be a valid PNG image."""
        result = gcode_thumbnail(SIMPLE_GCODE)
        img = _open_png(result)
        assert img.format == "PNG"

    def test_default_dimensions(self):
        """Default output is 256x256."""
        img = _open_png(gcode_thumbnail(SIMPLE_GCODE))
        assert img.size == (256, 256)

    def test_custom_dimensions(self):
        """Custom width/height are respected."""
        img = _open_png(gcode_thumbnail(SIMPLE_GCODE, width=128, height=64))
        assert img.size == (128, 64)

    def test_accepts_bytes_input(self):
        """bytes G-code should work identically to str."""
        result = gcode_thumbnail(SIMPLE_GCODE.encode())
        img = _open_png(result)
        assert img.size == (256, 256)

    def test_empty_gcode_returns_placeholder(self):
        """Empty input should produce a placeholder image, not crash."""
        result = gcode_thumbnail("")
        img = _open_png(result)
        assert img.size == (256, 256)

    def test_no_extrusion_returns_placeholder(self):
        """Moves without E parameter are travel moves — no toolpath to render."""
        gcode = """\
; Z_HEIGHT: 0.2
G1 X10 Y10 F3000
G1 X20 Y20 F3000
"""
        result = gcode_thumbnail(gcode)
        # Should be same as placeholder (no extrusion moves parsed)
        placeholder = _placeholder(256, 256)
        assert result == placeholder

    def test_negative_extrusion_not_rendered(self):
        """Retraction moves (negative E) should not produce toolpath lines."""
        gcode = """\
; Z_HEIGHT: 0.2
G1 X10 Y10 E-0.8
G1 X20 Y20 E-0.8
"""
        result = gcode_thumbnail(gcode)
        assert result == _placeholder(256, 256)

    def test_startup_gcode_skipped(self):
        """G-code before the first Z_HEIGHT marker should be ignored."""
        gcode = """\
G28
G1 X50 Y50 E5.0
M104 S200
; Z_HEIGHT: 0.2
G1 X10 Y10 E0.5
G1 X20 Y10 E0.5
"""
        result = gcode_thumbnail(gcode)
        # Should render — there's extrusion after Z_HEIGHT
        assert result != _placeholder(256, 256)

    def test_no_z_height_marker(self):
        """If no Z_HEIGHT marker exists, all G-code is parsed."""
        gcode = """\
G1 X10 Y10 E0.5
G1 X20 Y10 E0.5
G1 X20 Y20 E0.5
"""
        result = gcode_thumbnail(gcode)
        # Should still render extrusion moves
        assert result != _placeholder(256, 256)

    def test_multi_layer_renders(self):
        """Multi-layer G-code should produce a valid image."""
        result = gcode_thumbnail(MULTI_LAYER_GCODE)
        img = _open_png(result)
        assert img.size == (256, 256)
        assert result != _placeholder(256, 256)

    def test_extrude_color_present(self):
        """Rendered image should contain the teal extrusion color."""
        result = gcode_thumbnail(SIMPLE_GCODE)
        img = _open_png(result)
        raw = img.tobytes()
        found = any(
            raw[i] == 0 and raw[i + 1] == 180 and raw[i + 2] == 160
            for i in range(0, len(raw) - 2, 3)
        )
        assert found, "Extrusion color (teal) not found in rendered image"

    def test_background_color(self):
        """Background should be dark (25, 25, 30)."""
        result = gcode_thumbnail(SIMPLE_GCODE)
        img = _open_png(result)
        bg = (25, 25, 30)
        # Corner pixel should be background
        assert img.getpixel((0, 0)) == bg

    def test_case_insensitive_gcode(self):
        """G-code commands should be parsed case-insensitively."""
        gcode = """\
; Z_HEIGHT: 0.2
g1 x10 y10 e0.5
g1 x20 y10 e0.5
"""
        result = gcode_thumbnail(gcode)
        assert result != _placeholder(256, 256)

    def test_g0_moves_with_extrusion(self):
        """G0 rapid moves with E should also be rendered."""
        gcode = """\
; Z_HEIGHT: 0.2
G0 X10 Y10 E0.5
G0 X20 Y10 E0.5
"""
        result = gcode_thumbnail(gcode)
        assert result != _placeholder(256, 256)

    def test_single_point_no_crash(self):
        """A single extrusion move from origin should not crash."""
        gcode = """\
; Z_HEIGHT: 0.2
G1 X10 Y10 E0.5
"""
        result = gcode_thumbnail(gcode)
        img = _open_png(result)
        assert img.size == (256, 256)


# -- _placeholder --------------------------------------------------------------


class TestPlaceholder:
    def test_returns_valid_png(self):
        result = _placeholder()
        img = _open_png(result)
        assert img.format == "PNG"

    def test_default_dimensions(self):
        img = _open_png(_placeholder())
        assert img.size == (256, 256)

    def test_custom_dimensions(self):
        img = _open_png(_placeholder(128, 64))
        assert img.size == (128, 64)

    def test_background_color(self):
        img = _open_png(_placeholder())
        assert img.getpixel((0, 0)) == (25, 25, 30)

    def test_accent_border_present(self):
        """The bed rectangle outline should use the accent color."""
        img = _open_png(_placeholder())
        raw = img.tobytes()
        found = any(
            raw[i] == 0 and raw[i + 1] == 150 and raw[i + 2] == 136
            for i in range(0, len(raw) - 2, 3)
        )
        assert found, "Accent border color not found in placeholder"

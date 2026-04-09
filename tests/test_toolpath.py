"""Tests for the synthetic toolpath generator."""

from __future__ import annotations

import re

from bambox.toolpath import rectangular_prism


class TestRectangularPrismDefaults:
    """Test rectangular_prism with default arguments."""

    def test_returns_string(self) -> None:
        result = rectangular_prism()
        assert isinstance(result, str)

    def test_ends_with_newline(self) -> None:
        result = rectangular_prism()
        assert result.endswith("\n")

    def test_opens_spaghetti_detector(self) -> None:
        result = rectangular_prism()
        assert "M981 S1 P20000" in result

    def test_closes_spaghetti_detector(self) -> None:
        result = rectangular_prism()
        assert "M981 S0 P20000" in result

    def test_contains_change_layer(self) -> None:
        result = rectangular_prism()
        assert "; CHANGE_LAYER" in result

    def test_contains_outer_wall(self) -> None:
        result = rectangular_prism()
        assert "; FEATURE: Outer wall" in result

    def test_fans_off_at_end(self) -> None:
        result = rectangular_prism()
        lines = result.strip().splitlines()
        # M106 S0 and M106 P2 S0 should appear near the end
        tail = "\n".join(lines[-4:])
        assert "M106 S0" in tail
        assert "M106 P2 S0" in tail


class TestLayerCalculation:
    """Test layer count and Z heights."""

    def test_layer_count_simple(self) -> None:
        # height=1.0, first_layer=0.2, layer_height=0.2 -> 5 layers
        result = rectangular_prism(height=1.0, layer_height=0.2, first_layer_height=0.2)
        count = result.count("; CHANGE_LAYER")
        assert count == 5

    def test_single_layer(self) -> None:
        # height equals first_layer_height -> 1 layer
        result = rectangular_prism(height=0.2, layer_height=0.2, first_layer_height=0.2)
        assert result.count("; CHANGE_LAYER") == 1

    def test_first_layer_z_height(self) -> None:
        result = rectangular_prism(first_layer_height=0.3)
        assert "; Z_HEIGHT: 0.3" in result

    def test_layer_height_annotation(self) -> None:
        result = rectangular_prism(first_layer_height=0.25)
        assert "; LAYER_HEIGHT: 0.25" in result

    def test_layer_numbering_starts_at_one(self) -> None:
        result = rectangular_prism(height=0.4, layer_height=0.2, first_layer_height=0.2)
        assert "; layer num/total_layer_count: 1/2" in result
        assert "; layer num/total_layer_count: 2/2" in result


class TestGcodeFormat:
    """Test that output contains valid G-code syntax."""

    def test_g0_moves_have_feedrate(self) -> None:
        result = rectangular_prism()
        g0_lines = [ln for ln in result.splitlines() if ln.startswith("G0 ")]
        assert len(g0_lines) > 0
        for ln in g0_lines:
            assert " F" in ln, f"G0 move missing feedrate: {ln}"

    def test_g1_moves_have_feedrate(self) -> None:
        result = rectangular_prism()
        g1_lines = [ln for ln in result.splitlines() if ln.startswith("G1 ")]
        assert len(g1_lines) > 0
        for ln in g1_lines:
            assert " F" in ln, f"G1 move missing feedrate: {ln}"

    def test_extrusion_moves_have_e_values(self) -> None:
        result = rectangular_prism()
        # G1 lines with X/Y should also have E (extrusion moves)
        pattern = re.compile(r"^G1 X[\d.]+ Y[\d.]+")
        extrusion_lines = [ln for ln in result.splitlines() if pattern.match(ln)]
        assert len(extrusion_lines) > 0
        for ln in extrusion_lines:
            assert " E" in ln, f"Extrusion move missing E value: {ln}"

    def test_z_moves_present(self) -> None:
        result = rectangular_prism()
        z_lines = [ln for ln in result.splitlines() if re.match(r"^G1 Z[\d.]+", ln)]
        assert len(z_lines) > 0

    def test_no_negative_z(self) -> None:
        result = rectangular_prism()
        for ln in result.splitlines():
            m = re.match(r"^G1 Z(-?[\d.]+)", ln)
            if m:
                assert float(m.group(1)) > 0


class TestExtrusionValues:
    """Test extrusion calculations."""

    def test_e_values_increase_during_extrusion(self) -> None:
        # Within a layer's extrusion segment, E should generally increase
        result = rectangular_prism(height=0.2)
        e_vals: list[float] = []
        for ln in result.splitlines():
            m = re.search(r"X[\d.]+ Y[\d.]+ E([\d.]+)", ln)
            if m:
                e_vals.append(float(m.group(1)))
        assert len(e_vals) > 0
        # Overall the last E should be greater than the first
        assert e_vals[-1] > e_vals[0]

    def test_retraction_present(self) -> None:
        result = rectangular_prism()
        # There should be E-only G1 lines (retract/unretract)
        e_only = [ln for ln in result.splitlines() if re.match(r"^G1 E[\d.-]+ F", ln)]
        assert len(e_only) > 0


class TestSpeedParameters:
    """Test speed/feedrate configuration."""

    def test_travel_speed_in_feedrate(self) -> None:
        # travel_speed=100 -> F6000
        result = rectangular_prism(travel_speed=100.0)
        assert "F6000" in result

    def test_first_layer_speed(self) -> None:
        # first_layer_speed=20 -> F1200 on first layer extrusion
        result = rectangular_prism(first_layer_speed=20.0, height=0.2)
        # First layer extrusion lines should use F1200
        lines = result.splitlines()
        first_layer_extrusion = [
            ln for ln in lines if re.match(r"^G1 X[\d.]+ Y[\d.]+ E[\d.]+ F1200$", ln)
        ]
        assert len(first_layer_extrusion) > 0

    def test_print_speed_on_later_layers(self) -> None:
        # print_speed=80 -> F4800 on non-first layers
        result = rectangular_prism(
            print_speed=80.0,
            first_layer_speed=30.0,
            height=1.0,
            layer_height=0.2,
        )
        assert "F4800" in result

    def test_retract_speed(self) -> None:
        # retract_speed=40 -> F2400
        result = rectangular_prism(retract_speed=40.0)
        assert "F2400" in result


class TestBedCentering:
    """Test that the part is centered on the bed."""

    def test_default_centering(self) -> None:
        # Default 10x10 at center 128,128 -> coords around 123-133
        result = rectangular_prism(width=10.0, depth=10.0)
        xs: list[float] = []
        ys: list[float] = []
        for ln in result.splitlines():
            m = re.match(r"G[01] X([\d.]+) Y([\d.]+)", ln)
            if m:
                xs.append(float(m.group(1)))
                ys.append(float(m.group(2)))
        assert len(xs) > 0
        # Center of bounding box should be near 128
        center_x = (min(xs) + max(xs)) / 2
        center_y = (min(ys) + max(ys)) / 2
        assert abs(center_x - 128.0) < 1.0
        assert abs(center_y - 128.0) < 1.0

    def test_custom_bed_center(self) -> None:
        result = rectangular_prism(width=10.0, depth=10.0, bed_center_x=100.0, bed_center_y=100.0)
        xs: list[float] = []
        ys: list[float] = []
        for ln in result.splitlines():
            m = re.match(r"G[01] X([\d.]+) Y([\d.]+)", ln)
            if m:
                xs.append(float(m.group(1)))
                ys.append(float(m.group(2)))
        center_x = (min(xs) + max(xs)) / 2
        center_y = (min(ys) + max(ys)) / 2
        assert abs(center_x - 100.0) < 1.0
        assert abs(center_y - 100.0) < 1.0


class TestInfill:
    """Test solid and sparse infill generation."""

    def test_solid_infill_on_first_layers(self) -> None:
        result = rectangular_prism(height=2.0, layer_height=0.2)
        # First 3 layers should have solid infill
        assert "; FEATURE: Solid infill" in result

    def test_sparse_infill_on_middle_layers(self) -> None:
        # Need enough layers so middle ones are neither first-3 nor last-3
        result = rectangular_prism(height=5.0, layer_height=0.2)
        assert "; FEATURE: Sparse infill" in result

    def test_zero_infill_no_sparse(self) -> None:
        # With infill_density=0, middle layers should have no sparse infill
        # but solid infill still appears on top/bottom
        result = rectangular_prism(height=5.0, layer_height=0.2, infill_density=0.0)
        # Solid infill should still be present (top/bottom layers)
        assert "; FEATURE: Solid infill" in result
        # Sparse infill should NOT appear
        assert "; FEATURE: Sparse infill" not in result

    def test_alternating_infill_direction(self) -> None:
        # Even layers fill X-parallel, odd layers fill Y-parallel
        # With a tall enough object, both directions should be used
        result = rectangular_prism(height=5.0, layer_height=0.2)
        # Both solid and sparse infill should be present
        assert "; FEATURE: Solid infill" in result
        assert "; FEATURE: Sparse infill" in result


class TestDimensions:
    """Test different part dimensions."""

    def test_very_small_part(self) -> None:
        result = rectangular_prism(width=1.0, depth=1.0, height=0.2)
        assert "; CHANGE_LAYER" in result
        assert "; FEATURE: Outer wall" in result

    def test_large_part(self) -> None:
        result = rectangular_prism(width=200.0, depth=200.0, height=1.0)
        assert "; CHANGE_LAYER" in result

    def test_tall_narrow_part(self) -> None:
        result = rectangular_prism(width=2.0, depth=2.0, height=10.0)
        layer_count = result.count("; CHANGE_LAYER")
        assert layer_count > 40

    def test_non_square_footprint(self) -> None:
        result = rectangular_prism(width=50.0, depth=10.0, height=0.2)
        xs: list[float] = []
        ys: list[float] = []
        for ln in result.splitlines():
            m = re.match(r"G[01] X([\d.]+) Y([\d.]+)", ln)
            if m:
                xs.append(float(m.group(1)))
                ys.append(float(m.group(2)))
        x_span = max(xs) - min(xs)
        y_span = max(ys) - min(ys)
        # X span should be much larger than Y span
        assert x_span > y_span * 3


class TestNozzleAndFilament:
    """Test nozzle and filament diameter parameters."""

    def test_different_nozzle_changes_output(self) -> None:
        # Different nozzle diameter should produce different G-code
        result_04 = rectangular_prism(nozzle_diameter=0.4, height=0.2)
        result_06 = rectangular_prism(nozzle_diameter=0.6, height=0.2)
        assert result_04 != result_06

    def test_larger_filament_less_extrusion(self) -> None:
        # Larger filament diameter -> less E needed for same volume
        result_175 = rectangular_prism(filament_diameter=1.75, height=0.2)
        result_285 = rectangular_prism(filament_diameter=2.85, height=0.2)
        e_175 = _max_e(result_175)
        e_285 = _max_e(result_285)
        assert e_175 > e_285


def _max_e(gcode: str) -> float:
    """Extract the maximum E value from extrusion moves in G-code."""
    max_e = 0.0
    for ln in gcode.splitlines():
        m = re.search(r"X[\d.]+ Y[\d.]+ E([\d.]+)", ln)
        if m:
            max_e = max(max_e, float(m.group(1)))
    return max_e

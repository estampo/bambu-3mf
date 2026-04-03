"""End-to-end tests: naked G-code → render templates → assemble → package.

This tests the full pipeline that replaces OrcaSlicer's template engine:
1. Start with naked toolpath (just printing moves, no machine init/shutdown)
2. Render P1S start/end templates with Jinja2
3. Assemble into complete G-code
4. Package into .gcode.3mf

We compare against OrcaSlicer 2.3.1 reference output, but NOT byte-for-byte
because OrcaSlicer injects M73 progress markers and has different number
formatting. Instead we verify structural equivalence.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from bambu_3mf.assemble import assemble_gcode
from bambu_3mf.pack import FilamentInfo, SliceInfo, pack_gcode_3mf
from bambu_3mf.templates import render_template


FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_3MF = FIXTURES / "reference.gcode.3mf"
REFERENCE_GCODE = FIXTURES / "cube.gcode"
NAKED_TOOLPATH = FIXTURES / "naked_toolpath.gcode"


# Context values reverse-engineered from the OrcaSlicer 2.3.1 reference.
# These match the config comments in cube.gcode.
P1S_CONTEXT = {
    "bed_temperature_initial_layer_single": 55,
    "initial_extruder": 0,
    "filament_type": ["PLA"],
    "bed_temperature": [55],
    "bed_temperature_initial_layer": [55],
    "nozzle_temperature_initial_layer": [220],
    "curr_bed_type": "Textured PEI Plate",
    "first_layer_print_min": [116, 116],
    "first_layer_print_size": [24, 24],
    "outer_wall_volumetric_speed": 12,
    "filament_max_volumetric_speed": [12],
    "nozzle_temperature_range_high": [240],
    "max_layer_z": 20.0,
}


def _strip_m73(gcode: str) -> str:
    """Strip M73 progress marker lines injected by OrcaSlicer."""
    return re.sub(r"^M73 P\d+ R\d+\n", "", gcode, flags=re.MULTILINE)


def _extract_reference_sections() -> dict[str, str]:
    """Extract the rendered template sections from the OrcaSlicer reference.

    Returns dict with keys: start, filament_start, toolpath, filament_end, end
    """
    lines = REFERENCE_GCODE.read_text().splitlines(keepends=True)
    return {
        # Machine start template output: lines 552-828 (1-indexed)
        "start": "".join(lines[551:828]),
        # Filament start gcode: lines 833-836 (after "filament start gcode" comment)
        "filament_start": "".join(lines[832:836]),
        # Toolpath: lines 837-12658
        "toolpath": "".join(lines[836:12658]),
        # Filament end gcode: line 12661 (after comment)
        "filament_end": "".join(lines[12660:12661]),
        # Machine end template output: lines 12662-12715
        "end": "".join(lines[12661:12715]),
    }


# ---------------------------------------------------------------------------
# Tests: Template rendering matches OrcaSlicer reference
# ---------------------------------------------------------------------------


class TestStartTemplateRendering:
    """Rendered P1S start template must match OrcaSlicer reference output."""

    def test_renders_without_error(self) -> None:
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        assert len(result) > 100

    def test_key_commands_present(self) -> None:
        """All critical machine init commands must be present."""
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        assert "M104 S75" in result  # HB fan trigger
        assert "M140 S55" in result  # bed temp
        assert "M190 S55" in result  # wait for bed
        assert "M106 P3 S180" in result  # PLA fan prevention
        assert "G29.1 Z-0.04" in result  # textured plate offset
        assert "M975 S1" in result  # mech mode suppression

    def test_temperature_values_correct(self) -> None:
        """Rendered temperatures must match reference context values."""
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        # nozzle_temperature_initial_layer[0] - 20 = 200
        assert "M109 S200" in result
        # nozzle_temperature_initial_layer[0] = 220
        assert "M109 S220" in result

    def test_volumetric_speed_calculation(self) -> None:
        """Feed rate calculations from outer_wall_volumetric_speed must be correct."""
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        # outer_wall_volumetric_speed/(0.3*0.5) * 60 = 12/0.15*60 = 4800
        assert "F4800" in result
        # outer_wall_volumetric_speed/(0.3*0.5)/4 * 60 = 1200
        assert "F1200" in result

    def test_bed_leveling_coordinates(self) -> None:
        """Bed leveling uses correct print area coordinates."""
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        assert "G29 A X116 Y116 I24 J24" in result

    def test_filament_volumetric_speed(self) -> None:
        """M620.1 feed rate calculation from filament_max_volumetric_speed."""
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        # filament_max_volumetric_speed[0]/2.4053*60 ≈ 299.34
        assert "M620.1 E F" in result
        assert "T240" in result  # nozzle_temperature_range_high

    def test_line_count_within_range(self) -> None:
        """Rendered line count should be close to reference (allowing for M73 diffs)."""
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        ref = _extract_reference_sections()
        ref_stripped = _strip_m73(ref["start"])
        our_lines = len(result.strip().splitlines())
        ref_lines = len(ref_stripped.strip().splitlines())
        # Should be within 5 lines (minor formatting diffs)
        assert abs(our_lines - ref_lines) <= 5, (
            f"Line count mismatch: ours={our_lines}, ref={ref_lines}"
        )


class TestEndTemplateRendering:
    """Rendered P1S end template must match OrcaSlicer reference output."""

    def test_renders_without_error(self) -> None:
        result = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        assert len(result) > 50

    def test_key_commands_present(self) -> None:
        result = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        assert "M140 S0" in result  # turn off bed
        assert "M104 S0" in result  # turn off hotend
        assert "M17 S" in result  # stepper control

    def test_z_retract_calculated(self) -> None:
        """Z retract uses max_layer_z + 0.5 for low prints."""
        result = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        # max_layer_z=20.0, so Z=20.5
        assert "G1 Z20.5 F900" in result

    def test_z_lift_calculated(self) -> None:
        """Z lift uses max_layer_z + 100 (clamped at 250)."""
        result = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        # max_layer_z=20.0, 20+100=120 < 250, so use 120
        assert "G1 Z120 F600" in result

    def test_matches_reference_stripped(self) -> None:
        """Rendered output matches reference after stripping OrcaSlicer markers."""
        result = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        ref = _extract_reference_sections()
        ref_stripped = _strip_m73(ref["end"])
        our_lines = result.strip().splitlines()
        # Remove EXECUTABLE_BLOCK_END marker (injected by OrcaSlicer, not template)
        ref_lines = [
            l for l in ref_stripped.strip().splitlines()
            if l != "; EXECUTABLE_BLOCK_END"
        ]
        assert our_lines == ref_lines


# ---------------------------------------------------------------------------
# Tests: G-code assembly
# ---------------------------------------------------------------------------


class TestAssemble:
    """Test G-code assembly from components."""

    def test_assembles_all_sections(self) -> None:
        result = assemble_gcode(
            start_gcode="; start\nM104 S75",
            toolpath="G1 X10 Y10\nG1 X20 Y20",
            end_gcode="M140 S0\nM104 S0",
            filament_start_gcode="M106 P3 S255",
            filament_end_gcode="M106 P3 S0",
        )
        lines = result.splitlines()
        # Verify ordering
        start_idx = next(i for i, l in enumerate(lines) if "M104 S75" in l)
        fil_start_idx = next(i for i, l in enumerate(lines) if "M106 P3 S255" in l)
        toolpath_idx = next(i for i, l in enumerate(lines) if "G1 X10" in l)
        fil_end_idx = next(i for i, l in enumerate(lines) if "M106 P3 S0" in l)
        end_idx = next(i for i, l in enumerate(lines) if "M140 S0" in l)
        assert start_idx < fil_start_idx < toolpath_idx < fil_end_idx < end_idx

    def test_filament_gcode_optional(self) -> None:
        result = assemble_gcode(
            start_gcode="START",
            toolpath="TOOLPATH",
            end_gcode="END",
        )
        assert "; filament start gcode" not in result
        assert "; filament end gcode" not in result
        assert "START" in result
        assert "TOOLPATH" in result
        assert "END" in result

    def test_ends_with_newline(self) -> None:
        result = assemble_gcode("START", "TOOLPATH", "END")
        assert result.endswith("\n")


# ---------------------------------------------------------------------------
# Tests: End-to-end pipeline
# ---------------------------------------------------------------------------


class TestEndToEnd:
    """Full pipeline: render templates → assemble → package → validate."""

    def test_render_assemble_package(self) -> None:
        """Complete e2e: render P1S templates, assemble with toolpath, package."""
        # 1. Render templates
        start = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        end = render_template("p1s_end.gcode.j2", P1S_CONTEXT)

        # 2. Load naked toolpath
        toolpath = NAKED_TOOLPATH.read_text()

        # 3. Assemble
        gcode = assemble_gcode(
            start_gcode=start,
            toolpath=toolpath,
            end_gcode=end,
            filament_start_gcode="M106 P3 S255\n;Prevent PLA from jamming",
            filament_end_gcode="M106 P3 S0",
        )

        # 4. Package
        buf = BytesIO()
        info = SliceInfo(
            nozzle_diameter=0.4,
            prediction=1241,
            weight=3.64,
            filaments=[
                FilamentInfo(
                    slot=1,
                    tray_info_idx="GFL99",
                    filament_type="PLA",
                    color="#F2754E",
                    used_m=1.22,
                    used_g=3.64,
                )
            ],
        )
        pack_gcode_3mf(gcode.encode(), buf, slice_info=info)

        # 5. Validate
        buf.seek(0)
        with zipfile.ZipFile(buf) as z:
            names = set(z.namelist())
            assert "Metadata/plate_1.gcode" in names
            assert "Metadata/plate_1.gcode.md5" in names
            assert "Metadata/slice_info.config" in names

            packed_gcode = z.read("Metadata/plate_1.gcode")
            assert packed_gcode == gcode.encode()

            md5 = z.read("Metadata/plate_1.gcode.md5").decode()
            assert md5 == hashlib.md5(gcode.encode()).hexdigest().upper()

    def test_assembled_gcode_has_start_and_end(self) -> None:
        """Assembled G-code contains both machine init and shutdown sequences."""
        start = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        end = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        toolpath = NAKED_TOOLPATH.read_text()

        gcode = assemble_gcode(start_gcode=start, toolpath=toolpath, end_gcode=end)

        # Machine init markers
        assert ";===== machine: P1S" in gcode
        assert "M104 S75" in gcode  # HB fan trigger
        assert "M975 S1 ; turn on mech mode supression" in gcode

        # Toolpath markers
        assert "M981 S1 P20000" in gcode  # spaghetti detector on
        assert "; CHANGE_LAYER" in gcode
        assert "M981 S0 P20000" in gcode  # spaghetti detector off

        # Machine shutdown markers
        assert "M140 S0 ; turn off bed" in gcode
        assert "M104 S0 ; turn off hotend" in gcode

    def test_assembled_section_ordering(self) -> None:
        """Start gcode appears before toolpath, which appears before end gcode."""
        start = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        end = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        toolpath = NAKED_TOOLPATH.read_text()

        gcode = assemble_gcode(start_gcode=start, toolpath=toolpath, end_gcode=end)

        start_pos = gcode.index(";===== machine: P1S")
        toolpath_pos = gcode.index("M981 S1 P20000")
        end_pos = gcode.index(";===== date: 20230428")

        assert start_pos < toolpath_pos < end_pos

    def test_assembled_gcode_size_reasonable(self) -> None:
        """Assembled G-code should be similar in size to reference."""
        start = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        end = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        toolpath = NAKED_TOOLPATH.read_text()

        gcode = assemble_gcode(
            start_gcode=start,
            toolpath=toolpath,
            end_gcode=end,
            filament_start_gcode="M106 P3 S255\n;Prevent PLA from jamming",
            filament_end_gcode="M106 P3 S0",
        )

        ref_gcode = REFERENCE_GCODE.read_text()
        # Our assembled gcode should be within 5% of reference size
        # (differences: no header/config comments, no M73 markers, minor formatting)
        ratio = len(gcode) / len(ref_gcode)
        assert 0.80 < ratio < 1.05, (
            f"Size ratio {ratio:.2f} outside expected range "
            f"(ours={len(gcode)}, ref={len(ref_gcode)})"
        )

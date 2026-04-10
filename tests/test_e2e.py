"""End-to-end tests: render templates → assemble → package → validate.

Tests the full pipeline that replaces OrcaSlicer's template engine:
1. Render P1S start/end templates with Jinja2
2. Assemble into complete G-code with a synthetic toolpath
3. Package into .gcode.3mf with project_settings
4. Validate the archive is structurally correct for Bambu Connect
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO
from pathlib import Path

from bambox.assemble import assemble_gcode
from bambox.cli import _parse_filament_args, main
from bambox.gcode_compat import _FILAMENT_AREA
from bambox.pack import (
    FilamentInfo,
    SliceInfo,
    fixup_model_settings,
    pack_gcode_3mf,
    repack_3mf,
)
from bambox.templates import render_template

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_SETTINGS = json.loads((FIXTURES / "project_settings.json").read_text())

# Synthetic toolpath — mimics what a slicer would produce (just printing moves).
SYNTHETIC_TOOLPATH = """\
M981 S1 P20000 ;open spaghetti detector
M106 S0
M106 P2 S0
; CHANGE_LAYER
; Z_HEIGHT: 0.2
; LAYER_HEIGHT: 0.2
G1 E-.8 F1800
; layer num/total_layer_count: 1/5
G1 Z0.2 F1200
G1 X100 Y100 E0.5 F3000
G1 X120 Y100 E0.5
G1 X120 Y120 E0.5
G1 X100 Y120 E0.5
G1 X100 Y100 E0.5
; CHANGE_LAYER
; Z_HEIGHT: 0.4
G1 Z0.4 F1200
G1 X100 Y100 E0.5 F3000
G1 X120 Y100 E0.5
G1 X120 Y120 E0.5
G1 X100 Y120 E0.5
G1 X100 Y100 E0.5
M106 S0
M106 P2 S0
M981 S0 P20000 ; close spaghetti detector
"""

# P1S context for template rendering.
P1S_CONTEXT = {
    "bed_temperature_initial_layer_single": 55,
    "initial_extruder": 0,
    "filament_type": ["PLA"],
    "bed_temperature": [55],
    "bed_temperature_initial_layer": [55],
    "nozzle_temperature_initial_layer": [220],
    "curr_bed_type": "Textured PEI Plate",
    "first_layer_print_min": [100, 100],
    "first_layer_print_size": [20, 20],
    "outer_wall_volumetric_speed": 12,
    "filament_max_volumetric_speed": [12],
    "nozzle_temperature_range_high": [240],
    "max_layer_z": 0.4,
    "filament_area": _FILAMENT_AREA,
}


# ---------------------------------------------------------------------------
# Tests: Template rendering
# ---------------------------------------------------------------------------


class TestStartTemplateRendering:
    def test_renders_without_error(self) -> None:
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        assert len(result) > 100

    def test_key_commands_present(self) -> None:
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        assert "M104 S75" in result  # HB fan trigger
        assert "M140 S55" in result  # bed temp
        assert "M190 S55" in result  # wait for bed
        assert "M106 P3 S180" in result  # PLA fan prevention
        assert "G29.1 Z-0.04" in result  # textured plate offset
        assert "M975 S1" in result  # mech mode suppression

    def test_temperature_values(self) -> None:
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        assert "M109 S200" in result  # nozzle_temp[0] - 20
        assert "M109 S220" in result  # nozzle_temp[0]

    def test_volumetric_speed_calculation(self) -> None:
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        assert "F4800" in result  # 12/(0.3*0.5)*60
        assert "F1200" in result  # 12/(0.3*0.5)/4*60

    def test_bed_leveling_coordinates(self) -> None:
        result = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        assert "G29 A X100 Y100 I20 J20" in result


class TestEndTemplateRendering:
    def test_renders_without_error(self) -> None:
        result = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        assert len(result) > 50

    def test_key_commands_present(self) -> None:
        result = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        assert "M140 S0" in result
        assert "M104 S0" in result
        assert "M17 S" in result

    def test_z_retract(self) -> None:
        result = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        # max_layer_z=0.4, so Z=0.9
        assert "G1 Z0.9 F900" in result

    def test_z_lift(self) -> None:
        result = render_template("p1s_end.gcode.j2", P1S_CONTEXT)
        # max_layer_z=0.4, 0.4+100=100.4 < 250 → int → 100
        assert "G1 Z100 F600" in result


# ---------------------------------------------------------------------------
# Tests: G-code assembly
# ---------------------------------------------------------------------------


class TestAssemble:
    def test_assembles_all_sections(self) -> None:
        result = assemble_gcode(
            start_gcode="; start\nM104 S75",
            toolpath="G1 X10 Y10\nG1 X20 Y20",
            end_gcode="M140 S0\nM104 S0",
            filament_start_gcode="M106 P3 S255",
            filament_end_gcode="M106 P3 S0",
        )
        lines = result.splitlines()
        start_idx = next(i for i, line in enumerate(lines) if "M104 S75" in line)
        fil_start_idx = next(i for i, line in enumerate(lines) if "M106 P3 S255" in line)
        toolpath_idx = next(i for i, line in enumerate(lines) if "G1 X10" in line)
        fil_end_idx = next(i for i, line in enumerate(lines) if "M106 P3 S0" in line)
        end_idx = next(i for i, line in enumerate(lines) if "M140 S0" in line)
        assert start_idx < fil_start_idx < toolpath_idx < fil_end_idx < end_idx

    def test_filament_gcode_optional(self) -> None:
        result = assemble_gcode("START", "TOOLPATH", "END")
        assert "; filament start gcode" not in result
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
    def test_render_assemble_package(self) -> None:
        """Full e2e: render → assemble → package → validate archive."""
        start = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        end = render_template("p1s_end.gcode.j2", P1S_CONTEXT)

        gcode = assemble_gcode(
            start_gcode=start,
            toolpath=SYNTHETIC_TOOLPATH,
            end_gcode=end,
            filament_start_gcode="M106 P3 S255\n;Prevent PLA from jamming",
            filament_end_gcode="M106 P3 S0",
        )

        buf = BytesIO()
        info = SliceInfo(
            nozzle_diameter=0.4,
            prediction=60,
            weight=0.1,
            filaments=[FilamentInfo(slot=1, filament_type="PLA", used_m=0.05, used_g=0.1)],
        )
        pack_gcode_3mf(
            gcode.encode(),
            buf,
            slice_info=info,
            project_settings=PROJECT_SETTINGS,
        )

        buf.seek(0)
        with zipfile.ZipFile(buf) as z:
            names = set(z.namelist())
            # All Bambu Connect-required files present
            assert "Metadata/plate_1.gcode" in names
            assert "Metadata/plate_1.gcode.md5" in names
            assert "Metadata/project_settings.config" in names
            assert "Metadata/model_settings.config" in names
            assert "Metadata/plate_1.png" in names

            # G-code is translated to BBL format (Z-change fallback adds header)
            packed_gcode = z.read("Metadata/plate_1.gcode")
            assert packed_gcode.startswith(b"; HEADER_BLOCK_START\n")
            assert b"G1 Z0.2 F1200" in packed_gcode

            # MD5 matches the packed (translated) G-code
            md5 = z.read("Metadata/plate_1.gcode.md5").decode()
            assert md5 == hashlib.md5(packed_gcode).hexdigest().upper()

            # project_settings has padded arrays
            ps = json.loads(z.read("Metadata/project_settings.config"))
            for key, val in ps.items():
                if isinstance(val, list) and len(val) > 0:
                    assert len(val) >= 5

            # model_settings has thumbnail refs
            ms = z.read("Metadata/model_settings.config").decode()
            assert 'key="thumbnail_file"' in ms

    def test_assembled_gcode_has_start_and_end(self) -> None:
        start = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        end = render_template("p1s_end.gcode.j2", P1S_CONTEXT)

        gcode = assemble_gcode(start_gcode=start, toolpath=SYNTHETIC_TOOLPATH, end_gcode=end)

        # Machine init
        assert ";===== machine: P1S" in gcode
        assert "M104 S75" in gcode
        # Toolpath
        assert "M981 S1 P20000" in gcode
        assert "; CHANGE_LAYER" in gcode
        # Machine shutdown
        assert "M140 S0 ; turn off bed" in gcode
        assert "M104 S0 ; turn off hotend" in gcode

    def test_assembled_section_ordering(self) -> None:
        start = render_template("p1s_start.gcode.j2", P1S_CONTEXT)
        end = render_template("p1s_end.gcode.j2", P1S_CONTEXT)

        gcode = assemble_gcode(start_gcode=start, toolpath=SYNTHETIC_TOOLPATH, end_gcode=end)

        start_pos = gcode.index(";===== machine: P1S")
        toolpath_pos = gcode.index("M981 S1 P20000")
        end_pos = gcode.index(";===== date: 20230428")

        assert start_pos < toolpath_pos < end_pos


class TestParseFilamentArgs:
    def test_default_when_none(self) -> None:
        result = _parse_filament_args(None)
        assert result == [(None, "PLA", "#F2754E")]

    def test_single_type(self) -> None:
        result = _parse_filament_args(["PETG-CF"])
        assert result == [(None, "PETG-CF", "#F2754E")]

    def test_type_with_color(self) -> None:
        result = _parse_filament_args(["PLA:#FF0000"])
        assert result == [(None, "PLA", "#FF0000")]

    def test_color_without_hash(self) -> None:
        result = _parse_filament_args(["ASA:BCBCBC"])
        assert result == [(None, "ASA", "#BCBCBC")]

    def test_multiple_filaments(self) -> None:
        result = _parse_filament_args(["PETG-CF:#2850E0", "PLA"])
        assert result == [(None, "PETG-CF", "#2850E0"), (None, "PLA", "#F2754E")]

    def test_lowercase_normalized(self) -> None:
        result = _parse_filament_args(["pla"])
        assert result == [(None, "PLA", "#F2754E")]


class TestCliPack:
    def test_pack_generates_project_settings(self, tmp_path: Path) -> None:
        """CLI pack should auto-generate project_settings from machine+filament."""
        gcode_file = tmp_path / "test.gcode"
        gcode_file.write_text("G28\nG1 Z0.2 F1200\nG1 X10 Y10 E1 F600\n")
        output = tmp_path / "test.gcode.3mf"

        main(["pack", str(gcode_file), "-o", str(output), "-f", "PLA"])

        assert output.exists()
        with zipfile.ZipFile(output) as z:
            names = z.namelist()
            assert "Metadata/project_settings.config" in names
            ps = json.loads(z.read("Metadata/project_settings.config"))
            # Should have 544+ keys
            assert len(ps) > 500
            # Per-filament varying arrays must be padded to 5 slots
            from bambox.settings import _DATA_DIR, _load_json

            _varying = set(_load_json(_DATA_DIR / "_varying_keys.json"))
            for key, val in ps.items():
                if key in _varying and isinstance(val, list):
                    assert len(val) >= 5, f"{key} has {len(val)} elements"

    def test_pack_multi_filament(self, tmp_path: Path) -> None:
        gcode_file = tmp_path / "multi.gcode"
        gcode_file.write_text("G28\nG1 Z0.2 F1200\nG1 X10 Y10 E1 F600\n")
        output = tmp_path / "multi.gcode.3mf"

        main(
            [
                "pack",
                str(gcode_file),
                "-o",
                str(output),
                "-f",
                "PETG-CF:#2850E0",
                "-f",
                "PLA:#000000",
            ]
        )

        with zipfile.ZipFile(output) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            # filament_colour should have our colors
            colors = ps["filament_colour"]
            assert colors[0] == "#2850E0"
            assert colors[1] == "#000000"

    def test_pack_default_filament(self, tmp_path: Path) -> None:
        """Pack with no --filament flag defaults to PLA."""
        gcode_file = tmp_path / "default.gcode"
        gcode_file.write_text("G28\nG1 Z0.2 F1200\nG1 X10 Y10 E1 F600\n")
        output = tmp_path / "default.gcode.3mf"

        main(["pack", str(gcode_file), "-o", str(output)])

        with zipfile.ZipFile(output) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            assert len(ps) > 500


# ---------------------------------------------------------------------------
# Helpers for repack tests
# ---------------------------------------------------------------------------


def _make_orca_3mf(
    path: Path,
    *,
    project_settings: dict | None = None,
    model_settings: str | None = None,
    gcode: str = "G28\nG1 Z0.2 F1200\nG1 X10 Y10 E1 F600\n",
    thumbnail: bytes | None = None,
) -> None:
    """Create a minimal OrcaSlicer-style .gcode.3mf for testing."""
    if project_settings is None:
        # Minimal settings with short arrays (simulates --min-save)
        project_settings = {
            "printer_model_id": "C12",
            "nozzle_diameter": ["0.4"],
            "filament_type": ["PLA"],
            "filament_colour": ["#F2754E"],
            "temperature_vitrification": ["45"],
        }
    if model_settings is None:
        model_settings = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<config>\n"
            "  <plate>\n"
            '    <metadata key="plater_id" value="1"/>\n'
            '    <metadata key="filament_maps" value="1"/>\n'
            "  </plate>\n"
            "</config>\n"
        )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Metadata/plate_1.gcode", gcode)
        z.writestr(
            "Metadata/project_settings.config",
            json.dumps(project_settings, indent=4),
        )
        z.writestr("Metadata/model_settings.config", model_settings)
        z.writestr("Metadata/plate_1.png", thumbnail or b"\x89PNG tiny")
        z.writestr("[Content_Types].xml", "<Types/>")


# ---------------------------------------------------------------------------
# Tests: fixup_model_settings
# ---------------------------------------------------------------------------


class TestFixupModelSettings:
    def test_pads_filament_maps(self) -> None:
        xml = (
            "<config>\n"
            "  <plate>\n"
            '    <metadata key="filament_maps" value="1"/>\n'
            "  </plate>\n"
            "</config>"
        )
        result = fixup_model_settings(xml)
        assert 'value="1 1 1 1 1"' in result

    def test_pads_multi_filament_maps(self) -> None:
        xml = '<metadata key="filament_maps" value="1 2"/>'
        result = fixup_model_settings(xml)
        assert 'value="1 2 2 2 2"' in result

    def test_adds_missing_thumbnail_keys(self) -> None:
        xml = (
            '<config>\n  <plate>\n    <metadata key="plater_id" value="1"/>\n  </plate>\n</config>'
        )
        result = fixup_model_settings(xml)
        assert 'key="thumbnail_file"' in result
        assert 'key="top_file"' in result
        assert 'key="pick_file"' in result
        # pattern_bbox_file is NOT added by fixup_model_settings — it's handled
        # by repack_3mf which ensures the actual file exists first
        assert 'key="pattern_bbox_file"' not in result

    def test_preserves_existing_thumbnail_keys(self) -> None:
        xml = (
            "<config>\n"
            "  <plate>\n"
            '    <metadata key="thumbnail_file" value="custom.png"/>\n'
            "  </plate>\n"
            "</config>"
        )
        result = fixup_model_settings(xml)
        assert result.count('key="thumbnail_file"') == 1
        assert 'value="custom.png"' in result


# ---------------------------------------------------------------------------
# Tests: repack_3mf
# ---------------------------------------------------------------------------


class TestRepack:
    def test_patches_existing_settings(self, tmp_path: Path) -> None:
        """Repack without machine/filament patches existing project_settings."""
        threemf = tmp_path / "test.gcode.3mf"
        _make_orca_3mf(threemf)

        repack_3mf(threemf)

        with zipfile.ZipFile(threemf) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            # Short arrays should be padded to 5
            assert len(ps["filament_type"]) >= 5
            assert len(ps["filament_colour"]) >= 5
            # BC-required keys should be added
            assert "bbl_use_printhost" in ps

    def test_regenerates_settings_from_profiles(self, tmp_path: Path) -> None:
        """Repack with machine+filament regenerates project_settings from profiles."""
        threemf = tmp_path / "test.gcode.3mf"
        _make_orca_3mf(threemf)

        repack_3mf(threemf, machine="p1s", filaments=["PLA"])

        with zipfile.ZipFile(threemf) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            # Regenerated from profiles = 544+ keys
            assert len(ps) > 500
            # Per-filament varying arrays must be padded to 5 slots
            from bambox.settings import _DATA_DIR, _load_json

            _varying = set(_load_json(_DATA_DIR / "_varying_keys.json"))
            for key, val in ps.items():
                if key in _varying and isinstance(val, list):
                    assert len(val) >= 5, f"{key} has {len(val)} elements"

    def test_fixes_model_settings(self, tmp_path: Path) -> None:
        """Repack pads filament_maps and adds thumbnail refs in model_settings."""
        threemf = tmp_path / "test.gcode.3mf"
        _make_orca_3mf(threemf)

        repack_3mf(threemf)

        with zipfile.ZipFile(threemf) as z:
            ms = z.read("Metadata/model_settings.config").decode()
            assert 'value="1 1 1 1 1"' in ms
            assert 'key="thumbnail_file"' in ms

    def test_regenerates_broken_thumbnails(self, tmp_path: Path) -> None:
        """Repack regenerates thumbnails that are too small (broken)."""
        threemf = tmp_path / "test.gcode.3mf"
        # Tiny thumbnail simulates headless OrcaSlicer output
        _make_orca_3mf(threemf, thumbnail=b"\x89PNG tiny")

        repack_3mf(threemf)

        with zipfile.ZipFile(threemf) as z:
            png = z.read("Metadata/plate_1.png")
            # Should be replaced — either a real thumbnail or placeholder
            assert png != b"\x89PNG tiny"

    def test_preserves_valid_thumbnails(self, tmp_path: Path) -> None:
        """Repack keeps thumbnails that are large enough."""
        valid_png = b"\x89PNG" + b"\x00" * 2000  # > 1024 bytes
        threemf = tmp_path / "test.gcode.3mf"
        _make_orca_3mf(threemf, thumbnail=valid_png)

        repack_3mf(threemf)

        with zipfile.ZipFile(threemf) as z:
            png = z.read("Metadata/plate_1.png")
            assert png == valid_png

    def test_preserves_other_files(self, tmp_path: Path) -> None:
        """Repack preserves files it doesn't touch."""
        threemf = tmp_path / "test.gcode.3mf"
        _make_orca_3mf(threemf)

        repack_3mf(threemf)

        with zipfile.ZipFile(threemf) as z:
            assert "[Content_Types].xml" in z.namelist()
            gcode = z.read("Metadata/plate_1.gcode").decode()
            assert "G28" in gcode

    def test_multi_filament_regeneration(self, tmp_path: Path) -> None:
        """Repack with multiple filaments gets correct colors."""
        threemf = tmp_path / "test.gcode.3mf"
        _make_orca_3mf(threemf)

        repack_3mf(
            threemf,
            machine="p1s",
            filaments=["PETG-CF", "PLA"],
            filament_colors=["#2850E0", "#FF0000"],
        )

        with zipfile.ZipFile(threemf) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            assert ps["filament_colour"][0] == "#2850E0"
            assert ps["filament_colour"][1] == "#FF0000"

    def test_generates_plate_json_when_missing(self, tmp_path: Path) -> None:
        """Repack creates plate_1.json if not in original archive."""
        threemf = tmp_path / "test.gcode.3mf"
        _make_orca_3mf(threemf)  # helper doesn't include plate_1.json

        repack_3mf(threemf)

        with zipfile.ZipFile(threemf) as z:
            assert "Metadata/plate_1.json" in z.namelist()
            plate = json.loads(z.read("Metadata/plate_1.json"))
            assert "filament_colors" in plate
            assert plate["version"] == 2

    def test_adds_bbox_ref_with_plate_json(self, tmp_path: Path) -> None:
        """model_settings gets pattern_bbox_file ref when plate_1.json exists."""
        threemf = tmp_path / "test.gcode.3mf"
        _make_orca_3mf(threemf)

        repack_3mf(threemf)

        with zipfile.ZipFile(threemf) as z:
            ms = z.read("Metadata/model_settings.config").decode()
            assert 'key="pattern_bbox_file"' in ms

    def test_preserves_existing_plate_json(self, tmp_path: Path) -> None:
        """Repack doesn't overwrite existing plate_1.json."""
        threemf = tmp_path / "test.gcode.3mf"
        custom_plate = '{"custom":true}'
        with zipfile.ZipFile(threemf, "w") as z:
            z.writestr("Metadata/plate_1.gcode", "G28\n")
            z.writestr("Metadata/project_settings.config", '{"filament_type":["PLA"]}')
            ms_xml = (
                "<config>\n  <plate>\n"
                '    <metadata key="filament_maps" value="1"/>\n'
                "  </plate>\n</config>\n"
            )
            z.writestr("Metadata/model_settings.config", ms_xml)
            z.writestr("Metadata/plate_1.json", custom_plate)
            z.writestr("Metadata/plate_1.png", b"\x89PNG tiny")

        repack_3mf(threemf)

        with zipfile.ZipFile(threemf) as z:
            assert json.loads(z.read("Metadata/plate_1.json")) == {"custom": True}


class TestCliRepack:
    def test_repack_patches_in_place(self, tmp_path: Path) -> None:
        """CLI repack should modify the archive in-place."""
        threemf = tmp_path / "test.gcode.3mf"
        _make_orca_3mf(threemf)

        main(["repack", str(threemf)])

        with zipfile.ZipFile(threemf) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            assert len(ps["filament_type"]) >= 5

    def test_repack_with_filament_regenerates(self, tmp_path: Path) -> None:
        """CLI repack with -f regenerates from profiles."""
        threemf = tmp_path / "test.gcode.3mf"
        _make_orca_3mf(threemf)

        main(["repack", str(threemf), "-f", "PLA:#FF0000"])

        with zipfile.ZipFile(threemf) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            assert len(ps) > 500
            assert ps["filament_colour"][0] == "#FF0000"

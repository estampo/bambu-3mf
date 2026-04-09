"""Tests for CuraEngine printer definitions and BAMBOX header parsing."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from bambox.cli import _assign_filament_slots, _parse_filament_args, main
from bambox.cura import (
    available_cura_printers,
    build_template_context,
    cura_definitions_dir,
    extract_slice_stats,
    parse_bambox_headers,
    strip_bambox_header,
)


class TestCuraDefinitions:
    def test_definitions_dir_exists(self) -> None:
        d = cura_definitions_dir()
        assert d.exists()
        assert d.is_dir()

    def test_available_printers(self) -> None:
        printers = available_cura_printers()
        assert "bambox_p1s_ams" in printers

    def test_p1s_ams_definition_valid_json(self) -> None:
        defn = cura_definitions_dir() / "bambox_p1s_ams.def.json"
        data = json.loads(defn.read_text())
        assert data["version"] == 2
        assert data["inherits"] == "fdmprinter"
        assert data["overrides"]["machine_extruder_count"]["value"] == 4

    def test_p1s_ams_has_bambox_headers_in_start_gcode(self) -> None:
        defn = cura_definitions_dir() / "bambox_p1s_ams.def.json"
        data = json.loads(defn.read_text())
        start = data["overrides"]["machine_start_gcode"]["default_value"]
        assert "; BAMBOX_PRINTER=p1s" in start
        assert "; BAMBOX_ASSEMBLE=true" in start

    def test_p1s_ams_has_4_extruder_trains(self) -> None:
        defn = cura_definitions_dir() / "bambox_p1s_ams.def.json"
        data = json.loads(defn.read_text())
        trains = data["metadata"]["machine_extruder_trains"]
        assert len(trains) == 4
        for i in range(4):
            assert str(i) in trains

    def test_extruder_definitions_exist(self) -> None:
        d = cura_definitions_dir()
        for i in range(4):
            ext = d / f"bambox_p1s_ams_extruder_{i}.def.json"
            assert ext.exists(), f"Missing extruder definition: {ext.name}"

    def test_extruders_share_nozzle(self) -> None:
        """AMS extruders share a single nozzle (zero offset)."""
        d = cura_definitions_dir()
        for i in range(4):
            ext = json.loads((d / f"bambox_p1s_ams_extruder_{i}.def.json").read_text())
            overrides = ext["overrides"]
            assert overrides["machine_nozzle_offset_x"]["default_value"] == 0
            assert overrides["machine_nozzle_offset_y"]["default_value"] == 0

    def test_p1s_ams_has_roofing_flooring_counts(self) -> None:
        """CuraEngine 5.12+ requires explicit roofing/flooring_layer_count."""
        defn = cura_definitions_dir() / "bambox_p1s_ams.def.json"
        data = json.loads(defn.read_text())
        overrides = data["overrides"]
        assert "roofing_layer_count" in overrides
        assert "flooring_layer_count" in overrides

    def test_extruders_emit_filament_headers(self) -> None:
        """Each extruder emits BAMBOX_FILAMENT_SLOT and BAMBOX_FILAMENT_TYPE."""
        d = cura_definitions_dir()
        for i in range(4):
            ext = json.loads((d / f"bambox_p1s_ams_extruder_{i}.def.json").read_text())
            start = ext["overrides"]["machine_extruder_start_code"]["default_value"]
            assert f"; BAMBOX_FILAMENT_SLOT={i}" in start
            assert "; BAMBOX_FILAMENT_TYPE=" in start


class TestParseBamboxHeaders:
    def test_basic_headers(self) -> None:
        gcode = (
            "; BAMBOX_PRINTER=p1s\n"
            "; BAMBOX_BED_TEMP=60\n"
            "; BAMBOX_ASSEMBLE=true\n"
            "; BAMBOX_END\n"
            "G28\n"
        )
        h = parse_bambox_headers(gcode)
        assert h["PRINTER"] == "p1s"
        assert h["BED_TEMP"] == "60"
        assert h["ASSEMBLE"] == "true"

    def test_stops_at_bambox_end(self) -> None:
        gcode = "; BAMBOX_PRINTER=p1s\n; BAMBOX_END\n; BAMBOX_IGNORED=yes\n"
        h = parse_bambox_headers(gcode)
        assert "IGNORED" not in h

    def test_multi_value_keys(self) -> None:
        """Multiple BAMBOX_FILAMENT_TYPE lines become comma-separated."""
        gcode = "; BAMBOX_FILAMENT_TYPE=PLA\n; BAMBOX_FILAMENT_TYPE=PETG-CF\n; BAMBOX_END\n"
        h = parse_bambox_headers(gcode)
        assert h["FILAMENT_TYPE"] == "PLA,PETG-CF"

    def test_multi_slot_headers(self) -> None:
        """Simulates CuraEngine output with per-extruder headers."""
        gcode = (
            "; BAMBOX_PRINTER=p1s\n"
            "; BAMBOX_EXTRUDERS=4\n"
            "; BAMBOX_ASSEMBLE=true\n"
            "G28\n"
            ";TYPE:CUSTOM\n"
            "; BAMBOX_FILAMENT_SLOT=0\n"
            "; BAMBOX_FILAMENT_TYPE=PLA\n"
            "T0\n"
            "G1 X10 Y10 E1 F600\n"
        )
        h = parse_bambox_headers(gcode)
        assert h["PRINTER"] == "p1s"
        assert h["EXTRUDERS"] == "4"
        assert h["FILAMENT_SLOT"] == "0"
        assert h["FILAMENT_TYPE"] == "PLA"

    def test_no_bambox_headers(self) -> None:
        gcode = "G28\nG1 Z0.2 F1200\n"
        h = parse_bambox_headers(gcode)
        assert h == {}

    def test_mixed_with_regular_comments(self) -> None:
        gcode = (
            "; generated by CuraEngine\n"
            ";FLAVOR:Marlin\n"
            "; BAMBOX_PRINTER=p1s\n"
            ";TIME:1234\n"
            "; BAMBOX_END\n"
        )
        h = parse_bambox_headers(gcode)
        assert h["PRINTER"] == "p1s"
        # Defaults are injected for missing machine-level headers
        assert h["NOZZLE_DIAMETER"] == "0.4"
        assert h["BED_TYPE"] == "Textured PEI Plate"

    def test_full_scan_finds_filament_headers_after_bambox_end(self) -> None:
        """Per-extruder headers (FILAMENT_SLOT/TYPE) appear at tool-change
        points throughout the file, well past the machine_start_gcode block.
        parse_bambox_headers must scan the entire file for these keys."""
        gcode = (
            "; BAMBOX_PRINTER=p1s\n"
            "; BAMBOX_BED_TEMP=60\n"
            "; BAMBOX_ASSEMBLE=true\n"
            "; BAMBOX_FILAMENT_SLOT=0\n"
            "; BAMBOX_FILAMENT_TYPE=PLA\n"
            "; BAMBOX_END\n" + "G1 X0 Y0\n" * 500 + "; BAMBOX_FILAMENT_SLOT=3\n"
            "; BAMBOX_FILAMENT_TYPE=PLA\n"
        )
        h = parse_bambox_headers(gcode)
        assert h["PRINTER"] == "p1s"
        assert h["FILAMENT_SLOT"] == "0,3"
        assert h["FILAMENT_TYPE"] == "PLA,PLA"


class TestStripBamboxHeader:
    def test_strips_header_lines(self) -> None:
        gcode = "; BAMBOX_PRINTER=p1s\n; BAMBOX_BED_TEMP=60\n; BAMBOX_END\nG28\nG1 Z0.2 F1200\n"
        result = strip_bambox_header(gcode)
        assert "; BAMBOX_" not in result
        assert "G28\n" in result
        assert "G1 Z0.2 F1200\n" in result

    def test_preserves_non_bambox_comments(self) -> None:
        gcode = (
            "; generated by CuraEngine\n; BAMBOX_PRINTER=p1s\n;FLAVOR:Marlin\n; BAMBOX_END\nG28\n"
        )
        result = strip_bambox_header(gcode)
        assert "; generated by CuraEngine" in result
        assert ";FLAVOR:Marlin" in result

    def test_no_headers(self) -> None:
        gcode = "G28\nG1 Z0.2\n"
        assert strip_bambox_header(gcode) == gcode

    def test_preserves_bambox_comments_after_header(self) -> None:
        gcode = "; BAMBOX_PRINTER=p1s\n; BAMBOX_END\nG28\n; BAMBOX_MACRO=something\nG1 Z0.2\n"
        result = strip_bambox_header(gcode)
        assert "; BAMBOX_PRINTER" not in result
        assert "; BAMBOX_END" not in result
        assert "; BAMBOX_MACRO=something\n" in result
        assert "G28\n" in result


class TestBuildTemplateContext:
    def test_maps_header_temps(self) -> None:
        headers = {"BED_TEMP": "60", "NOZZLE_TEMP": "220"}
        ctx = build_template_context(headers, {})
        assert ctx["bed_temperature_initial_layer_single"] == 60
        assert ctx["nozzle_temperature_initial_layer"] == [220]

    def test_header_overrides_settings(self) -> None:
        headers = {"BED_TEMP": "70"}
        settings: dict[str, object] = {"bed_temperature_initial_layer_single": 55}
        ctx = build_template_context(headers, settings)
        assert ctx["bed_temperature_initial_layer_single"] == 70

    def test_bed_temp_header_overrides_existing_array(self) -> None:
        """BED_TEMP header must override pre-existing bed_temperature arrays."""
        headers = {"BED_TEMP": "80"}
        settings: dict[str, object] = {
            "bed_temperature": [55],
            "bed_temperature_initial_layer": [55],
        }
        ctx = build_template_context(headers, settings)
        assert ctx["bed_temperature"] == [80]
        assert ctx["bed_temperature_initial_layer"] == [80]
        assert ctx["bed_temperature_initial_layer_single"] == 80

    def test_nozzle_temp_header_overrides_existing_array(self) -> None:
        """NOZZLE_TEMP header must override pre-existing nozzle_temperature_initial_layer."""
        headers = {"NOZZLE_TEMP": "260"}
        settings: dict[str, object] = {
            "nozzle_temperature_initial_layer": [200],
        }
        ctx = build_template_context(headers, settings)
        assert ctx["nozzle_temperature_initial_layer"] == [260]

    def test_defaults_present(self) -> None:
        ctx = build_template_context({}, {})
        assert ctx["initial_extruder"] == 0
        assert ctx["max_layer_z"] == 0.4


class TestPackWithBamboxHeaders:
    def test_auto_configures_from_headers(self, tmp_path: Path) -> None:
        """pack should auto-detect machine/filament from BAMBOX headers."""
        gcode_file = tmp_path / "cura_output.gcode"
        gcode_file.write_text(
            "; BAMBOX_PRINTER=p1s\n"
            "; BAMBOX_FILAMENT_TYPE=PETG-CF\n"
            "; BAMBOX_BED_TEMP=80\n"
            "; BAMBOX_NOZZLE_TEMP=260\n"
            "; BAMBOX_END\n"
            "G28\nG1 Z0.2 F1200\nG1 X10 Y10 E1 F600\n"
        )
        output = tmp_path / "output.gcode.3mf"

        # No -f flag — headers should provide filament
        main(["pack", str(gcode_file), "-o", str(output)])

        with zipfile.ZipFile(output) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            # Should have detected PETG-CF from headers
            assert ps["filament_type"][0] == "PETG-CF"
            assert len(ps) > 500

    def test_assemble_wraps_toolpath(self, tmp_path: Path) -> None:
        """BAMBOX_ASSEMBLE=true should render start/end and wrap toolpath."""
        gcode_file = tmp_path / "cura_output.gcode"
        gcode_file.write_text(
            "; BAMBOX_PRINTER=p1s\n"
            "; BAMBOX_FILAMENT_TYPE=PLA\n"
            "; BAMBOX_BED_TEMP=55\n"
            "; BAMBOX_NOZZLE_TEMP=220\n"
            "; BAMBOX_ASSEMBLE=true\n"
            "; BAMBOX_END\n"
            "G1 Z0.2 F1200\nG1 X10 Y10 E1 F600\n"
        )
        output = tmp_path / "assembled.gcode.3mf"

        main(["pack", str(gcode_file), "-o", str(output)])

        with zipfile.ZipFile(output) as z:
            gcode = z.read("Metadata/plate_1.gcode").decode()
            # Should have P1S start sequence (from template)
            assert "M104" in gcode  # nozzle temp command from start template
            assert "M140" in gcode  # bed temp command from start template
            # Original toolpath preserved
            assert "G1 X10 Y10 E1" in gcode
            # BAMBOX headers stripped
            assert "; BAMBOX_PRINTER" not in gcode

    def test_headers_override_cli_filament(self, tmp_path: Path) -> None:
        """BAMBOX_FILAMENT_TYPE header takes precedence over --filament flag."""
        gcode_file = tmp_path / "cura_output.gcode"
        gcode_file.write_text(
            "; BAMBOX_PRINTER=p1s\n"
            "; BAMBOX_FILAMENT_TYPE=PLA\n"
            "; BAMBOX_END\n"
            "G28\nG1 Z0.2 F1200\nG1 X10 Y10 E1 F600\n"
        )
        output = tmp_path / "override.gcode.3mf"

        main(["pack", str(gcode_file), "-o", str(output), "-f", "PETG-CF"])

        with zipfile.ZipFile(output) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            # Header should win over CLI flag
            assert ps["filament_type"][0] == "PLA"

    def test_multi_filament_from_headers(self, tmp_path: Path) -> None:
        """Multiple BAMBOX_FILAMENT_TYPE headers become multi-filament."""
        gcode_file = tmp_path / "multi.gcode"
        gcode_file.write_text(
            "; BAMBOX_PRINTER=p1s\n"
            "; BAMBOX_FILAMENT_TYPE=PLA\n"
            "; BAMBOX_FILAMENT_TYPE=PETG-CF\n"
            "; BAMBOX_END\n"
            "G28\nG1 Z0.2 F1200\nG1 X10 Y10 E1 F600\n"
        )
        output = tmp_path / "multi.gcode.3mf"

        main(["pack", str(gcode_file), "-o", str(output)])

        with zipfile.ZipFile(output) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            assert ps["filament_type"][0] == "PLA"
            assert ps["filament_type"][1] == "PETG-CF"

    def test_multi_filament_assembly_rewrites_tool_changes(self, tmp_path: Path) -> None:
        """Multi-filament assembly should rewrite T commands to M620/M621."""
        gcode_file = tmp_path / "multi_tool.gcode"
        gcode_file.write_text(
            "; BAMBOX_PRINTER=p1s\n"
            "; BAMBOX_FILAMENT_TYPE=PLA\n"
            "; BAMBOX_FILAMENT_TYPE=PETG-CF\n"
            "; BAMBOX_ASSEMBLE=true\n"
            "; BAMBOX_BED_TEMP=60\n"
            "; BAMBOX_NOZZLE_TEMP=220\n"
            "; BAMBOX_END\n"
            "G1 Z0.2 F1200\n"
            "G1 X10 Y10 E1 F600\n"
            "T1\n"
            "G1 X20 Y20 E2 F600\n"
        )
        output = tmp_path / "multi_tool.gcode.3mf"

        main(["pack", str(gcode_file), "-o", str(output)])

        with zipfile.ZipFile(output) as z:
            gcode = z.read("Metadata/plate_1.gcode").decode()
            # T1 should be wrapped in M620/M621 sequence
            assert "M620 S1A" in gcode
            assert "M621 S1A" in gcode
            # Original toolpath preserved
            assert "G1 X10 Y10 E1" in gcode
            assert "G1 X20 Y20 E2" in gcode

    def test_slot_mapping_from_cli(self, tmp_path: Path) -> None:
        """bambox pack -f 3:PETG-CF places filament in slot 3."""
        gcode_file = tmp_path / "slot.gcode"
        gcode_file.write_text("G28\nG1 Z0.2 F1200\nG1 X10 Y10 E1 F600\n")
        output = tmp_path / "slot.gcode.3mf"

        main(["pack", str(gcode_file), "-o", str(output), "-f", "3:PETG-CF"])

        with zipfile.ZipFile(output) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            assert ps["filament_type"][0] == "PETG-CF"

    def test_slot_mapping_from_headers(self, tmp_path: Path) -> None:
        """BAMBOX_FILAMENT_SLOT headers auto-configure slot assignment.

        CuraEngine emits paired SLOT+TYPE in machine_extruder_start_code."""
        gcode_file = tmp_path / "slot_header.gcode"
        gcode_file.write_text(
            "; BAMBOX_PRINTER=p1s\n"
            "; BAMBOX_FILAMENT_SLOT=0\n"
            "; BAMBOX_FILAMENT_TYPE=PLA\n"
            "G28\nG1 Z0.2 F1200\n"
            "; BAMBOX_FILAMENT_SLOT=2\n"
            "; BAMBOX_FILAMENT_TYPE=PETG-CF\n"
            "; BAMBOX_END\n"
            "G1 X10 Y10 E1 F600\n"
        )
        output = tmp_path / "slot_header.gcode.3mf"

        main(["pack", str(gcode_file), "-o", str(output)])

        with zipfile.ZipFile(output) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            assert ps["filament_type"][0] == "PLA"
            assert ps["filament_type"][1] == "PETG-CF"


class TestParseFilamentArgs:
    def test_type_only(self) -> None:
        result = _parse_filament_args(["PLA"])
        assert result == [(None, "PLA", "#F2754E")]

    def test_type_color(self) -> None:
        result = _parse_filament_args(["PLA:#FF0000"])
        assert result == [(None, "PLA", "#FF0000")]

    def test_slot_type(self) -> None:
        result = _parse_filament_args(["3:PETG-CF"])
        assert result == [(3, "PETG-CF", "#F2754E")]

    def test_slot_type_color(self) -> None:
        result = _parse_filament_args(["2:PETG-CF:#2850E0"])
        assert result == [(2, "PETG-CF", "#2850E0")]

    def test_default(self) -> None:
        result = _parse_filament_args(None)
        assert result == [(None, "PLA", "#F2754E")]


class TestAssignFilamentSlots:
    def test_sequential(self) -> None:
        parsed = [(None, "PLA", "#F2754E"), (None, "PETG-CF", "#F2754E")]
        result = _assign_filament_slots(parsed)
        assert result == [(0, "PLA", "#F2754E"), (1, "PETG-CF", "#F2754E")]

    def test_explicit_slot(self) -> None:
        parsed = [(3, "PETG-CF", "#F2754E")]
        result = _assign_filament_slots(parsed)
        assert result == [(3, "PETG-CF", "#F2754E")]

    def test_mixed_explicit_and_sequential(self) -> None:
        parsed = [(None, "PLA", "#F2754E"), (2, "PETG-CF", "#2850E0")]
        result = _assign_filament_slots(parsed)
        assert result == [(0, "PLA", "#F2754E"), (2, "PETG-CF", "#2850E0")]

    def test_explicit_slot_skips_for_sequential(self) -> None:
        """Unslotted filaments skip over explicitly claimed slots."""
        parsed = [(0, "PETG-CF", "#F2754E"), (None, "PLA", "#F2754E")]
        result = _assign_filament_slots(parsed)
        assert result == [(0, "PETG-CF", "#F2754E"), (1, "PLA", "#F2754E")]


class TestExtractSliceStats:
    def test_time_from_time_header_only(self) -> None:
        gcode = ";TIME:1234\nG1 X0\n"
        stats = extract_slice_stats(gcode)
        assert stats.prediction == 1234

    def test_time_from_time_elapsed_only(self) -> None:
        gcode = ";LAYER:0\nG1 X0\n;TIME_ELAPSED:500.0\n"
        stats = extract_slice_stats(gcode)
        assert stats.prediction == 500

    def test_time_uses_max_of_time_and_elapsed(self) -> None:
        """When ;TIME: is larger (includes start gcode time), use it."""
        gcode = ";TIME:6666\n;LAYER:0\nG1 X0\n;TIME_ELAPSED:2799.0\n"
        stats = extract_slice_stats(gcode)
        assert stats.prediction == 6666

    def test_time_uses_elapsed_when_larger(self) -> None:
        """When TIME_ELAPSED is larger, use it (shouldn't normally happen)."""
        gcode = ";TIME:100\n;LAYER:0\nG1 X0\n;TIME_ELAPSED:500.0\n"
        stats = extract_slice_stats(gcode)
        assert stats.prediction == 500

    def test_no_time_info(self) -> None:
        gcode = "G28\nG1 X0\n"
        stats = extract_slice_stats(gcode)
        assert stats.prediction == 0

    def test_filament_used_parsing(self) -> None:
        gcode = ";Filament used: 1.234m, 0.567m\n"
        stats = extract_slice_stats(gcode)
        assert stats.filament_used_m == [1.234, 0.567]
        assert stats.weight > 0

    def test_weight_from_e_positions(self) -> None:
        """When Filament used is 0m, compute weight from E positions."""
        gcode = ";Filament used: 0m\nG92 E0\nG1 X10 E5.0\nG1 X20 E10.0\n"
        stats = extract_slice_stats(gcode)
        assert stats.weight > 0

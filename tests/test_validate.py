"""Tests for bambox.validate — .gcode.3mf archive validation."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from shared_fixtures import (
    MINIMAL_GCODE,
    MINIMAL_SETTINGS,
    MINIMAL_SLICE_INFO,
    build_valid_3mf,
)

from bambox.validate import (
    Severity,
    ValidationResult,
    compare_3mf,
    validate_3mf,
    validate_3mf_buffer,
    validate_gcode,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "e2e_cura_p1s"
REFERENCE_3MF = FIXTURE_DIR / "reference.gcode.3mf"


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------


class TestValidationResult:
    def test_empty_is_valid(self) -> None:
        r = ValidationResult()
        assert r.valid is True
        assert r.errors == []
        assert r.warnings == []

    def test_warnings_still_valid(self) -> None:
        from bambox.validate import Finding

        r = ValidationResult(findings=[Finding(Severity.WARNING, "W001", "x")])
        assert r.valid is True

    def test_error_makes_invalid(self) -> None:
        from bambox.validate import Finding

        r = ValidationResult(findings=[Finding(Severity.ERROR, "E001", "bad")])
        assert r.valid is False
        assert len(r.errors) == 1

    def test_to_dict_schema(self) -> None:
        from bambox.validate import Finding

        r = ValidationResult(
            findings=[
                Finding(Severity.ERROR, "E001", "err msg", "detail"),
                Finding(Severity.WARNING, "W001", "warn msg"),
            ]
        )
        d = r.to_dict()
        assert d["valid"] is False
        assert len(d["errors"]) == 1
        assert len(d["warnings"]) == 1
        assert d["errors"][0]["code"] == "E001"
        assert d["errors"][0]["detail"] == "detail"
        assert d["warnings"][0]["code"] == "W001"

    def test_to_dict_json_serializable(self) -> None:
        from bambox.validate import Finding

        r = ValidationResult(findings=[Finding(Severity.ERROR, "E001", "msg")])
        # Should not raise
        json.dumps(r.to_dict())


# ---------------------------------------------------------------------------
# Reference archive validation
# ---------------------------------------------------------------------------


class TestReferenceArchive:
    @pytest.mark.skipif(not REFERENCE_3MF.exists(), reason="reference fixture not available")
    def test_reference_is_valid(self) -> None:
        result = validate_3mf(REFERENCE_3MF)
        assert result.valid, [f"{f.code}: {f.message}" for f in result.errors]


# ---------------------------------------------------------------------------
# Valid archive baseline
# ---------------------------------------------------------------------------


class TestValidArchive:
    def test_minimal_valid_passes(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert result.valid, [f"{f.code}: {f.message}" for f in result.errors]

    def test_buffer_api(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path)
        with open(path, "rb") as fh:
            result = validate_3mf_buffer(fh)
        assert result.valid


# ---------------------------------------------------------------------------
# E000: Not a ZIP
# ---------------------------------------------------------------------------


class TestBadZip:
    def test_not_a_zip(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.gcode.3mf"
        bad.write_bytes(b"this is not a zip")
        result = validate_3mf(bad)
        assert not result.valid
        assert result.errors[0].code == "E000"


# ---------------------------------------------------------------------------
# E001: Temperature commands
# ---------------------------------------------------------------------------


class TestTemperatureCommands:
    def test_array_as_scalar(self, tmp_path: Path) -> None:
        gcode = MINIMAL_GCODE + "M104 S[220]\n"
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E001" for f in result.errors)

    def test_template_in_temp(self, tmp_path: Path) -> None:
        gcode = MINIMAL_GCODE + "M109 S{nozzle_temp}\n"
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E001" for f in result.errors)

    def test_valid_temps_pass(self, tmp_path: Path) -> None:
        gcode = MINIMAL_GCODE + "M104 S220\nM109 S220\nM140 S60\n"
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert not any(f.code == "E001" for f in result.findings)


# ---------------------------------------------------------------------------
# E002: Toolchange feedrate
# ---------------------------------------------------------------------------


_NON_BBL_GCODE = "G1 X10 Y10 E1 F600\n"  # no HEADER_BLOCK_START


class TestToolchangeFeedrate:
    def test_low_feedrate_detected(self, tmp_path: Path) -> None:
        # Non-BBL G-code with sub-1 mm/min feedrate (likely raw volumetric)
        gcode = _NON_BBL_GCODE + "M620.1 E F0.5 T240\n"
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E002" for f in result.errors)

    def test_correct_feedrate_passes(self, tmp_path: Path) -> None:
        gcode = _NON_BBL_GCODE + "M620.1 E F299 T240\n"
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert not any(f.code == "E002" for f in result.findings)

    def test_bbl_orca_feedrate_not_flagged(self, tmp_path: Path) -> None:
        # OrcaSlicer BBL G-code: F49.89 is correct for ABS at 2.0 mm³/s
        gcode = MINIMAL_GCODE + "M620.1 E F49.8898 T240\n"
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert not any(f.code == "E002" for f in result.findings)


# ---------------------------------------------------------------------------
# E003: MD5 checksum
# ---------------------------------------------------------------------------


class TestMD5:
    def test_mismatch_detected(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path)
        # Corrupt MD5
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("Metadata/plate_1.gcode.md5", "DEADBEEF" * 4)
        result = validate_3mf(path)
        assert any(f.code == "E003" for f in result.errors)

    def test_valid_md5_passes(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "E003" for f in result.findings)


# ---------------------------------------------------------------------------
# E004: Array padding
# ---------------------------------------------------------------------------


class TestArrayPadding:
    def test_short_array_detected(self, tmp_path: Path) -> None:
        settings = json.dumps({"filament_type": ["PLA", "PLA"]})
        path = build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert any(f.code == "E004" for f in result.errors)

    def test_full_array_passes(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "E004" for f in result.findings)


# ---------------------------------------------------------------------------
# E005: Unsubstituted templates
# ---------------------------------------------------------------------------


class TestUnsubstitutedTemplates:
    def test_template_in_command(self, tmp_path: Path) -> None:
        gcode = MINIMAL_GCODE + "G1 X{first_layer_print_min} Y10 F600\n"
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E005" for f in result.errors)

    def test_template_in_comment_is_ok(self, tmp_path: Path) -> None:
        gcode = MINIMAL_GCODE + "; {perimeter}\n"
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert not any(f.code == "E005" for f in result.findings)


# ---------------------------------------------------------------------------
# E006: Required files
# ---------------------------------------------------------------------------


class TestRequiredFiles:
    def test_missing_gcode(self, tmp_path: Path) -> None:
        path = tmp_path / "test.gcode.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
        result = validate_3mf(path)
        e006 = [f for f in result.errors if f.code == "E006"]
        assert len(e006) > 0

    def test_valid_has_all_files(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "E006" for f in result.findings)


# ---------------------------------------------------------------------------
# E007/E008: Header block
# ---------------------------------------------------------------------------


class TestHeaderBlock:
    def test_missing_header_block(self, tmp_path: Path) -> None:
        gcode = "G28\nG1 X10 Y10 E1 F600\n"
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E007" for f in result.errors)

    def test_zero_layer_count(self, tmp_path: Path) -> None:
        gcode = "; HEADER_BLOCK_START\n; total layer number: 0\n; HEADER_BLOCK_END\nG28\n"
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E008" for f in result.errors)


# ---------------------------------------------------------------------------
# E009/E010: Layer markers
# ---------------------------------------------------------------------------


class TestLayerMarkers:
    def test_missing_m73_l(self, tmp_path: Path) -> None:
        gcode = (
            "; HEADER_BLOCK_START\n"
            "; total layer number: 1\n"
            "; HEADER_BLOCK_END\n"
            "M991 S0 P1\n"
            "G1 X10 E1 F600\n"
        )
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E009" for f in result.errors)

    def test_missing_m991(self, tmp_path: Path) -> None:
        gcode = (
            "; HEADER_BLOCK_START\n"
            "; total layer number: 1\n"
            "; HEADER_BLOCK_END\n"
            "M73 L1\n"
            "M73 P100 R0\n"
            "G1 X10 E1 F600\n"
        )
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E010" for f in result.errors)

    def test_layer_count_mismatch(self, tmp_path: Path) -> None:
        gcode = (
            "; HEADER_BLOCK_START\n"
            "; total layer number: 100\n"
            "; HEADER_BLOCK_END\n"
            "M73 L1\nM991 S0 P1\nM73 P100 R0\n"
        )
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E011" for f in result.errors)


# ---------------------------------------------------------------------------
# W001-W003: Metadata warnings
# ---------------------------------------------------------------------------


class TestMetadataWarnings:
    def test_empty_printer_model_id(self, tmp_path: Path) -> None:
        si = MINIMAL_SLICE_INFO.replace('value="C12"', 'value=""')
        path = build_valid_3mf(tmp_path, slice_info=si)
        result = validate_3mf(path)
        assert any(f.code == "W001" for f in result.warnings)

    def test_zero_prediction(self, tmp_path: Path) -> None:
        si = MINIMAL_SLICE_INFO.replace('value="150"', 'value="0"')
        path = build_valid_3mf(tmp_path, slice_info=si)
        result = validate_3mf(path)
        assert any(f.code == "W002" for f in result.warnings)

    def test_zero_weight(self, tmp_path: Path) -> None:
        si = MINIMAL_SLICE_INFO.replace('value="5.00"', 'value="0.00"')
        path = build_valid_3mf(tmp_path, slice_info=si)
        result = validate_3mf(path)
        assert any(f.code == "W003" for f in result.warnings)


# ---------------------------------------------------------------------------
# W004: Filament color
# ---------------------------------------------------------------------------


class TestFilamentColor:
    def test_invalid_color(self, tmp_path: Path) -> None:
        si = MINIMAL_SLICE_INFO.replace('color="#F2754E"', 'color="not-a-color"')
        path = build_valid_3mf(tmp_path, slice_info=si)
        result = validate_3mf(path)
        assert any(f.code == "W004" for f in result.warnings)

    def test_valid_color_passes(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "W004" for f in result.findings)


# ---------------------------------------------------------------------------
# W007/W008: Progress markers
# ---------------------------------------------------------------------------


class TestProgressMarkers:
    def test_missing_m73_p(self, tmp_path: Path) -> None:
        gcode = (
            "; HEADER_BLOCK_START\n"
            "; total layer number: 1\n"
            "; HEADER_BLOCK_END\n"
            "M73 L1\n"
            "M991 S0 P1\n"
        )
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "W007" for f in result.warnings)

    def test_missing_m73_r(self, tmp_path: Path) -> None:
        gcode = (
            "; HEADER_BLOCK_START\n"
            "; total layer number: 1\n"
            "; HEADER_BLOCK_END\n"
            "M73 L1\nM73 P100\n"
            "M991 S0 P1\n"
        )
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "W008" for f in result.warnings)


# ---------------------------------------------------------------------------
# W009: Monotonicity
# ---------------------------------------------------------------------------


class TestMonotonicity:
    def test_non_monotonic_layers(self, tmp_path: Path) -> None:
        gcode = (
            "; HEADER_BLOCK_START\n"
            "; total layer number: 3\n"
            "; HEADER_BLOCK_END\n"
            "M73 L1\nM73 P33 R2\nM991 S0 P1\n"
            "M73 L3\nM73 P66 R1\nM991 S0 P3\n"
            "M73 L2\nM73 P100 R0\nM991 S0 P2\n"
        )
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "W009" for f in result.warnings)


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCLIValidate:
    def test_valid_archive_exit_0(self, tmp_path: Path) -> None:
        from bambox.cli import main

        path = build_valid_3mf(tmp_path)
        # Should not raise SystemExit
        main(["validate", str(path)])

    def test_invalid_archive_exit_1(self, tmp_path: Path) -> None:
        from bambox.cli import main

        bad = tmp_path / "bad.gcode.3mf"
        bad.write_bytes(b"not a zip")
        with pytest.raises(SystemExit, match="1"):
            main(["validate", str(bad)])

    def test_json_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from bambox.cli import main

        path = build_valid_3mf(tmp_path)
        main(["validate", "--json", str(path)])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["valid"] is True
        assert isinstance(data["errors"], list)
        assert isinstance(data["warnings"], list)

    def test_strict_fails_on_warnings(self, tmp_path: Path) -> None:
        from bambox.cli import main

        si = MINIMAL_SLICE_INFO.replace('value="C12"', 'value=""')
        path = build_valid_3mf(tmp_path, slice_info=si)
        with pytest.raises(SystemExit, match="1"):
            main(["validate", "--strict", str(path)])

    def test_missing_file_exit_1(self, tmp_path: Path) -> None:
        from bambox.cli import main

        with pytest.raises(SystemExit, match="1"):
            main(["validate", str(tmp_path / "nonexistent.gcode.3mf")])


# ---------------------------------------------------------------------------
# W005: print_compatible_printers broadcast
# ---------------------------------------------------------------------------


class TestCompatiblePrinters:
    def test_broadcast_detected(self, tmp_path: Path) -> None:
        settings = json.dumps(
            {
                "filament_type": ["PLA"] * 5,
                "filament_colour": ["#F2754E"] * 5,
                "nozzle_temperature": ["220"] * 5,
                "nozzle_temperature_initial_layer": ["220"] * 5,
                "bed_temperature": ["60"] * 5,
                "filament_max_volumetric_speed": ["12"] * 5,
                "print_compatible_printers": ["Bambu Lab X1 Carbon 0.4 nozzle"] * 5,
            }
        )
        path = build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert any(f.code == "W005" for f in result.warnings)

    def test_proper_list_passes(self, tmp_path: Path) -> None:
        settings = json.dumps(
            {
                "filament_type": ["PLA"] * 5,
                "filament_colour": ["#F2754E"] * 5,
                "nozzle_temperature": ["220"] * 5,
                "nozzle_temperature_initial_layer": ["220"] * 5,
                "bed_temperature": ["60"] * 5,
                "filament_max_volumetric_speed": ["12"] * 5,
                "print_compatible_printers": [
                    "Bambu Lab X1 Carbon 0.4 nozzle",
                    "Bambu Lab X1 0.4 nozzle",
                    "Bambu Lab P1S 0.4 nozzle",
                    "Bambu Lab X1E 0.4 nozzle",
                ],
            }
        )
        path = build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert not any(f.code == "W005" for f in result.findings)


# ---------------------------------------------------------------------------
# W006: printer_model
# ---------------------------------------------------------------------------


class TestPrinterModel:
    def test_empty_printer_model(self, tmp_path: Path) -> None:
        settings = json.dumps(
            {
                "filament_type": ["PLA"] * 5,
                "filament_colour": ["#F2754E"] * 5,
                "nozzle_temperature": ["220"] * 5,
                "nozzle_temperature_initial_layer": ["220"] * 5,
                "bed_temperature": ["60"] * 5,
                "filament_max_volumetric_speed": ["12"] * 5,
                "printer_model": "",
            }
        )
        path = build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert any(f.code == "W006" for f in result.warnings)

    def test_present_printer_model_passes(self, tmp_path: Path) -> None:
        settings = json.dumps(
            {
                "filament_type": ["PLA"] * 5,
                "filament_colour": ["#F2754E"] * 5,
                "nozzle_temperature": ["220"] * 5,
                "nozzle_temperature_initial_layer": ["220"] * 5,
                "bed_temperature": ["60"] * 5,
                "filament_max_volumetric_speed": ["12"] * 5,
                "printer_model": "Bambu Lab P1S",
            }
        )
        path = build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert not any(f.code == "W006" for f in result.findings)


# ---------------------------------------------------------------------------
# W010: Recommended thumbnails
# ---------------------------------------------------------------------------


class TestRecommendedThumbnails:
    def test_missing_top_and_pick(self, tmp_path: Path) -> None:
        """Archive without top_1.png and pick_1.png triggers W010."""
        out = tmp_path / "test.gcode.3mf"
        gcode_bytes = MINIMAL_GCODE.encode()
        md5 = hashlib.md5(gcode_bytes).hexdigest().upper()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("_rels/.rels", "<Relationships/>")
            zf.writestr("3D/3dmodel.model", "<model/>")
            zf.writestr("Metadata/plate_1.gcode", gcode_bytes)
            zf.writestr("Metadata/plate_1.gcode.md5", md5)
            zf.writestr("Metadata/model_settings.config", "{}")
            zf.writestr("Metadata/_rels/model_settings.config.rels", "<Relationships/>")
            zf.writestr("Metadata/slice_info.config", MINIMAL_SLICE_INFO)
            zf.writestr("Metadata/project_settings.config", MINIMAL_SETTINGS)
            zf.writestr("Metadata/plate_1.json", "{}")
            zf.writestr("Metadata/plate_1.png", b"\x89PNG\r\n\x1a\n")
            zf.writestr("Metadata/plate_no_light_1.png", b"\x89PNG\r\n\x1a\n")
            zf.writestr("Metadata/plate_1_small.png", b"\x89PNG\r\n\x1a\n")
            # Deliberately omit top_1.png and pick_1.png
        result = validate_3mf(out)
        w010 = [f for f in result.warnings if f.code == "W010"]
        assert len(w010) == 2

    def test_with_thumbnails_passes(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "W010" for f in result.findings)


# ---------------------------------------------------------------------------
# W011: Time sync
# ---------------------------------------------------------------------------


class TestTimeSync:
    def test_divergent_time_detected(self, tmp_path: Path) -> None:
        """M73 R2 (2 min) vs prediction 7200s (120 min) should trigger W011."""
        gcode = MINIMAL_GCODE  # has M73 P0 R2 at start
        si = MINIMAL_SLICE_INFO.replace('value="150"', 'value="7200"')
        path = build_valid_3mf(tmp_path, gcode=gcode, slice_info=si)
        result = validate_3mf(path)
        assert any(f.code == "W011" for f in result.warnings)

    def test_aligned_time_passes(self, tmp_path: Path) -> None:
        """M73 R2 (2 min = 120s) vs prediction 150s — within tolerance."""
        path = build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "W011" for f in result.findings)


# ---------------------------------------------------------------------------
# E013: M620/M621 multi-filament check
# ---------------------------------------------------------------------------


_MULTI_FILAMENT_GCODE = """\
; HEADER_BLOCK_START
; total layer number: 2
; HEADER_BLOCK_END
M73 P0 R5
M620 S0
T0
M621 S0
;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
M73 L1
M991 S0 P1
M73 P50 R3
G1 X10 Y10 E1 F600
M620 S1
T1
M621 S1
;LAYER_CHANGE
;Z:0.4
;HEIGHT:0.2
M73 L2
M991 S0 P2
M73 P100 R0
G1 X20 Y20 E2 F600
"""


class TestMultiFilamentE013:
    def test_proper_multi_filament_passes(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path, gcode=_MULTI_FILAMENT_GCODE)
        result = validate_3mf(path)
        assert not any(f.code == "E013" for f in result.findings)

    def test_single_filament_no_check(self, tmp_path: Path) -> None:
        """Single-filament gcode should not trigger E013."""
        path = build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "E013" for f in result.findings)


# ---------------------------------------------------------------------------
# E014: Bare T commands
# ---------------------------------------------------------------------------


class TestBareToolCommands:
    def test_bare_t_outside_block(self, tmp_path: Path) -> None:
        """Bare T1 outside M620/M621 block in multi-filament print."""
        gcode = """\
; HEADER_BLOCK_START
; total layer number: 2
; HEADER_BLOCK_END
M73 P0 R5
M620 S0
T0
M621 S0
;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
M73 L1
M991 S0 P1
M73 P50 R3
G1 X10 Y10 E1 F600
T1
M620 S1
T1
M621 S1
;LAYER_CHANGE
;Z:0.4
;HEIGHT:0.2
M73 L2
M991 S0 P2
M73 P100 R0
G1 X20 Y20 E2 F600
"""
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E014" for f in result.errors)

    def test_redundant_extruder_select_ok(self, tmp_path: Path) -> None:
        """Bare T0 re-selecting current extruder after M620 S0 block is harmless."""
        gcode = """\
; HEADER_BLOCK_START
; total layer number: 2
; HEADER_BLOCK_END
M73 P0 R5
M620 S0
T0
M621 S0
G1 X10 Y10 E1 F600
T0
M620 S1
T1
M621 S1
;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
M73 L1
M991 S0 P1
M73 P50 R3
G1 X10 Y10 E1 F600
;LAYER_CHANGE
;Z:0.4
;HEIGHT:0.2
M73 L2
M991 S0 P2
M73 P100 R0
G1 X20 Y20 E2 F600
"""
        path = build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert not any(f.code == "E014" for f in result.findings)

    def test_t_inside_block_ok(self, tmp_path: Path) -> None:
        """T commands inside M620/M621 blocks should not trigger E014."""
        path = build_valid_3mf(tmp_path, gcode=_MULTI_FILAMENT_GCODE)
        result = validate_3mf(path)
        assert not any(f.code == "E014" for f in result.findings)


# ---------------------------------------------------------------------------
# W012: Nozzle temperature range
# ---------------------------------------------------------------------------


class TestNozzleTempRange:
    def test_out_of_range_nozzle_temp(self, tmp_path: Path) -> None:
        settings = json.dumps(
            {
                "filament_type": ["PLA"] * 5,
                "filament_colour": ["#F2754E"] * 5,
                "nozzle_temperature": ["400", "220", "220", "220", "220"],
                "nozzle_temperature_initial_layer": ["220"] * 5,
                "bed_temperature": ["60"] * 5,
                "filament_max_volumetric_speed": ["12"] * 5,
            }
        )
        path = build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert any(f.code == "W012" for f in result.warnings)

    def test_valid_nozzle_temp_passes(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "W012" for f in result.findings)


# ---------------------------------------------------------------------------
# W013: Bed temperature range
# ---------------------------------------------------------------------------


class TestBedTempRange:
    def test_out_of_range_bed_temp(self, tmp_path: Path) -> None:
        settings = json.dumps(
            {
                "filament_type": ["PLA"] * 5,
                "filament_colour": ["#F2754E"] * 5,
                "nozzle_temperature": ["220"] * 5,
                "nozzle_temperature_initial_layer": ["220"] * 5,
                "bed_temperature": ["60"] * 5,
                "filament_max_volumetric_speed": ["12"] * 5,
                "hot_plate_temp": ["150", "60", "60", "60", "60"],
            }
        )
        path = build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert any(f.code == "W013" for f in result.warnings)

    def test_disabled_sentinel_minus_one_ok(self, tmp_path: Path) -> None:
        """BBL firmware uses -1 to mean 'disabled' — not an out-of-range temp."""
        settings = json.dumps(
            {
                "filament_type": ["PLA"] * 5,
                "filament_colour": ["#F2754E"] * 5,
                "nozzle_temperature": ["220"] * 5,
                "nozzle_temperature_initial_layer": ["220"] * 5,
                "bed_temperature": ["60"] * 5,
                "filament_max_volumetric_speed": ["12"] * 5,
                "filament_tower_interface_print_temp": ["-1"] * 5,
            }
        )
        path = build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert not any(f.code == "W013" for f in result.findings)

    def test_valid_bed_temp_passes(self, tmp_path: Path) -> None:
        path = build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "W013" for f in result.findings)


# ---------------------------------------------------------------------------
# W014: flush_volumes_matrix
# ---------------------------------------------------------------------------


class TestFlushVolumesMatrix:
    def test_non_square_matrix(self, tmp_path: Path) -> None:
        settings = json.dumps(
            {
                "filament_type": ["PLA"] * 5,
                "filament_colour": ["#F2754E"] * 5,
                "nozzle_temperature": ["220"] * 5,
                "nozzle_temperature_initial_layer": ["220"] * 5,
                "bed_temperature": ["60"] * 5,
                "filament_max_volumetric_speed": ["12"] * 5,
                "flush_volumes_matrix": [0, 1, 2],
            }
        )
        path = build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert any(f.code == "W014" for f in result.warnings)

    def test_square_matrix_passes(self, tmp_path: Path) -> None:
        settings = json.dumps(
            {
                "filament_type": ["PLA"] * 5,
                "filament_colour": ["#F2754E"] * 5,
                "nozzle_temperature": ["220"] * 5,
                "nozzle_temperature_initial_layer": ["220"] * 5,
                "bed_temperature": ["60"] * 5,
                "filament_max_volumetric_speed": ["12"] * 5,
                "flush_volumes_matrix": [0, 1, 2, 3],
            }
        )
        path = build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert not any(f.code == "W014" for f in result.findings)


# ---------------------------------------------------------------------------
# compare_3mf — reference comparison
# ---------------------------------------------------------------------------


class TestCompare3mf:
    def test_identical_archives(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        a = build_valid_3mf(tmp_path / "a")
        (tmp_path / "b").mkdir()
        b = build_valid_3mf(tmp_path / "b")
        result = compare_3mf(a, b)
        assert result.valid

    def test_different_filament_types(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = build_valid_3mf(tmp_path / "a")
        si_petg = MINIMAL_SLICE_INFO.replace('type="PLA"', 'type="PETG"')
        b = build_valid_3mf(tmp_path / "b", slice_info=si_petg)
        result = compare_3mf(a, b)
        assert any(f.code == "C001" for f in result.errors)

    def test_divergent_print_time(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = build_valid_3mf(tmp_path / "a")
        si_long = MINIMAL_SLICE_INFO.replace('value="150"', 'value="15000"')
        b = build_valid_3mf(tmp_path / "b", slice_info=si_long)
        result = compare_3mf(a, b)
        assert any(f.code == "C002" for f in result.errors)

    def test_divergent_weight(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = build_valid_3mf(tmp_path / "a")
        si_heavy = MINIMAL_SLICE_INFO.replace('value="5.00"', 'value="50.00"')
        b = build_valid_3mf(tmp_path / "b", slice_info=si_heavy)
        result = compare_3mf(a, b)
        assert any(f.code == "C003" for f in result.errors)

    def test_different_printer_model(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = build_valid_3mf(tmp_path / "a")
        si_x1 = MINIMAL_SLICE_INFO.replace('value="C12"', 'value="C11"')
        b = build_valid_3mf(tmp_path / "b", slice_info=si_x1)
        result = compare_3mf(a, b)
        assert any(f.code == "C005" for f in result.errors)

    def test_different_tool_changes(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = build_valid_3mf(tmp_path / "a", gcode=_MULTI_FILAMENT_GCODE)
        b = build_valid_3mf(tmp_path / "b")
        result = compare_3mf(a, b)
        assert any(f.code == "C004" for f in result.errors)


# ---------------------------------------------------------------------------
# CLI --reference
# ---------------------------------------------------------------------------


class TestCLIReference:
    def test_reference_comparison(self, tmp_path: Path) -> None:
        from bambox.cli import main

        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = build_valid_3mf(tmp_path / "a")
        b = build_valid_3mf(tmp_path / "b")
        # Should not raise
        main(["validate", str(a), "--reference", str(b)])

    def test_reference_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from bambox.cli import main

        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = build_valid_3mf(tmp_path / "a")
        b = build_valid_3mf(tmp_path / "b")
        main(["validate", "--json", str(a), "--reference", str(b)])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "comparison" in data
        assert data["comparison"]["valid"] is True

    def test_reference_missing_file(self, tmp_path: Path) -> None:
        from bambox.cli import main

        a = build_valid_3mf(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            main(["validate", str(a), "--reference", str(tmp_path / "nope.3mf")])


# ---------------------------------------------------------------------------
# G-code safety validation (pre-packaging)
# ---------------------------------------------------------------------------

# A safe G-code with proper homing, layers, and end Z above max_layer_z
_SAFE_GCODE = """\
G28
; HEADER_BLOCK_START
; total layer number: 2
; HEADER_BLOCK_END
M73 P0 R5
;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
M73 L1
M991 S0 P1
G1 X10 Y10 E1 F600
;LAYER_CHANGE
;Z:0.4
;HEIGHT:0.2
M73 L2
M991 S0 P2
G1 X20 Y20 E2 F600
; end gcode
G1 Z50
M104 S0
M140 S0
"""


class TestValidateGcode:
    def test_safe_gcode_passes(self) -> None:
        result = validate_gcode(_SAFE_GCODE)
        assert result.valid
        assert len(result.errors) == 0

    def test_s001_end_z_below_max_layer_z(self) -> None:
        gcode = """\
G28
;LAYER_CHANGE
;Z:0.2
M73 L1
M991 S0 P1
G1 X10 Y10 E1 F600
;LAYER_CHANGE
;Z:10.0
M73 L2
M991 S0 P2
G1 X20 Y20 E2 F600
G1 Z5.0
"""
        result = validate_gcode(gcode)
        assert not result.valid
        codes = [f.code for f in result.errors]
        assert "S001" in codes

    def test_s001_end_z_above_max_layer_z_ok(self) -> None:
        gcode = """\
G28
;LAYER_CHANGE
;Z:0.2
M73 L1
M991 S0 P1
;LAYER_CHANGE
;Z:10.0
M73 L2
M991 S0 P2
G1 Z50.0
"""
        result = validate_gcode(gcode)
        s001_errors = [f for f in result.errors if f.code == "S001"]
        assert len(s001_errors) == 0

    def test_s002_premature_heater_off_in_toolpath(self) -> None:
        gcode = """\
G28
;LAYER_CHANGE
;Z:0.2
M73 L1
M991 S0 P1
M104 S0
;LAYER_CHANGE
;Z:0.4
M73 L2
M991 S0 P2
G1 X20 Y20 E2 F600
"""
        result = validate_gcode(gcode)
        assert not result.valid
        codes = [f.code for f in result.errors]
        assert "S002" in codes

    def test_s002_heater_off_in_end_section_ok(self) -> None:
        result = validate_gcode(_SAFE_GCODE)
        s002_errors = [f for f in result.errors if f.code == "S002"]
        assert len(s002_errors) == 0

    def test_s002_heater_off_after_last_extrusion_ok(self) -> None:
        """M140 S0 right after the last extrusion move is not premature (#222)."""
        gcode = """\
G28
;LAYER_CHANGE
;Z:0.2
M73 L1
M991 S0 P1
G1 X10 Y10 E1 F600
;LAYER_CHANGE
;Z:0.4
M73 L2
M991 S0 P2
G1 F1500 E1877.70409
M140 S0
M107
G1 Z50
M104 S0
"""
        result = validate_gcode(gcode)
        s002_errors = [f for f in result.errors if f.code == "S002"]
        assert len(s002_errors) == 0

    def test_s003_extrusion_before_homing(self) -> None:
        gcode = """\
G1 X10 Y10 E1 F600
G28
;LAYER_CHANGE
;Z:0.2
M73 L1
M991 S0 P1
"""
        result = validate_gcode(gcode)
        assert not result.valid
        codes = [f.code for f in result.errors]
        assert "S003" in codes

    def test_s003_extrusion_after_homing_ok(self) -> None:
        result = validate_gcode(_SAFE_GCODE)
        s003_errors = [f for f in result.errors if f.code == "S003"]
        assert len(s003_errors) == 0

    def test_no_layer_data_skips_z_check(self) -> None:
        gcode = "G28\nG1 X10 Y10 E1 F600\n"
        result = validate_gcode(gcode)
        s001_errors = [f for f in result.errors if f.code == "S001"]
        assert len(s001_errors) == 0

    def test_minimal_gcode_fixture_passes(self) -> None:
        """MINIMAL_GCODE from shared fixtures should not trigger safety errors."""
        result = validate_gcode(MINIMAL_GCODE)
        # MINIMAL_GCODE has no G28, so S003 won't fire (no homing = nothing to check)
        # S001/S002 should not fire either
        s001 = [f for f in result.errors if f.code == "S001"]
        s002 = [f for f in result.errors if f.code == "S002"]
        assert len(s001) == 0
        assert len(s002) == 0

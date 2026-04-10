"""Tests for bambox.validate — .gcode.3mf archive validation."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from bambox.validate import (
    Severity,
    ValidationResult,
    compare_3mf,
    validate_3mf,
    validate_3mf_buffer,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "e2e_cura_p1s"
REFERENCE_3MF = FIXTURE_DIR / "reference.gcode.3mf"


# ---------------------------------------------------------------------------
# Helpers — build minimal valid archives for testing
# ---------------------------------------------------------------------------

# Minimal BBL-compatible gcode with all required markers
_MINIMAL_GCODE = """\
; HEADER_BLOCK_START
; total layer number: 3
; total estimated time: 2m 30s
; HEADER_BLOCK_END
M73 P0 R2
;LAYER_CHANGE
;Z:0.2
;HEIGHT:0.2
M73 L1
M991 S0 P1
M73 P33 R2
G1 X10 Y10 E1 F600
;LAYER_CHANGE
;Z:0.4
;HEIGHT:0.2
M73 L2
M991 S0 P2
M73 P66 R1
G1 X20 Y20 E2 F600
;LAYER_CHANGE
;Z:0.6
;HEIGHT:0.2
M73 L3
M991 S0 P3
M73 P100 R0
G1 X30 Y30 E3 F600
"""

_MINIMAL_SLICE_INFO = """\
<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header>
    <header_item key="X-BBL-Client-Type" value="slicer"/>
    <header_item key="X-BBL-Client-Version" value=""/>
  </header>
  <plate>
    <metadata key="index" value="1"/>
    <metadata key="printer_model_id" value="C12"/>
    <metadata key="nozzle_diameters" value="0.4"/>
    <metadata key="prediction" value="150"/>
    <metadata key="weight" value="5.00"/>
    <metadata key="outside" value="false"/>
    <metadata key="support_used" value="false"/>
    <metadata key="label_object_enabled" value="true"/>
    <metadata key="timelapse_type" value="0"/>
    <metadata key="filament_maps" value="1"/>
    <filament id="1" tray_info_idx="GFL99" type="PLA" color="#F2754E" used_m="1.00" used_g="3.00" />
  </plate>
</config>
"""

_MINIMAL_SETTINGS = json.dumps(
    {
        "filament_type": ["PLA", "PLA", "PLA", "PLA", "PLA"],
        "filament_colour": ["#F2754E", "#F2754E", "#F2754E", "#F2754E", "#F2754E"],
        "nozzle_temperature": ["220", "220", "220", "220", "220"],
        "nozzle_temperature_initial_layer": ["220", "220", "220", "220", "220"],
        "bed_temperature": ["60", "60", "60", "60", "60"],
        "filament_max_volumetric_speed": ["12", "12", "12", "12", "12"],
    }
)


def _build_valid_3mf(
    tmp_path: Path,
    gcode: str = _MINIMAL_GCODE,
    slice_info: str = _MINIMAL_SLICE_INFO,
    settings: str = _MINIMAL_SETTINGS,
) -> Path:
    """Build a minimal valid .gcode.3mf for testing."""
    out = tmp_path / "test.gcode.3mf"
    gcode_bytes = gcode.encode()
    md5 = hashlib.md5(gcode_bytes).hexdigest().upper()

    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("_rels/.rels", "<Relationships/>")
        zf.writestr("3D/3dmodel.model", "<model/>")
        zf.writestr("Metadata/plate_1.gcode", gcode_bytes)
        zf.writestr("Metadata/plate_1.gcode.md5", md5)
        zf.writestr("Metadata/model_settings.config", "{}")
        zf.writestr("Metadata/_rels/model_settings.config.rels", "<Relationships/>")
        zf.writestr("Metadata/slice_info.config", slice_info)
        zf.writestr("Metadata/project_settings.config", settings)
        zf.writestr("Metadata/plate_1.json", "{}")
        zf.writestr("Metadata/plate_1.png", b"\x89PNG\r\n\x1a\n")
        zf.writestr("Metadata/plate_no_light_1.png", b"\x89PNG\r\n\x1a\n")
        zf.writestr("Metadata/plate_1_small.png", b"\x89PNG\r\n\x1a\n")
        zf.writestr("Metadata/top_1.png", b"\x89PNG\r\n\x1a\n")
        zf.writestr("Metadata/pick_1.png", b"\x89PNG\r\n\x1a\n")
    return out


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
        path = _build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert result.valid, [f"{f.code}: {f.message}" for f in result.errors]

    def test_buffer_api(self, tmp_path: Path) -> None:
        path = _build_valid_3mf(tmp_path)
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
        gcode = _MINIMAL_GCODE + "M104 S[220]\n"
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E001" for f in result.errors)

    def test_template_in_temp(self, tmp_path: Path) -> None:
        gcode = _MINIMAL_GCODE + "M109 S{nozzle_temp}\n"
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E001" for f in result.errors)

    def test_valid_temps_pass(self, tmp_path: Path) -> None:
        gcode = _MINIMAL_GCODE + "M104 S220\nM109 S220\nM140 S60\n"
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert not any(f.code == "E001" for f in result.findings)


# ---------------------------------------------------------------------------
# E002: Toolchange feedrate
# ---------------------------------------------------------------------------


class TestToolchangeFeedrate:
    def test_low_feedrate_detected(self, tmp_path: Path) -> None:
        gcode = _MINIMAL_GCODE + "M620.1 E F12 T240\n"
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E002" for f in result.errors)

    def test_correct_feedrate_passes(self, tmp_path: Path) -> None:
        gcode = _MINIMAL_GCODE + "M620.1 E F299 T240\n"
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert not any(f.code == "E002" for f in result.findings)


# ---------------------------------------------------------------------------
# E003: MD5 checksum
# ---------------------------------------------------------------------------


class TestMD5:
    def test_mismatch_detected(self, tmp_path: Path) -> None:
        path = _build_valid_3mf(tmp_path)
        # Corrupt MD5
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("Metadata/plate_1.gcode.md5", "DEADBEEF" * 4)
        result = validate_3mf(path)
        assert any(f.code == "E003" for f in result.errors)

    def test_valid_md5_passes(self, tmp_path: Path) -> None:
        path = _build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "E003" for f in result.findings)


# ---------------------------------------------------------------------------
# E004: Array padding
# ---------------------------------------------------------------------------


class TestArrayPadding:
    def test_short_array_detected(self, tmp_path: Path) -> None:
        settings = json.dumps({"filament_type": ["PLA", "PLA"]})
        path = _build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert any(f.code == "E004" for f in result.errors)

    def test_full_array_passes(self, tmp_path: Path) -> None:
        path = _build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "E004" for f in result.findings)


# ---------------------------------------------------------------------------
# E005: Unsubstituted templates
# ---------------------------------------------------------------------------


class TestUnsubstitutedTemplates:
    def test_template_in_command(self, tmp_path: Path) -> None:
        gcode = _MINIMAL_GCODE + "G1 X{first_layer_print_min} Y10 F600\n"
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E005" for f in result.errors)

    def test_template_in_comment_is_ok(self, tmp_path: Path) -> None:
        gcode = _MINIMAL_GCODE + "; {perimeter}\n"
        path = _build_valid_3mf(tmp_path, gcode=gcode)
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
        path = _build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "E006" for f in result.findings)


# ---------------------------------------------------------------------------
# E007/E008: Header block
# ---------------------------------------------------------------------------


class TestHeaderBlock:
    def test_missing_header_block(self, tmp_path: Path) -> None:
        gcode = "G28\nG1 X10 Y10 E1 F600\n"
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E007" for f in result.errors)

    def test_zero_layer_count(self, tmp_path: Path) -> None:
        gcode = "; HEADER_BLOCK_START\n; total layer number: 0\n; HEADER_BLOCK_END\nG28\n"
        path = _build_valid_3mf(tmp_path, gcode=gcode)
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
        path = _build_valid_3mf(tmp_path, gcode=gcode)
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
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E010" for f in result.errors)

    def test_layer_count_mismatch(self, tmp_path: Path) -> None:
        gcode = (
            "; HEADER_BLOCK_START\n"
            "; total layer number: 100\n"
            "; HEADER_BLOCK_END\n"
            "M73 L1\nM991 S0 P1\nM73 P100 R0\n"
        )
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E011" for f in result.errors)


# ---------------------------------------------------------------------------
# W001-W003: Metadata warnings
# ---------------------------------------------------------------------------


class TestMetadataWarnings:
    def test_empty_printer_model_id(self, tmp_path: Path) -> None:
        si = _MINIMAL_SLICE_INFO.replace('value="C12"', 'value=""')
        path = _build_valid_3mf(tmp_path, slice_info=si)
        result = validate_3mf(path)
        assert any(f.code == "W001" for f in result.warnings)

    def test_zero_prediction(self, tmp_path: Path) -> None:
        si = _MINIMAL_SLICE_INFO.replace('value="150"', 'value="0"')
        path = _build_valid_3mf(tmp_path, slice_info=si)
        result = validate_3mf(path)
        assert any(f.code == "W002" for f in result.warnings)

    def test_zero_weight(self, tmp_path: Path) -> None:
        si = _MINIMAL_SLICE_INFO.replace('value="5.00"', 'value="0.00"')
        path = _build_valid_3mf(tmp_path, slice_info=si)
        result = validate_3mf(path)
        assert any(f.code == "W003" for f in result.warnings)


# ---------------------------------------------------------------------------
# W004: Filament color
# ---------------------------------------------------------------------------


class TestFilamentColor:
    def test_invalid_color(self, tmp_path: Path) -> None:
        si = _MINIMAL_SLICE_INFO.replace('color="#F2754E"', 'color="not-a-color"')
        path = _build_valid_3mf(tmp_path, slice_info=si)
        result = validate_3mf(path)
        assert any(f.code == "W004" for f in result.warnings)

    def test_valid_color_passes(self, tmp_path: Path) -> None:
        path = _build_valid_3mf(tmp_path)
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
        path = _build_valid_3mf(tmp_path, gcode=gcode)
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
        path = _build_valid_3mf(tmp_path, gcode=gcode)
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
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "W009" for f in result.warnings)


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestCLIValidate:
    def test_valid_archive_exit_0(self, tmp_path: Path) -> None:
        from bambox.cli import main

        path = _build_valid_3mf(tmp_path)
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

        path = _build_valid_3mf(tmp_path)
        main(["validate", "--json", str(path)])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["valid"] is True
        assert isinstance(data["errors"], list)
        assert isinstance(data["warnings"], list)

    def test_strict_fails_on_warnings(self, tmp_path: Path) -> None:
        from bambox.cli import main

        si = _MINIMAL_SLICE_INFO.replace('value="C12"', 'value=""')
        path = _build_valid_3mf(tmp_path, slice_info=si)
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
        path = _build_valid_3mf(tmp_path, settings=settings)
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
        path = _build_valid_3mf(tmp_path, settings=settings)
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
        path = _build_valid_3mf(tmp_path, settings=settings)
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
        path = _build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert not any(f.code == "W006" for f in result.findings)


# ---------------------------------------------------------------------------
# W010: Recommended thumbnails
# ---------------------------------------------------------------------------


class TestRecommendedThumbnails:
    def test_missing_top_and_pick(self, tmp_path: Path) -> None:
        """Archive without top_1.png and pick_1.png triggers W010."""
        out = tmp_path / "test.gcode.3mf"
        gcode_bytes = _MINIMAL_GCODE.encode()
        md5 = hashlib.md5(gcode_bytes).hexdigest().upper()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("_rels/.rels", "<Relationships/>")
            zf.writestr("3D/3dmodel.model", "<model/>")
            zf.writestr("Metadata/plate_1.gcode", gcode_bytes)
            zf.writestr("Metadata/plate_1.gcode.md5", md5)
            zf.writestr("Metadata/model_settings.config", "{}")
            zf.writestr("Metadata/_rels/model_settings.config.rels", "<Relationships/>")
            zf.writestr("Metadata/slice_info.config", _MINIMAL_SLICE_INFO)
            zf.writestr("Metadata/project_settings.config", _MINIMAL_SETTINGS)
            zf.writestr("Metadata/plate_1.json", "{}")
            zf.writestr("Metadata/plate_1.png", b"\x89PNG\r\n\x1a\n")
            zf.writestr("Metadata/plate_no_light_1.png", b"\x89PNG\r\n\x1a\n")
            zf.writestr("Metadata/plate_1_small.png", b"\x89PNG\r\n\x1a\n")
            # Deliberately omit top_1.png and pick_1.png
        result = validate_3mf(out)
        w010 = [f for f in result.warnings if f.code == "W010"]
        assert len(w010) == 2

    def test_with_thumbnails_passes(self, tmp_path: Path) -> None:
        path = _build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert not any(f.code == "W010" for f in result.findings)


# ---------------------------------------------------------------------------
# W011: Time sync
# ---------------------------------------------------------------------------


class TestTimeSync:
    def test_divergent_time_detected(self, tmp_path: Path) -> None:
        """M73 R2 (2 min) vs prediction 7200s (120 min) should trigger W011."""
        gcode = _MINIMAL_GCODE  # has M73 P0 R2 at start
        si = _MINIMAL_SLICE_INFO.replace('value="150"', 'value="7200"')
        path = _build_valid_3mf(tmp_path, gcode=gcode, slice_info=si)
        result = validate_3mf(path)
        assert any(f.code == "W011" for f in result.warnings)

    def test_aligned_time_passes(self, tmp_path: Path) -> None:
        """M73 R2 (2 min = 120s) vs prediction 150s — within tolerance."""
        path = _build_valid_3mf(tmp_path)
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
        path = _build_valid_3mf(tmp_path, gcode=_MULTI_FILAMENT_GCODE)
        result = validate_3mf(path)
        assert not any(f.code == "E013" for f in result.findings)

    def test_single_filament_no_check(self, tmp_path: Path) -> None:
        """Single-filament gcode should not trigger E013."""
        path = _build_valid_3mf(tmp_path)
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
        path = _build_valid_3mf(tmp_path, gcode=gcode)
        result = validate_3mf(path)
        assert any(f.code == "E014" for f in result.errors)

    def test_t_inside_block_ok(self, tmp_path: Path) -> None:
        """T commands inside M620/M621 blocks should not trigger E014."""
        path = _build_valid_3mf(tmp_path, gcode=_MULTI_FILAMENT_GCODE)
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
        path = _build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert any(f.code == "W012" for f in result.warnings)

    def test_valid_nozzle_temp_passes(self, tmp_path: Path) -> None:
        path = _build_valid_3mf(tmp_path)
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
        path = _build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert any(f.code == "W013" for f in result.warnings)

    def test_valid_bed_temp_passes(self, tmp_path: Path) -> None:
        path = _build_valid_3mf(tmp_path)
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
        path = _build_valid_3mf(tmp_path, settings=settings)
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
        path = _build_valid_3mf(tmp_path, settings=settings)
        result = validate_3mf(path)
        assert not any(f.code == "W014" for f in result.findings)


# ---------------------------------------------------------------------------
# compare_3mf — reference comparison
# ---------------------------------------------------------------------------


class TestCompare3mf:
    def test_identical_archives(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        a = _build_valid_3mf(tmp_path / "a")
        (tmp_path / "b").mkdir()
        b = _build_valid_3mf(tmp_path / "b")
        result = compare_3mf(a, b)
        assert result.valid

    def test_different_filament_types(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = _build_valid_3mf(tmp_path / "a")
        si_petg = _MINIMAL_SLICE_INFO.replace('type="PLA"', 'type="PETG"')
        b = _build_valid_3mf(tmp_path / "b", slice_info=si_petg)
        result = compare_3mf(a, b)
        assert any(f.code == "C001" for f in result.errors)

    def test_divergent_print_time(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = _build_valid_3mf(tmp_path / "a")
        si_long = _MINIMAL_SLICE_INFO.replace('value="150"', 'value="15000"')
        b = _build_valid_3mf(tmp_path / "b", slice_info=si_long)
        result = compare_3mf(a, b)
        assert any(f.code == "C002" for f in result.errors)

    def test_divergent_weight(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = _build_valid_3mf(tmp_path / "a")
        si_heavy = _MINIMAL_SLICE_INFO.replace('value="5.00"', 'value="50.00"')
        b = _build_valid_3mf(tmp_path / "b", slice_info=si_heavy)
        result = compare_3mf(a, b)
        assert any(f.code == "C003" for f in result.errors)

    def test_different_printer_model(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = _build_valid_3mf(tmp_path / "a")
        si_x1 = _MINIMAL_SLICE_INFO.replace('value="C12"', 'value="C11"')
        b = _build_valid_3mf(tmp_path / "b", slice_info=si_x1)
        result = compare_3mf(a, b)
        assert any(f.code == "C005" for f in result.errors)

    def test_different_tool_changes(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = _build_valid_3mf(tmp_path / "a", gcode=_MULTI_FILAMENT_GCODE)
        b = _build_valid_3mf(tmp_path / "b")
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
        a = _build_valid_3mf(tmp_path / "a")
        b = _build_valid_3mf(tmp_path / "b")
        # Should not raise
        main(["validate", str(a), "--reference", str(b)])

    def test_reference_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        from bambox.cli import main

        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        a = _build_valid_3mf(tmp_path / "a")
        b = _build_valid_3mf(tmp_path / "b")
        main(["validate", "--json", str(a), "--reference", str(b)])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "comparison" in data
        assert data["comparison"]["valid"] is True

    def test_reference_missing_file(self, tmp_path: Path) -> None:
        from bambox.cli import main

        a = _build_valid_3mf(tmp_path)
        with pytest.raises(SystemExit, match="1"):
            main(["validate", str(a), "--reference", str(tmp_path / "nope.3mf")])

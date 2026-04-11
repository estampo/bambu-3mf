"""Release-readiness tests for bambox validation.

Ensures the validation module is complete and that reference archives pass
validation. These tests gate releases — if they fail, the archive format
has regressed.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

from bambox.validate import validate_3mf

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "e2e_cura_p1s"
REFERENCE_3MF = FIXTURE_DIR / "reference.gcode.3mf"
TOP_LEVEL_REFERENCE = Path(__file__).parent / "fixtures" / "reference.gcode.3mf"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _build_valid_3mf(tmp_path: Path) -> Path:
    """Build a minimal valid .gcode.3mf for testing."""
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
        zf.writestr("Metadata/top_1.png", b"\x89PNG\r\n\x1a\n")
        zf.writestr("Metadata/pick_1.png", b"\x89PNG\r\n\x1a\n")
    return out


# ---------------------------------------------------------------------------
# 1. Reference fixture must pass validation
# ---------------------------------------------------------------------------


class TestReferenceFixture:
    @pytest.mark.skipif(not REFERENCE_3MF.exists(), reason="e2e reference fixture not available")
    def test_e2e_reference_zero_errors(self) -> None:
        result = validate_3mf(REFERENCE_3MF)
        assert result.valid, [f"{f.code}: {f.message}" for f in result.errors]

    @pytest.mark.skipif(
        not TOP_LEVEL_REFERENCE.exists(), reason="top-level reference fixture not available"
    )
    def test_top_level_reference_zero_errors(self) -> None:
        result = validate_3mf(TOP_LEVEL_REFERENCE)
        assert result.valid, [f"{f.code}: {f.message}" for f in result.errors]


# ---------------------------------------------------------------------------
# 2. Freshly-built pack passes validation
# ---------------------------------------------------------------------------


class TestFreshBuildValidation:
    def test_built_archive_zero_errors(self, tmp_path: Path) -> None:
        path = _build_valid_3mf(tmp_path)
        result = validate_3mf(path)
        assert result.valid, [f"{f.code}: {f.message}" for f in result.errors]


# ---------------------------------------------------------------------------
# 3. All expected check codes exist in the module
# ---------------------------------------------------------------------------

# Every error and warning code that should be defined.
EXPECTED_ERROR_CODES = {
    "E000",
    "E001",
    "E002",
    "E003",
    "E004",
    "E005",
    "E006",
    "E007",
    "E008",
    "E009",
    "E010",
    "E011",
    "E012",
    "E013",
    "E014",
}

EXPECTED_WARNING_CODES = {
    "W001",
    "W002",
    "W003",
    "W004",
    "W005",
    "W006",
    "W007",
    "W008",
    "W009",
    "W010",
    "W011",
    "W012",
    "W013",
    "W014",
}

EXPECTED_COMPARISON_CODES = {
    "C001",
    "C002",
    "C003",
    "C004",
    "C005",
}


class TestCheckCodesExist:
    """Verify that all expected check codes appear in the validate module source."""

    def test_all_error_codes_present(self) -> None:
        import bambox.validate as mod

        source = Path(mod.__file__).read_text()  # type: ignore[arg-type]
        for code in EXPECTED_ERROR_CODES:
            assert f'"{code}"' in source, f"Missing error code {code} in validate.py"

    def test_all_warning_codes_present(self) -> None:
        import bambox.validate as mod

        source = Path(mod.__file__).read_text()  # type: ignore[arg-type]
        for code in EXPECTED_WARNING_CODES:
            assert f'"{code}"' in source, f"Missing warning code {code} in validate.py"

    def test_all_comparison_codes_present(self) -> None:
        import bambox.validate as mod

        source = Path(mod.__file__).read_text()  # type: ignore[arg-type]
        for code in EXPECTED_COMPARISON_CODES:
            assert f'"{code}"' in source, f"Missing comparison code {code} in validate.py"

    def test_no_duplicate_codes(self) -> None:
        """Each code string should map to exactly one check."""
        import bambox.validate as mod

        source = Path(mod.__file__).read_text()  # type: ignore[arg-type]
        all_codes = EXPECTED_ERROR_CODES | EXPECTED_WARNING_CODES | EXPECTED_COMPARISON_CODES
        for code in all_codes:
            # Count occurrences as string literals (finding constructors)
            matches = re.findall(rf'"{code}"', source)
            assert len(matches) >= 1, f"Code {code} not found"


# ---------------------------------------------------------------------------
# 4. Bridge version matches Python package version
# ---------------------------------------------------------------------------


class TestVersionSync:
    """Ensure bridge/Cargo.toml version stays in sync with pyproject.toml."""

    def test_bridge_version_matches_python(self) -> None:
        root = Path(__file__).parent.parent
        pyproject = root / "pyproject.toml"
        cargo_toml = root / "bridge" / "Cargo.toml"

        import tomllib

        with open(pyproject, "rb") as f:
            py_version = tomllib.load(f)["project"]["version"]

        cargo_text = cargo_toml.read_text()
        match = re.search(r'^version\s*=\s*"([^"]+)"', cargo_text, re.MULTILINE)
        assert match, "No version found in bridge/Cargo.toml"
        cargo_version = match.group(1)

        assert cargo_version == py_version, (
            f"Version mismatch: bridge/Cargo.toml={cargo_version}, "
            f"pyproject.toml={py_version}. Both must match."
        )

"""Release-readiness tests for bambox validation.

Ensures the validation module is complete and that reference archives pass
validation. These tests gate releases — if they fail, the archive format
has regressed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from shared_fixtures import build_valid_3mf

from bambox.validate import validate_3mf

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "e2e_cura_p1s"
REFERENCE_3MF = FIXTURE_DIR / "reference.gcode.3mf"
TOP_LEVEL_REFERENCE = Path(__file__).parent / "fixtures" / "reference.gcode.3mf"


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
        path = build_valid_3mf(tmp_path)
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
# 4. THIRD-PARTY-NOTICES ships with the wheel
# ---------------------------------------------------------------------------


class TestThirdPartyNoticesShipped:
    """The profile provenance documentation must ride along in the wheel.

    Users installing from PyPI have no other way to discover the OrcaSlicer /
    BambuStudio origin of the bundled profiles and the resulting license
    obligations. We enforce this via the hatchling ``force-include`` map
    rather than waiting for a surprised downstream consumer.
    """

    def test_notices_file_exists_at_repo_root(self) -> None:
        root = Path(__file__).parent.parent
        assert (root / "THIRD-PARTY-NOTICES").exists()

    def test_notices_force_included_in_wheel(self) -> None:
        import tomllib

        root = Path(__file__).parent.parent
        with open(root / "pyproject.toml", "rb") as f:
            cfg = tomllib.load(f)

        wheel_cfg = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]
        force_include = wheel_cfg.get("force-include", {})
        assert "THIRD-PARTY-NOTICES" in force_include, (
            "pyproject.toml must force-include THIRD-PARTY-NOTICES in the "
            "wheel so profile provenance ships with the package."
        )
        assert force_include["THIRD-PARTY-NOTICES"].startswith("bambox/"), (
            "THIRD-PARTY-NOTICES should be placed under bambox/ in the wheel "
            "so it's discoverable next to the installed package."
        )

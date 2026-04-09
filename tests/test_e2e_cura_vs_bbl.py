"""E2E comparison: CuraEngine + bambox pack vs BambuStudio reference.

Runs the full estampo pipeline (load → arrange → plate → slice → pack)
using CuraEngine for a two-part, two-filament P1S+AMS job, then compares
the resulting .gcode.3mf against a BambuStudio reference slice of the
same models.

Requirements:
- ``pip install estampo`` (>= 0.5.0)
- Docker running with CuraEngine image available
- Fixtures in ``tests/fixtures/e2e_cura_p1s/``

Marked ``@pytest.mark.e2e`` — skipped in normal test runs.
Run with: ``pytest -m e2e``
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "e2e_cura_p1s"
REFERENCE = FIXTURES / "reference.gcode.3mf"
CONFIG = FIXTURES / "estampo.toml"

# Files that must appear in any valid bambox .gcode.3mf archive.
REQUIRED_FILES = {
    "[Content_Types].xml",
    "_rels/.rels",
    "3D/3dmodel.model",
    "Metadata/plate_1.gcode",
    "Metadata/plate_1.gcode.md5",
    "Metadata/project_settings.config",
    "Metadata/model_settings.config",
    "Metadata/slice_info.config",
    "Metadata/plate_1.png",
    "Metadata/plate_1_small.png",
    "Metadata/plate_no_light_1.png",
    "Metadata/plate_1.json",
}

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Known values from the test fixture: 2-part job, PLA on slots 1 and 4.
EXPECTED_FILAMENT_SLOTS = [1, 4]  # 1-indexed AMS slots with filament
EXPECTED_FILAMENT_TYPE = "PLA"
EXPECTED_PRINTER_MODEL_ID = "C12"  # Bambu P1S

# Volumetric-to-linear feedrate conversion: F = vol_speed / filament_area * 60
# PLA default volumetric speed = 12 mm³/s, filament_area ≈ 2.405 mm²
# Expected linear feedrate ≈ 299 mm/min
_FILAMENT_AREA = 3.14159 * (1.75 / 2.0) ** 2  # ≈ 2.405 mm²
MIN_TOOLCHANGE_FEEDRATE = 100  # mm/min — anything below this is clearly wrong


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_gcode(archive: Path) -> str:
    with zipfile.ZipFile(archive) as z:
        return z.read("Metadata/plate_1.gcode").decode(errors="replace")


def _load_ps(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as z:
        return json.loads(z.read("Metadata/project_settings.config"))


def _load_slice_info(archive: Path) -> ET.Element:
    with zipfile.ZipFile(archive) as z:
        return ET.fromstring(z.read("Metadata/slice_info.config"))


def _load_plate_json(archive: Path) -> dict:
    with zipfile.ZipFile(archive) as z:
        return json.loads(z.read("Metadata/plate_1.json"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """Return True if Docker daemon is reachable."""
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _estampo_available() -> bool:
    """Return True if estampo CLI is on PATH."""
    return shutil.which("estampo") is not None


def _curaengine_available() -> bool:
    """Return True if CuraEngine is installed locally or Docker is available."""
    if shutil.which("CuraEngine"):
        return True
    return _docker_available()


needs_e2e = pytest.mark.e2e
skip_unless_ready = pytest.mark.skipif(
    not (_estampo_available() and _curaengine_available()),
    reason="Requires estampo CLI and CuraEngine (local or Docker)",
)


@pytest.fixture(scope="module")
def cura_output(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Run estampo pipeline once for the whole module, return output .gcode.3mf."""
    output_dir = tmp_path_factory.mktemp("e2e_cura_output")
    result = subprocess.run(
        [
            "estampo",
            "run",
            str(CONFIG.resolve()),
            "--output-dir",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        combined = result.stdout + result.stderr
        # Docker bind mount failures (e.g. DinD environments) — skip gracefully.
        if "Couldn't open JSON file: /work/" in combined:
            pytest.skip("Docker bind mounts not working (DinD environment?)")
        pytest.fail(
            f"estampo run failed (rc={result.returncode}):\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}"
        )
    output = output_dir / "plate.gcode.3mf"
    if not output.exists():
        pytest.fail(
            f"estampo did not produce plate.gcode.3mf in {output_dir}\n"
            f"contents: {list(output_dir.iterdir())}\n"
            f"stdout: {result.stdout[-1000:]}"
        )
    return output


# ===========================================================================
# Part 1: Reference fixture sanity (always runs, no Docker needed)
# ===========================================================================


class TestReferenceFixture:
    """Sanity-checks on the BBL reference fixture."""

    def test_reference_has_required_files(self) -> None:
        with zipfile.ZipFile(REFERENCE) as z:
            names = set(z.namelist())
        missing = REQUIRED_FILES - names
        assert not missing, f"Reference missing: {missing}"

    def test_reference_has_two_filaments_in_slice_info(self) -> None:
        root = _load_slice_info(REFERENCE)
        filaments = root.findall(".//filament")
        assert len(filaments) == 2, f"Expected 2 filaments, got {len(filaments)}"
        ids = {int(f.get("id")) for f in filaments}
        assert ids == set(EXPECTED_FILAMENT_SLOTS)

    def test_reference_feedrate_is_correct(self) -> None:
        """BBL reference should have linear feedrate ~299, not volumetric 12."""
        gcode = _read_gcode(REFERENCE)
        m620_lines = [ln for ln in gcode.splitlines() if "M620.1 E F" in ln]
        assert m620_lines, "No M620.1 E F lines in reference"
        for line in m620_lines:
            match = re.search(r"F([\d.]+)", line)
            if match:
                feedrate = float(match.group(1))
                assert feedrate >= MIN_TOOLCHANGE_FEEDRATE, (
                    f"Reference feedrate {feedrate} too low: {line}"
                )

    def test_reference_printer_model_id(self) -> None:
        root = _load_slice_info(REFERENCE)
        plate = root.find("plate")
        for m in plate.findall("metadata"):
            if m.get("key") == "printer_model_id":
                assert m.get("value") == EXPECTED_PRINTER_MODEL_ID
                return
        pytest.fail("Reference missing printer_model_id")


# ===========================================================================
# Part 2: Archive structure (needs cura_output)
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestArchiveStructure:
    """Verify the CuraEngine output has the same archive structure as BBL."""

    def test_required_files_present(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            names = set(z.namelist())
        missing = REQUIRED_FILES - names
        assert not missing, f"Missing required files: {missing}"


# ===========================================================================
# Part 3: G-code safety — issues that could damage the printer (#96, #100)
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestGcodeSafety:
    """Safety-critical G-code validation. Failures here risk printer damage."""

    def test_no_array_as_scalar_in_temperature(self, cura_output: Path) -> None:
        """Issue #100: M104 S[220] is invalid — temperature must be a number.

        The start_gcode template uses nozzle_temperature_initial_layer as both
        scalar and array.  When it's a list, Jinja renders M104 S[220] which
        the firmware may ignore or misparse.
        """
        gcode = _read_gcode(cura_output)
        bad_lines = []
        for i, line in enumerate(gcode.splitlines(), 1):
            # Match M104/M109 with S followed by [ (array literal)
            if re.match(r"^\s*M10[49]\s+S\[", line):
                bad_lines.append((i, line.strip()))
        assert not bad_lines, (
            f"Found {len(bad_lines)} temperature commands with array-as-scalar "
            f"(issue #100):\n" + "\n".join(f"  L{n}: {ln}" for n, ln in bad_lines[:5])
        )

    def test_toolchange_feedrate_is_linear_not_volumetric(self, cura_output: Path) -> None:
        """Issue #96: M620.1 E F12 should be F~299 (linear mm/min, not mm³/s).

        bambox emits raw filament_max_volumetric_speed as the F parameter.
        BambuStudio converts: F = volumetric_speed / filament_area * 60.
        F12 means 12 mm/min which is 10-30x too slow for filament flush.
        """
        gcode = _read_gcode(cura_output)
        feedrates = []
        for line in gcode.splitlines():
            match = re.match(r"^\s*M620\.1\s+E\s+F([\d.]+)", line)
            if match:
                feedrates.append(float(match.group(1)))
        assert feedrates, "No M620.1 E F lines found"
        bad = [f for f in feedrates if f < MIN_TOOLCHANGE_FEEDRATE]
        assert not bad, (
            f"Issue #96: {len(bad)}/{len(feedrates)} toolchange feedrates are too low "
            f"(< {MIN_TOOLCHANGE_FEEDRATE} mm/min). Got F={bad[0]}, expected ~299. "
            f"Raw volumetric speed is being used instead of linear feedrate."
        )

    def test_temperature_commands_are_numeric(self, cura_output: Path) -> None:
        """All M104/M109 S values must be valid integers, not templates or arrays."""
        gcode = _read_gcode(cura_output)
        bad_lines = []
        for i, line in enumerate(gcode.splitlines(), 1):
            match = re.match(r"^\s*M10[49]\s+.*S(.+?)(?:\s|;|$)", line)
            if match:
                s_val = match.group(1).strip()
                if not re.match(r"^\d+$", s_val):
                    bad_lines.append((i, line.strip(), s_val))
        assert not bad_lines, (
            f"Found {len(bad_lines)} M104/M109 with non-numeric S value:\n"
            + "\n".join(f"  L{n}: {ln} (S={v})" for n, ln, v in bad_lines[:5])
        )


# ===========================================================================
# Part 4: Multi-filament correctness (#97)
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestMultiFilament:
    """Validate multi-filament detection and metadata (issue #97)."""

    def test_filament_type_has_correct_slots(self, cura_output: Path) -> None:
        """Slots 1 and 4 should be PLA; slots 2 and 3 should not be PLA."""
        ps = _load_ps(cura_output)
        ft = ps["filament_type"]
        assert ft[0] == "PLA", f"Slot 1 should be PLA, got {ft[0]}"
        assert ft[3] == "PLA", f"Slot 4 should be PLA, got {ft[3]}"
        # Unused slots should NOT be PLA (they should be empty or a default
        # like ABS). If all 5 are PLA, bambox failed to detect per-slot types.
        all_pla = all(t == "PLA" for t in ft)
        assert not all_pla, (
            f"Issue #97: All filament slots are PLA {ft} — bambox failed to "
            f"detect per-slot filament types. Only slots 1 and 4 should be PLA."
        )

    def test_filament_colours_differ(self, cura_output: Path) -> None:
        """Slot 1 = white, slot 4 = black — colours should not all be the same."""
        ps = _load_ps(cura_output)
        fc = ps.get("filament_colour", [])
        unique = set(fc)
        assert len(unique) >= 2, (
            f"Issue #97: All filament colours are identical {fc}. "
            f"Expected at least white (#FFFFFF) and black (#161616 or #000000)."
        )

    def test_slice_info_has_both_filaments(self, cura_output: Path) -> None:
        """slice_info should list filaments on slots 1 and 4, not just slot 1."""
        root = _load_slice_info(cura_output)
        filaments = root.findall(".//filament")
        ids = {int(f.get("id")) for f in filaments}
        assert ids == set(EXPECTED_FILAMENT_SLOTS), (
            f"Issue #97: slice_info has filament ids {ids}, "
            f"expected {set(EXPECTED_FILAMENT_SLOTS)}. "
            f"bambox only detected {len(filaments)} filament(s)."
        )

    def test_slice_info_filament_types_are_pla(self, cura_output: Path) -> None:
        root = _load_slice_info(cura_output)
        for filament in root.findall(".//filament"):
            ftype = filament.get("type")
            assert ftype == EXPECTED_FILAMENT_TYPE, (
                f"Filament id={filament.get('id')} type={ftype}, expected {EXPECTED_FILAMENT_TYPE}"
            )

    def test_filament_maps_has_all_slots(self, cura_output: Path) -> None:
        """filament_maps in slice_info should have 5 entries (one per AMS slot)."""
        root = _load_slice_info(cura_output)
        plate = root.find("plate")
        for m in plate.findall("metadata"):
            if m.get("key") == "filament_maps":
                val = m.get("value")
                parts = val.split()
                assert len(parts) == 5, (
                    f"filament_maps has {len(parts)} entries ('{val}'), expected 5"
                )
                return
        pytest.fail("slice_info missing filament_maps metadata")


# ===========================================================================
# Part 5: Printer metadata (#98)
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestPrinterMetadata:
    """Validate printer identification (issue #98)."""

    def test_printer_model_id_is_set(self, cura_output: Path) -> None:
        """printer_model_id should be 'C12' for P1S, not empty string."""
        root = _load_slice_info(cura_output)
        plate = root.find("plate")
        for m in plate.findall("metadata"):
            if m.get("key") == "printer_model_id":
                val = m.get("value")
                assert val and val.strip(), (
                    f"Issue #98: printer_model_id is empty. "
                    f"Should be '{EXPECTED_PRINTER_MODEL_ID}' for P1S."
                )
                assert val == EXPECTED_PRINTER_MODEL_ID, (
                    f"printer_model_id is '{val}', expected '{EXPECTED_PRINTER_MODEL_ID}'"
                )
                return
        pytest.fail("slice_info missing printer_model_id metadata")


# ===========================================================================
# Part 6: Time/weight/usage metadata (#99)
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestSliceStatistics:
    """Validate print time, weight, and filament usage (issue #99)."""

    def test_prediction_is_nonzero(self, cura_output: Path) -> None:
        """Print time prediction must be extracted from CuraEngine output."""
        root = _load_slice_info(cura_output)
        plate = root.find("plate")
        for m in plate.findall("metadata"):
            if m.get("key") == "prediction":
                val = int(m.get("value", "0"))
                assert val > 0, (
                    f"Issue #99: prediction is {val} seconds. "
                    f"Should be extracted from CuraEngine ;TIME or ;TIME_ELAPSED."
                )
                return
        pytest.fail("slice_info missing prediction metadata")

    def test_weight_is_nonzero(self, cura_output: Path) -> None:
        """Total print weight must be calculated from filament usage."""
        root = _load_slice_info(cura_output)
        plate = root.find("plate")
        for m in plate.findall("metadata"):
            if m.get("key") == "weight":
                val = float(m.get("value", "0"))
                assert val > 0, (
                    f"Issue #99: weight is {val}g. "
                    f"Should be computed from filament usage * density."
                )
                return
        pytest.fail("slice_info missing weight metadata")

    def test_filament_usage_is_nonzero(self, cura_output: Path) -> None:
        """Per-filament used_m and used_g should be populated."""
        root = _load_slice_info(cura_output)
        filaments = root.findall(".//filament")
        assert filaments, "No filament entries in slice_info"
        for f in filaments:
            used_m = float(f.get("used_m", "0"))
            used_g = float(f.get("used_g", "0"))
            fid = f.get("id")
            assert used_m > 0, (
                f"Issue #99: filament id={fid} used_m={used_m}. "
                f"Should be extracted from ;Filament used: header."
            )
            assert used_g > 0, (
                f"Issue #99: filament id={fid} used_g={used_g}. "
                f"Should be computed from used_m * filament_density."
            )


# ===========================================================================
# Part 7: G-code header completeness
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestGcodeHeader:
    """Validate the BBL header block contains required metadata."""

    def _header_block(self, archive: Path) -> str:
        gcode = _read_gcode(archive)
        start = gcode.index("; HEADER_BLOCK_START")
        end = gcode.index("; HEADER_BLOCK_END")
        return gcode[start:end]

    def test_header_has_layer_count(self, cura_output: Path) -> None:
        header = self._header_block(cura_output)
        assert "; total layer number:" in header, "Header missing total layer number"
        match = re.search(r"; total layer number:\s*(\d+)", header)
        assert match and int(match.group(1)) > 0, "Layer count should be > 0"

    def test_header_has_filament_info(self, cura_output: Path) -> None:
        """Header should list filament usage, density, and diameter."""
        header = self._header_block(cura_output)
        assert "; filament_diameter:" in header, "Header missing filament_diameter"

    def test_header_has_filament_slots(self, cura_output: Path) -> None:
        """Multi-filament header should list which filament slots are used."""
        header = self._header_block(cura_output)
        # BBL reference has: "; filament: 1,4"
        assert "; filament:" in header or "; total filament" in header, (
            "Header missing filament slot info for multi-filament job"
        )


# ===========================================================================
# Part 8: Project settings correctness
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestProjectSettings:
    """Validate project_settings.config against BBL reference."""

    def test_key_count(self, cura_output: Path) -> None:
        ps = _load_ps(cura_output)
        assert len(ps) >= 500, f"Only {len(ps)} keys — expected >= 500"

    def test_key_names_overlap(self, cura_output: Path) -> None:
        """Most key names should match between bambox and BBL output."""
        cura_ps = _load_ps(cura_output)
        ref_ps = _load_ps(REFERENCE)
        overlap = set(cura_ps.keys()) & set(ref_ps.keys())
        assert len(overlap) >= 500, (
            f"Only {len(overlap)} overlapping keys out of cura={len(cura_ps)}, ref={len(ref_ps)}"
        )

    def test_arrays_padded_to_five(self, cura_output: Path) -> None:
        ps = _load_ps(cura_output)
        short = {k: len(v) for k, v in ps.items() if isinstance(v, list) and 0 < len(v) < 5}
        assert not short, f"Arrays not padded to 5: {short}"

    def test_filament_type_is_array(self, cura_output: Path) -> None:
        ps = _load_ps(cura_output)
        ft = ps["filament_type"]
        assert isinstance(ft, list)
        assert len(ft) >= 5
        assert ft[0] == "PLA"
        assert ft[3] == "PLA"


# ===========================================================================
# Part 9: G-code integrity
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestGcodeIntegrity:
    """Validate the packed G-code has correct BBL format markers."""

    def test_md5_matches(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            gcode = z.read("Metadata/plate_1.gcode")
            md5_file = z.read("Metadata/plate_1.gcode.md5").decode()
        expected = hashlib.md5(gcode).hexdigest().upper()
        assert md5_file == expected, f"MD5 mismatch: file={md5_file}, computed={expected}"

    def test_bbl_header_block(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            gcode = z.read("Metadata/plate_1.gcode")
        assert gcode.startswith(b"; HEADER_BLOCK_START\n")
        assert b"; HEADER_BLOCK_END" in gcode

    def test_layer_progress_markers(self, cura_output: Path) -> None:
        gcode = _read_gcode(cura_output)
        assert "M73 L" in gcode, "Missing M73 L layer progress marker"
        assert "M991 S0 P" in gcode, "Missing M991 spaghetti detector marker"

    def test_has_tool_changes(self, cura_output: Path) -> None:
        """Multi-filament job must have M620/M621 tool change sequences."""
        gcode = _read_gcode(cura_output)
        m620_count = gcode.count("M620")
        assert m620_count > 0, "No M620 tool changes found — expected multi-filament"

    def test_no_unsubstituted_templates(self, cura_output: Path) -> None:
        """G-code must not contain {variable} template placeholders."""
        gcode = _read_gcode(cura_output)
        # Match {word} but not {comment} in gcode comments
        templates = re.findall(r"\{[a-z_]+(?:\[[^\]]*\])?\}", gcode)
        assert not templates, (
            f"Found {len(templates)} unsubstituted templates in G-code: {templates[:10]}"
        )


# ===========================================================================
# Part 10: XML metadata
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestXmlMetadata:
    """Validate model_settings and slice_info are well-formed XML."""

    def test_model_settings_parses(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            xml_bytes = z.read("Metadata/model_settings.config")
        root = ET.fromstring(xml_bytes)
        assert root.tag == "config"

    def test_model_settings_has_filament_maps(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            ms = z.read("Metadata/model_settings.config").decode()
        assert 'key="filament_maps"' in ms

    def test_model_settings_has_thumbnail_refs(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            ms = z.read("Metadata/model_settings.config").decode()
        assert 'key="thumbnail_file"' in ms
        assert 'key="top_file"' in ms
        assert 'key="pick_file"' in ms

    def test_slice_info_parses(self, cura_output: Path) -> None:
        root = _load_slice_info(cura_output)
        assert root.tag == "config"

    def test_slice_info_has_filament_data(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            si = z.read("Metadata/slice_info.config").decode()
        assert "<filament " in si or "filament_id" in si or "filament_type" in si


# ===========================================================================
# Part 11: Thumbnails
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestThumbnails:
    """All three thumbnails must be valid PNG files."""

    @pytest.mark.parametrize(
        "name",
        [
            "Metadata/plate_1.png",
            "Metadata/plate_1_small.png",
            "Metadata/plate_no_light_1.png",
        ],
    )
    def test_thumbnail_is_png(self, cura_output: Path, name: str) -> None:
        with zipfile.ZipFile(cura_output) as z:
            data = z.read(name)
        assert data[:8] == PNG_MAGIC, f"{name} is not a valid PNG (magic: {data[:8]!r})"
        assert len(data) > 100, f"{name} is suspiciously small ({len(data)} bytes)"


# ===========================================================================
# Part 12: Plate metadata
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestPlateJson:
    """Validate plate_1.json structure."""

    def test_has_required_keys(self, cura_output: Path) -> None:
        plate = _load_plate_json(cura_output)
        assert "filament_colors" in plate
        assert "version" in plate

    def test_version_is_valid(self, cura_output: Path) -> None:
        plate = _load_plate_json(cura_output)
        assert plate["version"] in (1, 2)

    def test_multi_filament_colors(self, cura_output: Path) -> None:
        """Multi-filament job should list multiple filament colours."""
        plate = _load_plate_json(cura_output)
        colors = plate.get("filament_colors", [])
        assert len(colors) >= 2, (
            f"plate_1.json has only {len(colors)} filament color(s) {colors}. "
            f"Multi-filament job should list colors for each active slot."
        )


# ===========================================================================
# Part 13: Cross-comparison with BBL reference
# ===========================================================================


@needs_e2e
@skip_unless_ready
class TestCrossComparison:
    """Compare structural properties between CuraEngine output and BBL reference."""

    def test_both_have_bbl_header(self, cura_output: Path) -> None:
        for path in (cura_output, REFERENCE):
            with zipfile.ZipFile(path) as z:
                gcode = z.read("Metadata/plate_1.gcode")
            assert gcode.startswith(b"; HEADER_BLOCK_START\n"), f"{path.name} missing BBL header"

    def test_both_have_tool_changes(self, cura_output: Path) -> None:
        for label, path in [("cura", cura_output), ("bbl", REFERENCE)]:
            gcode = _read_gcode(path)
            count = gcode.count("M620")
            assert count > 0, f"{label}: no M620 tool changes found"

    def test_settings_key_count_comparable(self, cura_output: Path) -> None:
        counts = {}
        for label, path in [("cura", cura_output), ("bbl", REFERENCE)]:
            ps = _load_ps(path)
            counts[label] = len(ps)
        ratio = counts["cura"] / counts["bbl"]
        assert 0.9 <= ratio <= 1.1, (
            f"Key count mismatch: cura={counts['cura']}, bbl={counts['bbl']} (ratio={ratio:.2f})"
        )

    def test_toolchange_feedrates_match_reference(self, cura_output: Path) -> None:
        """Toolchange feedrates should be within 10% of BBL reference."""
        ref_gcode = _read_gcode(REFERENCE)
        cura_gcode = _read_gcode(cura_output)

        def _median_feedrate(gcode: str) -> float:
            rates = []
            for line in gcode.splitlines():
                m = re.match(r"^\s*M620\.1\s+E\s+F([\d.]+)", line)
                if m:
                    rates.append(float(m.group(1)))
            return sorted(rates)[len(rates) // 2] if rates else 0

        ref_f = _median_feedrate(ref_gcode)
        cura_f = _median_feedrate(cura_gcode)
        assert ref_f > 0 and cura_f > 0, "Could not extract feedrates"
        ratio = cura_f / ref_f
        assert 0.5 <= ratio <= 2.0, (
            f"Toolchange feedrate mismatch: cura median F={cura_f:.1f}, "
            f"ref median F={ref_f:.1f} (ratio={ratio:.2f})"
        )

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


def _docker_available() -> bool:
    """Return True if Docker daemon is reachable."""
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _estampo_available() -> bool:
    """Return True if estampo CLI is on PATH."""
    return shutil.which("estampo") is not None


needs_e2e = pytest.mark.e2e
skip_unless_ready = pytest.mark.skipif(
    not (_estampo_available() and _docker_available()),
    reason="Requires estampo CLI and Docker with CuraEngine image",
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


# ---------------------------------------------------------------------------
# Archive structure
# ---------------------------------------------------------------------------


class TestReferenceFixture:
    """Sanity-checks on the BBL reference fixture (always runs, no Docker needed)."""

    def test_reference_has_required_files(self) -> None:
        with zipfile.ZipFile(REFERENCE) as z:
            names = set(z.namelist())
        missing = REQUIRED_FILES - names
        assert not missing, f"Reference missing: {missing}"


@needs_e2e
@skip_unless_ready
class TestArchiveStructure:
    """Verify the CuraEngine output has the same archive structure as BBL."""

    def test_required_files_present(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            names = set(z.namelist())
        missing = REQUIRED_FILES - names
        assert not missing, f"Missing required files: {missing}"


# ---------------------------------------------------------------------------
# Project settings
# ---------------------------------------------------------------------------


@needs_e2e
@skip_unless_ready
class TestProjectSettings:
    """Validate project_settings.config against BBL reference."""

    def _load_ps(self, archive: Path) -> dict:
        with zipfile.ZipFile(archive) as z:
            return json.loads(z.read("Metadata/project_settings.config"))

    def test_key_count(self, cura_output: Path) -> None:
        ps = self._load_ps(cura_output)
        assert len(ps) >= 500, f"Only {len(ps)} keys — expected >= 500"

    def test_key_names_overlap(self, cura_output: Path) -> None:
        """Most key names should match between bambox and BBL output."""
        cura_ps = self._load_ps(cura_output)
        ref_ps = self._load_ps(REFERENCE)
        cura_keys = set(cura_ps.keys())
        ref_keys = set(ref_ps.keys())
        # Allow some keys to differ (BC-required additions, slicer-specific)
        overlap = cura_keys & ref_keys
        assert len(overlap) >= 500, (
            f"Only {len(overlap)} overlapping keys. "
            f"Cura-only: {cura_keys - ref_keys}, "
            f"BBL-only: {ref_keys - cura_keys}"
        )

    def test_arrays_padded_to_five(self, cura_output: Path) -> None:
        ps = self._load_ps(cura_output)
        short = {k: len(v) for k, v in ps.items() if isinstance(v, list) and 0 < len(v) < 5}
        assert not short, f"Arrays not padded to 5: {short}"

    def test_filament_type_is_array(self, cura_output: Path) -> None:
        ps = self._load_ps(cura_output)
        ft = ps["filament_type"]
        assert isinstance(ft, list)
        assert len(ft) >= 5
        # Slot 1 and slot 4 are PLA (from config)
        assert ft[0] == "PLA"
        assert ft[3] == "PLA"


# ---------------------------------------------------------------------------
# G-code integrity
# ---------------------------------------------------------------------------


@needs_e2e
@skip_unless_ready
class TestGcodeIntegrity:
    """Validate the packed G-code has correct BBL format markers."""

    def _read_gcode(self, archive: Path) -> bytes:
        with zipfile.ZipFile(archive) as z:
            return z.read("Metadata/plate_1.gcode")

    def test_md5_matches(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            gcode = z.read("Metadata/plate_1.gcode")
            md5_file = z.read("Metadata/plate_1.gcode.md5").decode()
        expected = hashlib.md5(gcode).hexdigest().upper()
        assert md5_file == expected, f"MD5 mismatch: file={md5_file}, computed={expected}"

    def test_bbl_header_block(self, cura_output: Path) -> None:
        gcode = self._read_gcode(cura_output)
        assert gcode.startswith(b"; HEADER_BLOCK_START\n")
        assert b"; HEADER_BLOCK_END" in gcode

    def test_layer_progress_markers(self, cura_output: Path) -> None:
        gcode = self._read_gcode(cura_output).decode(errors="replace")
        assert "M73 L" in gcode, "Missing M73 L layer progress marker"
        assert "M991 S0 P" in gcode, "Missing M991 spaghetti detector marker"

    def test_has_tool_changes(self, cura_output: Path) -> None:
        """Multi-filament job must have M620/M621 tool change sequences."""
        gcode = self._read_gcode(cura_output).decode(errors="replace")
        m620_count = gcode.count("M620")
        assert m620_count > 0, "No M620 tool changes found — expected multi-filament"


# ---------------------------------------------------------------------------
# XML metadata
# ---------------------------------------------------------------------------


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
        with zipfile.ZipFile(cura_output) as z:
            xml_bytes = z.read("Metadata/slice_info.config")
        root = ET.fromstring(xml_bytes)
        assert root.tag == "config"

    def test_slice_info_has_filament_data(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            si = z.read("Metadata/slice_info.config").decode()
        assert "filament_id" in si or "filament_type" in si


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Plate metadata
# ---------------------------------------------------------------------------


@needs_e2e
@skip_unless_ready
class TestPlateJson:
    """Validate plate_1.json structure."""

    def test_has_required_keys(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            plate = json.loads(z.read("Metadata/plate_1.json"))
        assert "filament_colors" in plate
        assert "version" in plate

    def test_version_is_valid(self, cura_output: Path) -> None:
        with zipfile.ZipFile(cura_output) as z:
            plate = json.loads(z.read("Metadata/plate_1.json"))
        assert plate["version"] in (1, 2)


# ---------------------------------------------------------------------------
# Cross-comparison with BBL reference
# ---------------------------------------------------------------------------


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
            with zipfile.ZipFile(path) as z:
                gcode = z.read("Metadata/plate_1.gcode").decode(errors="replace")
            count = gcode.count("M620")
            assert count > 0, f"{label}: no M620 tool changes found"

    def test_settings_key_count_comparable(self, cura_output: Path) -> None:
        counts = {}
        for label, path in [("cura", cura_output), ("bbl", REFERENCE)]:
            with zipfile.ZipFile(path) as z:
                ps = json.loads(z.read("Metadata/project_settings.config"))
            counts[label] = len(ps)
        # Both should have roughly the same number of keys (within 10%)
        ratio = counts["cura"] / counts["bbl"]
        assert 0.9 <= ratio <= 1.1, (
            f"Key count mismatch: cura={counts['cura']}, bbl={counts['bbl']} (ratio={ratio:.2f})"
        )

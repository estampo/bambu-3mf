"""Test bambu-3mf packaging against OrcaSlicer reference output."""

from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from bambu_3mf.pack import FilamentInfo, SliceInfo, pack_gcode_3mf

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE_3MF = FIXTURES / "reference.gcode.3mf"
CUBE_GCODE = FIXTURES / "cube.gcode"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_zip(path_or_buf: Path | BytesIO) -> dict[str, bytes]:
    """Return {filename: content} for every entry in a ZIP."""
    with zipfile.ZipFile(path_or_buf) as z:
        return {info.filename: z.read(info.filename) for info in z.infolist()}


def _ref_files() -> dict[str, bytes]:
    """Load the OrcaSlicer reference .gcode.3mf."""
    return _read_zip(REFERENCE_3MF)


def _pack_cube(**kwargs: object) -> dict[str, bytes]:
    """Pack the cube.gcode with default settings and return archive contents."""
    gcode = CUBE_GCODE.read_bytes()
    buf = BytesIO()
    info = SliceInfo(
        printer_model_id="",
        nozzle_diameter=0.4,
        prediction=1241,
        weight=3.64,
        label_object_enabled=True,
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
        **kwargs,  # type: ignore[arg-type]
    )
    pack_gcode_3mf(gcode, buf, slice_info=info)
    buf.seek(0)
    return _read_zip(buf)


# ---------------------------------------------------------------------------
# Tests: archive structure
# ---------------------------------------------------------------------------


class TestArchiveStructure:
    """The output archive must contain the same required files as the reference."""

    REQUIRED_FILES = {
        "[Content_Types].xml",
        "_rels/.rels",
        "3D/3dmodel.model",
        "Metadata/plate_1.gcode",
        "Metadata/plate_1.gcode.md5",
        "Metadata/model_settings.config",
        "Metadata/_rels/model_settings.config.rels",
        "Metadata/slice_info.config",
        "Metadata/plate_1.json",
    }

    def test_required_files_present(self) -> None:
        ours = _pack_cube()
        missing = self.REQUIRED_FILES - set(ours.keys())
        assert not missing, f"Missing files: {missing}"

    def test_reference_has_same_required_files(self) -> None:
        ref = _ref_files()
        missing = self.REQUIRED_FILES - set(ref.keys())
        assert not missing, f"Reference missing: {missing}"


# ---------------------------------------------------------------------------
# Tests: G-code and MD5
# ---------------------------------------------------------------------------


class TestGcodeIntegrity:
    """The G-code must be byte-for-byte identical and the MD5 must be correct."""

    def test_gcode_matches_reference(self) -> None:
        ref = _ref_files()
        ours = _pack_cube()
        assert ours["Metadata/plate_1.gcode"] == ref["Metadata/plate_1.gcode"]

    def test_md5_matches_gcode(self) -> None:
        ours = _pack_cube()
        gcode = ours["Metadata/plate_1.gcode"]
        md5 = hashlib.md5(gcode).hexdigest().upper()
        assert ours["Metadata/plate_1.gcode.md5"].decode() == md5

    def test_md5_matches_reference(self) -> None:
        ref = _ref_files()
        ours = _pack_cube()
        assert ours["Metadata/plate_1.gcode.md5"] == ref["Metadata/plate_1.gcode.md5"]


# ---------------------------------------------------------------------------
# Tests: static boilerplate files
# ---------------------------------------------------------------------------


class TestBoilerplate:
    """Static XML files must match the reference exactly."""

    def test_content_types(self) -> None:
        ref = _ref_files()
        ours = _pack_cube()
        assert ours["[Content_Types].xml"] == ref["[Content_Types].xml"]

    def test_rels(self) -> None:
        ref = _ref_files()
        ours = _pack_cube()
        assert ours["_rels/.rels"] == ref["_rels/.rels"]

    def test_model_settings_rels(self) -> None:
        ref = _ref_files()
        ours = _pack_cube()
        assert (
            ours["Metadata/_rels/model_settings.config.rels"]
            == ref["Metadata/_rels/model_settings.config.rels"]
        )


# ---------------------------------------------------------------------------
# Tests: 3D model
# ---------------------------------------------------------------------------


class TestModel:
    """The 3D model XML must be minimal (empty resources/build)."""

    def test_model_has_bambu_version(self) -> None:
        ours = _pack_cube()
        root = ET.fromstring(ours["3D/3dmodel.model"])
        ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
        version = root.find(".//m:metadata[@name='BambuStudio:3mfVersion']", ns)
        assert version is not None
        assert version.text == "1"

    def test_model_has_empty_build(self) -> None:
        ours = _pack_cube()
        root = ET.fromstring(ours["3D/3dmodel.model"])
        ns = {"m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"}
        resources = root.find("m:resources", ns)
        assert resources is not None
        assert len(resources) == 0  # no child elements

    def test_model_matches_reference(self) -> None:
        ref = _ref_files()
        ours = _pack_cube()
        assert ours["3D/3dmodel.model"] == ref["3D/3dmodel.model"]


# ---------------------------------------------------------------------------
# Tests: model_settings.config
# ---------------------------------------------------------------------------


class TestModelSettings:
    """model_settings.config must contain plate definition linking to gcode."""

    def test_has_gcode_file_ref(self) -> None:
        ours = _pack_cube()
        xml = ours["Metadata/model_settings.config"].decode()
        assert 'value="Metadata/plate_1.gcode"' in xml

    def test_has_plater_id(self) -> None:
        ours = _pack_cube()
        xml = ours["Metadata/model_settings.config"].decode()
        assert 'key="plater_id" value="1"' in xml

    def test_matches_reference(self) -> None:
        ref = _ref_files()
        ours = _pack_cube()
        assert ours["Metadata/model_settings.config"] == ref["Metadata/model_settings.config"]


# ---------------------------------------------------------------------------
# Tests: slice_info.config
# ---------------------------------------------------------------------------


class TestSliceInfo:
    """slice_info.config must contain print metadata."""

    def test_has_prediction(self) -> None:
        ours = _pack_cube()
        xml = ours["Metadata/slice_info.config"].decode()
        assert 'key="prediction" value="1241"' in xml

    def test_has_weight(self) -> None:
        ours = _pack_cube()
        xml = ours["Metadata/slice_info.config"].decode()
        assert 'key="weight" value="3.64"' in xml

    def test_has_filament(self) -> None:
        ours = _pack_cube()
        xml = ours["Metadata/slice_info.config"].decode()
        assert 'type="PLA"' in xml
        assert 'tray_info_idx="GFL99"' in xml

    def test_has_nozzle_diameter(self) -> None:
        ours = _pack_cube()
        xml = ours["Metadata/slice_info.config"].decode()
        assert 'key="nozzle_diameters" value="0.4"' in xml

    def test_structure_matches_reference(self) -> None:
        """Key structure matches — values may differ for dynamic fields."""
        ref = _ref_files()
        ours = _pack_cube()
        ref_xml = ref["Metadata/slice_info.config"].decode()
        our_xml = ours["Metadata/slice_info.config"].decode()

        # Both must have the same set of metadata keys
        def extract_keys(xml: str) -> set[str]:
            root = ET.fromstring(xml)
            keys: set[str] = set()
            for elem in root.iter("metadata"):
                k = elem.get("key")
                if k:
                    keys.add(k)
            return keys

        assert extract_keys(our_xml) == extract_keys(ref_xml)


# ---------------------------------------------------------------------------
# Tests: plate_1.json
# ---------------------------------------------------------------------------


class TestPlateJson:
    """plate_1.json must contain filament and plate metadata."""

    def test_valid_json(self) -> None:
        ours = _pack_cube()
        data = json.loads(ours["Metadata/plate_1.json"])
        assert "filament_colors" in data
        assert "nozzle_diameter" in data
        assert data["version"] == 2

    def test_filament_colors(self) -> None:
        ours = _pack_cube()
        data = json.loads(ours["Metadata/plate_1.json"])
        assert data["filament_colors"] == ["#F2754E"]

    def test_nozzle_diameter(self) -> None:
        ours = _pack_cube()
        data = json.loads(ours["Metadata/plate_1.json"])
        assert data["nozzle_diameter"] == 0.4


# ---------------------------------------------------------------------------
# Tests: file output
# ---------------------------------------------------------------------------


class TestFileOutput:
    """pack_gcode_3mf can write to a file path."""

    def test_write_to_path(self, tmp_path: Path) -> None:
        out = tmp_path / "test.gcode.3mf"
        gcode = b"; simple test\nG28\n"
        pack_gcode_3mf(gcode, out)
        assert out.exists()
        with zipfile.ZipFile(out) as z:
            assert "Metadata/plate_1.gcode" in z.namelist()
            assert z.read("Metadata/plate_1.gcode") == gcode

    def test_md5_correct_for_custom_gcode(self, tmp_path: Path) -> None:
        out = tmp_path / "test.gcode.3mf"
        gcode = b"G28\nG1 X10 Y10 Z0.2 F3000\n"
        pack_gcode_3mf(gcode, out)
        with zipfile.ZipFile(out) as z:
            md5 = hashlib.md5(gcode).hexdigest().upper()
            assert z.read("Metadata/plate_1.gcode.md5").decode() == md5

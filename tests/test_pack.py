"""Tests for .gcode.3mf packaging against a Bambu Connect-accepted reference.

The reference fixture is plate_sliced.gcode.3mf from the decoy-case example,
which has been validated to work with Bambu Connect on a P1S with AMS.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from bambox.pack import (
    FilamentInfo,
    ObjectInfo,
    SliceInfo,
    WarningInfo,
    _filament_maps_str,
    _slice_info_xml,
    fixup_project_settings,
    pack_gcode_3mf,
)

FIXTURES = Path(__file__).parent / "fixtures"
REFERENCE = FIXTURES / "reference.gcode.3mf"
PROJECT_SETTINGS = json.loads((FIXTURES / "project_settings.json").read_text())


def _ref() -> zipfile.ZipFile:
    return zipfile.ZipFile(REFERENCE)


def _pack(**kwargs) -> zipfile.ZipFile:
    """Pack with reference-matching defaults and return the ZipFile."""
    gcode = kwargs.pop("gcode", b"; test gcode\nG1 X10 Y10\n")
    buf = BytesIO()
    info = kwargs.pop(
        "slice_info",
        SliceInfo(
            nozzle_diameter=0.4,
            prediction=1951,
            weight=10.36,
            filaments=[
                FilamentInfo(
                    slot=3,
                    tray_info_idx="GFG98",
                    filament_type="PETG-CF",
                    color="#F2754E",
                    used_m=3.45,
                    used_g=10.36,
                )
            ],
        ),
    )
    ps = kwargs.pop("project_settings", PROJECT_SETTINGS)
    pack_gcode_3mf(gcode, buf, slice_info=info, project_settings=ps, **kwargs)
    buf.seek(0)
    return zipfile.ZipFile(buf)


# ---------------------------------------------------------------------------
# Archive structure
# ---------------------------------------------------------------------------


class TestArchiveStructure:
    REQUIRED_FILES = {
        "[Content_Types].xml",
        "_rels/.rels",
        "3D/3dmodel.model",
        "Metadata/plate_1.gcode",
        "Metadata/plate_1.gcode.md5",
        "Metadata/model_settings.config",
        "Metadata/_rels/model_settings.config.rels",
        "Metadata/slice_info.config",
        "Metadata/project_settings.config",
        "Metadata/plate_1.json",
        "Metadata/plate_1.png",
        "Metadata/plate_no_light_1.png",
        "Metadata/plate_1_small.png",
    }

    def test_all_required_files_present(self) -> None:
        z = _pack()
        names = set(z.namelist())
        missing = self.REQUIRED_FILES - names
        assert not missing, f"Missing: {missing}"

    def test_reference_has_same_required_files(self) -> None:
        with _ref() as z:
            names = set(z.namelist())
            missing = self.REQUIRED_FILES - names
            assert not missing, f"Reference missing: {missing}"


# ---------------------------------------------------------------------------
# Gcode integrity
# ---------------------------------------------------------------------------


class TestGcodeIntegrity:
    def test_gcode_round_trips(self) -> None:
        # G-code without Z moves is not modified by the Z-change fallback
        gcode = b"; test\nG28\nG1 X50 Y50 E1\n"
        z = _pack(gcode=gcode)
        assert z.read("Metadata/plate_1.gcode") == gcode

    def test_md5_matches_gcode(self) -> None:
        gcode = b"G28\nG1 X10\n"
        z = _pack(gcode=gcode)
        packed_gcode = z.read("Metadata/plate_1.gcode")
        md5 = z.read("Metadata/plate_1.gcode.md5").decode()
        assert md5 == hashlib.md5(packed_gcode).hexdigest().upper()

    def test_md5_is_uppercase_hex(self) -> None:
        z = _pack()
        md5 = z.read("Metadata/plate_1.gcode.md5").decode()
        assert len(md5) == 32
        assert md5 == md5.upper()
        int(md5, 16)  # must be valid hex


# ---------------------------------------------------------------------------
# Boilerplate XML
# ---------------------------------------------------------------------------


class TestBoilerplateXml:
    def test_content_types_matches_reference(self) -> None:
        with _ref() as ref:
            expected = ref.read("[Content_Types].xml")
        z = _pack()
        assert z.read("[Content_Types].xml") == expected

    def test_rels_matches_reference(self) -> None:
        with _ref() as ref:
            ref_root = ET.fromstring(ref.read("_rels/.rels"))
        z = _pack()
        our_root = ET.fromstring(z.read("_rels/.rels"))
        ref_targets = {r.get("Target") for r in ref_root}
        our_targets = {r.get("Target") for r in our_root}
        assert ref_targets == our_targets

    def test_model_settings_rels_matches_reference(self) -> None:
        with _ref() as ref:
            expected = ref.read("Metadata/_rels/model_settings.config.rels")
        z = _pack()
        assert z.read("Metadata/_rels/model_settings.config.rels") == expected


# ---------------------------------------------------------------------------
# 3D model
# ---------------------------------------------------------------------------


class TestModel:
    def test_has_bambu_version(self) -> None:
        z = _pack()
        content = z.read("3D/3dmodel.model").decode()
        assert "BambuStudio:3mfVersion" in content

    def test_empty_build(self) -> None:
        z = _pack()
        content = z.read("3D/3dmodel.model").decode()
        assert "<build/>" in content

    def test_matches_reference(self) -> None:
        with _ref() as ref:
            expected = ref.read("3D/3dmodel.model")
        z = _pack()
        assert z.read("3D/3dmodel.model") == expected


# ---------------------------------------------------------------------------
# model_settings.config
# ---------------------------------------------------------------------------


class TestModelSettings:
    def test_has_gcode_file_ref(self) -> None:
        z = _pack()
        content = z.read("Metadata/model_settings.config").decode()
        assert 'value="Metadata/plate_1.gcode"' in content

    def test_has_thumbnail_refs(self) -> None:
        z = _pack()
        content = z.read("Metadata/model_settings.config").decode()
        assert 'key="thumbnail_file"' in content
        assert 'key="thumbnail_no_light_file"' in content
        assert 'key="top_file"' in content
        assert 'key="pick_file"' in content
        assert 'key="pattern_bbox_file"' in content

    def test_filament_maps_padded(self) -> None:
        z = _pack()
        content = z.read("Metadata/model_settings.config").decode()
        # Plate mapping index (always 1), padded to 5 slots
        assert 'value="1 1 1 1 1"' in content

    def test_matches_reference_keys(self) -> None:
        """All metadata keys from reference must be present."""
        with _ref() as ref:
            ref_xml = ref.read("Metadata/model_settings.config").decode()
        z = _pack()
        our_xml = z.read("Metadata/model_settings.config").decode()

        ref_root = ET.fromstring(ref_xml)
        our_root = ET.fromstring(our_xml)
        ref_keys = {m.get("key") for m in ref_root.findall(".//metadata")}
        our_keys = {m.get("key") for m in our_root.findall(".//metadata")}
        assert ref_keys == our_keys


# ---------------------------------------------------------------------------
# project_settings.config
# ---------------------------------------------------------------------------


class TestProjectSettings:
    def test_present_when_provided(self) -> None:
        z = _pack()
        assert "Metadata/project_settings.config" in z.namelist()

    def test_valid_json(self) -> None:
        z = _pack()
        data = json.loads(z.read("Metadata/project_settings.config"))
        assert isinstance(data, dict)
        assert len(data) > 500

    def test_arrays_padded_to_min_slots(self) -> None:
        z = _pack()
        data = json.loads(z.read("Metadata/project_settings.config"))
        for key, val in data.items():
            if isinstance(val, list) and len(val) > 0:
                assert len(val) >= 5, f"{key} has {len(val)} elements, expected >= 5"

    def test_omitted_when_none(self) -> None:
        z = _pack(project_settings=None)
        assert "Metadata/project_settings.config" not in z.namelist()


# ---------------------------------------------------------------------------
# slice_info.config
# ---------------------------------------------------------------------------


class TestSliceInfo:
    def test_has_prediction(self) -> None:
        z = _pack()
        content = z.read("Metadata/slice_info.config").decode()
        assert 'key="prediction" value="1951"' in content

    def test_has_weight(self) -> None:
        z = _pack()
        content = z.read("Metadata/slice_info.config").decode()
        assert 'value="10.36"' in content

    def test_has_filament(self) -> None:
        z = _pack()
        content = z.read("Metadata/slice_info.config").decode()
        assert 'type="PETG-CF"' in content
        assert 'tray_info_idx="GFG98"' in content

    def test_structure_matches_reference(self) -> None:
        with _ref() as ref:
            ref_xml = ref.read("Metadata/slice_info.config").decode()
        z = _pack()
        our_xml = z.read("Metadata/slice_info.config").decode()

        ref_root = ET.fromstring(ref_xml)
        our_root = ET.fromstring(our_xml)
        ref_keys = {m.get("key") for m in ref_root.findall(".//metadata")}
        our_keys = {m.get("key") for m in our_root.findall(".//metadata")}
        assert ref_keys == our_keys


class TestXmlEscaping:
    """Verify that special XML characters in user-controlled fields are escaped."""

    def test_object_name_with_special_chars(self) -> None:
        info = SliceInfo(
            objects=[ObjectInfo(identify_id=1, name='Box & "Lid" <v2>')],
            filaments=[
                FilamentInfo(
                    slot=1,
                    tray_info_idx="GFG98",
                    filament_type="PLA",
                    color="#FFFFFF",
                    used_m=1.0,
                    used_g=2.0,
                ),
            ],
        )
        xml = _slice_info_xml(info)
        root = ET.fromstring(xml)  # must parse without error
        obj = root.find(".//object")
        assert obj is not None
        assert obj.get("name") == 'Box & "Lid" <v2>'

    def test_warning_msg_with_ampersand(self) -> None:
        info = SliceInfo(
            warnings=[WarningInfo(msg="temp & pressure > limit", error_code="E&1")],
            filaments=[
                FilamentInfo(
                    slot=1,
                    tray_info_idx="GFG98",
                    filament_type="PLA",
                    color="#FFFFFF",
                    used_m=1.0,
                    used_g=2.0,
                ),
            ],
        )
        xml = _slice_info_xml(info)
        root = ET.fromstring(xml)
        warn = root.find(".//warning")
        assert warn is not None
        assert warn.get("msg") == "temp & pressure > limit"
        assert warn.get("error_code") == "E&1"

    def test_filament_type_with_quotes(self) -> None:
        info = SliceInfo(
            filaments=[
                FilamentInfo(
                    slot=1,
                    tray_info_idx='T"1',
                    filament_type='PLA&"Special"',
                    color="#FF<00>",
                    used_m=1.0,
                    used_g=2.0,
                ),
            ],
        )
        xml = _slice_info_xml(info)
        root = ET.fromstring(xml)
        fil = root.find(".//filament")
        assert fil is not None
        assert fil.get("type") == 'PLA&"Special"'
        assert fil.get("color") == "#FF<00>"
        assert fil.get("tray_info_idx") == 'T"1'

    def test_printer_model_id_escaped(self) -> None:
        info = SliceInfo(
            printer_model_id="P1S&<test>",
            filaments=[
                FilamentInfo(
                    slot=1,
                    tray_info_idx="GFG98",
                    filament_type="PLA",
                    color="#FFFFFF",
                    used_m=1.0,
                    used_g=2.0,
                ),
            ],
        )
        xml = _slice_info_xml(info)
        root = ET.fromstring(xml)
        meta = {m.get("key"): m.get("value") for m in root.findall(".//metadata")}
        assert meta["printer_model_id"] == "P1S&<test>"

    def test_extra_attrs_escaped(self) -> None:
        info = SliceInfo(
            filaments=[
                FilamentInfo(
                    slot=1,
                    tray_info_idx="GFG98",
                    filament_type="PLA",
                    color="#FFFFFF",
                    used_m=1.0,
                    used_g=2.0,
                    extra_attrs={"note": 'a&b<c"d'},
                ),
            ],
        )
        xml = _slice_info_xml(info)
        root = ET.fromstring(xml)
        fil = root.find(".//filament")
        assert fil is not None
        assert fil.get("note") == 'a&b<c"d'


# ---------------------------------------------------------------------------
# Thumbnails
# ---------------------------------------------------------------------------


class TestThumbnails:
    def test_placeholder_pngs_included(self) -> None:
        z = _pack()
        for name in [
            "Metadata/plate_1.png",
            "Metadata/plate_no_light_1.png",
            "Metadata/plate_1_small.png",
        ]:
            data = z.read(name)
            assert data[:4] == b"\x89PNG", f"{name} is not a valid PNG"

    def test_custom_thumbnails(self) -> None:
        custom_png = b"\x89PNG\r\n\x1a\nCUSTOM"
        z = _pack(thumbnails={"Metadata/plate_1.png": custom_png})
        assert z.read("Metadata/plate_1.png") == custom_png


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_filament_maps_default(self) -> None:
        assert _filament_maps_str() == "1 1 1 1 1"

    def test_filament_maps_custom_slots(self) -> None:
        assert _filament_maps_str(3) == "1 1 1"

    def test_fixup_project_settings(self) -> None:
        settings = {"filament_type": ["PLA", "PETG"], "speed": "100"}
        padded = fixup_project_settings(settings)
        assert padded["filament_type"] == ["PLA", "PETG", "PETG", "PETG", "PETG"]
        assert padded["speed"] == "100"

    def test_fixup_does_not_mutate_caller_dict(self) -> None:
        original_list = ["PLA", "PETG"]
        settings: dict[str, object] = {
            "filament_type": original_list,
            "speed": "100",
        }
        fixup_project_settings(settings)
        assert original_list == ["PLA", "PETG"], "caller's list was mutated"
        assert settings["filament_type"] == ["PLA", "PETG"], "caller's dict was mutated"

    def test_fixup_does_not_mutate_bc_required_keys(self) -> None:
        from bambox.pack import _BC_REQUIRED_KEYS

        snapshots = {k: list(v) if isinstance(v, list) else v for k, v in _BC_REQUIRED_KEYS.items()}
        fixup_project_settings({})
        fixup_project_settings({})  # second call to detect accumulated mutation
        for k, v in _BC_REQUIRED_KEYS.items():
            assert v == snapshots[k], f"_BC_REQUIRED_KEYS[{k!r}] was mutated"

    def test_fixup_returns_padded_arrays(self) -> None:
        settings: dict[str, object] = {"filament_colour": ["#FF0000"]}
        result = fixup_project_settings(settings, min_slots=5)
        assert result["filament_colour"] == ["#FF0000"] * 5


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# BBS 02.05+ optional metadata in slice_info
# ---------------------------------------------------------------------------


class TestSliceInfoOptionalFields:
    def test_extruder_type_and_nozzle_volume_type(self) -> None:
        """Lines 225-229: extruder_type and nozzle_volume_type in slice_info XML."""
        info = SliceInfo(
            extruder_type=1,
            nozzle_volume_type=2,
            filaments=[FilamentInfo()],
        )
        xml = _slice_info_xml(info)
        root = ET.fromstring(xml)
        meta = {m.get("key"): m.get("value") for m in root.findall(".//metadata")}
        assert meta["extruder_type"] == "1"
        assert meta["nozzle_volume_type"] == "2"

    def test_first_layer_time(self) -> None:
        """Line 244: first_layer_time in slice_info XML."""
        info = SliceInfo(
            first_layer_time=42.5,
            filaments=[FilamentInfo()],
        )
        xml = _slice_info_xml(info)
        root = ET.fromstring(xml)
        meta = {m.get("key"): m.get("value") for m in root.findall(".//metadata")}
        assert meta["first_layer_time"] == "42.5"

    def test_limit_filament_maps(self) -> None:
        """Line 256: limit_filament_maps in slice_info XML."""
        info = SliceInfo(
            limit_filament_maps="0 0 0 0 0",
            filaments=[FilamentInfo()],
        )
        xml = _slice_info_xml(info)
        root = ET.fromstring(xml)
        meta = {m.get("key"): m.get("value") for m in root.findall(".//metadata")}
        assert meta["limit_filament_maps"] == "0 0 0 0 0"

    def test_layer_filament_lists(self) -> None:
        """Lines 285-291: layer_filament_lists in slice_info XML."""
        info = SliceInfo(
            layer_filament_lists=[
                {"filament_list": "1 2", "layer_ranges": "0-10"},
                {"filament_list": "2 3", "layer_ranges": "11-20"},
            ],
            filaments=[FilamentInfo()],
        )
        xml = _slice_info_xml(info)
        root = ET.fromstring(xml)
        lfl = root.findall(".//layer_filament_list")
        assert len(lfl) == 2
        assert lfl[0].get("filament_list") == "1 2"
        assert lfl[0].get("layer_ranges") == "0-10"
        assert lfl[1].get("filament_list") == "2 3"

    def test_filament_volume_maps_in_model_settings(self) -> None:
        """Line 101: filament_volume_maps passed to model_settings."""
        from bambox.pack import _model_settings_xml

        xml = _model_settings_xml("1 1 1 1 1", filament_volume_maps="0 0 0 0 0")
        assert 'key="filament_volume_maps"' in xml
        assert 'value="0 0 0 0 0"' in xml


# ---------------------------------------------------------------------------
# plate_json with plate_data passthrough
# ---------------------------------------------------------------------------


class TestPlateJson:
    def test_plate_data_passthrough(self) -> None:
        """Line 311: plate_data should be used as base when provided."""
        from bambox.pack import _plate_json

        info = SliceInfo(
            plate_data={"custom_key": "custom_value", "version": 99},
        )
        result = json.loads(_plate_json(info, []))
        assert result["custom_key"] == "custom_value"
        assert result["version"] == 99  # from plate_data, not default
        # Defaults should still fill in missing keys
        assert "is_seq_print" in result


# ---------------------------------------------------------------------------
# Extra files in pack_gcode_3mf
# ---------------------------------------------------------------------------


class TestExtraFiles:
    def test_extra_files_included(self) -> None:
        """Lines 637-638: extra_files should be written to the archive."""
        z = _pack(extra_files={"Metadata/top_1.png": b"\x89PNGextra"})
        assert z.read("Metadata/top_1.png") == b"\x89PNGextra"


# ---------------------------------------------------------------------------
# repack_3mf edge cases
# ---------------------------------------------------------------------------


class TestRepack3mf:
    def _make_3mf(self, tmp_path: Path, **extra_entries: bytes) -> Path:
        """Create a minimal .gcode.3mf for repack testing."""
        out = tmp_path / "test.gcode.3mf"
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("Metadata/plate_1.gcode", b"G28\nG1 Z0.2\n")
            z.writestr(
                "Metadata/model_settings.config",
                '<?xml version="1.0"?>\n<config><plate>'
                '<metadata key="filament_maps" value="1"/>'
                '<metadata key="gcode_file" value="Metadata/plate_1.gcode"/>'
                "</plate></config>\n",
            )
            for name, data in extra_entries.items():
                z.writestr(name, data)
        return out

    def test_repack_no_project_settings(self, tmp_path: Path) -> None:
        """Lines 408-409, 423: repack with no existing project_settings and no machine."""
        from bambox.pack import repack_3mf

        out = self._make_3mf(tmp_path)
        repack_3mf(out)
        with zipfile.ZipFile(out) as z:
            # model_settings should be patched (filament_maps padded)
            ms = z.read("Metadata/model_settings.config").decode()
            assert 'value="1 1 1 1 1"' in ms

    def test_repack_with_machine_and_filaments(self, tmp_path: Path) -> None:
        """Lines 411-419: repack regenerates project_settings from profiles."""
        from bambox.pack import repack_3mf

        out = self._make_3mf(tmp_path)
        repack_3mf(out, machine="p1s", filaments=["PLA"])
        with zipfile.ZipFile(out) as z:
            ps = json.loads(z.read("Metadata/project_settings.config"))
            assert isinstance(ps, dict)
            assert len(ps) > 400

    def test_repack_no_model_settings(self, tmp_path: Path) -> None:
        """Lines 429-430: repack when model_settings.config is missing."""
        from bambox.pack import repack_3mf

        out = tmp_path / "no_ms.gcode.3mf"
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("Metadata/plate_1.gcode", b"G28\n")
        repack_3mf(out)
        with zipfile.ZipFile(out) as z:
            # Should not crash; model_settings simply not added
            assert "Metadata/plate_1.gcode" in z.namelist()

    def test_repack_generates_missing_plate_json(self, tmp_path: Path) -> None:
        """Lines 436-448, 518-519: repack generates plate_1.json when missing."""
        from bambox.pack import repack_3mf

        out = self._make_3mf(tmp_path)
        repack_3mf(out)
        with zipfile.ZipFile(out) as z:
            plate_data = json.loads(z.read("Metadata/plate_1.json"))
            assert "filament_colors" in plate_data
            assert plate_data["version"] == 2


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


class TestFileOutput:
    def test_write_to_path(self, tmp_path: Path) -> None:
        out = tmp_path / "test.gcode.3mf"
        pack_gcode_3mf(
            b"G28\n",
            out,
            slice_info=SliceInfo(),
            project_settings=PROJECT_SETTINGS,
        )
        assert out.exists()
        with zipfile.ZipFile(out) as z:
            assert "Metadata/plate_1.gcode" in z.namelist()
            assert "Metadata/project_settings.config" in z.namelist()

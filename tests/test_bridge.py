"""Tests for bridge.py — local binary fallback and Docker chain."""

from __future__ import annotations

import io
import json
import os
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from unittest.mock import patch

import pytest

from bambox.bridge import (
    EXPECTED_API_VERSION,
    _build_ams_mapping,
    _check_daemon_version,
    _cloud_print_impl,
    _find_local_bridge,
    _patch_config_3mf_colors,
    _run_bridge_local,
    _strip_gcode_from_3mf,
    _write_token_json,
    load_credentials,
    parse_ams_trays,
    query_status,
)


class TestFindLocalBridge:
    def test_finds_binary_on_path(self, tmp_path):
        """shutil.which hit should be returned."""
        with patch("bambox.bridge.shutil.which", return_value="/usr/local/bin/bambox-bridge"):
            assert _find_local_bridge() == "/usr/local/bin/bambox-bridge"

    def test_finds_binary_in_local_bin(self, tmp_path):
        """Falls back to ~/.local/bin when not on PATH."""
        with (
            patch("bambox.bridge.shutil.which", return_value=None),
            patch("bambox.bridge.Path.home", return_value=tmp_path),
        ):
            # Create the expected path structure
            local_bin = tmp_path / ".local" / "bin"
            local_bin.mkdir(parents=True)
            bridge = local_bin / "bambox-bridge"
            bridge.touch()
            bridge.chmod(0o755)
            assert _find_local_bridge() == str(bridge)

    def test_returns_none_when_not_found(self, tmp_path):
        """No binary anywhere should return None."""
        empty = tmp_path / "empty_home"
        empty.mkdir()
        with (
            patch("bambox.bridge.shutil.which", return_value=None),
            patch("bambox.bridge.Path.home", return_value=empty),
        ):
            assert _find_local_bridge() is None


class TestRunBridgeLocal:
    def test_passes_args_directly(self):
        """Args in Rust format should be passed through to the binary."""
        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _run_bridge_local(
                "/usr/local/bin/bambox-bridge",
                ["-c", "/tmp/token.json", "status", "DEV1"],
            )
            cmd = mock_run.call_args[0][0]
            assert cmd == [
                "/usr/local/bin/bambox-bridge",
                "-c",
                "/tmp/token.json",
                "status",
                "DEV1",
            ]

    def test_verbose_flag_before_args(self):
        """-v must appear before subcommand args."""
        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _run_bridge_local(
                "/bin/bambox-bridge",
                ["-c", "/tmp/token.json", "print", "/f.3mf", "DEV1"],
                verbose=True,
            )
            cmd = mock_run.call_args[0][0]
            assert cmd == [
                "/bin/bambox-bridge",
                "-v",
                "-c",
                "/tmp/token.json",
                "print",
                "/f.3mf",
                "DEV1",
            ]


class TestRunBridgeFallback:
    def test_uses_local_binary_when_available(self):
        """_run_bridge should call local binary when found."""
        with (
            patch("bambox.bridge._find_local_bridge", return_value="/usr/local/bin/bambox-bridge"),
            patch("bambox.bridge._run_bridge_local") as mock_local,
        ):
            from bambox.bridge import _run_bridge

            _run_bridge(["status", "DEVICE123", "/tmp/token.json"])
            mock_local.assert_called_once()
            assert mock_local.call_args[0][0] == "/usr/local/bin/bambox-bridge"

    def test_falls_back_to_docker(self):
        """_run_bridge should try Docker when no local binary found."""
        with (
            patch("bambox.bridge._find_local_bridge", return_value=None),
            patch("bambox.bridge._run_bridge_docker") as mock_docker,
        ):
            from bambox.bridge import _run_bridge

            _run_bridge(["status", "DEVICE123", "/tmp/token.json"])
            mock_docker.assert_called_once()


def _make_test_3mf(path, filaments, project_settings=None):
    """Create a minimal 3MF zip with slice_info and optional project_settings."""
    slice_info = "<config>\n  <plate>\n"
    for fid, ftype, color in filaments:
        slice_info += f'    <filament id="{fid}" type="{ftype}" color="#{color}" />\n'
    slice_info += "  </plate>\n</config>"

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info)
        if project_settings is not None:
            z.writestr(
                "Metadata/project_settings.config",
                json.dumps(project_settings),
            )


class TestBuildAmsMapping:
    def test_unmatched_filament_raises_error(self, tmp_path):
        """Filaments with no matching AMS tray must raise, not silently fallback."""
        threemf = tmp_path / "test.3mf"
        # Filament slot 1: PLA, red — no matching tray in AMS
        _make_test_3mf(threemf, [(1, "PLA", "FF0000")])

        # AMS has only PETG trays — no type or color match
        ams_trays = [
            {
                "phys_slot": 0,
                "ams_id": 0,
                "slot_id": 0,
                "type": "PETG",
                "color": "00FF00",
                "tray_info_idx": "",
            },
        ]

        with pytest.raises(RuntimeError, match="Filament slot 1.*no matching AMS tray"):
            _build_ams_mapping(threemf, ams_trays)

    def test_matched_filament_maps_correctly(self, tmp_path):
        """Filaments with a matching AMS tray get the correct physical slot."""
        threemf = tmp_path / "test.3mf"
        _make_test_3mf(threemf, [(1, "PLA", "FF0000")])

        ams_trays = [
            {
                "phys_slot": 2,
                "ams_id": 0,
                "slot_id": 2,
                "type": "PLA",
                "color": "FF0000",
                "tray_info_idx": "",
            },
        ]

        result = _build_ams_mapping(threemf, ams_trays)
        assert result["amsMapping"] == [2]
        assert result["amsMapping2"] == [{"ams_id": 0, "slot_id": 2}]

    def test_mixed_matched_and_unmatched_raises(self, tmp_path):
        """Partial mismatch must raise on the unmatched filament."""
        threemf = tmp_path / "test.3mf"
        _make_test_3mf(
            threemf,
            [(1, "PLA", "FF0000"), (2, "ABS", "0000FF")],
            project_settings={"filament_colour": ["#FF0000FF", "#0000FFFF"]},
        )

        # Only PLA/red available in AMS — ABS/blue has no match
        ams_trays = [
            {
                "phys_slot": 1,
                "ams_id": 0,
                "slot_id": 1,
                "type": "PLA",
                "color": "FF0000",
                "tray_info_idx": "",
            },
        ]

        with pytest.raises(RuntimeError, match="Filament slot 2.*no matching AMS tray"):
            _build_ams_mapping(threemf, ams_trays)


def _make_namespaced_3mf(path, filaments, namespace, project_settings=None):
    """Create a minimal 3MF with a namespaced slice_info.config."""
    slice_info = f'<config xmlns="{namespace}">\n  <plate>\n'
    for fid, ftype, color in filaments:
        slice_info += f'    <filament id="{fid}" type="{ftype}" color="#{color}" />\n'
    slice_info += "  </plate>\n</config>"

    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Metadata/slice_info.config", slice_info)
        if project_settings is not None:
            z.writestr(
                "Metadata/project_settings.config",
                json.dumps(project_settings),
            )


class TestBuildAmsMappingNamespaced:
    """Verify _build_ams_mapping handles XML with a default namespace."""

    def test_namespaced_xml_maps_correctly(self, tmp_path):
        threemf = tmp_path / "ns.3mf"
        _make_namespaced_3mf(
            threemf,
            [(1, "PLA", "FF0000")],
            namespace="http://example.com/bambu",
        )

        ams_trays = [
            {
                "phys_slot": 2,
                "ams_id": 0,
                "slot_id": 2,
                "type": "PLA",
                "color": "FF0000",
                "tray_info_idx": "",
            },
        ]

        result = _build_ams_mapping(threemf, ams_trays)
        assert result["amsMapping"] == [2]
        assert result["amsMapping2"] == [{"ams_id": 0, "slot_id": 2}]

    def test_namespaced_xml_unmatched_raises(self, tmp_path):
        threemf = tmp_path / "ns.3mf"
        _make_namespaced_3mf(
            threemf,
            [(1, "PLA", "FF0000")],
            namespace="http://example.com/bambu",
        )

        ams_trays = [
            {
                "phys_slot": 0,
                "ams_id": 0,
                "slot_id": 0,
                "type": "PETG",
                "color": "00FF00",
                "tray_info_idx": "",
            },
        ]

        with pytest.raises(RuntimeError, match="Filament slot 1.*no matching AMS tray"):
            _build_ams_mapping(threemf, ams_trays)


class TestPatchConfigColors:
    """Verify _patch_config_3mf_colors handles namespaced XML."""

    def _make_config_bytes(self, filaments, namespace=None):
        """Build a config-only 3MF in memory."""
        if namespace:
            xml = f'<config xmlns="{namespace}">\n  <plate>\n'
        else:
            xml = "<config>\n  <plate>\n"
        for fid, ftype, color in filaments:
            xml += f'    <filament id="{fid}" type="{ftype}" color="#{color}" />\n'
        xml += "  </plate>\n</config>"

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Metadata/slice_info.config", xml)
        return buf.getvalue()

    def test_patches_colors_no_namespace(self, tmp_path):
        config = self._make_config_bytes([(1, "PLA", "FF0000")])
        ams_trays = [
            {"phys_slot": 0, "ams_id": 0, "slot_id": 0, "type": "PLA", "color": "00FF00"},
        ]
        mapping = [0]
        source = tmp_path / "dummy.3mf"
        source.touch()

        patched = _patch_config_3mf_colors(config, source, ams_trays, mapping)
        with zipfile.ZipFile(io.BytesIO(patched), "r") as z:
            root = ET.fromstring(z.read("Metadata/slice_info.config"))
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag[: root.tag.index("}") + 1]
            fil = root.find(f"{ns}plate").find(f"{ns}filament")
            assert fil.get("color") == "#00FF00"

    def test_patches_colors_with_namespace(self, tmp_path):
        config = self._make_config_bytes(
            [(1, "PLA", "FF0000")], namespace="http://example.com/bambu"
        )
        ams_trays = [
            {"phys_slot": 0, "ams_id": 0, "slot_id": 0, "type": "PLA", "color": "00FF00"},
        ]
        mapping = [0]
        source = tmp_path / "dummy.3mf"
        source.touch()

        patched = _patch_config_3mf_colors(config, source, ams_trays, mapping)
        with zipfile.ZipFile(io.BytesIO(patched), "r") as z:
            root = ET.fromstring(z.read("Metadata/slice_info.config"))
            ns = ""
            if root.tag.startswith("{"):
                ns = root.tag[: root.tag.index("}") + 1]
            fil = root.find(f"{ns}plate").find(f"{ns}filament")
            assert fil.get("color") == "#00FF00"


# ---------------------------------------------------------------------------
# load_credentials
# ---------------------------------------------------------------------------


class TestLoadCredentials:
    def test_loads_valid_toml(self, tmp_path):
        cred = tmp_path / "credentials.toml"
        cred.write_text('[cloud]\ntoken = "tok"\nrefresh_token = "rt"\nemail = "a@b"\nuid = "u1"\n')
        result = load_credentials(cred)
        assert result["token"] == "tok"
        assert result["refresh_token"] == "rt"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Credentials file not found"):
            load_credentials(tmp_path / "nope.toml")

    def test_missing_cloud_section_raises(self, tmp_path):
        cred = tmp_path / "credentials.toml"
        cred.write_text("[other]\nfoo = 1\n")
        with pytest.raises(ValueError, match="No \\[cloud\\] credentials"):
            load_credentials(cred)

    def test_missing_token_raises(self, tmp_path):
        cred = tmp_path / "credentials.toml"
        cred.write_text('[cloud]\nemail = "a@b"\n')
        with pytest.raises(ValueError, match="No \\[cloud\\] credentials"):
            load_credentials(cred)

    def test_default_path_when_none(self, tmp_path):
        """When path is None, delegates to load_cloud_credentials()."""
        cloud = {"token": "t", "email": "a@b"}
        with patch("bambox.credentials.load_cloud_credentials", return_value=cloud):
            result = load_credentials(None)
            assert result["token"] == "t"


# ---------------------------------------------------------------------------
# _write_token_json
# ---------------------------------------------------------------------------


class TestWriteTokenJson:
    def test_writes_json_with_correct_keys(self, tmp_path):
        cloud = {"token": "tok", "refresh_token": "rt", "email": "a@b", "uid": "u1"}
        path = _write_token_json(cloud, directory=tmp_path)
        try:
            data = json.loads(path.read_text())
            assert data["token"] == "tok"
            assert data["refreshToken"] == "rt"
            assert data["email"] == "a@b"
            assert data["uid"] == "u1"
        finally:
            path.unlink()

    def test_defaults_for_missing_keys(self, tmp_path):
        cloud = {"token": "tok"}
        path = _write_token_json(cloud, directory=tmp_path)
        try:
            data = json.loads(path.read_text())
            assert data["refreshToken"] == ""
            assert data["email"] == ""
            assert data["uid"] == ""
        finally:
            path.unlink()

    @pytest.mark.skipif(not hasattr(os, "getuid"), reason="chmod 0o600 not enforced on Windows")
    def test_file_permissions(self, tmp_path):
        cloud = {"token": "tok"}
        path = _write_token_json(cloud, directory=tmp_path)
        try:
            assert oct(path.stat().st_mode & 0o777) == oct(0o600)
        finally:
            path.unlink()


# ---------------------------------------------------------------------------
# parse_ams_trays
# ---------------------------------------------------------------------------


class TestParseAmsTrays:
    def test_parses_single_ams_unit(self):
        status = {
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {
                                "id": "0",
                                "tray_type": "PLA",
                                "tray_color": "FF0000FF",
                                "tray_info_idx": "GFL00",
                            },
                            {
                                "id": "1",
                                "tray_type": "PETG",
                                "tray_color": "00FF00FF",
                                "tray_info_idx": "GFG00",
                            },
                        ],
                    }
                ]
            }
        }
        trays = parse_ams_trays(status)
        assert len(trays) == 2
        assert trays[0] == {
            "phys_slot": 0,
            "ams_id": 0,
            "slot_id": 0,
            "type": "PLA",
            "color": "FF0000",
            "tray_info_idx": "GFL00",
        }
        assert trays[1]["phys_slot"] == 1
        assert trays[1]["color"] == "00FF00"

    def test_skips_empty_trays(self):
        status = {
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "tray": [
                            {"id": "0", "tray_type": "", "tray_color": ""},
                            {"id": "1", "tray_type": "PLA", "tray_color": "FFFFFF"},
                        ],
                    }
                ]
            }
        }
        trays = parse_ams_trays(status)
        assert len(trays) == 1
        assert trays[0]["type"] == "PLA"

    def test_multi_ams_units(self):
        status = {
            "ams": {
                "ams": [
                    {
                        "id": "0",
                        "tray": [{"id": "2", "tray_type": "PLA", "tray_color": "FF0000"}],
                    },
                    {
                        "id": "1",
                        "tray": [{"id": "0", "tray_type": "ABS", "tray_color": "0000FF"}],
                    },
                ]
            }
        }
        trays = parse_ams_trays(status)
        assert len(trays) == 2
        assert trays[0]["phys_slot"] == 2  # ams_id=0, slot_id=2 -> 0*4+2
        assert trays[1]["phys_slot"] == 4  # ams_id=1, slot_id=0 -> 1*4+0

    def test_empty_status(self):
        assert parse_ams_trays({}) == []
        assert parse_ams_trays({"ams": {}}) == []
        assert parse_ams_trays({"ams": {"ams": []}}) == []


# ---------------------------------------------------------------------------
# _strip_gcode_from_3mf
# ---------------------------------------------------------------------------


class TestStripGcodeFrom3mf:
    def test_strips_gcode_keeps_metadata(self, tmp_path):
        threemf = tmp_path / "test.3mf"
        with zipfile.ZipFile(threemf, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
            z.writestr("_rels/.rels", "<Relationships/>")
            z.writestr("Metadata/slice_info.config", "<config/>")
            z.writestr("Metadata/project_settings.config", "{}")
            z.writestr("Metadata/plate_1.json", '{"plate": 1}')
            # These should be stripped
            z.writestr("Metadata/plate_1.gcode", "G28\nG1 X10")
            z.writestr("Metadata/plate_1.png", b"fake-png")
            z.writestr("Metadata/.md5", "checksums")

        result = _strip_gcode_from_3mf(threemf)
        with zipfile.ZipFile(io.BytesIO(result), "r") as z:
            names = z.namelist()
            assert "[Content_Types].xml" in names
            assert "_rels/.rels" in names
            assert "Metadata/slice_info.config" in names
            assert "Metadata/project_settings.config" in names
            assert "Metadata/plate_1.json" in names
            # Stripped entries
            assert "Metadata/plate_1.gcode" not in names
            assert "Metadata/plate_1.png" not in names
            assert "Metadata/.md5" not in names

    def test_preserves_model_settings_rels(self, tmp_path):
        threemf = tmp_path / "test.3mf"
        with zipfile.ZipFile(threemf, "w") as z:
            z.writestr("Metadata/_rels/model_settings.config.rels", "<rels/>")
            z.writestr("Metadata/model_settings.config", "<model/>")
        result = _strip_gcode_from_3mf(threemf)
        with zipfile.ZipFile(io.BytesIO(result), "r") as z:
            assert "Metadata/_rels/model_settings.config.rels" in z.namelist()
            assert "Metadata/model_settings.config" in z.namelist()


# ---------------------------------------------------------------------------
# query_status
# ---------------------------------------------------------------------------


class TestQueryStatus:
    def test_parses_print_key(self, tmp_path):
        token = tmp_path / "token.json"
        token.write_text("{}")
        status_json = json.dumps({"print": {"mc_percent": 50, "gcode_state": "RUNNING"}})
        with patch("bambox.bridge._run_bridge") as mock_bridge:
            mock_bridge.return_value = subprocess.CompletedProcess([], 0, status_json, "")
            result = query_status("DEV1", token)
            assert result["mc_percent"] == 50

    def test_returns_raw_when_no_print_key(self, tmp_path):
        token = tmp_path / "token.json"
        token.write_text("{}")
        status_json = json.dumps({"gcode_state": "IDLE"})
        with patch("bambox.bridge._run_bridge") as mock_bridge:
            mock_bridge.return_value = subprocess.CompletedProcess([], 0, status_json, "")
            result = query_status("DEV1", token)
            assert result["gcode_state"] == "IDLE"

    def test_non_json_raises(self, tmp_path):
        token = tmp_path / "token.json"
        token.write_text("{}")
        with patch("bambox.bridge._run_bridge") as mock_bridge:
            mock_bridge.return_value = subprocess.CompletedProcess([], 1, "error text", "fail")
            with pytest.raises(RuntimeError, match="Bridge returned non-JSON"):
                query_status("DEV1", token)


# ---------------------------------------------------------------------------
# _cloud_print_impl — argument building
# ---------------------------------------------------------------------------


class TestCloudPrintImpl:
    def _setup_3mf(self, tmp_path):
        """Create a minimal 3MF for _cloud_print_impl."""
        threemf = tmp_path / "test.gcode.3mf"
        _make_test_3mf(threemf, [(1, "PLA", "FF0000")])
        token = tmp_path / "token.json"
        token.write_text("{}")
        return threemf, token

    def test_skip_ams_builds_basic_args(self, tmp_path):
        threemf, token = self._setup_3mf(tmp_path)
        response = {"result": "success"}
        with patch("bambox.bridge._run_bridge") as mock_bridge:
            mock_bridge.return_value = subprocess.CompletedProcess([], 0, json.dumps(response), "")
            result = _cloud_print_impl(
                threemf,
                "DEV1",
                token,
                project_name="test",
                timeout=60,
                verbose=False,
                skip_ams_mapping=True,
                ams_trays=[],
            )
            assert result == response
            call_args = mock_bridge.call_args[0][0]
            assert call_args[0] == "-c"
            assert call_args[2] == "print"
            assert "--project" in call_args
            assert call_args[call_args.index("--project") + 1] == "test"
            assert "--timeout" in call_args
            assert "--config-3mf" in call_args

    def test_with_ams_trays_builds_mapping_args(self, tmp_path):
        threemf, token = self._setup_3mf(tmp_path)
        ams_trays = [
            {
                "phys_slot": 2,
                "ams_id": 0,
                "slot_id": 2,
                "type": "PLA",
                "color": "FF0000",
                "tray_info_idx": "",
            },
        ]
        response = {"result": "success"}
        with patch("bambox.bridge._run_bridge") as mock_bridge:
            mock_bridge.return_value = subprocess.CompletedProcess([], 0, json.dumps(response), "")
            _cloud_print_impl(
                threemf,
                "DEV1",
                token,
                project_name="bambox",
                timeout=120,
                verbose=False,
                skip_ams_mapping=False,
                ams_trays=ams_trays,
            )
            call_args = mock_bridge.call_args[0][0]
            assert "--ams-mapping" in call_args
            assert "--ams-mapping2" in call_args

    def test_non_json_response_raises(self, tmp_path):
        threemf, token = self._setup_3mf(tmp_path)
        with patch("bambox.bridge._run_bridge") as mock_bridge:
            mock_bridge.return_value = subprocess.CompletedProcess([], 1, "garbage", "err")
            with pytest.raises(RuntimeError, match="Bridge returned non-JSON"):
                _cloud_print_impl(
                    threemf,
                    "DEV1",
                    token,
                    project_name="bambox",
                    timeout=60,
                    verbose=False,
                    skip_ams_mapping=True,
                    ams_trays=[],
                )

    def test_cleans_up_config_3mf(self, tmp_path):
        threemf, token = self._setup_3mf(tmp_path)
        response = {"result": "success"}
        with patch("bambox.bridge._run_bridge") as mock_bridge:
            mock_bridge.return_value = subprocess.CompletedProcess([], 0, json.dumps(response), "")
            _cloud_print_impl(
                threemf,
                "DEV1",
                token,
                project_name="bambox",
                timeout=60,
                verbose=False,
                skip_ams_mapping=True,
                ams_trays=[],
            )
        # Config 3mf should be cleaned up
        config_path = tmp_path / "test.gcode_config.3mf"
        assert not config_path.exists()


# ---------------------------------------------------------------------------
# _check_daemon_version
# ---------------------------------------------------------------------------


class TestCheckDaemonVersion:
    def _mock_health(self, data: dict):
        """Return a context manager that mocks urlopen to return *data* as JSON."""
        import urllib.request

        body = json.dumps(data).encode()

        class FakeResp:
            status = 200

            def read(self):
                return body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        return patch.object(urllib.request, "urlopen", return_value=FakeResp())

    def test_compatible_version_passes(self):
        health = {
            "status": "ok",
            "bridge_version": "0.4.0",
            "api_version": EXPECTED_API_VERSION,
            "plugin_version": "02.05.00.00",
        }
        with self._mock_health(health):
            _check_daemon_version()

    def test_incompatible_version_raises(self):
        health = {
            "status": "ok",
            "bridge_version": "0.9.0",
            "api_version": 999,
            "plugin_version": "02.05.00.00",
        }
        with self._mock_health(health):
            with pytest.raises(RuntimeError, match="Bridge API version mismatch"):
                _check_daemon_version()

    def test_missing_api_version_warns(self, caplog):
        health = {"status": "ok"}
        with self._mock_health(health):
            import logging

            with caplog.at_level(logging.WARNING, logger="bambox.bridge"):
                _check_daemon_version()
            assert "does not report api_version" in caplog.text

    def test_unreachable_daemon_warns(self, caplog):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            import logging

            with caplog.at_level(logging.WARNING, logger="bambox.bridge"):
                _check_daemon_version()
            assert "Could not query bridge version" in caplog.text

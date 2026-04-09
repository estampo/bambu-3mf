"""Tests for bridge.py — local binary fallback and Docker chain."""

from __future__ import annotations

import json
import subprocess
import zipfile
from unittest.mock import patch

import pytest

from bambox.bridge import _build_ams_mapping, _find_local_bridge, _run_bridge_local


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
    def test_builds_command_without_verbose(self):
        """Args are passed through, no -v flag."""
        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _run_bridge_local("/usr/local/bin/bambox-bridge", ["status", "DEV1"])
            cmd = mock_run.call_args[0][0]
            assert cmd == ["/usr/local/bin/bambox-bridge", "status", "DEV1"]

    def test_verbose_flag_before_args(self):
        """-v must appear before positional args."""
        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, "", "")
            _run_bridge_local("/bin/bambox-bridge", ["print", "DEV1", "/f.3mf"], verbose=True)
            cmd = mock_run.call_args[0][0]
            assert cmd == ["/bin/bambox-bridge", "-v", "print", "DEV1", "/f.3mf"]


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

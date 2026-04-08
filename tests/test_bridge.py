"""Tests for bridge.py — local binary fallback and Docker chain."""

from __future__ import annotations

from unittest.mock import patch

from bambox.bridge import _find_local_bridge


class TestFindLocalBridge:
    def test_finds_binary_on_path(self, tmp_path):
        """shutil.which hit should be returned."""
        with patch("bambox.bridge.shutil.which", return_value="/usr/local/bin/bambox-bridge"):
            assert _find_local_bridge() == "/usr/local/bin/bambox-bridge"

    def test_finds_binary_in_local_bin(self, tmp_path):
        """Falls back to ~/.local/bin when not on PATH."""
        binary = tmp_path / "bambox-bridge"
        binary.touch()
        binary.chmod(0o755)
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

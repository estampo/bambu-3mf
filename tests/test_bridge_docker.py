"""Tests for bridge.py — Docker invocation paths."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from bambox.bridge import (
    DOCKER_IMAGE,
    _run_bridge_docker,
)

# -- _run_bridge_docker --------------------------------------------------------


class TestRunBridgeDocker:
    def test_docker_not_installed(self):
        """FileNotFoundError from 'docker info' should raise with install hint."""
        with patch("bambox.bridge.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="Docker is not installed"):
                _run_bridge_docker(["status", "DEV1", "/tmp/token.json"])

    def test_docker_not_running(self):
        """Non-zero 'docker info' should raise with install hint."""
        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, "", "error")
            with pytest.raises(RuntimeError, match="Docker is not running"):
                _run_bridge_docker(["status", "DEV1", "/tmp/token.json"])

    def test_bind_mount_basic_args(self):
        """Non-file args should be passed through without -v mounts."""
        with patch("bambox.bridge.subprocess.run") as mock_run:
            # docker info → docker pull → docker run
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, '{"result":"ok"}', ""),
            ]
            result = _run_bridge_docker(["status", "DEV1", "/tmp/token.json"])

            docker_run_call = mock_run.call_args_list[2]
            cmd = docker_run_call[0][0]
            assert cmd[0] == "docker"
            assert cmd[1] == "run"
            assert "--rm" in cmd
            assert "--platform" in cmd
            assert DOCKER_IMAGE in cmd
            assert result.returncode == 0

    def test_args_translated_to_rust_format(self):
        """C++ positional args should be translated to Rust -c flag format."""
        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),  # docker info
                subprocess.CompletedProcess([], 0, "", ""),  # docker pull
                subprocess.CompletedProcess([], 0, '{"result":"ok"}', ""),  # docker run
            ]
            _run_bridge_docker(["status", "DEV1", "/tmp/token.json"])

            docker_run_call = mock_run.call_args_list[2]
            cmd = docker_run_call[0][0]
            # After translation: -c /tmp/token.json status DEV1
            image_idx = cmd.index(DOCKER_IMAGE)
            bridge_args = cmd[image_idx + 1 :]
            assert bridge_args[0] == "-c"
            assert bridge_args[1] == "/tmp/token.json"
            assert bridge_args[2] == "status"
            assert bridge_args[3] == "DEV1"

    def test_bind_mount_file_args(self, tmp_path):
        """Existing file paths should be volume-mounted."""
        test_file = tmp_path / "test.3mf"
        test_file.write_text("fake 3mf")
        token_file = tmp_path / "token.json"
        token_file.write_text("{}")

        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),  # docker info
                subprocess.CompletedProcess([], 0, "", ""),  # docker pull
                subprocess.CompletedProcess([], 0, '{"result":"ok"}', ""),  # docker run
            ]
            _run_bridge_docker(["print", str(test_file), "DEV1", str(token_file)])

            docker_run_call = mock_run.call_args_list[2]
            cmd = docker_run_call[0][0]
            # Should have -v flags for both files
            v_indices = [i for i, c in enumerate(cmd) if c == "-v"]
            assert len(v_indices) >= 2
            mounts = [cmd[i + 1] for i in v_indices]
            assert any(":ro" in m and "/input/" in m for m in mounts)

    def test_verbose_flag_appended(self):
        """verbose=True should add -v to Docker run command."""
        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),  # docker info
                subprocess.CompletedProcess([], 0, "", ""),  # docker pull
                subprocess.CompletedProcess([], 0, '{"result":"ok"}', ""),  # docker run
            ]
            _run_bridge_docker(["status", "DEV1"], verbose=True)

            cmd = mock_run.call_args_list[2][0][0]
            # -v should appear after the image name (as bridge arg, not docker arg)
            image_idx = cmd.index(DOCKER_IMAGE)
            tail = cmd[image_idx + 1 :]
            assert "-v" in tail

    def test_docker_image_is_rust_bridge(self):
        """DOCKER_IMAGE should point to the Rust bambox-bridge image."""
        assert DOCKER_IMAGE == "estampo/bambox-bridge:bambu-02.05.00.00"

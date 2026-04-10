"""Tests for bridge.py — Docker invocation paths (bind-mount and baked fallback)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from bambox.bridge import (
    DOCKER_IMAGE,
    _run_bridge_baked,
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
            assert "--user" not in cmd
            assert DOCKER_IMAGE in cmd
            assert "status" in cmd
            assert "DEV1" in cmd
            assert result.returncode == 0

    def test_bind_mount_file_args(self, tmp_path):
        """Existing file paths should be volume-mounted."""
        test_file = tmp_path / "test.3mf"
        test_file.write_text("fake 3mf")

        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),  # docker info
                subprocess.CompletedProcess([], 0, "", ""),  # docker pull
                subprocess.CompletedProcess([], 0, '{"result":"ok"}', ""),  # docker run
            ]
            _run_bridge_docker(["print", str(test_file), "DEV1"])

            docker_run_call = mock_run.call_args_list[2]
            cmd = docker_run_call[0][0]
            # Should have a -v flag for the file
            assert "-v" in cmd
            v_idx = cmd.index("-v")
            mount = cmd[v_idx + 1]
            assert ":ro" in mount
            assert "/input/test.3mf" in mount

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

    def test_bind_mount_failure_triggers_baked_fallback(self, tmp_path):
        """'cannot read' in stderr should trigger baked fallback."""
        test_file = tmp_path / "test.3mf"
        test_file.write_text("fake 3mf")

        with (
            patch("bambox.bridge.subprocess.run") as mock_run,
            patch("bambox.bridge._run_bridge_baked") as mock_baked,
        ):
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),  # docker info
                subprocess.CompletedProcess([], 0, "", ""),  # docker pull
                subprocess.CompletedProcess([], 1, "", "cannot read /input/test.3mf"),
            ]
            mock_baked.return_value = subprocess.CompletedProcess([], 0, '{"result":"ok"}', "")

            _run_bridge_docker(["print", str(test_file), "DEV1"])
            mock_baked.assert_called_once()

    def test_non_read_error_returns_without_fallback(self, tmp_path):
        """Errors that aren't 'cannot read' should NOT trigger baked fallback."""
        test_file = tmp_path / "test.3mf"
        test_file.write_text("fake 3mf")

        with (
            patch("bambox.bridge.subprocess.run") as mock_run,
            patch("bambox.bridge._run_bridge_baked") as mock_baked,
        ):
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),  # docker info
                subprocess.CompletedProcess([], 0, "", ""),  # docker pull
                subprocess.CompletedProcess([], 1, "", "some other error"),
            ]
            result = _run_bridge_docker(["print", str(test_file), "DEV1"])
            mock_baked.assert_not_called()
            assert result.returncode == 1

    def test_no_file_args_skips_fallback(self):
        """Failure without file args should NOT attempt baked fallback."""
        with (
            patch("bambox.bridge.subprocess.run") as mock_run,
            patch("bambox.bridge._run_bridge_baked") as mock_baked,
        ):
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),  # docker info
                subprocess.CompletedProcess([], 0, "", ""),  # docker pull
                subprocess.CompletedProcess([], 1, "", "cannot read something"),
            ]
            result = _run_bridge_docker(["status", "DEV1"])
            mock_baked.assert_not_called()
            assert result.returncode == 1


# -- _run_bridge_baked ---------------------------------------------------------


class TestRunBridgeBaked:
    def test_builds_and_runs_temp_image(self, tmp_path):
        """Should build a temp Docker image, run it, then clean up."""
        test_file = tmp_path / "test.3mf"
        test_file.write_text("fake 3mf content")
        real_path = str(test_file.resolve())

        file_args = {real_path: "/input/test.3mf"}

        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),  # docker build
                subprocess.CompletedProcess([], 0, '{"result":"ok"}', ""),  # docker run
                subprocess.CompletedProcess([], 0, "", ""),  # docker rmi
            ]
            result = _run_bridge_baked(
                ["print", str(test_file), "DEV1"],
                file_args,
            )

            assert result.returncode == 0
            # Verify docker build was called
            build_call = mock_run.call_args_list[0]
            build_cmd = build_call[0][0]
            assert build_cmd[:3] == ["docker", "build", "-t"]

            # Verify docker run was called (without --user — bridge has no host output)
            run_call = mock_run.call_args_list[1]
            run_cmd = run_call[0][0]
            assert run_cmd[0:2] == ["docker", "run"]
            assert "--user" not in run_cmd
            assert "/input/test.3mf" in run_cmd

            # Verify cleanup (docker rmi)
            rmi_call = mock_run.call_args_list[2]
            assert "rmi" in rmi_call[0][0]

    def test_build_failure_raises(self, tmp_path):
        """Failed docker build should raise RuntimeError."""
        test_file = tmp_path / "test.3mf"
        test_file.write_text("fake")
        real_path = str(test_file.resolve())
        file_args = {real_path: "/input/test.3mf"}

        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 1, "", "build error here"),  # docker build fails
                subprocess.CompletedProcess([], 0, "", ""),  # docker rmi cleanup
            ]
            with pytest.raises(RuntimeError, match="Docker build failed"):
                _run_bridge_baked(["print", str(test_file)], file_args)

    def test_verbose_flag(self, tmp_path):
        """verbose=True should add -v to the run command."""
        test_file = tmp_path / "test.3mf"
        test_file.write_text("fake")
        real_path = str(test_file.resolve())
        file_args = {real_path: "/input/test.3mf"}

        with patch("bambox.bridge.subprocess.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, "", ""),  # build
                subprocess.CompletedProcess([], 0, '{"result":"ok"}', ""),  # run
                subprocess.CompletedProcess([], 0, "", ""),  # rmi
            ]
            _run_bridge_baked(["print", str(test_file)], file_args, verbose=True)

            run_cmd = mock_run.call_args_list[1][0][0]
            assert run_cmd[-1] == "-v"

    def test_dockerfile_contents(self, tmp_path):
        """Generated Dockerfile should COPY files from base image."""
        test_file = tmp_path / "test.3mf"
        test_file.write_text("fake")
        real_path = str(test_file.resolve())
        file_args = {real_path: "/input/test.3mf"}

        dockerfiles_written: list[str] = []

        def capture_dockerfile(cmd, **kwargs):
            cwd = kwargs.get("cwd", "")
            if cwd and cmd[:2] == ["docker", "build"]:
                df = Path(cwd) / "Dockerfile"
                if df.exists():
                    dockerfiles_written.append(df.read_text())
            return subprocess.CompletedProcess([], 0, "", "")

        with patch("bambox.bridge.subprocess.run", side_effect=capture_dockerfile):
            _run_bridge_baked(["print", str(test_file)], file_args)

        assert len(dockerfiles_written) == 1
        df = dockerfiles_written[0]
        assert df.startswith(f"FROM {DOCKER_IMAGE}")
        assert "COPY test.3mf /input/test.3mf" in df

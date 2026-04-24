"""Tests for bambox CLI (cli.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bambox.cli import (
    _assign_filament_slots,
    _parse_filament_args,
    main,
)


def _touch_output_side_effect(gcode_bytes, output, **kwargs):  # type: ignore[no-untyped-def]
    """Side effect for pack_gcode_3mf that creates the output file."""
    output.write_bytes(b"fake-3mf")


# ---------------------------------------------------------------------------
# _parse_filament_args
# ---------------------------------------------------------------------------


class TestParseFilamentArgs:
    def test_none_returns_default_pla(self) -> None:
        result = _parse_filament_args(None)
        assert result == [(None, "PLA", "#F2754E")]

    def test_empty_list_returns_default_pla(self) -> None:
        result = _parse_filament_args([])
        assert result == [(None, "PLA", "#F2754E")]

    def test_type_only(self) -> None:
        result = _parse_filament_args(["PETG"])
        assert result == [(None, "PETG", "#F2754E")]

    def test_type_lowercased_is_uppercased(self) -> None:
        result = _parse_filament_args(["petg"])
        assert result == [(None, "PETG", "#F2754E")]

    def test_type_color(self) -> None:
        result = _parse_filament_args(["PLA:#FF0000"])
        assert result == [(None, "PLA", "#FF0000")]

    def test_type_color_without_hash(self) -> None:
        result = _parse_filament_args(["PLA:FF0000"])
        assert result == [(None, "PLA", "#FF0000")]

    def test_slot_type(self) -> None:
        result = _parse_filament_args(["2:PETG"])
        assert result == [(2, "PETG", "#F2754E")]

    def test_slot_type_color(self) -> None:
        result = _parse_filament_args(["3:PETG-CF:#2850E0"])
        assert result == [(3, "PETG-CF", "#2850E0")]

    def test_slot_type_color_without_hash(self) -> None:
        result = _parse_filament_args(["1:PLA:00FF00"])
        assert result == [(1, "PLA", "#00FF00")]

    def test_multiple_filaments(self) -> None:
        result = _parse_filament_args(["PLA", "3:PETG-CF:#2850E0"])
        assert len(result) == 2
        assert result[0] == (None, "PLA", "#F2754E")
        assert result[1] == (3, "PETG-CF", "#2850E0")

    def test_too_many_colons_fallback(self) -> None:
        # More than 3 parts: line 50 fallback
        result = _parse_filament_args(["a:b:c:d"])
        assert result == [(None, "A:B:C:D", "#F2754E")]


# ---------------------------------------------------------------------------
# _assign_filament_slots
# ---------------------------------------------------------------------------


class TestAssignFilamentSlots:
    def test_single_unslotted(self) -> None:
        result = _assign_filament_slots([(None, "PLA", "#FFF")])
        assert result == [(0, "PLA", "#FFF")]

    def test_multiple_unslotted(self) -> None:
        result = _assign_filament_slots(
            [
                (None, "PLA", "#FFF"),
                (None, "PETG", "#000"),
            ]
        )
        assert result == [(0, "PLA", "#FFF"), (1, "PETG", "#000")]

    def test_explicit_slot(self) -> None:
        result = _assign_filament_slots([(2, "PETG", "#000")])
        assert result == [(2, "PETG", "#000")]

    def test_unslotted_skips_explicit(self) -> None:
        result = _assign_filament_slots(
            [
                (None, "PLA", "#FFF"),
                (0, "PETG", "#000"),
            ]
        )
        # PLA should go to slot 1 since 0 is taken by PETG
        assert result == [(0, "PETG", "#000"), (1, "PLA", "#FFF")]

    def test_mixed_explicit_and_unslotted(self) -> None:
        result = _assign_filament_slots(
            [
                (None, "PLA", "#FFF"),
                (2, "PETG", "#000"),
                (None, "ABS", "#111"),
            ]
        )
        # PLA->0, ABS->1, PETG->2
        assert result == [
            (0, "PLA", "#FFF"),
            (1, "ABS", "#111"),
            (2, "PETG", "#000"),
        ]

    def test_duplicate_explicit_slot_raises(self) -> None:
        with pytest.raises(ValueError, match="Duplicate filament slot 0"):
            _assign_filament_slots([(0, "PLA", "#FFF"), (0, "PETG", "#000")])

    def test_all_explicit(self) -> None:
        result = _assign_filament_slots(
            [
                (3, "ABS", "#111"),
                (1, "PLA", "#FFF"),
            ]
        )
        assert result == [(1, "PLA", "#FFF"), (3, "ABS", "#111")]


# ---------------------------------------------------------------------------
# _cmd_pack via main()
# ---------------------------------------------------------------------------


class TestCmdPack:
    def test_pack_missing_gcode_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "nope.gcode"
        with pytest.raises(SystemExit, match="1"):
            main(["pack", str(missing)])
        assert "not found" in capsys.readouterr().err

    def test_pack_basic(self, tmp_path: Path) -> None:
        gcode = tmp_path / "test.gcode"
        gcode.write_text("; simple gcode\nG28\n")
        output = tmp_path / "test.gcode.3mf"

        with (
            patch("bambox.cli.pack_gcode_3mf", side_effect=_touch_output_side_effect) as mock_pack,
            patch("bambox.cli.build_project_settings", return_value={"key": "val"}),
        ):
            main(["pack", str(gcode), "-o", str(output)])
            mock_pack.assert_called_once()
            assert mock_pack.call_args[0][1] == output

    def test_pack_default_output_name(self, tmp_path: Path) -> None:
        gcode = tmp_path / "model.gcode"
        gcode.write_text("G28\n")

        with (
            patch("bambox.cli.pack_gcode_3mf", side_effect=_touch_output_side_effect) as mock_pack,
            patch("bambox.cli.build_project_settings", return_value={}),
        ):
            main(["pack", str(gcode)])
            assert mock_pack.call_args[0][1] == gcode.with_suffix(".gcode.3mf")

    def test_pack_with_machine_flag(self, tmp_path: Path) -> None:
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G28\n")

        with (
            patch("bambox.cli.pack_gcode_3mf", side_effect=_touch_output_side_effect),
            patch("bambox.cli.build_project_settings", return_value={}) as mock_settings,
            patch("bambox.cli.validate_printer_profile"),
        ):
            main(["pack", str(gcode), "-m", "a1mini"])
            assert mock_settings.call_args[1]["machine"] == "a1mini"

    def test_pack_derives_printer_model_id_from_machine(self, tmp_path: Path) -> None:
        """``-m p1s`` alone should set ``printer_model_id=C12`` so W001 stays quiet."""
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G28\n")

        with (
            patch("bambox.cli.pack_gcode_3mf", side_effect=_touch_output_side_effect) as mock_pack,
            patch("bambox.cli.build_project_settings", return_value={}),
        ):
            main(["pack", str(gcode), "-m", "p1s"])
            info = mock_pack.call_args[1]["slice_info"]
            assert info.printer_model_id == "C12"

    def test_pack_explicit_printer_model_id_wins(self, tmp_path: Path) -> None:
        """Explicit ``--printer-model-id`` overrides the -m fallback."""
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G28\n")

        with (
            patch("bambox.cli.pack_gcode_3mf", side_effect=_touch_output_side_effect) as mock_pack,
            patch("bambox.cli.build_project_settings", return_value={}),
        ):
            main(["pack", str(gcode), "-m", "p1s", "--printer-model-id", "OVERRIDE"])
            info = mock_pack.call_args[1]["slice_info"]
            assert info.printer_model_id == "OVERRIDE"

    def test_pack_with_filament_flag(self, tmp_path: Path) -> None:
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G28\n")

        with (
            patch("bambox.cli.pack_gcode_3mf", side_effect=_touch_output_side_effect),
            patch("bambox.cli.build_project_settings", return_value={}) as mock_settings,
        ):
            main(["pack", str(gcode), "-f", "PETG"])
            assert mock_settings.call_args[0][0] == ["PETG"]

    def test_pack_with_bambox_headers(self, tmp_path: Path) -> None:
        gcode = tmp_path / "test.gcode"
        gcode.write_text(
            "; BAMBOX_PRINTER=a1mini\n; BAMBOX_FILAMENT_TYPE=PETG\n; BAMBOX_END\nG28\n"
        )

        with (
            patch("bambox.cli.pack_gcode_3mf", side_effect=_touch_output_side_effect),
            patch("bambox.cli.build_project_settings", return_value={}) as mock_settings,
            patch("bambox.cli.validate_printer_profile"),
        ):
            main(["pack", str(gcode)])
            assert mock_settings.call_args[1]["machine"] == "a1mini"
            assert mock_settings.call_args[0][0] == ["PETG"]

    def test_pack_bambox_header_with_filament_slot(self, tmp_path: Path) -> None:
        gcode = tmp_path / "test.gcode"
        gcode.write_text(
            "; BAMBOX_PRINTER=p1s\n"
            "; BAMBOX_FILAMENT_TYPE=PLA\n"
            "; BAMBOX_FILAMENT_TYPE=PETG\n"
            "; BAMBOX_FILAMENT_SLOT=0,2\n"
            "; BAMBOX_END\n"
            "G28\n"
        )

        with (
            patch("bambox.cli.pack_gcode_3mf", side_effect=_touch_output_side_effect),
            patch("bambox.cli.build_project_settings", return_value={}) as mock_settings,
        ):
            main(["pack", str(gcode)])
            # Filament types should be assigned to correct slots
            assert mock_settings.call_args[0][0] == ["PLA", "PETG"]

    def test_pack_settings_value_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G28\n")

        with patch("bambox.cli.build_project_settings", side_effect=ValueError("bad machine")):
            with pytest.raises(SystemExit, match="1"):
                main(["pack", str(gcode)])
        assert "bad machine" in capsys.readouterr().err

    def test_pack_unknown_printer_no_artifact(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unknown printer → clean error + no artifact produced (#226)."""
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G28\n")
        output = tmp_path / "test.gcode.3mf"

        with pytest.raises(SystemExit, match="1"):
            main(["pack", str(gcode), "-o", str(output), "-m", "nonexistent_printer"])
        err = capsys.readouterr().err
        assert "Unknown printer 'nonexistent_printer'" in err
        assert "p1s" in err
        assert not output.exists()

    def test_pack_malformed_profile_names_key(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Known printer, malformed profile → error names the missing key(s) (#226)."""
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G28\n")
        output = tmp_path / "test.gcode.3mf"

        profiles_dir = tmp_path / "profiles"
        profiles_dir.mkdir()
        # Intentionally missing printer_variant, printer_settings_id, etc.
        (profiles_dir / "base_broken.json").write_text(json.dumps({"printer_model": "Broken"}))

        with (
            patch("bambox.settings._DATA_DIR", profiles_dir),
            patch("bambox.cura.PRINTER_MODEL_IDS", {"broken": "XYZ"}),
            pytest.raises(SystemExit, match="1"),
        ):
            main(["pack", str(gcode), "-o", str(output), "-m", "broken"])
        err = capsys.readouterr().err
        assert "malformed" in err
        assert "printer_variant" in err
        assert not output.exists()

    def test_pack_nozzle_and_model_id(self, tmp_path: Path) -> None:
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G28\n")

        with (
            patch("bambox.cli.pack_gcode_3mf", side_effect=_touch_output_side_effect) as mock_pack,
            patch("bambox.cli.build_project_settings", return_value={}),
        ):
            main(
                [
                    "pack",
                    str(gcode),
                    "--nozzle-diameter",
                    "0.6",
                    "--printer-model-id",
                    "C11",
                ]
            )
            slice_info = mock_pack.call_args[1]["slice_info"]
            assert slice_info.nozzle_diameter == 0.6
            assert slice_info.printer_model_id == "C11"


# ---------------------------------------------------------------------------
# _cmd_repack via main()
# ---------------------------------------------------------------------------


class TestCmdRepack:
    def test_repack_missing_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        missing = tmp_path / "nope.gcode.3mf"
        with pytest.raises(SystemExit, match="1"):
            main(["repack", str(missing)])
        assert "not found" in capsys.readouterr().err

    def test_repack_basic(self, tmp_path: Path) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")

        with patch("bambox.cli.repack_3mf") as mock_repack:
            main(["repack", str(threemf)])
            mock_repack.assert_called_once()
            assert mock_repack.call_args[1]["machine"] == "p1s"

    def test_repack_machine_without_filament(self, tmp_path: Path) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")

        with (
            patch("bambox.cli.repack_3mf") as mock_repack,
            patch("bambox.cli.validate_printer_profile"),
        ):
            main(["repack", str(threemf), "-m", "x1c"])
            assert mock_repack.call_args[1]["machine"] == "x1c"
            assert mock_repack.call_args[1]["filaments"] is None

    def test_repack_with_filament(self, tmp_path: Path) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")

        with (
            patch("bambox.cli.repack_3mf") as mock_repack,
            patch("bambox.cli.validate_printer_profile"),
        ):
            main(["repack", str(threemf), "-f", "PETG", "-m", "a1mini"])
            assert mock_repack.call_args[1]["filaments"] == ["PETG"]
            assert mock_repack.call_args[1]["machine"] == "a1mini"

    def test_repack_value_error(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")

        with patch("bambox.cli.repack_3mf", side_effect=ValueError("bad")):
            with pytest.raises(SystemExit, match="1"):
                main(["repack", str(threemf), "-f", "PLA"])
        assert "bad" in capsys.readouterr().err

    def test_repack_key_error(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")

        with patch("bambox.cli.repack_3mf", side_effect=KeyError("missing")):
            with pytest.raises(SystemExit, match="1"):
                main(["repack", str(threemf), "-f", "PLA"])
        assert "missing" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() dispatch
# ---------------------------------------------------------------------------


class TestMainDispatch:
    def test_verbose_enables_debug_logging(self, tmp_path: Path) -> None:
        gcode = tmp_path / "test.gcode"
        gcode.write_text("G28\n")

        with (
            patch("bambox.cli.pack_gcode_3mf", side_effect=_touch_output_side_effect),
            patch("bambox.cli.build_project_settings", return_value={}),
            patch("logging.basicConfig") as mock_log,
        ):
            main(["-v", "pack", str(gcode)])
            mock_log.assert_called_once()

    def test_no_command_shows_help(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit, match="2"):
            main([])
        out = capsys.readouterr().out
        assert "Usage" in out

    def test_no_command_with_flag_only(self) -> None:
        with pytest.raises(SystemExit, match="2"):
            main(["--nonexistent-flag"])

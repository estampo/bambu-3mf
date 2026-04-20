"""Tests for bambox CLI (cli.py)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bambox.cli import (
    _assign_filament_slots,
    _format_progress_bar,
    _format_status,
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
# _cmd_print via main()
# ---------------------------------------------------------------------------


class TestCmdPrint:
    @pytest.fixture(autouse=True)
    def _skip_validation(self) -> ...:  # type: ignore[override]
        """Most print tests use fake archives — bypass validate_3mf."""
        from bambox.validate import ValidationResult

        with patch("bambox.validate.validate_3mf", return_value=ValidationResult()):
            yield

    def test_print_missing_file(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        missing = tmp_path / "nope.gcode.3mf"
        with patch("bambox.bridge.load_credentials", return_value={"token": "t"}):
            with pytest.raises(SystemExit, match="1"):
                main(["print", str(missing), "-d", "SERIAL"])
        assert "not found" in capsys.readouterr().err

    def test_print_bad_credentials(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")

        with patch("bambox.bridge.load_credentials", side_effect=FileNotFoundError("no creds")):
            with pytest.raises(SystemExit, match="1"):
                main(["print", str(threemf), "-d", "SERIAL"])
        assert "no creds" in capsys.readouterr().err

    def test_print_no_device_no_serial(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        creds = {"token": "tok", "email": "e"}
        # Use a credentials file that has no printer serial
        creds_file = tmp_path / "credentials.toml"
        creds_file.write_text('[cloud]\ntoken = "tok"\n')

        with patch("bambox.bridge.load_credentials", return_value=creds):
            with pytest.raises(SystemExit, match="1"):
                main(["print", str(threemf), "-c", str(creds_file)])
        assert "no printer configured" in capsys.readouterr().err

    def test_print_device_from_credentials_file(self, tmp_path: Path) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        creds_file = tmp_path / "credentials.toml"
        creds_file.write_text('[cloud]\ntoken = "tok"\n[printers.myprinter]\nserial = "ABC123"\n')
        creds = {"token": "tok", "email": "e"}

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge.cloud_print", return_value={"result": "success"}) as mock_cp,
        ):
            main(["print", str(threemf), "-c", str(creds_file)])
            assert mock_cp.call_args[0][1] == "ABC123"

    def test_print_success(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        creds = {"token": "tok"}

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge.cloud_print", return_value={"result": "sent"}),
        ):
            main(["print", str(threemf), "-d", "SERIAL123"])
        assert "successfully" in capsys.readouterr().out

    def test_print_unknown_result(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        creds = {"token": "tok"}

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch(
                "bambox.bridge.cloud_print",
                return_value={"result": "pending", "info": "x"},
            ),
        ):
            main(["print", str(threemf), "-d", "SERIAL123"])
        out = capsys.readouterr().out
        assert "Bridge response" in out

    def test_print_exception(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        creds = {"token": "tok"}

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge.cloud_print", side_effect=RuntimeError("bridge failed")),
        ):
            with pytest.raises(SystemExit, match="1"):
                main(["print", str(threemf), "-d", "SERIAL123"])
        assert "bridge failed" in capsys.readouterr().err

    def test_print_with_ams_tray(self, tmp_path: Path) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        creds = {"token": "tok"}

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge.cloud_print", return_value={"result": "success"}) as mock_cp,
        ):
            main(
                [
                    "print",
                    str(threemf),
                    "-d",
                    "SER",
                    "--ams-tray",
                    "2:PETG-CF:2850E0",
                ]
            )
            call_kw = mock_cp.call_args[1]
            trays = call_kw["ams_trays"]
            assert len(trays) == 1
            assert trays[0]["phys_slot"] == 2
            assert trays[0]["type"] == "PETG-CF"
            assert trays[0]["color"] == "2850E0"

    def test_print_bad_ams_tray_format(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        creds = {"token": "tok"}

        with patch("bambox.bridge.load_credentials", return_value=creds):
            with pytest.raises(SystemExit, match="1"):
                main(["print", str(threemf), "-d", "SER", "--ams-tray", "bad"])
        assert "SLOT:TYPE:COLOR" in capsys.readouterr().err

    def test_print_with_project_name(self, tmp_path: Path) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        creds = {"token": "tok"}

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge.cloud_print", return_value={"result": "success"}) as mock_cp,
        ):
            main(["print", str(threemf), "-d", "SER", "--project", "MyProject"])
            assert mock_cp.call_args[1]["project_name"] == "MyProject"

    def test_print_no_ams_mapping_flag(self, tmp_path: Path) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        creds = {"token": "tok"}

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge.cloud_print", return_value={"result": "success"}) as mock_cp,
        ):
            main(["print", str(threemf), "-d", "SER", "--no-ams-mapping"])
            assert mock_cp.call_args[1]["skip_ams_mapping"] is True

    def test_print_timeout_flag(self, tmp_path: Path) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        creds = {"token": "tok"}

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge.cloud_print", return_value={"result": "success"}) as mock_cp,
        ):
            main(["print", str(threemf), "-d", "SER", "--timeout", "300"])
            assert mock_cp.call_args[1]["timeout"] == 300


class TestPrintValidation:
    """Tests for pre-print archive validation gate."""

    def test_print_blocks_on_validation_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from bambox.validate import Finding, Severity, ValidationResult

        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        bad_result = ValidationResult([Finding(Severity.ERROR, "E000", "Not a valid ZIP file")])
        with (
            patch("bambox.validate.validate_3mf", return_value=bad_result),
            patch("bambox.bridge.load_credentials", return_value={"token": "t"}),
        ):
            with pytest.raises(SystemExit, match="1"):
                main(["print", str(threemf), "-d", "SER"])
        err = capsys.readouterr().err
        assert "E000" in err
        assert "--force" in err

    def test_print_force_overrides_validation(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from bambox.validate import Finding, Severity, ValidationResult

        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        bad_result = ValidationResult([Finding(Severity.ERROR, "E000", "Not a valid ZIP file")])
        with (
            patch("bambox.validate.validate_3mf", return_value=bad_result),
            patch("bambox.bridge.load_credentials", return_value={"token": "t"}),
            patch("bambox.bridge.cloud_print", return_value={"result": "sent"}),
        ):
            main(["print", str(threemf), "-d", "SER", "--force"])
        combined = capsys.readouterr()
        assert "proceeding anyway" in combined.err
        assert "successfully" in combined.out

    def test_print_warnings_shown_but_not_blocked(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from bambox.validate import Finding, Severity, ValidationResult

        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"fake")
        warn_result = ValidationResult(
            [Finding(Severity.WARNING, "W001", "printer_model_id is empty")]
        )
        with (
            patch("bambox.validate.validate_3mf", return_value=warn_result),
            patch("bambox.bridge.load_credentials", return_value={"token": "t"}),
            patch("bambox.bridge.cloud_print", return_value={"result": "sent"}),
        ):
            main(["print", str(threemf), "-d", "SER"])
        combined = capsys.readouterr()
        assert "W001" in combined.err
        assert "successfully" in combined.out


def _make_threemf_with_bed_type(path: Path, bed_type: str) -> None:
    """Write a minimal .gcode.3mf containing only project_settings.config."""
    import zipfile

    with zipfile.ZipFile(path, "w") as z:
        z.writestr(
            "Metadata/project_settings.config",
            json.dumps({"curr_bed_type": bed_type}),
        )


class TestPrintBedTypeMismatch:
    """Tests for bed_type mismatch detection (issue #228)."""

    @pytest.fixture(autouse=True)
    def _skip_validation(self) -> ...:  # type: ignore[override]
        from bambox.validate import ValidationResult

        with patch("bambox.validate.validate_3mf", return_value=ValidationResult()):
            yield

    def test_mismatch_emits_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        _make_threemf_with_bed_type(threemf, "Cool Plate")
        creds_file = tmp_path / "credentials.toml"
        creds_file.write_text(
            '[cloud]\ntoken = "tok"\n'
            '[printers.myp]\nserial = "ABC"\nplate_type = "Textured PEI Plate"\n'
        )
        with (
            patch("bambox.bridge.load_credentials", return_value={"token": "t"}),
            patch("bambox.bridge.cloud_print", return_value={"result": "sent"}),
        ):
            main(["print", str(threemf), "-p", "myp", "-c", str(creds_file), "--no-ams-mapping"])
        err = capsys.readouterr().err
        assert "Cool Plate" in err
        assert "Textured PEI Plate" in err
        assert "nozzle crash" in err

    def test_match_emits_no_warning(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        _make_threemf_with_bed_type(threemf, "Textured PEI Plate")
        creds_file = tmp_path / "credentials.toml"
        creds_file.write_text(
            '[cloud]\ntoken = "tok"\n'
            '[printers.myp]\nserial = "ABC"\nplate_type = "Textured PEI Plate"\n'
        )
        with (
            patch("bambox.bridge.load_credentials", return_value={"token": "t"}),
            patch("bambox.bridge.cloud_print", return_value={"result": "sent"}),
        ):
            main(["print", str(threemf), "-p", "myp", "-c", str(creds_file), "--no-ams-mapping"])
        assert "nozzle crash" not in capsys.readouterr().err

    def test_match_case_insensitive(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        _make_threemf_with_bed_type(threemf, "textured pei plate")
        creds_file = tmp_path / "credentials.toml"
        creds_file.write_text(
            '[cloud]\ntoken = "tok"\n'
            '[printers.myp]\nserial = "ABC"\nplate_type = "Textured PEI Plate"\n'
        )
        with (
            patch("bambox.bridge.load_credentials", return_value={"token": "t"}),
            patch("bambox.bridge.cloud_print", return_value={"result": "sent"}),
        ):
            main(["print", str(threemf), "-p", "myp", "-c", str(creds_file), "--no-ams-mapping"])
        assert "nozzle crash" not in capsys.readouterr().err

    def test_no_plate_configured_skips_silently(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        _make_threemf_with_bed_type(threemf, "Cool Plate")
        creds_file = tmp_path / "credentials.toml"
        creds_file.write_text('[cloud]\ntoken = "tok"\n[printers.myp]\nserial = "ABC"\n')
        with (
            patch("bambox.bridge.load_credentials", return_value={"token": "t"}),
            patch("bambox.bridge.cloud_print", return_value={"result": "sent"}),
        ):
            main(["print", str(threemf), "-p", "myp", "-c", str(creds_file), "--no-ams-mapping"])
        assert "nozzle crash" not in capsys.readouterr().err

    def test_unreadable_archive_does_not_warn(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        threemf = tmp_path / "test.gcode.3mf"
        threemf.write_bytes(b"not a zip")
        creds_file = tmp_path / "credentials.toml"
        creds_file.write_text(
            '[cloud]\ntoken = "tok"\n'
            '[printers.myp]\nserial = "ABC"\nplate_type = "Textured PEI Plate"\n'
        )
        with (
            patch("bambox.bridge.load_credentials", return_value={"token": "t"}),
            patch("bambox.bridge.cloud_print", return_value={"result": "sent"}),
        ):
            main(["print", str(threemf), "-p", "myp", "-c", str(creds_file), "--no-ams-mapping"])
        assert "nozzle crash" not in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cancel via main()
# ---------------------------------------------------------------------------


class TestCmdCancel:
    def test_cancel_bad_credentials(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("bambox.bridge.load_credentials", side_effect=FileNotFoundError("no creds")):
            with pytest.raises(SystemExit, match="1"):
                main(["cancel", "-d", "SERIAL"])
        assert "no creds" in capsys.readouterr().err

    def test_cancel_success(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        creds = {"token": "tok"}
        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge.cancel_print", return_value={"result": "success"}) as mock_cp,
            patch("bambox.cli.ui.prompt_yn", return_value=True),
        ):
            main(["cancel", "-d", "SERIAL123"])
            assert mock_cp.call_args[0][0] == "SERIAL123"
        assert "cancelled" in capsys.readouterr().out.lower()

    def test_cancel_aborted_by_user(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        creds = {"token": "tok"}
        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge.cancel_print") as mock_cp,
            patch("bambox.cli.ui.prompt_yn", return_value=False),
        ):
            main(["cancel", "-d", "SERIAL123"])
            mock_cp.assert_not_called()

    def test_cancel_by_printer_name(self, tmp_path: Path) -> None:
        creds_file = tmp_path / "credentials.toml"
        creds_file.write_text('[cloud]\ntoken = "tok"\n[printers.myprinter]\nserial = "ABC123"\n')
        creds = {"token": "tok"}
        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge.cancel_print", return_value={"result": "ok"}) as mock_cp,
            patch("bambox.cli.ui.prompt_yn", return_value=True),
        ):
            main(["cancel", "-p", "myprinter", "-c", str(creds_file)])
            assert mock_cp.call_args[0][0] == "ABC123"


# ---------------------------------------------------------------------------
# _cmd_status via main()
# ---------------------------------------------------------------------------


class TestCmdStatus:
    def _make_status(self, **overrides: object) -> dict:
        base: dict = {
            "gcode_state": "IDLE",
            "nozzle_temper": 25,
            "bed_temper": 22,
        }
        base.update(overrides)
        return base

    def test_status_basic(self, capsys: pytest.CaptureFixture[str]) -> None:
        creds = {"token": "tok"}
        status = self._make_status()
        token = MagicMock()

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge._write_token_json", return_value=token),
            patch("bambox.bridge.query_status", return_value=status),
            patch("bambox.bridge.parse_ams_trays", return_value=[]),
        ):
            main(["status", "DEVICE123"])
        out = capsys.readouterr().out
        assert "IDLE" in out
        assert "25" in out

    def test_status_with_progress(self, capsys: pytest.CaptureFixture[str]) -> None:
        creds = {"token": "tok"}
        status = self._make_status(mc_percent=42, mc_remaining_time=15, subtask_name="benchy.3mf")
        token = MagicMock()

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge._write_token_json", return_value=token),
            patch("bambox.bridge.query_status", return_value=status),
            patch("bambox.bridge.parse_ams_trays", return_value=[]),
        ):
            main(["status", "DEVICE123"])
        out = capsys.readouterr().out
        assert "42%" in out
        assert "15" in out
        assert "benchy.3mf" in out

    def test_status_with_ams_trays(self, capsys: pytest.CaptureFixture[str]) -> None:
        creds = {"token": "tok"}
        status = self._make_status()
        trays = [
            {
                "phys_slot": 0,
                "type": "PLA",
                "color": "FFFFFF",
                "tray_info_idx": "GFL00",
            },
        ]
        token = MagicMock()

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge._write_token_json", return_value=token),
            patch("bambox.bridge.query_status", return_value=status),
            patch("bambox.bridge.parse_ams_trays", return_value=trays),
        ):
            main(["status", "DEVICE123"])
        out = capsys.readouterr().out
        assert "AMS" in out
        assert "PLA" in out

    def test_status_token_cleanup_on_error(self) -> None:
        creds = {"token": "tok"}
        token = MagicMock()

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge._write_token_json", return_value=token),
            patch("bambox.bridge.query_status", side_effect=RuntimeError("fail")),
        ):
            with pytest.raises(RuntimeError):
                main(["status", "DEVICE123"])
        token.unlink.assert_called_once()


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


# ---------------------------------------------------------------------------
# _format_status / _format_progress_bar
# ---------------------------------------------------------------------------


class TestFormatProgressBar:
    def test_zero_percent(self) -> None:
        bar = _format_progress_bar(0, width=10)
        assert bar == "[░░░░░░░░░░] 0%"

    def test_hundred_percent(self) -> None:
        bar = _format_progress_bar(100, width=10)
        assert bar == "[██████████] 100%"

    def test_fifty_percent(self) -> None:
        bar = _format_progress_bar(50, width=10)
        assert bar == "[█████░░░░░] 50%"

    def test_clamps_above_100(self) -> None:
        bar = _format_progress_bar(120, width=10)
        assert bar == "[██████████] 100%"

    def test_clamps_below_0(self) -> None:
        bar = _format_progress_bar(-5, width=10)
        assert bar == "[░░░░░░░░░░] 0%"

    def test_default_width(self) -> None:
        bar = _format_progress_bar(50)
        # Default width=24, half filled = 12
        assert bar.startswith("[")
        assert "50%" in bar


class TestFormatStatus:
    def test_idle_no_color(self) -> None:
        status = {"gcode_state": "IDLE", "nozzle_temper": 25, "bed_temper": 22}
        text = _format_status(status, use_color=False)
        assert "State:" in text and "IDLE" in text
        assert "25\u00b0C" in text
        assert "22\u00b0C" in text

    def test_temps_rounded(self) -> None:
        status = {"gcode_state": "IDLE", "nozzle_temper": 18.71875, "bed_temper": 16.375}
        text = _format_status(status, use_color=False)
        assert "19\u00b0C" in text
        assert "16\u00b0C" in text

    def test_target_temps_shown(self) -> None:
        status = {
            "gcode_state": "RUNNING",
            "nozzle_temper": 180,
            "nozzle_target_temper": 220,
            "bed_temper": 40,
            "bed_target_temper": 60,
        }
        text = _format_status(status, use_color=False)
        assert "180\u00b0C \u2192 220\u00b0C" in text
        assert "40\u00b0C \u2192 60\u00b0C" in text

    def test_running_with_color(self) -> None:
        status = {"gcode_state": "RUNNING", "nozzle_temper": 220, "bed_temper": 60}
        text = _format_status(status, use_color=True)
        assert "[green]" in text
        assert "RUNNING" in text

    def test_failed_color(self) -> None:
        status = {"gcode_state": "FAILED", "nozzle_temper": 0, "bed_temper": 0}
        text = _format_status(status, use_color=True)
        assert "[red bold]" in text

    def test_pause_color(self) -> None:
        status = {"gcode_state": "PAUSE", "nozzle_temper": 0, "bed_temper": 0}
        text = _format_status(status, use_color=True)
        assert "[yellow]" in text

    def test_finish_color(self) -> None:
        status = {"gcode_state": "FINISH", "nozzle_temper": 0, "bed_temper": 0}
        text = _format_status(status, use_color=True)
        assert "[blue]" in text

    def test_unknown_state_no_color_escape(self) -> None:
        status = {"gcode_state": "WEIRD", "nozzle_temper": 0, "bed_temper": 0}
        text = _format_status(status, use_color=True)
        assert "WEIRD" in text

    def test_progress_bar_rendered(self) -> None:
        status = {
            "gcode_state": "RUNNING",
            "nozzle_temper": 220,
            "bed_temper": 60,
            "mc_percent": 42,
            "mc_remaining_time": 83,
        }
        text = _format_status(status, use_color=False)
        assert "42%" in text
        assert "1h 23m" in text
        assert "█" in text

    def test_progress_no_eta(self) -> None:
        status = {
            "gcode_state": "RUNNING",
            "nozzle_temper": 220,
            "bed_temper": 60,
            "mc_percent": 10,
            "mc_remaining_time": None,
        }
        text = _format_status(status, use_color=False)
        assert "10%" in text
        assert "ETA ?" in text

    def test_subtask_name(self) -> None:
        status = {
            "gcode_state": "RUNNING",
            "nozzle_temper": 220,
            "bed_temper": 60,
            "subtask_name": "benchy.3mf",
        }
        text = _format_status(status, use_color=False)
        assert "benchy.3mf" in text

    def test_ams_trays_1_indexed(self) -> None:
        status = {"gcode_state": "IDLE", "nozzle_temper": 25, "bed_temper": 22}
        trays = [{"phys_slot": 0, "type": "PLA", "color": "FFFFFF", "tray_info_idx": "GFL00"}]
        text = _format_status(status, ams_trays=trays, use_color=False)
        assert "AMS:" in text
        assert "slot 1" in text
        assert "PLA" in text
        assert "#FFFFFF" in text

    def test_ams_active_tray_indicator(self) -> None:
        status = {
            "gcode_state": "RUNNING",
            "nozzle_temper": 220,
            "bed_temper": 60,
            "ams": {"tray_now": 0},
        }
        trays = [
            {"phys_slot": 0, "type": "PLA", "color": "FFFFFF", "tray_info_idx": "GFL00"},
            {"phys_slot": 1, "type": "PETG", "color": "2850E0", "tray_info_idx": "GFG98"},
        ]
        text = _format_status(status, ams_trays=trays, use_color=False)
        assert "<-- printing" in text
        # Only slot 0 (displayed as slot 1) should have the indicator
        lines = text.split("\n")
        pla_line = [ln for ln in lines if "PLA" in ln][0]
        petg_line = [ln for ln in lines if "PETG" in ln][0]
        assert "<-- printing" in pla_line
        assert "<-- printing" not in petg_line

    def test_ams_color_swatch(self) -> None:
        status = {"gcode_state": "IDLE", "nozzle_temper": 25, "bed_temper": 22}
        trays = [{"phys_slot": 0, "type": "PLA", "color": "2850E0", "tray_info_idx": "GFL00"}]
        text = _format_status(status, ams_trays=trays, use_color=True)
        # Should contain Rich color swatch markup
        assert "on rgb(" in text

    def test_print_stage_shown(self) -> None:
        status = {
            "gcode_state": "RUNNING",
            "nozzle_temper": 220,
            "bed_temper": 60,
            "mc_print_stage": 2,
            "layer_num": 0,
        }
        text = _format_status(status, use_color=False)
        assert "Stage:" in text
        assert "heatbed preheating" in text

    def test_print_stage_printing_when_layer_positive(self) -> None:
        status = {
            "gcode_state": "RUNNING",
            "nozzle_temper": 220,
            "bed_temper": 60,
            "mc_print_stage": 2,
            "layer_num": 5,
        }
        text = _format_status(status, use_color=False)
        assert "Stage:" in text
        assert "printing" in text

    def test_no_stage_when_idle(self) -> None:
        status = {"gcode_state": "IDLE", "nozzle_temper": 25, "bed_temper": 22}
        text = _format_status(status, use_color=False)
        assert "Stage:" not in text

    def test_eta_minutes_only(self) -> None:
        status = {
            "gcode_state": "RUNNING",
            "nozzle_temper": 220,
            "bed_temper": 60,
            "mc_percent": 90,
            "mc_remaining_time": 5,
        }
        text = _format_status(status, use_color=False)
        assert "5m" in text
        # Should NOT have hours
        assert "0h" not in text


class TestStatusWatchArgs:
    """Test that --watch and --interval flags are parsed correctly."""

    def test_watch_flag_parsed(self) -> None:
        creds = {"token": "tok"}
        status = {"gcode_state": "IDLE", "nozzle_temper": 25, "bed_temper": 22}
        token = MagicMock()

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge._write_token_json", return_value=token),
            patch("bambox.bridge.query_status", return_value=status),
            patch("bambox.bridge.parse_ams_trays", return_value=[]),
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            main(["status", "DEVICE123", "--watch"])
        # If we get here without error, the watch loop ran and exited on KeyboardInterrupt

    def test_interval_flag_parsed(self) -> None:
        creds = {"token": "tok"}
        status = {"gcode_state": "IDLE", "nozzle_temper": 25, "bed_temper": 22}
        token = MagicMock()

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge._write_token_json", return_value=token),
            patch("bambox.bridge._ensure_daemon", return_value=False),
            patch("bambox.bridge.query_status", return_value=status),
            patch("bambox.bridge.parse_ams_trays", return_value=[]),
            patch("time.sleep", side_effect=KeyboardInterrupt) as mock_sleep,
        ):
            main(["status", "DEVICE123", "--watch", "--interval", "5"])
        mock_sleep.assert_called_once_with(5)

    def test_watch_short_flag(self) -> None:
        creds = {"token": "tok"}
        status = {"gcode_state": "IDLE", "nozzle_temper": 25, "bed_temper": 22}
        token = MagicMock()

        with (
            patch("bambox.bridge.load_credentials", return_value=creds),
            patch("bambox.bridge._write_token_json", return_value=token),
            patch("bambox.bridge._ensure_daemon", return_value=False),
            patch("bambox.bridge.query_status", return_value=status),
            patch("bambox.bridge.parse_ams_trays", return_value=[]),
            patch("time.sleep", side_effect=KeyboardInterrupt),
        ):
            main(["status", "DEVICE123", "-w", "-i", "3"])

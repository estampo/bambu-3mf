"""CLI entry point for bambox."""

import json
import logging
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated, Optional

import click
import typer
from rich.markup import escape

from bambox import ui
from bambox.cura import (
    PRINTER_MODEL_IDS,
    extract_slice_stats,
    parse_bambox_headers,
)
from bambox.pack import FilamentInfo, SliceInfo, pack_gcode_3mf, repack_3mf
from bambox.settings import (
    available_filaments,
    available_machines,
    build_project_settings,
    validate_printer_profile,
)

log = logging.getLogger(__name__)

app = typer.Typer(
    name="bambox",
    help="Package and print G-code on Bambu Lab printers",
    no_args_is_help=True,
    add_completion=True,
)

_verbose: bool = False


def _version_callback(value: bool) -> None:
    if value:
        ui.console.print(f"bambox {pkg_version('bambox')}")
        raise typer.Exit()


@app.callback()
def _callback(
    verbose: Annotated[bool, typer.Option("-v", "--verbose", help="Enable debug logging")] = False,
    version: Annotated[
        bool,
        typer.Option(
            "-V",
            "--version",
            help="Show version and exit",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    global _verbose  # noqa: PLW0603
    _verbose = verbose
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")


_WARNING = (
    "[yellow]WARNING:[/yellow] bambox is experimental. "
    "Incorrect G-code or settings may damage your printer. Use at your own risk."
)


def _warn_experimental() -> None:
    ui.err_console.print(f"  {_WARNING}")


# ---------------------------------------------------------------------------
# Filament parsing (pure logic — no I/O)
# ---------------------------------------------------------------------------


def _parse_filament_args(
    filament_args: list[str] | None,
) -> list[tuple[int | None, str, str]]:
    """Parse --filament specs into (slot, type, color) triples.

    Accepted formats::

        TYPE            -> (None, TYPE, default_color)
        TYPE:COLOR      -> (None, TYPE, COLOR)
        SLOT:TYPE       -> (SLOT, TYPE, default_color)   -- SLOT is an int
        SLOT:TYPE:COLOR -> (SLOT, TYPE, COLOR)
    """
    default_color = "#F2754E"
    if not filament_args:
        return [(None, "PLA", default_color)]
    result: list[tuple[int | None, str, str]] = []
    for spec in filament_args:
        parts = spec.split(":")
        if len(parts) == 1:
            result.append((None, parts[0].upper(), default_color))
        elif len(parts) == 2:
            if parts[0].isdigit():
                result.append((int(parts[0]), parts[1].upper(), default_color))
            else:
                color = parts[1] if parts[1].startswith("#") else "#" + parts[1]
                result.append((None, parts[0].upper(), color))
        elif len(parts) == 3:
            slot = int(parts[0])
            color = parts[2] if parts[2].startswith("#") else "#" + parts[2]
            result.append((slot, parts[1].upper(), color))
        else:
            result.append((None, spec.upper(), default_color))
    return result


def _assign_filament_slots(
    parsed: list[tuple[int | None, str, str]],
) -> list[tuple[int, str, str]]:
    """Assign slot numbers to filaments, respecting explicit slot assignments.

    Filaments with explicit slots are placed first, then unslotted filaments
    fill remaining slots starting from 0.
    """
    explicit: dict[int, tuple[str, str]] = {}
    unslotted: list[tuple[str, str]] = []
    for slot, ftype, color in parsed:
        if slot is not None:
            if slot in explicit:
                raise ValueError(
                    f"Duplicate filament slot {slot}: "
                    f"'{explicit[slot][0]}' and '{ftype}' both assigned to slot {slot}"
                )
            explicit[slot] = (ftype, color)
        else:
            unslotted.append((ftype, color))

    result: list[tuple[int, str, str]] = []
    next_slot = 0
    for ftype, color in unslotted:
        while next_slot in explicit:
            next_slot += 1
        result.append((next_slot, ftype, color))
        next_slot += 1

    for slot, (ftype, color) in explicit.items():
        result.append((slot, ftype, color))

    result.sort(key=lambda x: x[0])
    return result


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def pack(
    gcode: Annotated[Path, typer.Argument(help="Input G-code file")],
    output: Annotated[
        Optional[Path], typer.Option("-o", "--output", help="Output .gcode.3mf path")
    ] = None,
    machine: Annotated[
        str,
        typer.Option(
            "-m",
            "--machine",
            help=f"Machine profile ({', '.join(available_machines())})",
        ),
    ] = "p1s",
    filament: Annotated[
        Optional[list[str]],
        typer.Option(
            "-f",
            "--filament",
            metavar="[SLOT:]TYPE[:COLOR]",
            help=f"Filament spec, repeatable. Available: {', '.join(available_filaments())}",
        ),
    ] = None,
    printer_model_id: Annotated[
        str, typer.Option("--printer-model-id", help="Printer model ID")
    ] = "",
    nozzle_diameter: Annotated[
        float, typer.Option("--nozzle-diameter", help="Nozzle diameter in mm")
    ] = 0.4,
    skip_safety: Annotated[
        bool, typer.Option("--skip-safety", help="Skip pre-packaging G-code safety checks")
    ] = False,
) -> None:
    """Package G-code into .gcode.3mf."""
    _warn_experimental()
    if not gcode.exists():
        ui.error(f"{gcode} not found")
        sys.exit(1)

    real_output = output or gcode.with_suffix(".gcode.3mf")
    gcode_bytes = gcode.read_bytes()
    gcode_str = gcode_bytes.decode(errors="replace")

    # Check for BAMBOX headers in the G-code
    headers = parse_bambox_headers(gcode_str)

    # Determine machine and filaments: headers override CLI flags
    if "PRINTER" in headers:
        machine = headers["PRINTER"]

    if "FILAMENT_SLOT" in headers:
        header_slots = headers["FILAMENT_SLOT"].split(",")
        header_types = headers["FILAMENT_TYPE"].split(",") if "FILAMENT_TYPE" in headers else []
        parsed_filaments: list[tuple[int | None, str, str]] = []
        for i, slot_str in enumerate(header_slots):
            ftype = header_types[i].strip().upper() if i < len(header_types) else ""
            if not ftype:
                ftype = "PLA"
            parsed_filaments.append((int(slot_str), ftype, "#F2754E"))
        assigned = _assign_filament_slots(parsed_filaments)
    elif "FILAMENT_TYPE" in headers:
        header_types = headers["FILAMENT_TYPE"].split(",")
        parsed_filaments = []
        for t in header_types:
            parsed_filaments.append((None, t.strip().upper() or "PLA", "#F2754E"))
        assigned = _assign_filament_slots(parsed_filaments)
    else:
        assigned = _assign_filament_slots(_parse_filament_args(filament))

    # Pre-flight: make sure the printer we resolved (CLI flag or header)
    # has a well-formed bundled profile before doing any real work. This
    # surfaces unknown / malformed printers at pack time rather than at
    # print time with a cryptic firmware error.
    try:
        validate_printer_profile(machine)
    except ValueError as e:
        ui.error(str(e))
        sys.exit(1)

    filament_types = [f[1] for f in assigned]
    filament_colors = [f[2] for f in assigned]

    filament_infos = [
        FilamentInfo(
            slot=slot + 1,
            filament_type=ftype,
            color=color,
        )
        for slot, ftype, color in assigned
    ]

    # Look up tray_info_idx from filament profiles
    from bambox.settings import _filament_profile_path, _load_json

    for fi in filament_infos:
        try:
            profile = _load_json(_filament_profile_path(fi.filament_type))
            if "filament_ids" in profile:
                fi.tray_info_idx = profile["filament_ids"]
        except ValueError:
            pass

    # Auto-derive printer_model_id from the resolved machine name (set via -m
    # or a BAMBOX_PRINTER header). Without this fallback, callers that don't
    # know to pass --printer-model-id — including estampo's CuraEngine
    # pipeline — produce archives that fail W001 and may be rejected by the
    # printer firmware.
    real_printer_model_id = printer_model_id
    if not real_printer_model_id:
        real_printer_model_id = PRINTER_MODEL_IDS.get(machine.lower(), "")

    # Extract print time and filament weight from slicer G-code comments
    stats = extract_slice_stats(gcode_str)

    # Populate per-filament usage from stats
    if stats.filament_used_m:
        from bambox.cura import _PLA_DENSITY_G_PER_MM3
        from bambox.gcode_compat import _FILAMENT_AREA

        for fi in filament_infos:
            slot_idx = fi.slot - 1
            if slot_idx < len(stats.filament_used_m):
                fi.used_m = stats.filament_used_m[slot_idx]
                length_mm = stats.filament_used_m[slot_idx] * 1000
                fi.used_g = round(length_mm * _FILAMENT_AREA * _PLA_DENSITY_G_PER_MM3, 2)

    info = SliceInfo(
        printer_model_id=real_printer_model_id,
        nozzle_diameter=nozzle_diameter,
        prediction=stats.prediction,
        weight=stats.weight,
        filaments=filament_infos,
    )

    # Generate project_settings from machine + filament profiles
    try:
        project_settings = build_project_settings(
            filament_types,
            machine=machine,
            filament_colors=filament_colors,
        )
    except ValueError as e:
        ui.error(str(e))
        sys.exit(1)

    if not skip_safety:
        from bambox.validate import validate_gcode

        safety_result = validate_gcode(gcode_str)
        if not safety_result.valid:
            for f in safety_result.findings:
                if f.severity.value == "error":
                    ui.error(f"[{f.code}] {f.message}")
                    if f.detail:
                        ui.error(f"  {f.detail}")
            ui.error("G-code safety check failed. Use --skip-safety to override.")
            sys.exit(1)
        for f in safety_result.warnings:
            ui.warn(f"[{f.code}] {f.message}")

    pack_gcode_3mf(gcode_bytes, real_output, slice_info=info, project_settings=project_settings)
    ui.success(f"Wrote {real_output} ({real_output.stat().st_size} bytes)")


@app.command()
def repack(
    threemf: Annotated[Path, typer.Argument(help="Input .gcode.3mf file (modified in-place)")],
    machine: Annotated[
        str,
        typer.Option(
            "-m",
            "--machine",
            help=f"Machine profile ({', '.join(available_machines())})",
        ),
    ] = "p1s",
    filament: Annotated[
        Optional[list[str]],
        typer.Option(
            "-f",
            "--filament",
            metavar="TYPE[:COLOR]",
            help="Filament type to regenerate settings",
        ),
    ] = None,
) -> None:
    """Fix up existing .gcode.3mf for Bambu Connect."""
    _warn_experimental()
    if not threemf.exists():
        ui.error(f"{threemf} not found")
        sys.exit(1)

    if filament:
        assigned_repack = _assign_filament_slots(_parse_filament_args(filament))
        filament_types: list[str] | None = [f[1] for f in assigned_repack]
        filament_colors: list[str] | None = [f[2] for f in assigned_repack]
    else:
        filament_types = None
        filament_colors = None
    real_machine = machine

    try:
        validate_printer_profile(real_machine)
    except ValueError as e:
        ui.error(str(e))
        sys.exit(1)

    try:
        repack_3mf(
            threemf,
            machine=real_machine,
            filaments=filament_types,
            filament_colors=filament_colors,
        )
    except (ValueError, KeyError) as e:
        ui.error(str(e))
        sys.exit(1)

    ui.success(f"Repacked {threemf} ({threemf.stat().st_size} bytes)")


@app.command()
def validate(
    threemf: Annotated[Path, typer.Argument(help="Input .gcode.3mf file")],
    json_output: Annotated[bool, typer.Option("--json", help="Output results as JSON")] = False,
    strict: Annotated[
        bool, typer.Option("--strict", help="Treat warnings as errors (non-zero exit)")
    ] = False,
    reference: Annotated[
        Optional[Path], typer.Option("--reference", help="Reference .gcode.3mf to compare against")
    ] = None,
) -> None:
    """Validate a .gcode.3mf archive."""
    _warn_experimental()
    from bambox.validate import Severity, compare_3mf, validate_3mf

    if not threemf.exists():
        ui.error(f"{threemf} not found")
        sys.exit(1)

    result = validate_3mf(threemf)

    ref_result = None
    if reference:
        if not reference.exists():
            ui.error(f"reference {reference} not found")
            sys.exit(1)
        ref_result = compare_3mf(threemf, reference)

    if json_output:
        output = result.to_dict()
        if ref_result is not None:
            output["comparison"] = ref_result.to_dict()
        ui.console.print(json.dumps(output, indent=2), markup=False)
    else:
        name = threemf.name
        for f in result.findings:
            if f.severity == Severity.ERROR:
                prefix = f"  [red]ERROR[/red] {f.code}"
            else:
                prefix = f"  [yellow]WARN[/yellow]  {f.code}"
            detail_str = f" \\[{escape(f.detail)}]" if f.detail else ""
            ui.console.print(f"{prefix}: {escape(f.message)}{detail_str}")

        if ref_result is not None:
            for f in ref_result.findings:
                prefix = f"  COMP  {f.code}"
                detail_str = f" \\[{escape(f.detail)}]" if f.detail else ""
                ui.console.print(f"{prefix}: {escape(f.message)}{detail_str}")

        n_err = len(result.errors)
        n_warn = len(result.warnings)
        n_comp = len(ref_result.errors) if ref_result else 0
        total_err = n_err + n_comp

        if total_err == 0 and n_warn == 0:
            ui.success(f"{name}: valid")
        elif total_err == 0:
            ui.success(f"{name}: valid ({n_warn} warning{'s' if n_warn != 1 else ''})")
        else:
            ui.error(
                f"{name}: INVALID ({total_err} error{'s' if total_err != 1 else ''}, "
                f"{n_warn} warning{'s' if n_warn != 1 else ''})"
            )

    has_errors = not result.valid or (ref_result is not None and not ref_result.valid)
    if has_errors:
        sys.exit(1)
    if strict and result.warnings:
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    try:
        app(argv, standalone_mode=False)
    except click.UsageError as exc:
        ui.error(str(exc))
        sys.exit(2)

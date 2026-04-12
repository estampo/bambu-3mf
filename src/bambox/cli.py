"""CLI entry point for bambox."""

import json
import logging
import sys
import time
from collections.abc import Callable
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated, Optional

import click
import typer
from rich.markup import escape

from bambox import ui
from bambox.cura import (
    PRINTER_MODEL_IDS,
    build_template_context,
    extract_slice_stats,
    parse_bambox_headers,
    strip_bambox_header,
)
from bambox.pack import FilamentInfo, SliceInfo, pack_gcode_3mf, repack_3mf
from bambox.settings import available_filaments, available_machines, build_project_settings

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
# Display helpers
# ---------------------------------------------------------------------------


def _format_progress_bar(percent: int, width: int = 24) -> str:
    """Render a simple progress bar string from a percentage (0-100)."""
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    empty = width - filled
    return f"[{'█' * filled}{'░' * empty}] {percent}%"


_PRINT_STAGES: dict[str, str] = {
    "0": "printing",
    "1": "auto bed leveling",
    "2": "heatbed preheating",
    "3": "sweeping XY mech mode",
    "4": "changing filament",
    "5": "M400 pause",
    "6": "filament runout pause",
    "7": "heating hotend",
    "8": "calibrating extrusion",
    "9": "scanning bed surface",
    "10": "inspecting first layer",
    "11": "identifying build plate type",
    "12": "calibrating micro lidar",
    "13": "homing toolhead",
    "14": "cleaning nozzle tip",
    "17": "calibrating extrusion flow",
    "18": "vibration compensation",
    "19": "motor noise calibration",
}


def _format_status(
    status: dict,
    ams_trays: list[dict] | None = None,
    use_color: bool = True,
) -> str:
    """Format printer status dict into a human-readable string.

    When *use_color* is True, Rich markup is used for styling.
    """
    lines: list[str] = []

    state = status.get("gcode_state", "?")
    if use_color:
        lines.append(f"  State:    {ui.format_state(state)}")
    else:
        lines.append(f"  State:    {state}")

    # Task name
    task_name = status.get("subtask_name", "")
    if task_name:
        lines.append(f"  Task:     {task_name}")

    # Print stage (only when actively printing)
    if state not in ("IDLE", "FINISH", "FAILED", "", "?"):
        layer = status.get("layer_num", 0)
        stage_id = str(status.get("mc_print_stage", ""))
        if layer and int(layer) > 0:
            stage = "printing"
        else:
            stage = _PRINT_STAGES.get(stage_id, "")
        if stage:
            lines.append(f"  Stage:    {stage}")

    # Temperatures — rounded to integers, with target arrows
    nozzle = status.get("nozzle_temper", 0)
    nozzle_target = status.get("nozzle_target_temper", 0)
    bed = status.get("bed_temper", 0)
    bed_target = status.get("bed_target_temper", 0)
    try:
        nozzle_str = f"{float(nozzle):.0f}\u00b0C"
        if nozzle_target and float(nozzle_target) > 0:
            nozzle_str += f" \u2192 {float(nozzle_target):.0f}\u00b0C"
    except (ValueError, TypeError):
        nozzle_str = f"{nozzle}\u00b0C"
    try:
        bed_str = f"{float(bed):.0f}\u00b0C"
        if bed_target and float(bed_target) > 0:
            bed_str += f" \u2192 {float(bed_target):.0f}\u00b0C"
    except (ValueError, TypeError):
        bed_str = f"{bed}\u00b0C"
    lines.append(f"  Nozzle:   {nozzle_str}")
    lines.append(f"  Bed:      {bed_str}")

    # Progress bar
    mc_percent = status.get("mc_percent")
    if mc_percent:
        bar = _format_progress_bar(int(mc_percent))
        # Escape brackets so Rich doesn't interpret the bar as markup
        bar = bar.replace("[", "\\[")
        remaining = status.get("mc_remaining_time", "?")
        if remaining != "?" and remaining is not None:
            try:
                mins = int(remaining)
                hrs, m = divmod(mins, 60)
                eta_str = f"{hrs}h {m:02d}m" if hrs else f"{m}m"
            except (ValueError, TypeError):
                eta_str = f"{remaining}min"
        else:
            eta_str = "?"
        lines.append(f"  Progress: {bar}  ETA {eta_str}")

    # AMS trays — 1-indexed with color swatches
    if ams_trays:
        tray_now = int(status.get("ams", {}).get("tray_now", 255))
        lines.append("  AMS:")
        for t in ams_trays:
            slot_num = t["phys_slot"] + 1  # 1-indexed display
            active = " <-- printing" if t["phys_slot"] == tray_now else ""
            color_hex = t["color"]
            if use_color:
                swatch = ui.color_swatch(color_hex)
            else:
                swatch = "  "
            lines.append(f"    slot {slot_num}  {t['type']:<12}  {swatch} #{color_hex}{active}")

    return "\n".join(lines)


def _show_print_info(threemf: Path) -> None:
    """Display print metadata (time, weight, layers, filaments) from a 3MF."""
    import xml.etree.ElementTree as ET
    import zipfile

    from bambox.bridge import _xml_ns

    try:
        with zipfile.ZipFile(threemf, "r") as z:
            prediction = 0
            weight = 0.0
            filaments: list[tuple[str, str, str, str]] = []

            if "Metadata/slice_info.config" in z.namelist():
                root = ET.fromstring(z.read("Metadata/slice_info.config"))
                ns = _xml_ns(root)
                plate_el = root.find(f"{ns}plate")
                if plate_el is not None:
                    meta = {}
                    for md in plate_el.findall(f"{ns}metadata"):
                        meta[md.get("key", "")] = md.get("value", "")
                    try:
                        prediction = int(meta.get("prediction", "0"))
                    except ValueError:
                        pass
                    try:
                        weight = float(meta.get("weight", "0"))
                    except ValueError:
                        pass
                    for f in plate_el.findall(f"{ns}filament"):
                        filaments.append(
                            (
                                f.get("type", "?"),
                                f.get("color", "?"),
                                f.get("used_m", "0"),
                                f.get("used_g", "0"),
                            )
                        )

            layers = 0
            gcode_name = None
            for name in z.namelist():
                if name.startswith("Metadata/plate_") and name.endswith(".gcode"):
                    gcode_name = name
                    break
            if gcode_name:
                gcode_head = z.read(gcode_name)[:4096].decode(errors="replace")
                import re

                m = re.search(r"; total layer number:\s*(\d+)", gcode_head)
                if m:
                    layers = int(m.group(1))
                else:
                    m = re.search(r";LAYER_COUNT:(\d+)", gcode_head)
                    if m:
                        layers = int(m.group(1))

    except (zipfile.BadZipFile, ET.ParseError, KeyError) as e:
        ui.warn(f"could not read 3MF metadata: {e}")
        return

    if prediction > 0:
        hrs, remainder = divmod(prediction, 3600)
        mins = remainder // 60
        time_str = f"{hrs}h{mins}m" if hrs else f"{mins}m"
    else:
        time_str = "unknown"

    ui.console.print()
    ui.console.print(f"Print: {threemf.name}")
    if layers:
        ui.info(f"Layers:    {layers}")
    ui.info(f"Time:      {time_str}")
    ui.info(f"Weight:    {weight:.1f}g")
    if filaments:
        ui.info("Filaments:")
        for ftype, color, used_m, used_g in filaments:
            ui.info(f"  - {ftype} {color}  ({used_m}m / {used_g}g)")
    ui.console.print()


def _show_ams_mapping(threemf: Path, ams_trays: list[dict], mapping: list[int]) -> None:
    """Display the AMS filament mapping that will be used for the print."""
    import xml.etree.ElementTree as ET
    import zipfile

    from bambox.bridge import _xml_ns

    if not any(v >= 0 for v in mapping):
        return

    filaments: dict[int, tuple[str, str]] = {}
    try:
        with zipfile.ZipFile(threemf, "r") as z:
            if "Metadata/slice_info.config" in z.namelist():
                root = ET.fromstring(z.read("Metadata/slice_info.config"))
                ns = _xml_ns(root)
                plate_el = root.find(f"{ns}plate")
                if plate_el is not None:
                    for f in plate_el.findall(f"{ns}filament"):
                        fid = int(f.get("id", "1"))
                        filaments[fid] = (f.get("type", "?"), f.get("color", "?"))
    except (OSError, KeyError, ET.ParseError):
        log.debug("Failed to parse slice_info for AMS display", exc_info=True)

    tray_by_phys = {t["phys_slot"]: t for t in ams_trays}

    ui.console.print("AMS filament mapping:")
    for idx, phys_slot in enumerate(mapping):
        filament_id = idx + 1
        if phys_slot < 0:
            continue
        fil_type, fil_color = filaments.get(filament_id, ("?", "?"))
        tray = tray_by_phys.get(phys_slot, {})
        tray_type = tray.get("type", "?")
        tray_color = tray.get("color", "?")
        ui.info(
            f"Slot {filament_id} ({fil_type} {fil_color}) "
            f"-> AMS tray {phys_slot} ({tray_type} #{tray_color})"
        )
    ui.console.print()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _resolve_printer(printer_name: str | None, creds_path: Path | None) -> tuple[str, str]:
    """Resolve a printer name to (serial, display_name).

    Tries: named printer from credentials, then first cloud printer found.
    """
    import tomllib

    if creds_path:
        path = creds_path
    else:
        from bambox.credentials import _credentials_path

        path = _credentials_path()

    if printer_name:
        if not path.exists():
            ui.error(f"credentials file not found: {path}")
            sys.exit(1)
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        printers = raw.get("printers", {})
        if printer_name not in printers:
            ui.error(f"printer '{printer_name}' not found")
            sys.exit(1)
        serial = printers[printer_name].get("serial", "")
        if not serial:
            ui.error(f"printer '{printer_name}' has no serial number")
            sys.exit(1)
        return serial, printer_name

    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        for name, p in raw.get("printers", {}).items():
            if p.get("serial"):
                return p["serial"], name

    ui.error("no printer configured. Run 'bambox login' or use --device.")
    sys.exit(1)


def _name_printers(token: str) -> None:
    """List bound printers and let user name up to 5."""
    from bambox.auth import _get_devices
    from bambox.credentials import mask_serial, save_printer

    try:
        devices = _get_devices(token)
    except (OSError, KeyError):
        ui.warn("Could not fetch printer list.")
        return

    if not devices:
        ui.info("No printers found on this account.")
        return

    ui.console.print(f"\n  Found {len(devices)} printer(s):")
    for i, d in enumerate(devices, 1):
        name = d.get("name", "unnamed")
        model = d.get("dev_product_name", d.get("dev_model_name", "?"))
        serial = d.get("dev_id", "?")
        online = "online" if d.get("online") else "offline"
        ui.console.print(f"  {i}. {name} ({model}) — {mask_serial(serial)} \\[{online}]")

    limit = min(len(devices), 5)
    ui.console.print(f"\n  Name your printer(s) (up to {limit}). Press Enter to skip.")
    for i in range(limit):
        d = devices[i]
        dev_name = d.get("name", f"printer-{i + 1}")
        serial = d.get("dev_id", "")
        default = dev_name.lower().replace(" ", "-")
        raw = ui.prompt_str(f"Name for #{i + 1} [{default}] (enter '-' to skip)")
        if raw == "-":
            continue
        name = raw or default
        save_printer(name, {"type": "bambu-cloud", "serial": serial})
        ui.success(f"Saved '{name}' ({mask_serial(serial)})")


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

    # Auto-derive printer_model_id from BAMBOX_PRINTER header if not set via CLI
    real_printer_model_id = printer_model_id
    if not real_printer_model_id and "PRINTER" in headers:
        real_printer_model_id = PRINTER_MODEL_IDS.get(headers["PRINTER"].lower(), "")

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

    # If BAMBOX_ASSEMBLE=true, render start/end templates and wrap toolpath
    if headers.get("ASSEMBLE") == "true":
        from bambox.assemble import assemble_gcode
        from bambox.gcode_compat import rewrite_tool_changes
        from bambox.templates import render_template

        toolpath = strip_bambox_header(gcode_str)

        if len(filament_types) > 1:
            toolpath = rewrite_tool_changes(toolpath, project_settings, machine)

        ctx = build_template_context(headers, project_settings)

        from bambox.cura import first_layer_bbox

        bbox = first_layer_bbox(toolpath)
        if bbox:
            ctx["first_layer_print_min"] = bbox[0]
            ctx["first_layer_print_size"] = bbox[1]

        start = render_template(f"{machine}_start.gcode.j2", ctx)
        end = render_template(f"{machine}_end.gcode.j2", ctx)
        gcode_bytes = assemble_gcode(start, toolpath, end).encode()
        if headers:
            ui.info(f"Auto-configured from BAMBOX headers: {machine}, {filament_types}")

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
    real_machine = machine if filament_types else None

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


@app.command()
def login() -> None:
    """Log in to Bambu Cloud and configure printers."""
    _warn_experimental()
    import os

    from bambox.auth import _get_user_profile, _login
    from bambox.credentials import load_cloud_credentials, save_cloud_credentials

    cloud = load_cloud_credentials()
    if cloud and cloud.get("token"):
        try:
            profile = _get_user_profile(cloud["token"])
            ui.success(f"Already logged in as {profile.get('name') or profile['uid']}")
            if not ui.prompt_yn("Re-login?", default=False):
                _name_printers(cloud["token"])
                return
        except (OSError, KeyError):
            ui.warn("Cached token is invalid or expired.")

    email = os.environ.get("BAMBU_EMAIL") or ui.prompt_str("Email")
    password = os.environ.get("BAMBU_PASSWORD") or ui.prompt_password("Password")
    if not email or not password:
        ui.error("email and password required")
        sys.exit(1)

    token, refresh_token = _login(email, password)
    profile = _get_user_profile(token)

    save_cloud_credentials(
        token=token,
        refresh_token=refresh_token,
        email=email,
        uid=profile["uid"],
    )

    ui.success(f"Login successful! User: {profile.get('name') or profile['uid']}")
    _name_printers(token)


@app.command(name="print")
def print_cmd(
    threemf: Annotated[Path, typer.Argument(help="Input .gcode.3mf file")],
    device: Annotated[str, typer.Option("-d", "--device", help="Printer serial number")] = "",
    printer: Annotated[
        Optional[str], typer.Option("-p", "--printer", help="Named printer from credentials.toml")
    ] = None,
    credentials: Annotated[
        Optional[Path], typer.Option("-c", "--credentials", help="Path to credentials.toml")
    ] = None,
    project: Annotated[
        Optional[str], typer.Option("--project", help="Project name shown in cloud")
    ] = None,
    timeout: Annotated[int, typer.Option("--timeout", help="Timeout in seconds")] = 180,
    no_ams_mapping: Annotated[
        bool, typer.Option("--no-ams-mapping", help="Skip AMS mapping")
    ] = False,
    ams_tray: Annotated[
        Optional[list[str]],
        typer.Option(
            "--ams-tray", metavar="SLOT:TYPE:COLOR", help="Manually specify AMS tray. Repeatable."
        ),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("-n", "--dry-run", help="Show print info without sending")
    ] = False,
) -> None:
    """Send a .gcode.3mf to a Bambu printer via cloud bridge."""
    _warn_experimental()
    from bambox.bridge import cloud_print, load_credentials

    if not threemf.exists():
        ui.error(f"{threemf} not found")
        sys.exit(1)

    creds_path = credentials
    try:
        creds = load_credentials(creds_path)
    except (FileNotFoundError, ValueError) as e:
        ui.error(str(e))
        sys.exit(1)

    device_id = device
    if not device_id:
        device_id, _ = _resolve_printer(printer, creds_path)

    project_name = project or threemf.stem

    # Parse --ams-tray flags (e.g. "2:PETG-CF:2850E0")
    ams_trays: list[dict] = []
    for spec in ams_tray or []:
        parts = spec.split(":")
        if len(parts) != 3:
            ui.error(f"--ams-tray must be SLOT:TYPE:COLOR, got '{spec}'")
            sys.exit(1)
        slot_str, ftype, color = parts
        phys_slot = int(slot_str)
        ams_trays.append(
            {
                "phys_slot": phys_slot,
                "ams_id": phys_slot // 4,
                "slot_id": phys_slot % 4,
                "type": ftype,
                "color": color.upper(),
                "tray_info_idx": "",
            }
        )

    _show_print_info(threemf)

    if dry_run:
        if not no_ams_mapping:
            from bambox.bridge import (
                _build_ams_mapping,
                _write_token_json,
                parse_ams_trays,
                query_status,
            )

            token_file = _write_token_json(creds, directory=threemf.parent)
            try:
                if not ams_trays:
                    try:
                        live_status = query_status(device_id, token_file, verbose=_verbose)
                        ams_trays = parse_ams_trays(live_status)
                    except Exception as e:
                        ui.warn(f"could not query AMS state: {e}")
                if ams_trays:
                    try:
                        ams_data = _build_ams_mapping(threemf, ams_trays)
                        mapping = ams_data["amsMapping"]
                        _show_ams_mapping(threemf, ams_trays, mapping)
                    except RuntimeError as e:
                        ui.warn(f"AMS mapping error: {e}")
            finally:
                try:
                    token_file.unlink()
                except OSError:
                    pass
        ui.info("Dry run — not sending to printer.")
        return

    ui.info(f"Sending {threemf.name} to {device_id}...")
    try:
        result = cloud_print(
            threemf,
            device_id,
            credentials=creds,
            project_name=project_name,
            timeout=timeout,
            verbose=_verbose,
            skip_ams_mapping=no_ams_mapping,
            ams_trays=ams_trays or None,
        )

        ams_mapping = result.get("_ams_mapping")
        ams_trays_used = result.get("_ams_trays")
        if ams_mapping and ams_trays_used:
            _show_ams_mapping(threemf, ams_trays_used, ams_mapping)

        resp_status = result.get("result", "unknown")
        if resp_status in ("success", "sent"):
            ui.success(f"Print sent successfully! ({resp_status})")
        else:
            ui.console.print(f"Bridge response: {json.dumps(result, indent=2)}", markup=False)
    except Exception as e:
        ui.error(str(e))
        sys.exit(1)


@app.command()
def cancel(
    device: Annotated[str, typer.Option("-d", "--device", help="Printer serial number")] = "",
    printer: Annotated[
        Optional[str], typer.Option("-p", "--printer", help="Named printer from credentials.toml")
    ] = None,
    credentials: Annotated[
        Optional[Path], typer.Option("-c", "--credentials", help="Path to credentials.toml")
    ] = None,
) -> None:
    """Cancel the current print on a Bambu printer."""
    _warn_experimental()
    from bambox.bridge import cancel_print, load_credentials

    creds_path = credentials
    try:
        creds = load_credentials(creds_path)
    except (FileNotFoundError, ValueError) as e:
        ui.error(str(e))
        sys.exit(1)

    serial = device
    if not serial:
        if printer:
            serial, name = _resolve_printer(printer, creds_path)
        else:
            serial, name = _resolve_printer(None, creds_path)

    if not serial:
        ui.error("No printer specified. Use --device or --printer.")
        sys.exit(1)

    if not ui.prompt_yn("Cancel the current print?", default=False):
        ui.info("Cancelled.")
        return

    try:
        result = cancel_print(serial, credentials=creds, verbose=_verbose)
        resp = result.get("result", "unknown")
        if resp in ("success", "ok"):
            ui.success("Print cancelled.")
        else:
            ui.console.print(f"Bridge response: {json.dumps(result, indent=2)}", markup=False)
    except Exception as e:
        ui.error(str(e))
        sys.exit(1)


@app.command()
def status(
    device: Annotated[str, typer.Argument(help="Printer serial number")] = "",
    printer: Annotated[
        Optional[str], typer.Option("-p", "--printer", help="Named printer from credentials.toml")
    ] = None,
    credentials: Annotated[
        Optional[Path], typer.Option("-c", "--credentials", help="Path to credentials.toml")
    ] = None,
    watch: Annotated[
        bool, typer.Option("-w", "--watch", help="Continuously refresh status display")
    ] = False,
    interval: Annotated[
        int,
        typer.Option("-i", "--interval", help="Poll-mode refresh interval (daemon uses 1s)"),
    ] = 5,
) -> None:
    """Query printer status."""
    from bambox.bridge import _write_token_json, load_credentials, parse_ams_trays, query_status

    creds_path = credentials

    device_id = device
    printer_name = ""
    if not device_id:
        device_id, printer_name = _resolve_printer(printer, creds_path)

    def _print_header() -> None:
        if printer_name:
            ui.console.print(f"[bold]{printer_name}[/bold]  (bambu-cloud)")
        else:
            ui.console.print(f"[bold]{device_id}[/bold]")

    creds = load_credentials(creds_path)
    token_file = _write_token_json(creds)
    try:
        if watch:
            _status_watch(device_id, token_file, interval, _print_header, _verbose)
        else:
            _print_header()
            st = query_status(device_id, token_file, verbose=_verbose)
            trays = parse_ams_trays(st)
            ui.console.print(_format_status(st, ams_trays=trays))
    finally:
        try:
            token_file.unlink()
        except OSError:
            pass


def _status_watch(
    device_id: str,
    token_file: Path,
    interval: int,
    print_header: Callable,
    verbose: bool,
) -> None:
    """Watch mode: use daemon for fast polling, refresh display in-place."""
    from datetime import datetime, timezone

    from bambox.bridge import (
        _ensure_daemon,
        parse_ams_trays,
        query_status,
        query_status_daemon,
    )

    use_daemon = _ensure_daemon(token_file, verbose=verbose)
    if use_daemon:
        ui.console.print("[dim]Connected via daemon (fast mode)[/dim]")
    else:
        ui.console.print("[dim]Daemon not available, using poll mode[/dim]")

    last_lines = 0
    try:
        while True:
            try:
                if use_daemon:
                    st = query_status_daemon(device_id)
                else:
                    st = query_status(device_id, token_file, verbose=verbose)
                trays = parse_ams_trays(st)
            except Exception as e:
                if use_daemon:
                    # Daemon may have died — fall back to poll
                    use_daemon = False
                    continue
                ui.error(f"Query failed: {e}")
                time.sleep(interval)
                continue

            # Move cursor up to overwrite previous output
            if last_lines > 0:
                ui.console.print(f"\033[{last_lines}A\033[J", end="")

            print_header()
            output = _format_status(st, ams_trays=trays)
            now = datetime.now(tz=timezone.utc).astimezone()
            timestamp = now.strftime("%H:%M:%S")
            mode = "daemon" if use_daemon else "poll"
            output += f"\n  [dim]Updated {timestamp} [{mode}]  (Ctrl-C to exit)[/dim]"
            ui.console.print(output)

            # Count lines for next overwrite (+1 for the header)
            last_lines = output.count("\n") + 2

            # Daemon cache refreshes every ~1s; poll mode is expensive (process per call)
            time.sleep(1 if use_daemon else interval)
    except KeyboardInterrupt:
        ui.console.print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    try:
        app(argv, standalone_mode=False)
    except click.UsageError as exc:
        ui.error(str(exc))
        sys.exit(2)

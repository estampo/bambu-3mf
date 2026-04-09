"""CLI entry point for bambox."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from bambox.cura import build_template_context, parse_bambox_headers, strip_bambox_header
from bambox.pack import FilamentInfo, SliceInfo, pack_gcode_3mf, repack_3mf
from bambox.settings import available_filaments, available_machines, build_project_settings


def _parse_filament_args(
    filament_args: list[str] | None,
) -> list[tuple[int | None, str, str]]:
    """Parse --filament specs into (slot, type, color) triples.

    Accepted formats::

        TYPE            → (None, TYPE, default_color)
        TYPE:COLOR      → (None, TYPE, COLOR)
        SLOT:TYPE       → (SLOT, TYPE, default_color)   — SLOT is an int
        SLOT:TYPE:COLOR → (SLOT, TYPE, COLOR)
    """
    default_color = "#F2754E"
    if not filament_args:
        return [(None, "PLA", default_color)]
    result: list[tuple[int | None, str, str]] = []
    for spec in filament_args:
        parts = spec.split(":")
        if len(parts) == 1:
            # TYPE
            result.append((None, parts[0].upper(), default_color))
        elif len(parts) == 2:
            # Could be SLOT:TYPE or TYPE:COLOR
            if parts[0].isdigit():
                result.append((int(parts[0]), parts[1].upper(), default_color))
            else:
                color = parts[1] if parts[1].startswith("#") else "#" + parts[1]
                result.append((None, parts[0].upper(), color))
        elif len(parts) == 3:
            # SLOT:TYPE:COLOR
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
    # Collect explicit slots
    explicit: dict[int, tuple[str, str]] = {}
    unslotted: list[tuple[str, str]] = []
    for slot, ftype, color in parsed:
        if slot is not None:
            explicit[slot] = (ftype, color)
        else:
            unslotted.append((ftype, color))

    # Fill unslotted into the first available positions starting from 0
    result: list[tuple[int, str, str]] = []
    next_slot = 0
    for ftype, color in unslotted:
        while next_slot in explicit:
            next_slot += 1
        result.append((next_slot, ftype, color))
        next_slot += 1

    # Add explicit slots
    for slot, (ftype, color) in explicit.items():
        result.append((slot, ftype, color))

    # Sort by slot number
    result.sort(key=lambda x: x[0])
    return result


def _cmd_pack(args: argparse.Namespace) -> None:
    """Pack G-code into a .gcode.3mf file."""
    if not args.gcode.exists():
        print(f"Error: {args.gcode} not found", file=sys.stderr)
        sys.exit(1)

    output = args.output or args.gcode.with_suffix(".gcode.3mf")
    gcode_bytes = args.gcode.read_bytes()
    gcode_str = gcode_bytes.decode(errors="replace")

    # Check for BAMBOX headers in the G-code
    headers = parse_bambox_headers(gcode_str)

    # Determine machine and filaments: headers override CLI flags
    if "PRINTER" in headers:
        machine = headers["PRINTER"]
    else:
        machine = args.machine

    if "FILAMENT_TYPE" in headers:
        # Headers provide filament types (comma-separated for multi-filament)
        header_types = headers["FILAMENT_TYPE"].split(",")
        header_slots = headers["FILAMENT_SLOT"].split(",") if "FILAMENT_SLOT" in headers else []
        parsed_filaments: list[tuple[int | None, str, str]] = []
        for i, t in enumerate(header_types):
            slot = int(header_slots[i]) if i < len(header_slots) else None
            parsed_filaments.append((slot, t.strip().upper(), "#F2754E"))
        assigned = _assign_filament_slots(parsed_filaments)
    else:
        assigned = _assign_filament_slots(_parse_filament_args(args.filament))

    filament_types = [f[1] for f in assigned]
    filament_colors = [f[2] for f in assigned]

    filament_infos = [
        FilamentInfo(
            slot=slot + 1,  # FilamentInfo uses 1-indexed slots
            filament_type=ftype,
            color=color,
        )
        for slot, ftype, color in assigned
    ]

    info = SliceInfo(
        printer_model_id=args.printer_model_id,
        nozzle_diameter=args.nozzle_diameter,
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
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # If BAMBOX_ASSEMBLE=true, render start/end templates and wrap toolpath
    if headers.get("ASSEMBLE") == "true":
        from bambox.assemble import assemble_gcode
        from bambox.gcode_compat import rewrite_tool_changes
        from bambox.templates import render_template

        toolpath = strip_bambox_header(gcode_str)

        # Rewrite T0/T1/... tool changes to M620/M621 sequences
        if len(filament_types) > 1:
            toolpath = rewrite_tool_changes(toolpath, project_settings, machine)

        ctx = build_template_context(headers, project_settings)
        start = render_template(f"{machine}_start.gcode.j2", ctx)
        end = render_template(f"{machine}_end.gcode.j2", ctx)
        gcode_bytes = assemble_gcode(start, toolpath, end).encode()
        if headers:
            print(f"Auto-configured from BAMBOX headers: {machine}, {filament_types}")

    pack_gcode_3mf(gcode_bytes, output, slice_info=info, project_settings=project_settings)
    print(f"Wrote {output} ({output.stat().st_size} bytes)")


def _cmd_repack(args: argparse.Namespace) -> None:
    """Fix up an existing .gcode.3mf for Bambu Connect."""
    if not args.threemf.exists():
        print(f"Error: {args.threemf} not found", file=sys.stderr)
        sys.exit(1)

    if args.filament:
        assigned_repack = _assign_filament_slots(_parse_filament_args(args.filament))
        filament_types = [f[1] for f in assigned_repack]
        filament_colors = [f[2] for f in assigned_repack]
    else:
        filament_types = None
        filament_colors = None
    machine = args.machine if filament_types else None

    try:
        repack_3mf(
            args.threemf,
            machine=machine,
            filaments=filament_types,
            filament_colors=filament_colors,
        )
    except (ValueError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Repacked {args.threemf} ({args.threemf.stat().st_size} bytes)")


def _cmd_print(args: argparse.Namespace) -> None:
    """Send a .gcode.3mf to a Bambu printer via cloud bridge."""
    from bambox.bridge import cloud_print, load_credentials

    threemf = args.threemf
    if not threemf.exists():
        print(f"Error: {threemf} not found", file=sys.stderr)
        sys.exit(1)

    creds_path = Path(args.credentials) if args.credentials else None
    try:
        credentials = load_credentials(creds_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    device_id = args.device
    if not device_id:
        # Try to get serial from credentials file
        cpath = creds_path or Path.home() / ".config" / "estampo" / "credentials.toml"
        if cpath.exists():
            import tomllib

            with open(cpath, "rb") as f:
                raw = tomllib.load(f)
            # Find the first bambu-cloud printer
            for name, p in raw.get("printers", {}).items():
                if p.get("serial"):
                    device_id = p["serial"]
                    print(f"Using printer '{name}' ({device_id})")
                    break

    if not device_id:
        print(
            "Error: --device is required (or set a printer serial in credentials.toml)",
            file=sys.stderr,
        )
        sys.exit(1)

    project_name = args.project or threemf.stem

    # Parse --ams-tray flags (e.g. "2:PETG-CF:2850E0")
    ams_trays: list[dict] = []
    for spec in args.ams_tray or []:
        parts = spec.split(":")
        if len(parts) != 3:
            print(f"Error: --ams-tray must be SLOT:TYPE:COLOR, got '{spec}'", file=sys.stderr)
            sys.exit(1)
        slot, ftype, color = parts
        phys_slot = int(slot)
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

    print(f"Sending {threemf.name} to {device_id}...")
    try:
        result = cloud_print(
            threemf,
            device_id,
            credentials=credentials,
            project_name=project_name,
            timeout=args.timeout,
            verbose=args.verbose,
            skip_ams_mapping=args.no_ams_mapping,
            ams_trays=ams_trays or None,
        )
        status = result.get("result", "unknown")
        if status in ("success", "sent"):
            print(f"Print sent successfully! ({status})")
        else:
            print(f"Bridge response: {json.dumps(result, indent=2)}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_status(args: argparse.Namespace) -> None:
    """Query printer status."""
    from bambox.bridge import _write_token_json, load_credentials, parse_ams_trays, query_status

    creds_path = Path(args.credentials) if args.credentials else None
    credentials = load_credentials(creds_path)
    token_file = _write_token_json(credentials)
    try:
        status = query_status(args.device, token_file, verbose=args.verbose)
        # Show key info
        state = status.get("gcode_state", "?")
        nozzle = status.get("nozzle_temper", "?")
        bed = status.get("bed_temper", "?")
        print(f"State: {state}")
        print(f"Nozzle: {nozzle}°C  Bed: {bed}°C")
        if status.get("mc_percent"):
            print(
                f"Progress: {status['mc_percent']}%  ETA: {status.get('mc_remaining_time', '?')}min"
            )
        if status.get("subtask_name"):
            print(f"Job: {status['subtask_name']}")

        trays = parse_ams_trays(status)
        if trays:
            print("AMS trays:")
            for t in trays:
                print(f"  Slot {t['phys_slot']}: {t['type']} #{t['color']} ({t['tray_info_idx']})")
    finally:
        try:
            token_file.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bambox",
        description="Package and print G-code on Bambu Lab printers",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command")

    # --- pack subcommand ---
    pack_p = sub.add_parser("pack", help="Package G-code into .gcode.3mf")
    pack_p.add_argument("gcode", type=Path, help="Input G-code file")
    pack_p.add_argument("-o", "--output", type=Path, help="Output .gcode.3mf path")
    pack_p.add_argument(
        "-m",
        "--machine",
        default="p1s",
        help=f"Machine profile ({', '.join(available_machines())})",
    )
    pack_p.add_argument(
        "-f",
        "--filament",
        action="append",
        metavar="[SLOT:]TYPE[:COLOR]",
        help=f"Filament type, optionally with AMS slot and color (e.g. 'PLA', '3:PETG-CF', "
        f"'PLA:#FF0000', '2:PETG-CF:#2850E0'). "
        f"Repeatable for multi-filament. Available: {', '.join(available_filaments())}",
    )
    pack_p.add_argument("--printer-model-id", default="")
    pack_p.add_argument("--nozzle-diameter", type=float, default=0.4)

    # --- repack subcommand ---
    repack_p = sub.add_parser("repack", help="Fix up existing .gcode.3mf for Bambu Connect")
    repack_p.add_argument("threemf", type=Path, help="Input .gcode.3mf file (modified in-place)")
    repack_p.add_argument(
        "-m",
        "--machine",
        default="p1s",
        help=f"Machine profile for settings regeneration ({', '.join(available_machines())})",
    )
    repack_p.add_argument(
        "-f",
        "--filament",
        action="append",
        metavar="TYPE[:COLOR]",
        help="Filament type to regenerate settings (omit to patch existing settings only)",
    )

    # --- print subcommand ---
    print_p = sub.add_parser("print", help="Send .gcode.3mf to printer via cloud bridge")
    print_p.add_argument("threemf", type=Path, help="Input .gcode.3mf file")
    print_p.add_argument("-d", "--device", default="", help="Printer serial number")
    print_p.add_argument(
        "-c",
        "--credentials",
        default=None,
        help="Path to credentials.toml (default: ~/.config/estampo/credentials.toml)",
    )
    print_p.add_argument("--project", default=None, help="Project name shown in cloud")
    print_p.add_argument("--timeout", type=int, default=180)
    print_p.add_argument("--no-ams-mapping", action="store_true", help="Skip AMS mapping")
    print_p.add_argument(
        "--ams-tray",
        action="append",
        metavar="SLOT:TYPE:COLOR",
        help="Manually specify AMS tray (e.g. '2:PETG-CF:2850E0'). Repeatable.",
    )

    # --- status subcommand ---
    status_p = sub.add_parser("status", help="Query printer status")
    status_p.add_argument("device", help="Printer serial number")
    status_p.add_argument(
        "-c",
        "--credentials",
        default=None,
        help="Path to credentials.toml",
    )

    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")

    if args.command == "pack":
        _cmd_pack(args)
    elif args.command == "repack":
        _cmd_repack(args)
    elif args.command == "print":
        _cmd_print(args)
    elif args.command == "status":
        _cmd_status(args)
    else:
        # Backward compat: if no subcommand, treat first positional as gcode file for pack
        # Check if there's an unrecognized arg that looks like a file
        if argv is None:
            argv = sys.argv[1:]
        if argv and not argv[0].startswith("-") and Path(argv[0]).suffix in (".gcode", ".g"):
            # Legacy mode: bambox file.gcode
            ns = argparse.Namespace(
                gcode=Path(argv[0]),
                output=None,
                machine="p1s",
                filament=None,
                printer_model_id="",
                nozzle_diameter=0.4,
                verbose=False,
            )
            _cmd_pack(ns)
        else:
            parser.print_help()
            sys.exit(1)

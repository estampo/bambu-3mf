"""CLI entry point for bambu-3mf."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from bambu_3mf.pack import FilamentInfo, SliceInfo, pack_gcode_3mf


def _cmd_pack(args: argparse.Namespace) -> None:
    """Pack G-code into a .gcode.3mf file."""
    if not args.gcode.exists():
        print(f"Error: {args.gcode} not found", file=sys.stderr)
        sys.exit(1)

    output = args.output or args.gcode.with_suffix(".gcode.3mf")
    gcode = args.gcode.read_bytes()

    info = SliceInfo(
        printer_model_id=args.printer_model_id,
        nozzle_diameter=args.nozzle_diameter,
        filaments=[
            FilamentInfo(
                slot=1,
                filament_type=args.filament_type,
                color=args.filament_color,
            )
        ],
    )

    pack_gcode_3mf(gcode, output, slice_info=info)
    print(f"Wrote {output} ({output.stat().st_size} bytes)")


def _cmd_print(args: argparse.Namespace) -> None:
    """Send a .gcode.3mf to a Bambu printer via cloud bridge."""
    from bambu_3mf.bridge import cloud_print, load_credentials

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
        print("Error: --device is required (or set a printer serial in credentials.toml)", file=sys.stderr)
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
        ams_trays.append({
            "phys_slot": phys_slot,
            "ams_id": phys_slot // 4,
            "slot_id": phys_slot % 4,
            "type": ftype,
            "color": color.upper(),
            "tray_info_idx": "",
        })

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
    from bambu_3mf.bridge import load_credentials, query_status, _write_token_json, parse_ams_trays

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
            print(f"Progress: {status['mc_percent']}%  ETA: {status.get('mc_remaining_time', '?')}min")
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
        prog="bambu-3mf",
        description="Package and print G-code on Bambu Lab printers",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    sub = parser.add_subparsers(dest="command")

    # --- pack subcommand ---
    pack_p = sub.add_parser("pack", help="Package G-code into .gcode.3mf")
    pack_p.add_argument("gcode", type=Path, help="Input G-code file")
    pack_p.add_argument("-o", "--output", type=Path, help="Output .gcode.3mf path")
    pack_p.add_argument("--printer-model-id", default="")
    pack_p.add_argument("--nozzle-diameter", type=float, default=0.4)
    pack_p.add_argument("--filament-type", default="PLA")
    pack_p.add_argument("--filament-color", default="#F2754E")

    # --- print subcommand ---
    print_p = sub.add_parser("print", help="Send .gcode.3mf to printer via cloud bridge")
    print_p.add_argument("threemf", type=Path, help="Input .gcode.3mf file")
    print_p.add_argument("-d", "--device", default="", help="Printer serial number")
    print_p.add_argument(
        "-c", "--credentials", default=None,
        help="Path to credentials.toml (default: ~/.config/estampo/credentials.toml)",
    )
    print_p.add_argument("--project", default=None, help="Project name shown in cloud")
    print_p.add_argument("--timeout", type=int, default=180)
    print_p.add_argument("--no-ams-mapping", action="store_true", help="Skip AMS mapping")
    print_p.add_argument(
        "--ams-tray", action="append", metavar="SLOT:TYPE:COLOR",
        help="Manually specify AMS tray (e.g. '2:PETG-CF:2850E0'). Repeatable.",
    )

    # --- status subcommand ---
    status_p = sub.add_parser("status", help="Query printer status")
    status_p.add_argument("device", help="Printer serial number")
    status_p.add_argument(
        "-c", "--credentials", default=None,
        help="Path to credentials.toml",
    )

    args = parser.parse_args(argv)

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s %(message)s")

    if args.command == "pack":
        _cmd_pack(args)
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
            # Legacy mode: bambu-3mf file.gcode
            ns = argparse.Namespace(
                gcode=Path(argv[0]),
                output=None,
                printer_model_id="",
                nozzle_diameter=0.4,
                filament_type="PLA",
                filament_color="#F2754E",
                verbose=False,
            )
            _cmd_pack(ns)
        else:
            parser.print_help()
            sys.exit(1)

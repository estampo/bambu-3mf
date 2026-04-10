"""CLI entry point for bambox."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from importlib.metadata import version
from pathlib import Path

from bambox.cura import (
    PRINTER_MODEL_IDS,
    build_template_context,
    extract_slice_stats,
    parse_bambox_headers,
    strip_bambox_header,
)
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
            if slot in explicit:
                raise ValueError(
                    f"Duplicate filament slot {slot}: "
                    f"'{explicit[slot][0]}' and '{ftype}' both assigned to slot {slot}"
                )
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

    if "FILAMENT_SLOT" in headers:
        # Per-extruder headers from CuraEngine. FILAMENT_TYPE may be empty
        # when CuraEngine CLI fails to substitute {material_type}.
        header_slots = headers["FILAMENT_SLOT"].split(",")
        header_types = headers["FILAMENT_TYPE"].split(",") if "FILAMENT_TYPE" in headers else []
        parsed_filaments: list[tuple[int | None, str, str]] = []
        for i, slot_str in enumerate(header_slots):
            ftype = header_types[i].strip().upper() if i < len(header_types) else ""
            if not ftype:
                ftype = "PLA"  # default when CuraEngine doesn't substitute
            parsed_filaments.append((int(slot_str), ftype, "#F2754E"))
        assigned = _assign_filament_slots(parsed_filaments)
    elif "FILAMENT_TYPE" in headers:
        header_types = headers["FILAMENT_TYPE"].split(",")
        parsed_filaments = []
        for t in header_types:
            parsed_filaments.append((None, t.strip().upper() or "PLA", "#F2754E"))
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

    # Look up tray_info_idx from filament profiles so slice_info.config
    # carries the correct Bambu filament IDs for AMS slot matching.
    from bambox.settings import _filament_profile_path, _load_json

    for fi in filament_infos:
        try:
            profile = _load_json(_filament_profile_path(fi.filament_type))
            if "filament_ids" in profile:
                fi.tray_info_idx = profile["filament_ids"]
        except ValueError:
            pass  # unknown filament type — keep default

    # Auto-derive printer_model_id from BAMBOX_PRINTER header if not set via CLI
    printer_model_id = args.printer_model_id
    if not printer_model_id and "PRINTER" in headers:
        printer_model_id = PRINTER_MODEL_IDS.get(headers["PRINTER"].lower(), "")

    # Extract print time and filament weight from slicer G-code comments
    stats = extract_slice_stats(gcode_str)

    # Populate per-filament usage from stats
    if stats.filament_used_m:
        from bambox.cura import _PLA_DENSITY_G_PER_MM3
        from bambox.gcode_compat import _FILAMENT_AREA

        for fi in filament_infos:
            slot_idx = fi.slot - 1  # FilamentInfo is 1-indexed
            if slot_idx < len(stats.filament_used_m):
                fi.used_m = stats.filament_used_m[slot_idx]
                length_mm = stats.filament_used_m[slot_idx] * 1000
                fi.used_g = round(length_mm * _FILAMENT_AREA * _PLA_DENSITY_G_PER_MM3, 2)

    info = SliceInfo(
        printer_model_id=printer_model_id,
        nozzle_diameter=args.nozzle_diameter,
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

        # Derive first-layer bounding box for adaptive bed leveling
        from bambox.cura import first_layer_bbox

        bbox = first_layer_bbox(toolpath)
        if bbox:
            ctx["first_layer_print_min"] = bbox[0]
            ctx["first_layer_print_size"] = bbox[1]

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


def _cmd_validate(args: argparse.Namespace) -> None:
    """Validate a .gcode.3mf archive."""
    from bambox.validate import Severity, compare_3mf, validate_3mf

    if not args.threemf.exists():
        print(f"Error: {args.threemf} not found", file=sys.stderr)
        sys.exit(1)

    result = validate_3mf(args.threemf)

    # Run reference comparison if requested
    ref_result = None
    if args.reference:
        if not args.reference.exists():
            print(f"Error: reference {args.reference} not found", file=sys.stderr)
            sys.exit(1)
        ref_result = compare_3mf(args.threemf, args.reference)

    if args.json_output:
        output = result.to_dict()
        if ref_result is not None:
            output["comparison"] = ref_result.to_dict()
        print(json.dumps(output, indent=2))
    else:
        name = args.threemf.name
        for f in result.findings:
            if f.severity == Severity.ERROR:
                prefix = f"  ERROR {f.code}"
            else:
                prefix = f"  WARN  {f.code}"
            line = f"{prefix}: {f.message}"
            if f.detail:
                line += f" [{f.detail}]"
            print(line)

        if ref_result is not None:
            for f in ref_result.findings:
                if f.severity == Severity.ERROR:
                    prefix = f"  COMP  {f.code}"
                else:
                    prefix = f"  COMP  {f.code}"
                line = f"{prefix}: {f.message}"
                if f.detail:
                    line += f" [{f.detail}]"
                print(line)

        n_err = len(result.errors)
        n_warn = len(result.warnings)
        n_comp = len(ref_result.errors) if ref_result else 0
        total_err = n_err + n_comp

        if total_err == 0 and n_warn == 0:
            print(f"{name}: valid")
        elif total_err == 0:
            print(f"{name}: valid ({n_warn} warning{'s' if n_warn != 1 else ''})")
        else:
            print(
                f"{name}: INVALID ({total_err} error{'s' if total_err != 1 else ''}, "
                f"{n_warn} warning{'s' if n_warn != 1 else ''})"
            )

    has_errors = not result.valid or (ref_result is not None and not ref_result.valid)
    if has_errors:
        sys.exit(1)
    if args.strict and result.warnings:
        sys.exit(1)


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
        device_id, _ = _resolve_printer(getattr(args, "printer", None), creds_path)

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

    # Show print summary from 3MF metadata
    _show_print_info(threemf)

    if args.dry_run:
        # Dry run: query AMS and show mapping, but don't send
        if not args.no_ams_mapping:
            from bambox.bridge import (
                _build_ams_mapping,
                _write_token_json,
                parse_ams_trays,
                query_status,
            )

            token_file = _write_token_json(credentials, directory=threemf.parent)
            try:
                if not ams_trays:
                    try:
                        live_status = query_status(device_id, token_file, verbose=args.verbose)
                        ams_trays = parse_ams_trays(live_status)
                    except Exception as e:
                        print(f"Warning: could not query AMS state: {e}", file=sys.stderr)
                if ams_trays:
                    try:
                        ams_data = _build_ams_mapping(threemf, ams_trays)
                        mapping = ams_data["amsMapping"]
                        _show_ams_mapping(threemf, ams_trays, mapping)
                    except RuntimeError as e:
                        print(f"AMS mapping error: {e}", file=sys.stderr)
            finally:
                try:
                    token_file.unlink()
                except OSError:
                    pass
        print("Dry run — not sending to printer.")
        return

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

        # Show AMS mapping if the bridge computed one
        ams_mapping = result.get("_ams_mapping")
        ams_trays_used = result.get("_ams_trays")
        if ams_mapping and ams_trays_used:
            _show_ams_mapping(threemf, ams_trays_used, ams_mapping)

        status = result.get("result", "unknown")
        if status in ("success", "sent"):
            print(f"Print sent successfully! ({status})")
        else:
            print(f"Bridge response: {json.dumps(result, indent=2)}")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def _show_print_info(threemf: Path) -> None:
    """Display print metadata (time, weight, layers, filaments) from a 3MF."""
    import xml.etree.ElementTree as ET
    import zipfile

    from bambox.bridge import _xml_ns

    try:
        with zipfile.ZipFile(threemf, "r") as z:
            # Extract slice_info for filaments and prediction
            prediction = 0
            weight = 0.0
            filaments: list[tuple[str, str, str, str]] = []  # (type, color, used_m, used_g)

            if "Metadata/slice_info.config" in z.namelist():
                root = ET.fromstring(z.read("Metadata/slice_info.config"))
                ns = _xml_ns(root)
                plate_el = root.find(f"{ns}plate")
                if plate_el is not None:
                    # Metadata is stored as <metadata key="X" value="Y"/> children
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

            # Extract layer count from G-code header
            layers = 0
            gcode_name = None
            for name in z.namelist():
                if name.startswith("Metadata/plate_") and name.endswith(".gcode"):
                    gcode_name = name
                    break
            if gcode_name:
                # Read just the header (first 4KB)
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
        print(f"Warning: could not read 3MF metadata: {e}", file=sys.stderr)
        return

    # Format time
    if prediction > 0:
        hrs, remainder = divmod(prediction, 3600)
        mins = remainder // 60
        time_str = f"{hrs}h{mins}m" if hrs else f"{mins}m"
    else:
        time_str = "unknown"

    print(f"\nPrint: {threemf.name}")
    if layers:
        print(f"  Layers:    {layers}")
    print(f"  Time:      {time_str}")
    print(f"  Weight:    {weight:.1f}g")
    if filaments:
        print("  Filaments:")
        for ftype, color, used_m, used_g in filaments:
            print(f"    - {ftype} {color}  ({used_m}m / {used_g}g)")
    print()


def _show_ams_mapping(threemf: Path, ams_trays: list[dict], mapping: list[int]) -> None:
    """Display the AMS filament mapping that will be used for the print."""
    import xml.etree.ElementTree as ET
    import zipfile

    from bambox.bridge import _xml_ns

    if not any(v >= 0 for v in mapping):
        return

    # Read filament info from slice_info for display
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
    except Exception:
        pass

    tray_by_phys = {t["phys_slot"]: t for t in ams_trays}

    print("AMS filament mapping:")
    for idx, phys_slot in enumerate(mapping):
        filament_id = idx + 1
        if phys_slot < 0:
            continue
        fil_type, fil_color = filaments.get(filament_id, ("?", "?"))
        tray = tray_by_phys.get(phys_slot, {})
        tray_type = tray.get("type", "?")
        tray_color = tray.get("color", "?")
        print(
            f"  Slot {filament_id} ({fil_type} {fil_color}) "
            f"-> AMS tray {phys_slot} ({tray_type} #{tray_color})"
        )
    print()


def _cmd_status(args: argparse.Namespace) -> None:
    """Query printer status."""
    from bambox.bridge import _write_token_json, load_credentials, parse_ams_trays, query_status

    creds_path = Path(args.credentials) if args.credentials else None

    # Resolve device serial: explicit flag, named printer, or first cloud printer
    device_id = args.device
    if not device_id:
        device_id, _ = _resolve_printer(args.printer, creds_path)

    credentials = load_credentials(creds_path)
    token_file = _write_token_json(credentials)
    try:
        status = query_status(device_id, token_file, verbose=args.verbose)
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


def _resolve_printer(printer_name: str | None, creds_path: Path | None) -> tuple[str, str]:
    """Resolve a printer name to (serial, display_name).

    Tries: named printer from credentials, then first cloud printer found.
    """
    import tomllib

    # Determine which credentials file to read
    if creds_path:
        path = creds_path
    else:
        from bambox.credentials import _credentials_path

        path = _credentials_path()

    if printer_name:
        if not path.exists():
            print(f"Error: credentials file not found: {path}", file=sys.stderr)
            sys.exit(1)
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        printers = raw.get("printers", {})
        if printer_name not in printers:
            print(f"Error: printer '{printer_name}' not found", file=sys.stderr)
            sys.exit(1)
        serial = printers[printer_name].get("serial", "")
        if not serial:
            print(f"Error: printer '{printer_name}' has no serial number", file=sys.stderr)
            sys.exit(1)
        return serial, printer_name

    # No name given — try to find the first cloud printer
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        for name, p in raw.get("printers", {}).items():
            if p.get("serial"):
                print(f"Using printer '{name}' ({p['serial']})")
                return p["serial"], name

    print(
        "Error: no printer configured. Run 'bambox login' or use --device.",
        file=sys.stderr,
    )
    sys.exit(1)


def _cmd_login(args: argparse.Namespace) -> None:
    """Log in to Bambu Cloud and configure printers."""
    import getpass
    import os

    from bambox.auth import _get_user_profile, _login
    from bambox.credentials import load_cloud_credentials, save_cloud_credentials

    # Check for existing valid token
    cloud = load_cloud_credentials()
    if cloud and cloud.get("token"):
        try:
            profile = _get_user_profile(cloud["token"])
            print(f"Already logged in as {profile.get('name') or profile['uid']}")
            answer = input("  Re-login? [y/N] ").strip().lower()
            if answer != "y":
                # Still offer to configure printers
                _name_printers(cloud["token"])
                return
        except (OSError, KeyError):
            print("  Cached token is invalid or expired.")

    # Get credentials from env vars or prompt
    email = os.environ.get("BAMBU_EMAIL") or input("  Email: ").strip()
    password = os.environ.get("BAMBU_PASSWORD") or getpass.getpass("  Password: ")
    if not email or not password:
        print("Error: email and password required", file=sys.stderr)
        sys.exit(1)

    token, refresh_token = _login(email, password)
    profile = _get_user_profile(token)

    save_cloud_credentials(
        token=token,
        refresh_token=refresh_token,
        email=email,
        uid=profile["uid"],
    )

    print(f"Login successful! User: {profile.get('name') or profile['uid']}")

    # Name printers
    _name_printers(token)


def _name_printers(token: str) -> None:
    """List bound printers and let user name up to 5."""
    from bambox.auth import _get_devices
    from bambox.credentials import mask_serial, save_printer

    try:
        devices = _get_devices(token)
    except (OSError, KeyError):
        print("  Could not fetch printer list.")
        return

    if not devices:
        print("  No printers found on this account.")
        return

    # Show available printers
    print(f"\n  Found {len(devices)} printer(s):")
    for i, d in enumerate(devices, 1):
        name = d.get("name", "unnamed")
        model = d.get("dev_product_name", d.get("dev_model_name", "?"))
        serial = d.get("dev_id", "?")
        online = "online" if d.get("online") else "offline"
        print(f"  {i}. {name} ({model}) — {mask_serial(serial)} [{online}]")

    # Let user name up to 5 printers
    limit = min(len(devices), 5)
    print(f"\n  Name your printer(s) (up to {limit}). Press Enter to skip.")
    for i in range(limit):
        d = devices[i]
        dev_name = d.get("name", f"printer-{i + 1}")
        serial = d.get("dev_id", "")
        default = dev_name.lower().replace(" ", "-")
        raw = input(f"  Name for #{i + 1} [{default}] (enter '-' to skip): ").strip()
        if raw == "-":
            continue
        name = raw or default
        save_printer(name, {"type": "bambu-cloud", "serial": serial})
        print(f"    Saved '{name}' ({mask_serial(serial)})")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bambox",
        description="Package and print G-code on Bambu Lab printers",
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {version('bambox')}"
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

    # --- login subcommand ---
    sub.add_parser("login", help="Log in to Bambu Cloud and configure printers")

    # --- print subcommand ---
    print_p = sub.add_parser("print", help="Send .gcode.3mf to printer via cloud bridge")
    print_p.add_argument("threemf", type=Path, help="Input .gcode.3mf file")
    print_p.add_argument("-d", "--device", default="", help="Printer serial number")
    print_p.add_argument(
        "-p", "--printer", default=None, help="Named printer from credentials.toml"
    )
    print_p.add_argument(
        "-c",
        "--credentials",
        default=None,
        help="Path to credentials.toml",
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
    print_p.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show print info and AMS mapping without sending",
    )

    # --- validate subcommand ---
    validate_p = sub.add_parser("validate", help="Validate a .gcode.3mf archive")
    validate_p.add_argument("threemf", type=Path, help="Input .gcode.3mf file")
    validate_p.add_argument(
        "--json", dest="json_output", action="store_true", help="Output results as JSON"
    )
    validate_p.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors (non-zero exit)",
    )
    validate_p.add_argument(
        "--reference",
        type=Path,
        default=None,
        help="Reference .gcode.3mf to compare against",
    )

    # --- status subcommand ---
    status_p = sub.add_parser("status", help="Query printer status")
    status_p.add_argument("device", nargs="?", default="", help="Printer serial number")
    status_p.add_argument(
        "-p", "--printer", default=None, help="Named printer from credentials.toml"
    )
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
    elif args.command == "validate":
        _cmd_validate(args)
    elif args.command == "login":
        _cmd_login(args)
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

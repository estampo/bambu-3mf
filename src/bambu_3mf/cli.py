"""CLI entry point for bambu-3mf."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bambu_3mf.pack import FilamentInfo, SliceInfo, pack_gcode_3mf


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="bambu-3mf",
        description="Package plain G-code into a Bambu Lab .gcode.3mf file",
    )
    parser.add_argument("gcode", type=Path, help="Input G-code file")
    parser.add_argument("-o", "--output", type=Path, help="Output .gcode.3mf path")
    parser.add_argument("--printer-model-id", default="", help="BBL printer model ID")
    parser.add_argument("--nozzle-diameter", type=float, default=0.4)
    parser.add_argument("--filament-type", default="PLA")
    parser.add_argument("--filament-color", default="#F2754E")

    args = parser.parse_args(argv)

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

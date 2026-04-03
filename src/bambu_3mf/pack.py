"""Core logic for packaging G-code into Bambu Lab .gcode.3mf files."""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

# ---------------------------------------------------------------------------
# Static boilerplate (identical for every .gcode.3mf)
# ---------------------------------------------------------------------------

CONTENT_TYPES_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
 <Default Extension="gcode" ContentType="text/x.gcode"/>
</Types>"""

RELS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
 <Relationship Target="/Metadata/plate_1.png" Id="rel-2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"/>
 <Relationship Target="/Metadata/plate_1.png" Id="rel-4" Type="http://schemas.bambulab.com/package/2021/cover-thumbnail-middle"/>
<Relationship Target="/Metadata/plate_1_small.png" Id="rel-5" Type="http://schemas.bambulab.com/package/2021/cover-thumbnail-small"/>
</Relationships>"""

MODEL_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">
 <metadata name="Application">{application}</metadata>
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <metadata name="Copyright"></metadata>
 <metadata name="CreationDate"></metadata>
 <metadata name="Description"></metadata>
 <metadata name="Designer"></metadata>
 <metadata name="DesignerCover"></metadata>
 <metadata name="DesignerUserId"></metadata>
 <metadata name="License"></metadata>
 <metadata name="ModificationDate"></metadata>
 <metadata name="Origin"></metadata>
 <metadata name="Title"></metadata>
 <resources>
 </resources>
 <build/>
</model>
"""

MODEL_SETTINGS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="plater_name" value=""/>
    <metadata key="locked" value="false"/>
    <metadata key="filament_map_mode" value="Auto For Flush"/>
    <metadata key="filament_maps" value="{filament_maps}"/>
    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>
  </plate>
</config>
"""

MODEL_SETTINGS_RELS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/Metadata/plate_1.gcode" Id="rel-1" Type="http://schemas.bambulab.com/package/2021/gcode"/>
</Relationships>"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FilamentInfo:
    """Metadata for a single filament used in the print."""

    slot: int = 1  # 1-indexed filament slot
    tray_info_idx: str = "GFL99"  # Bambu filament ID (e.g. GFL99 = generic PLA)
    filament_type: str = "PLA"
    color: str = "#F2754E"
    used_m: float = 0.0  # metres of filament used
    used_g: float = 0.0  # grams of filament used


@dataclass
class SliceInfo:
    """Metadata about the sliced print."""

    printer_model_id: str = ""  # e.g. "BL-P001" for X1C, "C12" for A1
    nozzle_diameter: float = 0.4
    prediction: int = 0  # estimated print time in seconds
    weight: float = 0.0  # total filament weight in grams
    outside: bool = False  # objects extend beyond bed
    support_used: bool = False
    label_object_enabled: bool = True
    timelapse_type: int = 0  # 0=traditional, 1=smooth, -1=disabled
    filaments: list[FilamentInfo] = field(default_factory=list)
    application: str = "BambuStudio-2.3.1"


def _slice_info_xml(info: SliceInfo) -> str:
    """Generate Metadata/slice_info.config XML."""
    filament_maps = " ".join(str(f.slot) for f in info.filaments) if info.filaments else "1"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<config>",
        "  <header>",
        '    <header_item key="X-BBL-Client-Type" value="slicer"/>',
        '    <header_item key="X-BBL-Client-Version" value=""/>',
        "  </header>",
        "  <plate>",
        f'    <metadata key="index" value="1"/>',
        f'    <metadata key="printer_model_id" value="{info.printer_model_id}"/>',
        f'    <metadata key="nozzle_diameters" value="{info.nozzle_diameter}"/>',
        f'    <metadata key="timelapse_type" value="{info.timelapse_type}"/>',
        f'    <metadata key="prediction" value="{info.prediction}"/>',
        f'    <metadata key="weight" value="{info.weight:.2f}"/>',
        f'    <metadata key="outside" value="{str(info.outside).lower()}"/>',
        f'    <metadata key="support_used" value="{str(info.support_used).lower()}"/>',
        f'    <metadata key="label_object_enabled" value="{str(info.label_object_enabled).lower()}"/>',
        f'    <metadata key="filament_maps" value="{filament_maps}"/>',
    ]

    for f in info.filaments:
        lines.append(
            f'    <filament id="{f.slot}" tray_info_idx="{f.tray_info_idx}"'
            f' type="{f.filament_type}" color="{f.color}"'
            f' used_m="{f.used_m:.2f}" used_g="{f.used_g:.2f}" />'
        )

    lines.extend([
        "  </plate>",
        "</config>",
    ])
    return "\n".join(lines)


def _plate_json(info: SliceInfo, filaments: list[FilamentInfo]) -> str:
    """Generate Metadata/plate_1.json."""
    data = {
        "filament_colors": [f.color for f in filaments] if filaments else ["#F2754E"],
        "filament_ids": [f.slot - 1 for f in filaments] if filaments else [0],
        "first_extruder": 0,
        "is_seq_print": False,
        "nozzle_diameter": info.nozzle_diameter,
        "version": 2,
    }
    return json.dumps(data)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def pack_gcode_3mf(
    gcode: bytes,
    output: Path | BytesIO,
    slice_info: SliceInfo | None = None,
    project_settings: dict[str, object] | None = None,
) -> None:
    """Package G-code bytes into a Bambu Lab .gcode.3mf archive.

    Args:
        gcode: Raw G-code bytes.
        output: Output file path or BytesIO buffer.
        slice_info: Print metadata (time, weight, filaments). Defaults are
            provided if omitted.
        project_settings: Full slicer settings dict for
            project_settings.config. Optional — omitted if not provided.
    """
    if slice_info is None:
        slice_info = SliceInfo()

    # MD5 of raw gcode (uppercase hex, as firmware expects)
    md5 = hashlib.md5(gcode).hexdigest().upper()

    filament_maps = (
        " ".join(str(f.slot) for f in slice_info.filaments)
        if slice_info.filaments
        else "1"
    )

    # Build the ZIP archive
    if isinstance(output, (str, Path)):
        fh = open(output, "wb")
        should_close = True
    else:
        fh = output
        should_close = False

    try:
        with zipfile.ZipFile(fh, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
            z.writestr("_rels/.rels", RELS_XML)
            z.writestr(
                "3D/3dmodel.model",
                MODEL_XML.format(application=slice_info.application),
            )
            z.writestr(
                "Metadata/model_settings.config",
                MODEL_SETTINGS_XML.format(filament_maps=filament_maps),
            )
            z.writestr(
                "Metadata/_rels/model_settings.config.rels",
                MODEL_SETTINGS_RELS_XML,
            )
            z.writestr("Metadata/plate_1.gcode", gcode)
            z.writestr("Metadata/plate_1.gcode.md5", md5)
            z.writestr("Metadata/slice_info.config", _slice_info_xml(slice_info))
            z.writestr("Metadata/plate_1.json", _plate_json(slice_info, slice_info.filaments))

            if project_settings is not None:
                z.writestr(
                    "Metadata/project_settings.config",
                    json.dumps(project_settings, indent=4),
                )
    finally:
        if should_close:
            fh.close()

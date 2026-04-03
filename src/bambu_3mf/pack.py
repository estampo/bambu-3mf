"""Core logic for packaging G-code into Bambu Lab .gcode.3mf files.

Produces archives accepted by Bambu Connect. The format requirements are
documented in docs/gcode-3mf-format.md in the estampo repo.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

# Minimum AMS slot count for a P1S (4 AMS + 1 external spool).
# All per-filament arrays in project_settings must be padded to this length.
MIN_SLOTS = 5

# Keys absent from OrcaSlicer CLI --min-save output but required by Bambu Connect.
_BC_REQUIRED_KEYS: dict[str, object] = {
    "bbl_use_printhost": "1",
    "default_bed_type": "",
    "filament_retract_lift_above": ["0"],
    "filament_retract_lift_below": ["0"],
    "filament_retract_lift_enforce": [""],
    "host_type": "octoprint",
    "pellet_flow_coefficient": "0",
    "pellet_modded_printer": "0",
    "printhost_authorization_type": "key",
    "printhost_ssl_ignore_revoke": "0",
    "thumbnails_format": "BTT_TFT",
}

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

def _model_settings_xml(filament_maps: str, filament_volume_maps: str = "") -> str:
    """Generate Metadata/model_settings.config."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<config>",
        "  <plate>",
        '    <metadata key="plater_id" value="1"/>',
        '    <metadata key="plater_name" value=""/>',
        '    <metadata key="locked" value="false"/>',
        '    <metadata key="filament_map_mode" value="Auto For Flush"/>',
        f'    <metadata key="filament_maps" value="{filament_maps}"/>',
    ]
    if filament_volume_maps:
        lines.append(f'    <metadata key="filament_volume_maps" value="{filament_volume_maps}"/>')
    lines.extend([
        '    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>',
        '    <metadata key="thumbnail_file" value="Metadata/plate_1.png"/>',
        '    <metadata key="thumbnail_no_light_file" value="Metadata/plate_no_light_1.png"/>',
        '    <metadata key="top_file" value="Metadata/top_1.png"/>',
        '    <metadata key="pick_file" value="Metadata/pick_1.png"/>',
        '    <metadata key="pattern_bbox_file" value="Metadata/plate_1.json"/>',
        "  </plate>",
        "</config>",
        "",
    ])
    return "\n".join(lines)

MODEL_SETTINGS_RELS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/Metadata/plate_1.gcode" Id="rel-1" Type="http://schemas.bambulab.com/package/2021/gcode"/>
</Relationships>"""

# 1x1 transparent PNG (67 bytes) — minimal valid placeholder thumbnail.
_PLACEHOLDER_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


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
    # BBS 02.05+ extra attributes (omitted from XML if empty)
    extra_attrs: dict[str, str] | None = None  # e.g. {"used_for_object": "true", ...}


@dataclass
class SliceInfo:
    """Metadata about the sliced print."""

    printer_model_id: str = ""  # e.g. "BL-P001" for X1C, "C12" for P1S
    nozzle_diameter: float = 0.4
    prediction: int = 0  # estimated print time in seconds
    weight: float = 0.0  # total filament weight in grams
    outside: bool = False  # objects extend beyond bed
    support_used: bool = False
    label_object_enabled: bool = True
    timelapse_type: int = 0  # 0=traditional, 1=smooth, -1=disabled
    filaments: list[FilamentInfo] = field(default_factory=list)
    objects: list[ObjectInfo] = field(default_factory=list)
    warnings: list[WarningInfo] = field(default_factory=list)
    plate_data: dict[str, object] | None = None  # raw plate_1.json passthrough
    application: str = "BambuStudio-2.3.1"
    model_xml: str = ""  # raw 3D/3dmodel.model passthrough (overrides application)
    # BBS 02.05+ fields (optional, omitted if empty/zero)
    client_version: str = ""  # e.g. "02.05.00.66"
    extruder_type: int | None = None
    nozzle_volume_type: int | None = None
    first_layer_time: float | None = None
    filament_maps: str = ""  # override filament_maps in slice_info (e.g. "1 1 1 1 1")
    limit_filament_maps: str = ""  # e.g. "0 0 0 0 0"
    layer_filament_lists: list[dict[str, str]] | None = None  # [{filament_list, layer_ranges}]
    filament_volume_maps: str = ""  # e.g. "0 0 0 0 0" (BBS 02.05+)


def _filament_maps_str(min_slots: int = MIN_SLOTS) -> str:
    """Build padded filament_maps string for model_settings (e.g. '1 1 1 1 1').

    This is the plate mapping index (always 1 for single-plate prints), NOT
    the filament slot number.
    """
    return " ".join(["1"] * min_slots)


@dataclass
class ObjectInfo:
    """An object entry for slice_info.config."""

    identify_id: int
    name: str = ""
    skipped: bool = False


@dataclass
class WarningInfo:
    """A warning entry for slice_info.config."""

    msg: str
    level: int = 1
    error_code: str = ""


def _slice_info_xml(info: SliceInfo) -> str:
    """Generate Metadata/slice_info.config XML."""
    filament_maps = info.filament_maps or "1"

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<config>",
        "  <header>",
        '    <header_item key="X-BBL-Client-Type" value="slicer"/>',
        f'    <header_item key="X-BBL-Client-Version" value="{info.client_version}"/>',
        "  </header>",
        "  <plate>",
        '    <metadata key="index" value="1"/>',
    ]

    # BBS 02.05+ optional metadata
    if info.extruder_type is not None:
        lines.append(f'    <metadata key="extruder_type" value="{info.extruder_type}"/>')
    if info.nozzle_volume_type is not None:
        lines.append(f'    <metadata key="nozzle_volume_type" value="{info.nozzle_volume_type}"/>')

    lines.extend([
        f'    <metadata key="printer_model_id" value="{info.printer_model_id}"/>',
        f'    <metadata key="nozzle_diameters" value="{info.nozzle_diameter}"/>',
        f'    <metadata key="timelapse_type" value="{info.timelapse_type}"/>',
        f'    <metadata key="prediction" value="{info.prediction}"/>',
        f'    <metadata key="weight" value="{info.weight:.2f}"/>',
    ])

    if info.first_layer_time is not None:
        lines.append(f'    <metadata key="first_layer_time" value="{info.first_layer_time}"/>')

    lines.extend([
        f'    <metadata key="outside" value="{str(info.outside).lower()}"/>',
        f'    <metadata key="support_used" value="{str(info.support_used).lower()}"/>',
        f'    <metadata key="label_object_enabled" value="{str(info.label_object_enabled).lower()}"/>',
        f'    <metadata key="filament_maps" value="{filament_maps}"/>',
    ])

    if info.limit_filament_maps:
        lines.append(f'    <metadata key="limit_filament_maps" value="{info.limit_filament_maps}"/>')

    for obj in info.objects:
        lines.append(
            f'    <object identify_id="{obj.identify_id}"'
            f' name="{obj.name}" skipped="{str(obj.skipped).lower()}" />'
        )

    for f in info.filaments:
        attrs = (
            f'id="{f.slot}" tray_info_idx="{f.tray_info_idx}"'
            f' type="{f.filament_type}" color="{f.color}"'
            f' used_m="{f.used_m:.2f}" used_g="{f.used_g:.2f}"'
        )
        if f.extra_attrs:
            for k, v in f.extra_attrs.items():
                attrs += f' {k}="{v}"'
            lines.append(f"    <filament {attrs}/>")
        else:
            lines.append(f"    <filament {attrs} />")

    for w in info.warnings:
        lines.append(
            f'    <warning msg="{w.msg}" level="{w.level}"'
            f' error_code ="{w.error_code}"  />'
        )

    if info.layer_filament_lists:
        lines.append("    <layer_filament_lists>")
        for lfl in info.layer_filament_lists:
            lines.append(
                f'      <layer_filament_list filament_list="{lfl["filament_list"]}"'
                f' layer_ranges="{lfl["layer_ranges"]}" />'
            )
        lines.append("    </layer_filament_lists>")

    lines.extend([
        "  </plate>",
        "</config>",
        "",  # trailing newline
    ])
    return "\n".join(lines)


def _plate_json(info: SliceInfo, filaments: list[FilamentInfo]) -> str:
    """Generate Metadata/plate_1.json.

    If ``info.plate_data`` is provided, it is used as the base and only missing
    keys are filled in from SliceInfo/FilamentInfo.
    """
    data: dict[str, object] = {}
    if info.plate_data:
        data.update(info.plate_data)

    # Fill in defaults for keys not already present
    data.setdefault(
        "filament_colors", [f.color for f in filaments] if filaments else ["#F2754E"]
    )
    data.setdefault(
        "filament_ids", [f.slot - 1 for f in filaments] if filaments else [0]
    )
    data.setdefault("first_extruder", filaments[0].slot - 1 if filaments else 0)
    data.setdefault("is_seq_print", False)
    data.setdefault("nozzle_diameter", info.nozzle_diameter)
    data.setdefault("version", 2)

    return json.dumps(data, separators=(",", ":"))


def fixup_project_settings(settings: dict[str, object], min_slots: int = MIN_SLOTS) -> dict[str, object]:
    """Make project_settings Bambu Connect-ready.

    1. Add required keys that OrcaSlicer CLI ``--min-save`` omits.
    2. Pad short per-filament arrays to *min_slots* (P1S = 5).

    Called automatically by :func:`pack_gcode_3mf`. Also useful standalone
    for patching existing 3MF archives.
    """
    result = dict(settings)
    for key, default in _BC_REQUIRED_KEYS.items():
        if key not in result:
            result[key] = default
    for key, val in result.items():
        if isinstance(val, list) and 0 < len(val) < min_slots:
            while len(val) < min_slots:
                val.append(val[-1])
    return result


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def pack_gcode_3mf(
    gcode: bytes,
    output: Path | BytesIO,
    *,
    slice_info: SliceInfo | None = None,
    project_settings: dict[str, object] | None = None,
    thumbnails: dict[str, bytes] | None = None,
    extra_files: dict[str, bytes] | None = None,
) -> None:
    """Package G-code bytes into a Bambu Lab .gcode.3mf archive.

    Args:
        gcode: Raw G-code bytes.
        output: Output file path or BytesIO buffer.
        slice_info: Print metadata (time, weight, filaments). Defaults are
            provided if omitted.
        project_settings: Full slicer settings dict for
            project_settings.config. Automatically fixed up for Bambu
            Connect compatibility (missing keys added, arrays padded).
            Use :func:`build_project_settings` from ``bambu_3mf.settings``
            to generate from templates.
        thumbnails: Optional dict mapping archive paths to PNG bytes, e.g.
            ``{"Metadata/plate_1.png": png_bytes}``. If not provided,
            1x1 placeholder PNGs are used.
        extra_files: Optional dict of additional archive entries, e.g.
            ``{"Metadata/top_1.png": png_bytes}``.
    """
    if slice_info is None:
        slice_info = SliceInfo()

    # MD5 of raw gcode (uppercase hex, as firmware expects)
    md5 = hashlib.md5(gcode).hexdigest().upper()

    filament_maps = _filament_maps_str()

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
            z.writestr(
                "Metadata/plate_1.json",
                _plate_json(slice_info, slice_info.filaments),
            )
            z.writestr(
                "3D/3dmodel.model",
                slice_info.model_xml or MODEL_XML.format(application=slice_info.application),
            )

            if project_settings is not None:
                fixed_ps = fixup_project_settings(project_settings)
                z.writestr(
                    "Metadata/project_settings.config",
                    json.dumps(fixed_ps, indent=4) + "\n",
                )

            z.writestr("Metadata/plate_1.gcode.md5", md5)
            z.writestr("Metadata/plate_1.gcode", gcode)
            z.writestr(
                "Metadata/_rels/model_settings.config.rels",
                MODEL_SETTINGS_RELS_XML,
            )
            z.writestr(
                "Metadata/model_settings.config",
                _model_settings_xml(filament_maps, slice_info.filament_volume_maps),
            )
            z.writestr("Metadata/slice_info.config", _slice_info_xml(slice_info))
            z.writestr("_rels/.rels", RELS_XML)

            # Thumbnails
            thumb_map = thumbnails or {}
            for path in [
                "Metadata/plate_1.png",
                "Metadata/plate_no_light_1.png",
                "Metadata/plate_1_small.png",
            ]:
                z.writestr(path, thumb_map.get(path, _PLACEHOLDER_PNG))

            # Extra files (e.g. top_1.png, pick_1.png, cut_information.xml)
            if extra_files:
                for path, data in extra_files.items():
                    z.writestr(path, data)
    finally:
        if should_close:
            fh.close()

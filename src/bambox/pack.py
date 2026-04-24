"""Core logic for packaging G-code into Bambu Lab .gcode.3mf files.

Produces archives accepted by Bambu Connect. The format requirements are
documented in docs/gcode-3mf-format.md in the estampo repo.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO
from xml.sax.saxutils import escape as _xml_escape_base

log = logging.getLogger(__name__)

# BambuStudio version we present in archive metadata and HTTP headers.
# Must match a real release — the cloud API validates this for request signing.
# Source: https://github.com/bambulab/BambuStudio/blob/master/src/libslic3r/ProjectTask.hpp
BAMBU_STUDIO_VERSION = "02.05.00.66"


def xml_escape(value: str) -> str:
    """Escape a string for use inside XML double-quoted attribute values."""
    return _xml_escape_base(value, {'"': "&quot;"})


# Minimum AMS slot count for a P1S (4 AMS + 1 external spool).
# All per-filament arrays in project_settings must be padded to this length.
MIN_SLOTS = 5


def pad_to_slots(items: list, min_slots: int = MIN_SLOTS) -> list:
    """Pad *items* by repeating its last element until it reaches *min_slots*.

    Returns a new list (never mutates the input).
    """
    if not items or len(items) >= min_slots:
        return list(items)
    result = list(items)
    while len(result) < min_slots:
        result.append(result[-1])
    return result


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
 <metadata name="ProfileCover"></metadata>
 <metadata name="ProfileDescription"></metadata>
 <metadata name="ProfileTitle"></metadata>
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
    lines.extend(
        [
            '    <metadata key="gcode_file" value="Metadata/plate_1.gcode"/>',
            '    <metadata key="thumbnail_file" value="Metadata/plate_1.png"/>',
            '    <metadata key="thumbnail_no_light_file" value="Metadata/plate_no_light_1.png"/>',
            '    <metadata key="top_file" value="Metadata/top_1.png"/>',
            '    <metadata key="pick_file" value="Metadata/pick_1.png"/>',
            '    <metadata key="pattern_bbox_file" value="Metadata/plate_1.json"/>',
            "  </plate>",
            "</config>",
            "",
        ]
    )
    return "\n".join(lines)


MODEL_SETTINGS_RELS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/Metadata/plate_1.gcode" Id="rel-1" Type="http://schemas.bambulab.com/package/2021/gcode"/>
</Relationships>"""

# 1x1 transparent PNG (67 bytes) — minimal valid placeholder thumbnail.
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _is_valid_thumbnail(data: bytes) -> bool:
    """Return True if *data* is a PNG image with dimensions larger than 1×1.

    Reads only the 24-byte PNG signature + IHDR chunk — no PIL required.
    PNG spec: bytes 0-7 = magic, bytes 8-15 = IHDR length+type,
    bytes 16-19 = width (big-endian uint32), bytes 20-23 = height.
    """
    if len(data) < 24 or not data.startswith(_PNG_MAGIC):
        return False
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width > 1 and height > 1


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
    # BBS 02.05+ attributes
    used_for_object: bool = True
    used_for_support: bool = False
    group_id: int = 0
    nozzle_diameter: float = 0.4
    volume_type: str = "Standard"


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
    bed_type: str = "textured_plate"  # plate_1.json bed type
    plate_data: dict[str, object] | None = None  # raw plate_1.json passthrough
    application: str = f"BambuStudio-{BAMBU_STUDIO_VERSION}"
    model_xml: str = ""  # raw 3D/3dmodel.model passthrough (overrides application)
    # BBS 02.05+ fields
    client_version: str = BAMBU_STUDIO_VERSION
    extruder_type: int = 0
    nozzle_volume_type: int = 0
    first_layer_time: float | None = None
    filament_maps: str = ""  # override filament_maps in slice_info (e.g. "1 1 1 1 1")
    limit_filament_maps: str = "0 0 0 0 0"
    layer_filament_lists: list[dict[str, str]] | None = None  # [{filament_list, layer_ranges}]
    filament_volume_maps: str = "0 0 0 0 0"  # BBS 02.05+


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
    filament_maps = info.filament_maps or _filament_maps_str()

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<config>",
        "  <header>",
        '    <header_item key="X-BBL-Client-Type" value="slicer"/>',
        f'    <header_item key="X-BBL-Client-Version" value="{xml_escape(info.client_version)}"/>',
        "  </header>",
        "  <plate>",
        '    <metadata key="index" value="1"/>',
    ]

    lines.append(f'    <metadata key="extruder_type" value="{info.extruder_type}"/>')
    lines.append(f'    <metadata key="nozzle_volume_type" value="{info.nozzle_volume_type}"/>')

    lines.extend(
        [
            f'    <metadata key="printer_model_id" value="{xml_escape(info.printer_model_id)}"/>',
            f'    <metadata key="nozzle_diameters" value="{info.nozzle_diameter}"/>',
            f'    <metadata key="timelapse_type" value="{info.timelapse_type}"/>',
            f'    <metadata key="prediction" value="{info.prediction}"/>',
            f'    <metadata key="weight" value="{info.weight:.2f}"/>',
        ]
    )

    if info.first_layer_time is not None:
        lines.append(f'    <metadata key="first_layer_time" value="{info.first_layer_time}"/>')

    lines.extend(
        [
            f'    <metadata key="outside" value="{str(info.outside).lower()}"/>',
            f'    <metadata key="support_used" value="{str(info.support_used).lower()}"/>',
            f'    <metadata key="label_object_enabled" value="{str(info.label_object_enabled).lower()}"/>',
            f'    <metadata key="filament_maps" value="{xml_escape(filament_maps)}"/>',
        ]
    )

    if info.limit_filament_maps:
        lines.append(
            f'    <metadata key="limit_filament_maps" value="{xml_escape(info.limit_filament_maps)}"/>'
        )

    for obj in info.objects:
        lines.append(
            f'    <object identify_id="{obj.identify_id}"'
            f' name="{xml_escape(obj.name)}" skipped="{str(obj.skipped).lower()}" />'
        )

    for f in info.filaments:
        attrs = (
            f'id="{f.slot}" tray_info_idx="{xml_escape(f.tray_info_idx)}"'
            f' type="{xml_escape(f.filament_type)}" color="{xml_escape(f.color)}"'
            f' used_m="{f.used_m:.2f}" used_g="{f.used_g:.2f}"'
            f' used_for_object="{str(f.used_for_object).lower()}"'
            f' used_for_support="{str(f.used_for_support).lower()}"'
            f' group_id="{f.group_id}"'
            f' nozzle_diameter="{f.nozzle_diameter:.2f}"'
            f' volume_type="{xml_escape(f.volume_type)}"'
        )
        lines.append(f"    <filament {attrs}/>")

    for w in info.warnings:
        lines.append(
            f'    <warning msg="{xml_escape(w.msg)}" level="{w.level}" error_code ="{xml_escape(w.error_code)}"  />'
        )

    if info.layer_filament_lists:
        lines.append("    <layer_filament_lists>")
        for lfl in info.layer_filament_lists:
            lines.append(
                f'      <layer_filament_list filament_list="{xml_escape(lfl["filament_list"])}"'
                f' layer_ranges="{xml_escape(lfl["layer_ranges"])}" />'
            )
        lines.append("    </layer_filament_lists>")

    lines.extend(
        [
            "  </plate>",
            "</config>",
            "",  # trailing newline
        ]
    )
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
    data.setdefault("bed_type", info.bed_type)
    data.setdefault("filament_colors", [f.color for f in filaments] if filaments else ["#F2754E"])
    data.setdefault("filament_ids", [f.slot - 1 for f in filaments] if filaments else [0])
    data.setdefault("first_extruder", filaments[0].slot - 1 if filaments else 0)
    if info.first_layer_time is not None:
        data.setdefault("first_layer_time", info.first_layer_time)
    data.setdefault("is_seq_print", False)
    data.setdefault("nozzle_diameter", info.nozzle_diameter)
    data.setdefault("version", 2)

    return json.dumps(data, separators=(",", ":"))


def fixup_project_settings(
    settings: dict[str, object], min_slots: int = MIN_SLOTS
) -> dict[str, object]:
    """Make project_settings Bambu Connect-ready.

    1. Add required keys that OrcaSlicer CLI ``--min-save`` omits.
    2. Pad short per-filament arrays to *min_slots* (P1S = 5).

    Called automatically by :func:`pack_gcode_3mf`. Also useful standalone
    for patching existing 3MF archives.
    """
    result = {k: (list(v) if isinstance(v, list) else v) for k, v in settings.items()}
    for key, default in _BC_REQUIRED_KEYS.items():
        if key not in result:
            result[key] = list(default) if isinstance(default, list) else default
    for key, val in result.items():
        if isinstance(val, list) and 0 < len(val) < min_slots:
            result[key] = pad_to_slots(val, min_slots)
    return result


def fixup_model_settings(xml: str, min_slots: int = MIN_SLOTS) -> str:
    """Fix model_settings.config for Bambu Connect.

    1. Pad ``filament_maps`` value to *min_slots* (e.g. ``"1"`` → ``"1 1 1 1 1"``).
    2. Add missing thumbnail/bbox metadata keys that Bambu Connect requires.
    """

    def _pad_maps(m: re.Match[str]) -> str:
        parts = m.group(1).split()
        while len(parts) < min_slots:
            parts.append(parts[-1] if parts else "1")
        return f'key="filament_maps" value="{" ".join(parts)}"'

    result = re.sub(r'key="filament_maps" value="([^"]*)"', _pad_maps, xml)

    extra_keys = {
        "filament_volume_maps": " ".join(["0"] * min_slots),
        "thumbnail_file": "Metadata/plate_1.png",
        "thumbnail_no_light_file": "Metadata/plate_no_light_1.png",
        "top_file": "Metadata/top_1.png",
        "pick_file": "Metadata/pick_1.png",
    }
    for key, val in extra_keys.items():
        if f'key="{key}"' not in result:
            result = result.replace(
                "  </plate>",
                f'    <metadata key="{key}" value="{val}"/>\n  </plate>',
            )
    return result


def _patch_slice_info_compat(xml_str: str, min_slots: int = MIN_SLOTS) -> str:
    """Add missing BambuStudio 02.05 keys to OrcaSlicer slice_info.config.

    OrcaSlicer 2.x omits several metadata keys that Bambu Connect expects:
    - X-BBL-Client-Version (left blank by OrcaSlicer)
    - extruder_type / nozzle_volume_type
    - limit_filament_maps
    Pads filament_maps to min_slots if it is a single token.
    """
    # Set client version if blank
    result = re.sub(
        r'(<header_item key="X-BBL-Client-Version" value=")(")',
        rf"\g<1>{BAMBU_STUDIO_VERSION}\g<2>",
        xml_str,
    )

    # Pad filament_maps inside slice_info (separate from model_settings)
    def _pad_si_maps(m: re.Match[str]) -> str:
        parts = m.group(1).split()
        while len(parts) < min_slots:
            parts.append(parts[-1] if parts else "1")
        return f'key="filament_maps" value="{" ".join(parts)}"'

    result = re.sub(r'key="filament_maps" value="([^"]*)"', _pad_si_maps, result)
    # extruder_type and nozzle_volume_type must appear before printer_model_id
    for key, val in [("extruder_type", "0"), ("nozzle_volume_type", "0")]:
        if f'key="{key}"' not in result:
            result = result.replace(
                '    <metadata key="printer_model_id"',
                f'    <metadata key="{key}" value="{val}"/>\n    <metadata key="printer_model_id"',
                1,
            )
    # limit_filament_maps goes before </plate>
    if 'key="limit_filament_maps"' not in result:
        limit = " ".join(["0"] * min_slots)
        result = result.replace(
            "  </plate>", f'    <metadata key="limit_filament_maps" value="{limit}"/>\n  </plate>'
        )
    return result


def _patch_slice_info_printer_model(xml_str: str, printer_model_id: str) -> str:
    """Replace the printer_model_id value in slice_info.config XML."""
    return re.sub(
        r'(<metadata key="printer_model_id" value=")[^"]*(")',
        rf"\g<1>{printer_model_id}\g<2>",
        xml_str,
    )


def _patch_3dmodel(xml_str: str) -> str:
    """Fix 3D/3dmodel.model for Bambu Connect.

    OrcaSlicer writes Application as "BambuStudio-2.3.1"; BC expects
    "BambuStudio-02.05.00.66".  Also adds ProfileCover, ProfileDescription,
    and ProfileTitle metadata nodes if absent (present in BambuStudio output).
    """
    result = re.sub(
        r'(<metadata name="Application">)[^<]*(</metadata>)',
        rf"\g<1>BambuStudio-{BAMBU_STUDIO_VERSION}\g<2>",
        xml_str,
    )
    for name in ("ProfileCover", "ProfileDescription", "ProfileTitle"):
        if f'name="{name}"' not in result:
            result = result.replace(
                "<resources>",
                f' <metadata name="{name}"></metadata>\n <resources>',
            )
    return result


def _patch_slice_info_weight(xml_str: str, fallback_g: float = 0.0) -> str | None:
    """Fix weight=0 or weight="" in slice_info.config.

    Tries per-filament ``used_g`` sums first; falls back to ``fallback_g``
    (e.g. computed from G-code footer) when those are also zero.
    Returns the patched XML or None if weight is already non-zero.
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    plate = root.find("plate")
    if plate is None:
        return None
    meta = {el.get("key", ""): el.get("value", "") for el in plate.findall("metadata")}
    weight_str = meta.get("weight", "0")
    try:
        current_weight = float(weight_str)
    except ValueError:
        current_weight = 0.0  # OrcaSlicer emits weight="" when filament_density=0
    if current_weight > 0:
        return None  # already correct
    total_g = sum(float(f.get("used_g", "0") or "0") for f in plate.findall("filament"))
    if total_g <= 0:
        total_g = fallback_g
    if total_g <= 0:
        return None  # nothing to fix
    return re.sub(
        r'(<metadata key="weight" value=")[^"]*(")',
        rf"\g<1>{total_g:.2f}\g<2>",
        xml_str,
    )


# Density fallback for weight computation when filament_density=0 in the profile.
# Used only when slice_info has no usable used_g values.
_FILAMENT_DENSITY: dict[str, float] = {
    "PLA": 1.24,
    "PETG": 1.27,
    "ABS": 1.04,
    "ASA": 1.07,
    "PA": 1.14,
    "PC": 1.20,
    "TPU": 1.20,
    "PVA": 1.23,
}
_DENSITY_DEFAULT = 1.24  # PLA


def _extract_weight_from_gcode(gcode_str: str, filament_type: str = "") -> float:
    """Extract total filament weight in grams from OrcaSlicer G-code footer.

    Prefers an explicit ``; filament used [g]`` comment; falls back to
    ``; filament used [cm3]`` multiplied by the filament density.
    """
    total_g = 0.0
    total_cm3 = 0.0
    for line in gcode_str.splitlines():
        if not line.startswith(";"):
            continue
        if m := re.match(r";\s*(?:total )?filament used \[g\]\s*=\s*(.+)", line):
            vals = [float(v) for v in m.group(1).split(",") if v.strip()]
            total_g = sum(vals)
        elif m := re.match(r";\s*(?:total )?filament used \[cm3\]\s*=\s*(.+)", line):
            vals = [float(v) for v in m.group(1).split(",") if v.strip()]
            total_cm3 = sum(vals)
    if total_g > 0:
        return round(total_g, 2)
    if total_cm3 > 0:
        density = _FILAMENT_DENSITY.get(filament_type.upper(), _DENSITY_DEFAULT)
        return round(total_cm3 * density, 2)
    return 0.0


_PRINTER_MODEL_TO_MACHINE: dict[str, str] = {
    "Bambu Lab P1S": "p1s",
    "Bambu Lab P1P": "p1s",  # close enough — same profile base
}

_FILAMENT_TYPE_TO_PROFILE: dict[str, str] = {
    "PLA": "PLA",
    "ASA": "ASA",
    "PETG-CF": "PETG-CF",
    "PETG": "PLA",  # no PETG profile; PLA is the safest fallback
    "ABS": "ASA",  # closest available (both engineering/high-temp)
    "PA": "ASA",
    "PC": "ASA",
    "TPU": "PLA",
}


def _autodetect_machine_filaments(
    orca_ps: dict[str, object], existing_filaments: list[str]
) -> tuple[str | None, list[str] | None]:
    """Infer bambox machine + filament profile names from OrcaSlicer project_settings.

    Returns (machine, filaments) if a matching bambox profile exists, else
    (None, None) so the caller falls back to fixup_project_settings.
    """
    from bambox.settings import available_filaments, available_machines

    printer_model = str(orca_ps.get("printer_model", ""))
    machine = _PRINTER_MODEL_TO_MACHINE.get(printer_model)
    if not machine or machine not in available_machines():
        return None, None

    if existing_filaments:
        filaments = existing_filaments
    else:
        raw_types = orca_ps.get("filament_type", [])
        if isinstance(raw_types, list) and raw_types:
            first = str(raw_types[0]).upper()
        elif isinstance(raw_types, str) and raw_types:
            first = raw_types.upper()
        else:
            first = "PLA"
        avail = set(available_filaments())
        profile = _FILAMENT_TYPE_TO_PROFILE.get(first, "PLA")
        if profile not in avail:
            profile = "PLA"
        filaments = [profile]

    return machine, filaments


def repack_3mf(
    path: Path,
    *,
    machine: str | None = None,
    filaments: list[str] | None = None,
    filament_colors: list[str] | None = None,
    min_slots: int = MIN_SLOTS,
) -> None:
    """Fix up an existing OrcaSlicer .gcode.3mf for Bambu Connect.

    Applies all BBL firmware fixups in-place:

    1. **project_settings.config** — add missing keys, pad arrays. If *machine*
       and *filaments* are given, regenerate from profiles instead of patching.
    2. **model_settings.config** — pad filament_maps, add thumbnail references.
    3. **Thumbnails** — regenerate from G-code toolpath if missing or broken
       (headless OrcaSlicer without Xvfb produces empty PNGs).

    Args:
        path: Path to the .gcode.3mf file (modified in-place).
        machine: Machine profile name for settings regeneration. If ``None``,
            existing settings are patched rather than regenerated.
        filaments: Filament type names for settings regeneration.
        filament_colors: Hex colors per filament slot.
        min_slots: Minimum slot count for per-filament arrays (default 5).
    """
    with zipfile.ZipFile(path, "r") as zin:
        # --- Fix project_settings.config ---
        try:
            ps_raw = zin.read("Metadata/project_settings.config")
        except KeyError:
            ps_raw = None

        if not machine and ps_raw is not None:
            machine, filaments = _autodetect_machine_filaments(json.loads(ps_raw), filaments or [])

        if machine and filaments:
            from bambox.settings import build_project_settings

            ps = fixup_project_settings(
                build_project_settings(
                    filaments,
                    machine=machine,
                    filament_colors=filament_colors,
                    min_slots=min_slots,
                ),
                min_slots=min_slots,
            )
        elif ps_raw is not None:
            ps = fixup_project_settings(json.loads(ps_raw), min_slots=min_slots)
        else:
            ps = None

        # --- Fix 3D/3dmodel.model ---
        try:
            model_raw = zin.read("3D/3dmodel.model").decode()
            model_patched: str | None = _patch_3dmodel(model_raw)
        except KeyError:
            model_patched = None

        # --- Regenerate model_settings.config from scratch ---
        # Patching the OrcaSlicer output produces wrong key ordering; BC is
        # sensitive to the order. Regenerate cleanly, extracting filament_maps
        # from the original if present.
        try:
            ms_raw = zin.read("Metadata/model_settings.config").decode()
            _fm_match = re.search(r'key="filament_maps" value="([^"]*)"', ms_raw)
            _fm_parts = _fm_match.group(1).split() if _fm_match else []
            while len(_fm_parts) < min_slots:
                _fm_parts.append(_fm_parts[-1] if _fm_parts else "1")
            ms_patched: str | None = _model_settings_xml(
                " ".join(_fm_parts), " ".join(["0"] * min_slots)
            )
        except KeyError:
            ms_patched = None

        # --- Fix plate_1.json ---
        _PLATE_JSON_PATH = "Metadata/plate_1.json"
        plate_json_override: str | None = None
        try:
            _pj = json.loads(zin.read(_PLATE_JSON_PATH))
            # BC requires textured_plate; OrcaSlicer may emit cool_plate or others
            if _pj.get("bed_type") != "textured_plate":
                _pj["bed_type"] = "textured_plate"
                plate_json_override = json.dumps(_pj, separators=(",", ":"))
        except KeyError:
            # Generate a minimal plate_1.json so model_settings refs are valid
            colors = filament_colors or ["#F2754E"]
            plate_data: dict[str, object] = {
                "bed_type": "textured_plate",
                "filament_colors": colors,
                "filament_ids": list(range(len(colors))),
                "first_extruder": 0,
                "is_seq_print": False,
                "nozzle_diameter": 0.4,
                "version": 2,
            }
            plate_json_override = json.dumps(plate_data, separators=(",", ":"))

        # --- Fix slice_info.config ---
        try:
            si_raw = zin.read("Metadata/slice_info.config").decode()
        except KeyError:
            si_raw = None
        si_patched: str | None = None
        gcode_str: str | None = None
        if si_raw is not None:
            weight_fix = _patch_slice_info_weight(si_raw)
            if weight_fix is not None:
                si_patched = weight_fix
            else:
                # used_g=0 or weight="" — try extracting weight from G-code footer
                import xml.etree.ElementTree as ET

                try:
                    _si_root = ET.fromstring(si_raw)
                    _plate = _si_root.find("plate")
                    _filament_type = (
                        (_plate.find("filament") or ET.Element("f")).get("type", "")
                        if _plate is not None
                        else ""
                    )
                except ET.ParseError:
                    _filament_type = ""
                if gcode_str is None:
                    try:
                        gcode_str = zin.read("Metadata/plate_1.gcode").decode(errors="replace")
                    except KeyError:
                        gcode_str = ""
                fallback_g = _extract_weight_from_gcode(gcode_str, _filament_type)
                weight_fix2 = _patch_slice_info_weight(si_raw, fallback_g=fallback_g)
                if weight_fix2 is not None:
                    si_patched = weight_fix2
            # Apply BambuStudio compat patches (version, missing keys, padded maps)
            base = si_patched if si_patched is not None else si_raw
            si_patched = _patch_slice_info_compat(base, min_slots=min_slots)
            if machine:
                from bambox.cura import PRINTER_MODEL_IDS

                model_id = PRINTER_MODEL_IDS.get(machine.lower(), "")
                if model_id:
                    si_patched = _patch_slice_info_printer_model(si_patched, model_id)

        # --- Fix thumbnails ---
        thumb_files = [
            "Metadata/plate_1.png",
            "Metadata/plate_no_light_1.png",
            "Metadata/plate_1_small.png",
            "Metadata/top_1.png",
            "Metadata/pick_1.png",
        ]
        thumbnail_overrides: dict[str, bytes] = {}
        for fname in thumb_files:
            try:
                existing = zin.read(fname)
                if _is_valid_thumbnail(existing):
                    continue  # valid thumbnail — keep as-is
            except KeyError:
                pass
            # Need to generate — load G-code lazily
            if gcode_str is None:
                try:
                    gcode_bytes = zin.read("Metadata/plate_1.gcode")
                    gcode_str = gcode_bytes.decode(errors="replace")
                except KeyError:
                    gcode_str = ""
            if fname in ("Metadata/top_1.png", "Metadata/pick_1.png"):
                thumbnail_overrides[fname] = _PLACEHOLDER_PNG
            elif gcode_str:
                try:
                    from bambox.thumbnail import gcode_thumbnail

                    size = 128 if "small" in fname else 512
                    thumbnail_overrides[fname] = gcode_thumbnail(gcode_str, size, size)
                except Exception:
                    log.debug("Thumbnail generation failed for %s", fname, exc_info=True)
                    thumbnail_overrides[fname] = _PLACEHOLDER_PNG
            else:
                thumbnail_overrides[fname] = _PLACEHOLDER_PNG

        # --- Rewrite the archive ---
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if item.filename in thumbnail_overrides:
                    continue  # replaced below
                elif item.filename == "3D/3dmodel.model" and model_patched is not None:
                    zout.writestr(item, model_patched)
                elif item.filename == "Metadata/project_settings.config" and ps is not None:
                    zout.writestr(item, json.dumps(ps, indent=4) + "\n")
                elif item.filename == "Metadata/model_settings.config" and ms_patched:
                    zout.writestr(item, ms_patched)
                elif item.filename == "Metadata/slice_info.config" and si_patched:
                    zout.writestr(item, si_patched)
                elif item.filename == _PLATE_JSON_PATH and plate_json_override is not None:
                    zout.writestr(item, plate_json_override)
                else:
                    zout.writestr(item, zin.read(item.filename))

            # Write generated thumbnails
            for fname, data in thumbnail_overrides.items():
                zout.writestr(fname, data)

            # Add files that didn't exist in the original
            if ps is not None and ps_raw is None:
                zout.writestr(
                    "Metadata/project_settings.config",
                    json.dumps(ps, indent=4) + "\n",
                )
            if plate_json_override is not None and _PLATE_JSON_PATH not in zin.namelist():
                zout.writestr(_PLATE_JSON_PATH, plate_json_override)

    backup = path.with_name(path.name + ".bak")
    path.rename(backup)
    try:
        path.write_bytes(buf.getvalue())
    except Exception:
        backup.rename(path)
        raise
    backup.unlink()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def pack_gcode_3mf(
    gcode: bytes,
    output: Path | IO[bytes],
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
            Use :func:`build_project_settings` from ``bambox.settings``
            to generate from templates.
        thumbnails: Optional dict mapping archive paths to PNG bytes, e.g.
            ``{"Metadata/plate_1.png": png_bytes}``. If not provided,
            1x1 placeholder PNGs are used.
        extra_files: Optional dict of additional archive entries, e.g.
            ``{"Metadata/top_1.png": png_bytes}``.
    """
    if slice_info is None:
        slice_info = SliceInfo()

    # Translate non-BBL G-code (CuraEngine, PrusaSlicer, etc.) to
    # Bambu-firmware-compatible format with HEADER_BLOCK, M73 L, M991.
    from bambox.gcode_compat import is_bbl_gcode, translate_to_bbl

    if not is_bbl_gcode(gcode):
        gcode = translate_to_bbl(gcode)

    # MD5 of final gcode (uppercase hex, as firmware expects)
    md5 = hashlib.md5(gcode).hexdigest().upper()

    filament_maps = _filament_maps_str()

    # Build the ZIP archive
    fh: IO[bytes]
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

            # Thumbnails — generate from G-code toolpath if not provided
            thumb_map = thumbnails or {}
            if not thumb_map:
                try:
                    from bambox.thumbnail import gcode_thumbnail

                    gcode_str = gcode if isinstance(gcode, str) else gcode.decode(errors="replace")
                    main_png = gcode_thumbnail(gcode_str, 512, 512)
                    small_png = gcode_thumbnail(gcode_str, 128, 128)
                    thumb_map = {
                        "Metadata/plate_1.png": main_png,
                        "Metadata/plate_no_light_1.png": main_png,
                        "Metadata/plate_1_small.png": small_png,
                        "Metadata/top_1.png": _PLACEHOLDER_PNG,
                        "Metadata/pick_1.png": _PLACEHOLDER_PNG,
                    }
                except Exception:
                    log.debug("Thumbnail generation failed, using placeholders", exc_info=True)
            extra = extra_files or {}
            for path in [
                "Metadata/plate_1.png",
                "Metadata/plate_no_light_1.png",
                "Metadata/plate_1_small.png",
                "Metadata/top_1.png",
                "Metadata/pick_1.png",
            ]:
                if path not in extra:
                    z.writestr(path, thumb_map.get(path, _PLACEHOLDER_PNG))

            # Extra files (e.g. custom thumbnails, cut_information.xml)
            for path, data in extra.items():
                z.writestr(path, data)
    finally:
        if should_close:
            fh.close()

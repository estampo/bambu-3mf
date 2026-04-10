"""Validate Bambu Lab .gcode.3mf archives.

Checks archive structure, G-code safety markers, metadata completeness,
and firmware compatibility.  Returns structured results that can be
rendered as human-readable text or JSON for programmatic consumption
(e.g., by estampo).

This module owns validation logic only.  It must NOT contain settings
generation, archive packing, slicer logic, or printer communication.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import IO

# Minimum AMS slot count for P1S (4 AMS + 1 external spool).
MIN_SLOTS = 5

# Files that must exist in every valid .gcode.3mf archive.
REQUIRED_ARCHIVE_FILES = {
    "[Content_Types].xml",
    "_rels/.rels",
    "3D/3dmodel.model",
    "Metadata/plate_1.gcode",
    "Metadata/plate_1.gcode.md5",
    "Metadata/model_settings.config",
    "Metadata/_rels/model_settings.config.rels",
    "Metadata/slice_info.config",
    "Metadata/plate_1.json",
    "Metadata/plate_1.png",
    "Metadata/plate_no_light_1.png",
    "Metadata/plate_1_small.png",
}

# Thumbnail files that BambuStudio 2.5+ includes for full firmware support.
RECOMMENDED_THUMBNAIL_FILES = {
    "Metadata/top_1.png",
    "Metadata/pick_1.png",
}

# Keys in project_settings that are fixed-length machine lists, NOT per-filament arrays.
_FIXED_LIST_KEYS = {
    "bed_exclude_area",
    "print_compatible_printers",
    "printable_area",
    "start_end_points",
    "upward_compatible_machine",
}

# Compiled regex patterns (module-level for performance on large gcode files)
_RE_TEMP_ARRAY = re.compile(r"M10[49]\s+S\[")
_RE_TEMP_TEMPLATE = re.compile(r"M10[49]\s+S\{")
_RE_TEMP_BAD_BED = re.compile(r"M1[49]0\s+S\[")
_RE_TEMP_BAD_BED_TPL = re.compile(r"M1[49]0\s+S\{")
_RE_TOOLCHANGE_FEEDRATE = re.compile(r"M620\.1\s+E\s+F([\d.]+)")
_RE_UNSUBSTITUTED = re.compile(r"\{[a-zA-Z_]\w*\}")
_RE_HEADER_START = re.compile(r"^; HEADER_BLOCK_START", re.MULTILINE)
_RE_HEADER_END = re.compile(r"^; HEADER_BLOCK_END", re.MULTILINE)
_RE_TOTAL_LAYERS = re.compile(r"; total layer number:\s*(\d+)")
_RE_M73_L = re.compile(r"^M73 L(\d+)", re.MULTILINE)
_RE_M991 = re.compile(r"^M991 S0 P(\d+)", re.MULTILINE)
_RE_M73_P = re.compile(r"^M73 P\d+", re.MULTILINE)
_RE_M73_R = re.compile(r"^M73 P\d+ R\d+", re.MULTILINE)
_RE_M73_R_VALUE = re.compile(r"^M73 P\d+ R(\d+)", re.MULTILINE)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class Severity(Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass
class Finding:
    """A single validation finding (error or warning)."""

    severity: Severity
    code: str  # e.g. "E001", "W003"
    message: str  # human-readable description
    detail: str = ""  # optional extra context


@dataclass
class ValidationResult:
    """Aggregated validation outcome."""

    findings: list[Finding] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        """True if no errors (warnings are allowed)."""
        return not any(f.severity == Severity.ERROR for f in self.findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.WARNING]

    def to_dict(self) -> dict[str, object]:
        """JSON-serializable dict for ``--json`` output."""
        return {
            "valid": self.valid,
            "errors": [
                {"code": f.code, "message": f.message, "detail": f.detail} for f in self.errors
            ],
            "warnings": [
                {"code": f.code, "message": f.message, "detail": f.detail} for f in self.warnings
            ],
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_3mf(path: Path) -> ValidationResult:
    """Validate a ``.gcode.3mf`` archive on disk."""
    with open(path, "rb") as fh:
        return validate_3mf_buffer(fh)


def validate_3mf_buffer(buf: IO[bytes]) -> ValidationResult:
    """Validate a ``.gcode.3mf`` from an open file-like object."""
    findings: list[Finding] = []

    try:
        zf = zipfile.ZipFile(buf)
    except zipfile.BadZipFile:
        findings.append(Finding(Severity.ERROR, "E000", "Not a valid ZIP file"))
        return ValidationResult(findings)

    with zf:
        names = set(zf.namelist())
        _check_required_files(names, findings)
        _check_recommended_thumbnails(names, findings)

        # Read gcode and metadata — guard against missing files
        gcode_bytes = _safe_read(zf, "Metadata/plate_1.gcode")
        md5_stored = _safe_read_str(zf, "Metadata/plate_1.gcode.md5")
        slice_info = _safe_read_str(zf, "Metadata/slice_info.config")
        project_settings_raw = _safe_read_str(zf, "Metadata/project_settings.config")

        if gcode_bytes is not None and md5_stored is not None:
            _check_md5(gcode_bytes, md5_stored, findings)

        gcode: str | None = None
        if gcode_bytes is not None:
            gcode = gcode_bytes.decode(errors="replace")
            _check_gcode(gcode, findings)

        if slice_info is not None:
            _check_slice_info(slice_info, findings)

        if project_settings_raw is not None:
            _check_project_settings(project_settings_raw, findings)

        if gcode is not None and slice_info is not None:
            _check_time_sync(gcode, slice_info, findings)

    return ValidationResult(findings)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_read(zf: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        return zf.read(name)
    except KeyError:
        return None


def _safe_read_str(zf: zipfile.ZipFile, name: str) -> str | None:
    data = _safe_read(zf, name)
    return data.decode(errors="replace") if data is not None else None


# ---------------------------------------------------------------------------
# Archive-level checks
# ---------------------------------------------------------------------------


def _check_required_files(names: set[str], findings: list[Finding]) -> None:
    """E006: All required archive files must be present."""
    missing = REQUIRED_ARCHIVE_FILES - names
    for m in sorted(missing):
        findings.append(Finding(Severity.ERROR, "E006", f"Missing required file: {m}"))


def _check_recommended_thumbnails(names: set[str], findings: list[Finding]) -> None:
    """W010: BambuStudio 2.5+ includes top_1.png and pick_1.png for full thumbnail support."""
    missing = RECOMMENDED_THUMBNAIL_FILES - names
    for m in sorted(missing):
        findings.append(Finding(Severity.WARNING, "W010", f"Missing recommended thumbnail: {m}"))


def _check_md5(gcode: bytes, stored: str, findings: list[Finding]) -> None:
    """E003: MD5 checksum must match gcode content."""
    computed = hashlib.md5(gcode).hexdigest().upper()
    stored = stored.strip().upper()
    if computed != stored:
        findings.append(
            Finding(
                Severity.ERROR,
                "E003",
                "MD5 checksum mismatch",
                f"expected {stored}, computed {computed}",
            )
        )


# ---------------------------------------------------------------------------
# G-code checks
# ---------------------------------------------------------------------------


def _check_gcode(gcode: str, findings: list[Finding]) -> None:
    """Run all G-code structural checks."""
    _check_temperature_commands(gcode, findings)
    _check_toolchange_feedrate(gcode, findings)
    _check_unsubstituted_templates(gcode, findings)
    _check_header_block(gcode, findings)
    _check_layer_markers(gcode, findings)


def _check_temperature_commands(gcode: str, findings: list[Finding]) -> None:
    """E001: Temperature S parameters must be numeric integers.

    Only checks non-comment lines — BambuStudio embeds template syntax
    in gcode comments (e.g. ``; change_filament_gcode = ...``).
    """
    patterns = [
        (_RE_TEMP_ARRAY, "extruder temp with array value"),
        (_RE_TEMP_TEMPLATE, "extruder temp with template value"),
        (_RE_TEMP_BAD_BED, "bed temp with array value"),
        (_RE_TEMP_BAD_BED_TPL, "bed temp with template value"),
    ]
    for line in gcode.splitlines():
        stripped = line.strip()
        if stripped.startswith(";"):
            continue
        for pattern, desc in patterns:
            if pattern.search(stripped):
                findings.append(
                    Finding(
                        Severity.ERROR,
                        "E001",
                        f"Non-numeric temperature command ({desc})",
                        stripped[:120],
                    )
                )
                return  # one finding is enough


def _check_toolchange_feedrate(gcode: str, findings: list[Finding]) -> None:
    """E002: M620.1 E feedrate must be >= 100 mm/min (linear, not volumetric)."""
    for line in gcode.splitlines():
        stripped = line.strip()
        if stripped.startswith(";"):
            continue
        m = _RE_TOOLCHANGE_FEEDRATE.search(stripped)
        if m:
            feedrate = float(m.group(1))
            if feedrate < 100:
                findings.append(
                    Finding(
                        Severity.ERROR,
                        "E002",
                        f"Toolchange feedrate too low: F{feedrate} "
                        "(< 100 mm/min, likely raw volumetric)",
                        stripped[:120],
                    )
                )
                return  # one finding is enough


def _check_unsubstituted_templates(gcode: str, findings: list[Finding]) -> None:
    """E005: No unsubstituted ``{variable}`` templates in G-code commands."""
    for line in gcode.splitlines():
        stripped = line.strip()
        # Skip comments — some slicers use {type} annotations in comments
        if stripped.startswith(";"):
            continue
        m = _RE_UNSUBSTITUTED.search(stripped)
        if m:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "E005",
                    f"Unsubstituted template: {m.group()}",
                    stripped[:120],
                )
            )
            break  # one finding is enough


def _check_header_block(gcode: str, findings: list[Finding]) -> None:
    """E007/E008: BBL firmware header block must be present with layer count."""
    has_start = bool(_RE_HEADER_START.search(gcode))
    has_end = bool(_RE_HEADER_END.search(gcode))

    if not has_start or not has_end:
        findings.append(
            Finding(
                Severity.ERROR,
                "E007",
                "Missing BBL firmware header block (HEADER_BLOCK_START / HEADER_BLOCK_END)",
            )
        )
        return

    m = _RE_TOTAL_LAYERS.search(gcode)
    if not m:
        findings.append(
            Finding(
                Severity.ERROR,
                "E008",
                "Header block missing 'total layer number'",
            )
        )
    elif int(m.group(1)) == 0:
        findings.append(
            Finding(
                Severity.ERROR,
                "E008",
                "total layer number is 0",
            )
        )


def _check_layer_markers(gcode: str, findings: list[Finding]) -> None:
    """E009/E010/E011/W007/W008/W009: Layer progress markers."""
    m73_l = _RE_M73_L.findall(gcode)
    m991 = _RE_M991.findall(gcode)

    if not m73_l:
        findings.append(
            Finding(
                Severity.ERROR,
                "E009",
                "No M73 L layer progress markers found",
            )
        )

    if not m991:
        findings.append(
            Finding(
                Severity.ERROR,
                "E010",
                "No M991 S0 P layer-change notifications found (printer cannot track layers)",
            )
        )

    # E011: layer marker count vs declared total
    m_total = _RE_TOTAL_LAYERS.search(gcode)
    if m_total and m73_l:
        declared = int(m_total.group(1))
        actual = len(m73_l)
        if declared > 0 and abs(actual - declared) > max(2, declared * 0.1):
            findings.append(
                Finding(
                    Severity.ERROR,
                    "E011",
                    f"Layer marker count mismatch: {actual} M73 L markers "
                    f"vs {declared} declared layers",
                )
            )

    # W007: M73 P (percentage progress)
    if not _RE_M73_P.search(gcode):
        findings.append(
            Finding(
                Severity.WARNING,
                "W007",
                "No M73 P percentage progress markers",
            )
        )

    # W008: M73 R (remaining time)
    if not _RE_M73_R.search(gcode):
        findings.append(
            Finding(
                Severity.WARNING,
                "W008",
                "No M73 R remaining time markers",
            )
        )

    # W009: monotonicity
    if len(m73_l) >= 2:
        values = [int(v) for v in m73_l]
        for i in range(1, len(values)):
            if values[i] < values[i - 1]:
                findings.append(
                    Finding(
                        Severity.WARNING,
                        "W009",
                        "Layer markers not monotonically increasing",
                        f"L{values[i - 1]} followed by L{values[i]}",
                    )
                )
                break


# ---------------------------------------------------------------------------
# Metadata checks (slice_info.config)
# ---------------------------------------------------------------------------


def _check_slice_info(xml_str: str, findings: list[Finding]) -> None:
    """W001/W002/W003/W004/W006: Validate slice_info.config metadata."""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        findings.append(Finding(Severity.ERROR, "E012", "slice_info.config is not valid XML"))
        return

    plate = root.find("plate")
    if plate is None:
        findings.append(
            Finding(Severity.ERROR, "E012", "slice_info.config missing <plate> element")
        )
        return

    meta = {el.get("key", ""): el.get("value", "") for el in plate.findall("metadata")}

    # W001: printer_model_id
    if not meta.get("printer_model_id"):
        findings.append(Finding(Severity.WARNING, "W001", "printer_model_id is empty"))

    # W002: prediction
    pred = meta.get("prediction", "0")
    if pred == "0" or pred == "":
        findings.append(Finding(Severity.WARNING, "W002", "prediction (print time) is 0"))

    # W003: weight
    weight = meta.get("weight", "0")
    if weight in ("0", "0.00", ""):
        findings.append(Finding(Severity.WARNING, "W003", "weight is 0"))

    # Check filaments
    filaments = plate.findall("filament")

    # W004: filament color format
    hex_re = re.compile(r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
    for f in filaments:
        color = f.get("color", "")
        if color and not hex_re.match(color):
            findings.append(
                Finding(
                    Severity.WARNING,
                    "W004",
                    f"Filament color not valid hex: '{color}'",
                    f"filament id={f.get('id', '?')}",
                )
            )


# ---------------------------------------------------------------------------
# Project settings checks
# ---------------------------------------------------------------------------

# Keys known to be per-filament arrays (must have >= MIN_SLOTS entries)
_KNOWN_ARRAY_KEYS = {
    "filament_type",
    "filament_colour",
    "nozzle_temperature",
    "nozzle_temperature_initial_layer",
    "bed_temperature",
    "filament_max_volumetric_speed",
}


def _check_project_settings(raw: str, findings: list[Finding]) -> None:
    """E004/W005/W006: Validate project_settings.config."""
    try:
        settings = json.loads(raw)
    except json.JSONDecodeError:
        findings.append(
            Finding(
                Severity.ERROR,
                "E004",
                "project_settings.config is not valid JSON",
            )
        )
        return

    # E004: per-filament arrays must be padded to MIN_SLOTS
    for key in _KNOWN_ARRAY_KEYS:
        val = settings.get(key)
        if isinstance(val, list) and 0 < len(val) < MIN_SLOTS:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "E004",
                    f"Array '{key}' has {len(val)} elements, needs {MIN_SLOTS}",
                )
            )

    # W005: print_compatible_printers should be a list, not a per-slot broadcast
    pcp = settings.get("print_compatible_printers")
    if isinstance(pcp, list) and len(pcp) >= MIN_SLOTS:
        unique = set(pcp)
        if len(unique) == 1:
            findings.append(
                Finding(
                    Severity.WARNING,
                    "W005",
                    "print_compatible_printers appears to be a per-slot broadcast "
                    f"(all {len(pcp)} entries are '{next(iter(unique))}')",
                    "Should be a flat list of compatible printer models",
                )
            )

    # W006: printer_model should be set
    pm = settings.get("printer_model", "")
    if not pm:
        findings.append(
            Finding(Severity.WARNING, "W006", "printer_model is empty in project_settings")
        )


# ---------------------------------------------------------------------------
# Time sync checks
# ---------------------------------------------------------------------------


def _check_time_sync(gcode: str, slice_info_xml: str, findings: list[Finding]) -> None:
    """W011: M73 R remaining time should roughly agree with slice_info prediction."""
    # Extract prediction from slice_info
    try:
        root = ET.fromstring(slice_info_xml)
    except ET.ParseError:
        return
    plate = root.find("plate")
    if plate is None:
        return
    meta = {el.get("key", ""): el.get("value", "") for el in plate.findall("metadata")}
    pred_str = meta.get("prediction", "")
    if not pred_str or pred_str == "0":
        return
    try:
        prediction_secs = int(pred_str)
    except ValueError:
        return

    # Extract max M73 R value (first one in gcode = total remaining time)
    r_values = _RE_M73_R_VALUE.findall(gcode)
    if not r_values:
        return
    m73_max_mins = max(int(v) for v in r_values)
    m73_max_secs = m73_max_mins * 60

    # Allow 30% tolerance — different estimation methods will always diverge somewhat
    if prediction_secs > 0 and m73_max_secs > 0:
        ratio = m73_max_secs / prediction_secs
        if ratio < 0.5 or ratio > 2.0:
            findings.append(
                Finding(
                    Severity.WARNING,
                    "W011",
                    f"M73 remaining time ({m73_max_mins}m) diverges from "
                    f"slice_info prediction ({prediction_secs // 60}m)",
                    f"ratio={ratio:.2f}, expected 0.5–2.0",
                )
            )

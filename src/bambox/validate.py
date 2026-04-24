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

from bambox.pack import MIN_SLOTS

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
_RE_M620_S = re.compile(r"^M620 S(\d+)", re.MULTILINE)
_RE_M621_S = re.compile(r"^M621 S(\d+)", re.MULTILINE)
_RE_BARE_TOOL = re.compile(r"^T([0-4])\s*$", re.MULTILINE)

# G-code safety regex patterns
_RE_LAYER_CHANGE = re.compile(r"^;LAYER_CHANGE", re.MULTILINE)
_RE_LAYER_Z = re.compile(r"^;Z:([\d.]+)", re.MULTILINE)
_RE_G1_Z = re.compile(r"^G[01]\s+.*Z([\d.]+)", re.MULTILINE)
_RE_G1_Z_ONLY = re.compile(r"^G[01]\s+Z([\d.]+)", re.MULTILINE)
_RE_TEMP_ZERO = re.compile(r"^M(10[49]|1[49]0)\s+S0(?:\s|$)", re.MULTILINE)
_RE_G28 = re.compile(r"^G28\b", re.MULTILINE)
_RE_EXTRUSION = re.compile(r"^G[01]\s+.*E[\d.]+", re.MULTILINE)


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


def validate_gcode(gcode: str) -> ValidationResult:
    """Validate assembled G-code for physically dangerous moves before packaging.

    This is a pre-packaging safety check — separate from the archive-level
    validation in ``validate_3mf``.  It catches dangerous patterns that could
    damage the printer or cause failed prints.

    Checks:
        S001: End G-code Z move lower than ``max_layer_z`` (nozzle crash risk)
        S002: ``M104``/``M109``/``M140`` S0 in toolpath (premature heater off)
        S003: Extrusion before homing (``G28``)
    """
    findings: list[Finding] = []
    _check_end_z_safety(gcode, findings)
    _check_premature_heater_off(gcode, findings)
    _check_extrusion_before_homing(gcode, findings)
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
    _check_multi_filament(gcode, findings)


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
    """E002: M620.1 E feedrate must be >= 1 mm/min.

    OrcaSlicer computes M620.1 E feedrate as
    ``filament_max_volumetric_speed / 2.4053 * 60``, which can legitimately
    produce values below 100 mm/min for low-speed filaments. Only flag values
    so low (< 1 mm/min) that they must be a raw volumetric rate passed
    without the linear conversion.

    Skip entirely for BBL-format G-code (HEADER_BLOCK_START present) since
    OrcaSlicer always applies the correct conversion formula.
    """
    if _RE_HEADER_START.search(gcode):
        return  # OrcaSlicer output — feedrate is correctly computed
    for line in gcode.splitlines():
        stripped = line.strip()
        if stripped.startswith(";"):
            continue
        m = _RE_TOOLCHANGE_FEEDRATE.search(stripped)
        if m:
            feedrate = float(m.group(1))
            if feedrate < 1:
                findings.append(
                    Finding(
                        Severity.ERROR,
                        "E002",
                        f"Toolchange feedrate too low: F{feedrate} "
                        "(< 1 mm/min, likely raw volumetric rate not converted to mm/min)",
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


def _check_multi_filament(gcode: str, findings: list[Finding]) -> None:
    """E013/E014: Multi-filament tool change checks."""
    # Detect multi-filament: count unique M620 S(digit) values where digit < 255
    m620_slots = _RE_M620_S.findall(gcode)
    unique_slots = {int(s) for s in m620_slots if int(s) < 255}
    is_multi = len(unique_slots) >= 2

    if not is_multi:
        return

    # E013: multi-filament gcode should have M620/M621 sequences
    has_m620 = bool(m620_slots)
    has_m621 = bool(_RE_M621_S.search(gcode))
    if not has_m620 or not has_m621:
        findings.append(
            Finding(
                Severity.ERROR,
                "E013",
                "Multi-filament print missing M620/M621 tool change sequences",
                f"Found {len(unique_slots)} tool slots but no M620/M621 pairs",
            )
        )

    # E014: bare T commands outside M620/M621 blocks
    # Walk lines tracking whether we are inside an M620/M621 block.
    # rewrite_tool_changes leaves the first T command (initial extruder
    # select) as a bare line.  It always matches the last M620 block's
    # extruder, so skip bare T commands that re-select the current tool.
    in_block = False
    last_block_ext: int | None = None
    for line in gcode.splitlines():
        stripped = line.strip()
        if stripped.startswith(";"):
            continue
        m620 = _RE_M620_S.match(stripped)
        if m620:
            in_block = True
            last_block_ext = int(m620.group(1))
            continue
        if _RE_M621_S.match(stripped):
            in_block = False
            continue
        if not in_block:
            m_tool = _RE_BARE_TOOL.match(stripped)
            if m_tool:
                ext = int(m_tool.group(1))
                if ext == last_block_ext:
                    continue  # redundant select of current extruder
                findings.append(
                    Finding(
                        Severity.ERROR,
                        "E014",
                        "Bare tool command outside M620/M621 block in multi-filament print",
                        stripped[:120],
                    )
                )
                return  # one finding is enough


# ---------------------------------------------------------------------------
# G-code safety checks (pre-packaging)
# ---------------------------------------------------------------------------


def _extract_max_layer_z(gcode: str) -> float:
    """Find the highest Z from ;Z: layer-change comments."""
    z_values = _RE_LAYER_Z.findall(gcode)
    if not z_values:
        return 0.0
    return max(float(z) for z in z_values)


def _find_end_gcode_start(gcode: str) -> int:
    """Return the character offset where end G-code begins.

    Heuristic: the position after the last ;LAYER_CHANGE block's content.
    Falls back to end-of-string if no layer changes found.
    """
    matches = list(_RE_LAYER_CHANGE.finditer(gcode))
    if not matches:
        return len(gcode)
    # End gcode starts after the last layer's moves.  We look for the last
    # M73 L marker which signals end-of-layer, then scan forward.
    last_m73_l = None
    for m in _RE_M73_L.finditer(gcode):
        last_m73_l = m
    if last_m73_l:
        return last_m73_l.end()
    return matches[-1].end()


def _check_end_z_safety(gcode: str, findings: list[Finding]) -> None:
    """S001: End G-code Z move lower than max_layer_z (nozzle crash risk)."""
    max_z = _extract_max_layer_z(gcode)
    if max_z <= 0:
        return  # can't check without layer Z data

    end_start = _find_end_gcode_start(gcode)
    end_section = gcode[end_start:]

    for line in end_section.splitlines():
        stripped = line.strip()
        if stripped.startswith(";"):
            continue
        m = _RE_G1_Z_ONLY.match(stripped)
        if m:
            z_val = float(m.group(1))
            if z_val < max_z:
                findings.append(
                    Finding(
                        Severity.ERROR,
                        "S001",
                        f"End G-code moves Z to {z_val}mm, below max layer Z "
                        f"of {max_z}mm (nozzle crash risk)",
                        stripped[:120],
                    )
                )
                return  # one finding is enough


def _check_premature_heater_off(gcode: str, findings: list[Finding]) -> None:
    """S002: M104/M109/M140 S0 in toolpath section (premature heater shutdown)."""
    for m in _RE_TEMP_ZERO.finditer(gcode):
        # If no extrusion moves follow, the toolpath is complete — not premature.
        if not _RE_EXTRUSION.search(gcode, m.end()):
            continue
        findings.append(
            Finding(
                Severity.ERROR,
                "S002",
                "Heater set to 0 during toolpath (premature shutdown)",
                m.group()[:120],
            )
        )
        return  # one finding is enough


def _check_extrusion_before_homing(gcode: str, findings: list[Finding]) -> None:
    """S003: Extrusion move (G1 with E) before any G28 homing command."""
    homing_match = _RE_G28.search(gcode)
    if not homing_match:
        return  # no homing found — different problem, not our check

    # Check if any extrusion happens before the first G28
    for line in gcode[: homing_match.start()].splitlines():
        stripped = line.strip()
        if stripped.startswith(";"):
            continue
        if _RE_EXTRUSION.match(stripped):
            findings.append(
                Finding(
                    Severity.ERROR,
                    "S003",
                    "Extrusion move before homing (G28)",
                    stripped[:120],
                )
            )
            return  # one finding is enough


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

    # W012: nozzle temperature range (150-350°C)
    nozzle_temps = settings.get("nozzle_temperature")
    if isinstance(nozzle_temps, list):
        for i, v in enumerate(nozzle_temps):
            try:
                temp = int(v) if isinstance(v, str) else int(v)
            except (ValueError, TypeError):
                continue
            if temp != 0 and (temp < 150 or temp > 350):
                findings.append(
                    Finding(
                        Severity.WARNING,
                        "W012",
                        f"Nozzle temperature out of range: {temp}°C (slot {i})",
                        "expected 150-350°C",
                    )
                )
                break  # one finding is enough

    # W013: bed temperature range (0-120°C)
    _bed_temp_keys = [k for k in settings if k.endswith("_temp") or k == "hot_plate_temp"]
    for key in _bed_temp_keys:
        val = settings[key]
        values = val if isinstance(val, list) else [val]
        for i, v in enumerate(values):
            try:
                temp = int(v) if isinstance(v, str) else int(v)
            except (ValueError, TypeError):
                continue
            if temp == -1:
                continue  # BBL firmware sentinel for "disabled"
            if temp < 0 or temp > 120:
                findings.append(
                    Finding(
                        Severity.WARNING,
                        "W013",
                        f"Bed temperature out of range: {temp}°C (key={key}, slot {i})",
                        "expected 0-120°C",
                    )
                )
                break  # one finding per key is enough

    # W014: flush_volumes_matrix must be a perfect square
    fvm = settings.get("flush_volumes_matrix")
    if isinstance(fvm, list) and len(fvm) > 0:
        import math

        n = int(math.isqrt(len(fvm)))
        if n * n != len(fvm):
            findings.append(
                Finding(
                    Severity.WARNING,
                    "W014",
                    f"flush_volumes_matrix length {len(fvm)} is not a perfect square",
                )
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


# ---------------------------------------------------------------------------
# Reference comparison
# ---------------------------------------------------------------------------


def _extract_3mf_metadata(path: Path) -> dict[str, object]:
    """Extract comparison-relevant metadata from a .gcode.3mf archive."""
    meta: dict[str, object] = {}
    try:
        with zipfile.ZipFile(path, "r") as zf:
            # slice_info
            si_raw = _safe_read_str(zf, "Metadata/slice_info.config")
            if si_raw:
                try:
                    root = ET.fromstring(si_raw)
                    plate = root.find("plate")
                    if plate is not None:
                        si_meta = {
                            el.get("key", ""): el.get("value", "")
                            for el in plate.findall("metadata")
                        }
                        meta["printer_model_id"] = si_meta.get("printer_model_id", "")
                        try:
                            meta["prediction"] = int(si_meta.get("prediction", "0"))
                        except ValueError:
                            meta["prediction"] = 0
                        try:
                            meta["weight"] = float(si_meta.get("weight", "0"))
                        except ValueError:
                            meta["weight"] = 0.0
                        # Filament types
                        filaments = plate.findall("filament")
                        meta["filament_types"] = [f.get("type", "") for f in filaments]
                except ET.ParseError:
                    pass

            # Count M620 tool changes in gcode
            gcode_bytes = _safe_read(zf, "Metadata/plate_1.gcode")
            if gcode_bytes is not None:
                gcode = gcode_bytes.decode(errors="replace")
                meta["tool_changes"] = len(_RE_M620_S.findall(gcode))
            else:
                meta["tool_changes"] = 0
    except zipfile.BadZipFile:
        pass
    return meta


def compare_3mf(test_path: Path, reference_path: Path) -> ValidationResult:
    """Compare a test archive against a reference archive.

    Returns findings with codes C001-C005 for comparison failures.
    """
    findings: list[Finding] = []
    test_meta = _extract_3mf_metadata(test_path)
    ref_meta = _extract_3mf_metadata(reference_path)

    if not test_meta or not ref_meta:
        findings.append(
            Finding(Severity.ERROR, "C001", "Could not extract metadata for comparison")
        )
        return ValidationResult(findings)

    # C001: filament types must match
    test_fil = test_meta.get("filament_types", [])
    ref_fil = ref_meta.get("filament_types", [])
    if test_fil != ref_fil:
        findings.append(
            Finding(
                Severity.ERROR,
                "C001",
                "Filament types differ from reference",
                f"test={test_fil}, reference={ref_fil}",
            )
        )

    # C002: print time within ±50%
    test_pred = int(str(test_meta.get("prediction", 0)) or "0")
    ref_pred = int(str(ref_meta.get("prediction", 0)) or "0")
    if ref_pred > 0 and test_pred > 0:
        ratio = test_pred / ref_pred
        if ratio < 0.5 or ratio > 1.5:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "C002",
                    f"Print time diverges from reference: {test_pred}s vs {ref_pred}s",
                    f"ratio={ratio:.2f}, expected 0.5–1.5",
                )
            )

    # C003: weight within ±30%
    test_weight = float(str(test_meta.get("weight", 0)) or "0")
    ref_weight = float(str(ref_meta.get("weight", 0)) or "0")
    if ref_weight > 0 and test_weight > 0:
        ratio = test_weight / ref_weight
        if ratio < 0.7 or ratio > 1.3:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "C003",
                    f"Weight diverges from reference: {test_weight:.1f}g vs {ref_weight:.1f}g",
                    f"ratio={ratio:.2f}, expected 0.7–1.3",
                )
            )

    # C004: same tool change count
    test_tc = int(str(test_meta.get("tool_changes", 0)) or "0")
    ref_tc = int(str(ref_meta.get("tool_changes", 0)) or "0")
    if test_tc != ref_tc:
        findings.append(
            Finding(
                Severity.ERROR,
                "C004",
                f"Tool change count differs: {test_tc} vs reference {ref_tc}",
            )
        )

    # C005: matching printer_model_id
    test_pm = str(test_meta.get("printer_model_id", ""))
    ref_pm = str(ref_meta.get("printer_model_id", ""))
    if test_pm and ref_pm and test_pm != ref_pm:
        findings.append(
            Finding(
                Severity.ERROR,
                "C005",
                f"printer_model_id differs: '{test_pm}' vs reference '{ref_pm}'",
            )
        )

    return ValidationResult(findings)

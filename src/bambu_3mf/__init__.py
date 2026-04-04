"""Package plain G-code into Bambu Lab .gcode.3mf files."""

from bambu_3mf.assemble import assemble_gcode
from bambu_3mf.gcode_compat import is_bbl_gcode, translate_to_bbl
from bambu_3mf.pack import FilamentInfo, ObjectInfo, SliceInfo, WarningInfo, fixup_project_settings, pack_gcode_3mf

__all__ = [
    "assemble_gcode",
    "fixup_project_settings",
    "is_bbl_gcode",
    "pack_gcode_3mf",
    "translate_to_bbl",
    "FilamentInfo",
    "ObjectInfo",
    "SliceInfo",
    "WarningInfo",
]

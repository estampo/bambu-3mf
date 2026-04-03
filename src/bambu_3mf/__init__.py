"""Package plain G-code into Bambu Lab .gcode.3mf files."""

from bambu_3mf.assemble import assemble_gcode
from bambu_3mf.pack import FilamentInfo, ObjectInfo, SliceInfo, WarningInfo, fixup_project_settings, pack_gcode_3mf

__all__ = ["assemble_gcode", "fixup_project_settings", "pack_gcode_3mf", "SliceInfo", "FilamentInfo", "ObjectInfo", "WarningInfo"]

"""Tests for assemble.py — G-code component assembly."""

from __future__ import annotations

from bambox.assemble import assemble_gcode


class TestAssembleGcode:
    def test_basic_assembly(self):
        """Start + toolpath + end joined with newlines."""
        result = assemble_gcode("G28", "G1 X10 Y10 E0.5", "M84")
        assert result == "G28\nG1 X10 Y10 E0.5\nM84\n"

    def test_trailing_newlines_stripped(self):
        """Trailing newlines on each component should be stripped."""
        result = assemble_gcode("G28\n\n", "G1 X10\n", "M84\n\n\n")
        assert result == "G28\nG1 X10\nM84\n"

    def test_filament_start_inserted(self):
        """Filament start G-code appears between start and toolpath."""
        result = assemble_gcode("G28", "G1 X10 E0.5", "M84", filament_start_gcode="M104 S200")
        lines = result.splitlines()
        assert lines[0] == "G28"
        assert "; filament start gcode" in lines
        fil_idx = lines.index("; filament start gcode")
        assert lines[fil_idx + 1] == "M104 S200"
        # Toolpath comes after filament start
        assert lines.index("G1 X10 E0.5") > fil_idx

    def test_filament_end_inserted(self):
        """Filament end G-code appears between toolpath and end."""
        result = assemble_gcode("G28", "G1 X10 E0.5", "M84", filament_end_gcode="M104 S0")
        lines = result.splitlines()
        assert "; filament end gcode" in lines
        fil_idx = lines.index("; filament end gcode")
        assert lines[fil_idx + 1] == "M104 S0"
        # End comes after filament end
        assert lines.index("M84") > fil_idx

    def test_both_filament_sections(self):
        """Both filament start and end should be in correct positions."""
        result = assemble_gcode(
            "G28",
            "G1 X10 E0.5",
            "M84",
            filament_start_gcode="M104 S200",
            filament_end_gcode="M104 S0",
        )
        lines = result.splitlines()
        start_idx = lines.index("; filament start gcode")
        end_idx = lines.index("; filament end gcode")
        toolpath_idx = lines.index("G1 X10 E0.5")
        assert start_idx < toolpath_idx < end_idx

    def test_empty_components_omitted(self):
        """Empty strings should be omitted entirely."""
        result = assemble_gcode("", "G1 X10 E0.5", "")
        assert result == "G1 X10 E0.5\n"

    def test_all_empty(self):
        """All empty components should produce just a newline."""
        result = assemble_gcode("", "", "")
        assert result == "\n"

    def test_empty_filament_gcode_omitted(self):
        """Empty filament start/end should not insert comment markers."""
        result = assemble_gcode("G28", "G1 X10", "M84")
        assert "; filament start gcode" not in result
        assert "; filament end gcode" not in result

    def test_multiline_components(self):
        """Multi-line start/toolpath/end should work correctly."""
        start = "G28\nG29\nM104 S200"
        toolpath = "G1 X10 E0.5\nG1 X20 E0.5\nG1 X30 E0.5"
        end = "M104 S0\nM84"
        result = assemble_gcode(start, toolpath, end)
        lines = result.splitlines()
        assert lines[0] == "G28"
        assert lines[-1] == "M84"
        assert len(lines) == 8

    def test_result_ends_with_newline(self):
        """Output must always end with exactly one newline."""
        result = assemble_gcode("G28", "G1 X10", "M84")
        assert result.endswith("\n")
        assert not result.endswith("\n\n")

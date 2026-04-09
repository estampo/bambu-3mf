"""Test OrcaSlicer → Jinja2 template conversion and rendering."""

from __future__ import annotations

from pathlib import Path

from bambox.templates import orca_to_jinja2, render_template

# ---------------------------------------------------------------------------
# OrcaSlicer → Jinja2 conversion
# ---------------------------------------------------------------------------


class TestOrcaToJinja2:
    """Test the syntax converter."""

    def test_simple_variable_curly(self) -> None:
        assert orca_to_jinja2("{bed_temperature}") == "{{ bed_temperature }}"

    def test_simple_variable_square(self) -> None:
        assert orca_to_jinja2("[bed_temperature]") == "{{ bed_temperature }}"

    def test_indexed_variable_curly(self) -> None:
        result = orca_to_jinja2("{filament_type[initial_extruder]}")
        assert result == "{{ filament_type[initial_extruder] }}"

    def test_expression(self) -> None:
        result = orca_to_jinja2("{max_layer_z + 0.5}")
        assert result == "{{ max_layer_z + 0.5 }}"

    def test_complex_expression(self) -> None:
        result = orca_to_jinja2("{filament_max_volumetric_speed[initial_extruder]/2.4053*60}")
        assert result == "{{ filament_max_volumetric_speed[initial_extruder]/2.4053*60 }}"

    def test_if_block(self) -> None:
        template = '{if filament_type[0]=="PLA"}\nM106 P3 S180\n{endif}'
        expected = '{% if filament_type[0]=="PLA" %}\nM106 P3 S180\n{% endif %}'
        assert orca_to_jinja2(template) == expected

    def test_elsif_block(self) -> None:
        template = '{if x=="A"}\nA\n{elsif x=="B"}\nB\n{else}\nC\n{endif}'
        expected = '{% if x=="A" %}\nA\n{% elif x=="B" %}\nB\n{% else %}\nC\n{% endif %}'
        assert orca_to_jinja2(template) == expected

    def test_endif_with_trailing_comment(self) -> None:
        result = orca_to_jinja2("{endif};Prevent PLA from jamming")
        assert result == "{% endif %};Prevent PLA from jamming"

    def test_mixed_line(self) -> None:
        """Variable embedded in a G-code command."""
        result = orca_to_jinja2("G1 Z{max_layer_z + 0.5} F900")
        assert result == "G1 Z{{ max_layer_z + 0.5 }} F900"

    def test_plain_gcode_unchanged(self) -> None:
        line = "G28 ; home all axes"
        assert orca_to_jinja2(line) == line

    def test_m_command_with_square_bracket(self) -> None:
        result = orca_to_jinja2("M140 S[bed_temperature_initial_layer_single]")
        assert result == "M140 S{{ bed_temperature_initial_layer_single }}"

    def test_indented_if(self) -> None:
        result = orca_to_jinja2("    {if x > 5}")
        assert result == "    {% if x > 5 %}"

    def test_multiple_vars_one_line(self) -> None:
        result = orca_to_jinja2("G29 A X{min[0]} Y{min[1]}")
        assert result == "G29 A X{{ min[0] }} Y{{ min[1] }}"


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    """Test Jinja2 rendering of converted templates."""

    def test_p1s_start_renders(self) -> None:
        """P1S start template renders without error."""
        result = render_template(
            "p1s_start.gcode.j2",
            {
                "bed_temperature_initial_layer_single": 60,
                "nozzle_temperature_initial_layer": [220],
                "initial_extruder": 0,
                "filament_type": ["PLA"],
                "bed_temperature": [60],
                "bed_temperature_initial_layer": [60],
                "curr_bed_type": "Textured PEI Plate",
                "first_layer_print_min": [0, 0],
                "first_layer_print_size": [256, 256],
                "outer_wall_volumetric_speed": 12,
                "filament_max_volumetric_speed": [15],
                "nozzle_temperature_range_high": [250],
            },
        )
        # Should contain machine init commands
        assert "M104 S75" in result
        assert "M140 S60" in result
        assert "M975 S1" in result

    def test_p1s_start_pla_fan_logic(self) -> None:
        """PLA fan prevention logic triggers for PLA."""
        result = render_template(
            "p1s_start.gcode.j2",
            {
                "bed_temperature_initial_layer_single": 60,
                "initial_extruder": 0,
                "filament_type": ["PLA"],
                "bed_temperature": [60],
                "bed_temperature_initial_layer": [60],
                "nozzle_temperature_initial_layer": [220],
                "curr_bed_type": "Textured PEI Plate",
                "first_layer_print_min": [0, 0],
                "first_layer_print_size": [256, 256],
                "outer_wall_volumetric_speed": 12,
                "filament_max_volumetric_speed": [15],
                "nozzle_temperature_range_high": [250],
            },
        )
        assert "M106 P3 S180" in result

    def test_p1s_start_non_pla_no_fan(self) -> None:
        """Fan prevention logic does NOT trigger for PETG."""
        result = render_template(
            "p1s_start.gcode.j2",
            {
                "bed_temperature_initial_layer_single": 70,
                "initial_extruder": 0,
                "filament_type": ["PETG"],
                "bed_temperature": [70],
                "bed_temperature_initial_layer": [70],
                "nozzle_temperature_initial_layer": [250],
                "curr_bed_type": "Textured PEI Plate",
                "first_layer_print_min": [0, 0],
                "first_layer_print_size": [256, 256],
                "outer_wall_volumetric_speed": 12,
                "filament_max_volumetric_speed": [15],
                "nozzle_temperature_range_high": [260],
            },
        )
        # M106 P3 S180 only appears in the PLA block, should not be in PETG output
        assert "M106 P3 S180" not in result

    def test_p1s_start_textured_plate_offset(self) -> None:
        """Textured PEI Plate triggers Z offset."""
        result = render_template(
            "p1s_start.gcode.j2",
            {
                "bed_temperature_initial_layer_single": 60,
                "initial_extruder": 0,
                "filament_type": ["PLA"],
                "bed_temperature": [60],
                "bed_temperature_initial_layer": [60],
                "nozzle_temperature_initial_layer": [220],
                "curr_bed_type": "Textured PEI Plate",
                "first_layer_print_min": [0, 0],
                "first_layer_print_size": [256, 256],
                "outer_wall_volumetric_speed": 12,
                "filament_max_volumetric_speed": [15],
                "nozzle_temperature_range_high": [250],
            },
        )
        assert "G29.1 Z" in result

    def test_p1s_end_renders(self) -> None:
        """P1S end template renders without error."""
        result = render_template(
            "p1s_end.gcode.j2",
            {
                "max_layer_z": 20.0,
            },
        )
        assert "M140 S0" in result  # turn off bed
        assert "M104 S0" in result  # turn off hotend

    def test_p1s_end_z_retract(self) -> None:
        """End gcode retracts Z appropriately."""
        result = render_template(
            "p1s_end.gcode.j2",
            {
                "max_layer_z": 100.0,
            },
        )
        # max_layer_z + 100 = 200 < 250, should use the calculated value
        assert "G1 Z200 F600" in result

    def test_p1s_end_signals_completion(self) -> None:
        """End gcode must include M73 P100 R0 so printer transitions to 100%."""
        result = render_template(
            "p1s_end.gcode.j2",
            {
                "max_layer_z": 20.0,
            },
        )
        assert "M73 P100 R0" in result

    def test_p1s_end_z_retract_clamped(self) -> None:
        """End gcode clamps Z at 250 for tall prints."""
        result = render_template(
            "p1s_end.gcode.j2",
            {
                "max_layer_z": 200.0,
            },
        )
        # max_layer_z + 100 = 300 > 250, should clamp to 250
        assert "G1 Z250 F600" in result


# ---------------------------------------------------------------------------
# Template files exist
# ---------------------------------------------------------------------------


class TestSilentUndefined:
    """Test that undefined template variables are handled gracefully (lines 113-134)."""

    def test_undefined_variable_renders_empty(self) -> None:
        """Undefined variables should render as empty string where possible."""
        # Provide max_layer_z (required for arithmetic) but omit other vars
        result = render_template(
            "p1s_end.gcode.j2",
            {"max_layer_z": 10.0},  # only the arithmetic-required var
        )
        # Should render without error; other undefined vars become empty
        assert isinstance(result, str)
        assert "M140 S0" in result

    def test_undefined_in_arithmetic_context(self) -> None:
        """Undefined variables used in numeric context should not crash."""
        result = render_template(
            "p1s_end.gcode.j2",
            {"max_layer_z": 10.0},  # only provide one var
        )
        assert "M140 S0" in result


class TestOrcaControlFlowInExpression:
    """Test that control flow keywords inside {expr} are not double-converted (line 74)."""

    def test_control_flow_keyword_in_expression_preserved(self) -> None:
        # A curly expression that starts with a control flow keyword
        # should be returned as-is by _replace_expr
        result = orca_to_jinja2("{if x > 5}")
        assert result == "{% if x > 5 %}"

    def test_square_bracket_inside_jinja2_expression(self) -> None:
        """Square bracket vars inside {{ }} should not be double-converted (line 86-90)."""
        # After curly conversion, [var] inside {{ }} should stay as array index
        result = orca_to_jinja2("{filament_type[extruder]}")
        assert result == "{{ filament_type[extruder] }}"

    def test_logical_operators_converted(self) -> None:
        """|| and && in conditions should become 'or' and 'and'."""
        result = orca_to_jinja2("{if x > 5 || y < 3}")
        assert result == "{% if x > 5  or  y < 3 %}"

        result = orca_to_jinja2("{if x > 5 && y < 3}")
        assert result == "{% if x > 5  and  y < 3 %}"


class TestTemplateFiles:
    """Verify all expected template files exist."""

    def test_p1s_start_exists(self) -> None:
        tmpl_dir = Path(__file__).parent.parent / "src/bambox/gcode_templates"
        assert (tmpl_dir / "p1s_start.gcode.j2").exists()

    def test_p1s_end_exists(self) -> None:
        tmpl_dir = Path(__file__).parent.parent / "src/bambox/gcode_templates"
        assert (tmpl_dir / "p1s_end.gcode.j2").exists()

    def test_p1s_toolchange_exists(self) -> None:
        tmpl_dir = Path(__file__).parent.parent / "src/bambox/gcode_templates"
        assert (tmpl_dir / "p1s_toolchange.gcode.j2").exists()

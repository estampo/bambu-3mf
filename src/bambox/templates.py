"""Convert OrcaSlicer G-code templates to Jinja2 and render them."""

from __future__ import annotations

import re
from pathlib import Path

_TEMPLATE_DIR = Path(__file__).parent / "gcode_templates"


def orca_to_jinja2(template: str) -> str:
    """Convert an OrcaSlicer-syntax G-code template to Jinja2 syntax.

    OrcaSlicer uses a custom DSL with two variable syntaxes:
      - ``{variable}`` and ``{expression}`` — curly braces
      - ``[variable]`` — square brackets (simple substitution only)

    And control flow:
      - ``{if condition}`` / ``{elsif condition}`` / ``{else}`` / ``{endif}``

    This function translates to Jinja2 equivalents.
    """
    lines = template.split("\n")
    result: list[str] = []

    for line in lines:
        result.append(_convert_line(line))

    return "\n".join(result)


def _convert_condition(cond: str) -> str:
    """Convert OrcaSlicer condition syntax to Jinja2."""
    cond = cond.replace("||", " or ")
    cond = cond.replace("&&", " and ")
    return cond


def _convert_line(line: str) -> str:
    """Convert a single line from OrcaSlicer syntax to Jinja2."""
    # Control flow: {if ...}, {elsif ...}, {else}, {endif}
    # These occupy the entire token but may appear mid-line with leading whitespace
    stripped = line.strip()

    if stripped.startswith("{if ") and stripped.endswith("}"):
        cond = _convert_condition(stripped[4:-1])
        indent = line[: len(line) - len(line.lstrip())]
        return f"{indent}{{% if {cond} %}}"

    if stripped.startswith("{elsif ") and stripped.endswith("}"):
        cond = _convert_condition(stripped[7:-1])
        indent = line[: len(line) - len(line.lstrip())]
        return f"{indent}{{% elif {cond} %}}"

    if stripped == "{else}":
        indent = line[: len(line) - len(line.lstrip())]
        return f"{indent}{{% else %}}"

    if stripped == "{endif}" or stripped.startswith("{endif}"):
        indent = line[: len(line) - len(line.lstrip())]
        # Preserve trailing comment after {endif}
        rest = stripped[7:]  # after "{endif}"
        if rest:
            return f"{indent}{{% endif %}}{rest}"
        return f"{indent}{{% endif %}}"

    # First pass: convert curly-brace expressions {expr} → {{ expr }}
    # This must happen before square-bracket conversion because curly
    # expressions can contain square-bracket indexing like {var[idx]}.
    def _replace_expr(m: re.Match[str]) -> str:
        content = m.group(1)
        # Skip if this looks like control flow (already handled above)
        if content.startswith(("if ", "elsif ", "else", "endif")):
            return m.group(0)
        return "{{ " + content + " }}"

    line = re.sub(r"\{([^{}]+)\}", _replace_expr, line)

    # Second pass: convert standalone square-bracket variables [var] → {{ var }}
    # Only match identifiers NOT already inside {{ }} (i.e. not preceded by {{ )
    def _replace_square(m: re.Match[str]) -> str:
        start = m.start()
        # Check if we're inside a {{ ... }} block by looking back
        prefix = line[:start]
        # Count unmatched {{ before this position
        opens = prefix.count("{{ ")
        closes = prefix.count(" }}")
        if opens > closes:
            # Inside a Jinja2 expression — leave the square brackets alone
            return m.group(0)
        return "{{ " + m.group(1) + " }}"

    line = re.sub(r"\[([a-zA-Z_][a-zA-Z0-9_]*)\]", _replace_square, line)

    return line


def render_template(
    template_name: str,
    context: dict[str, object],
) -> str:
    """Render a bundled Jinja2 G-code template with the given context.

    Args:
        template_name: Template filename (e.g. "p1s_start.gcode.j2")
        context: Dict of slicer variables to substitute.

    Returns:
        Rendered G-code string.
    """
    try:
        from jinja2 import Environment, FileSystemLoader, Undefined
    except ImportError:
        raise ImportError(
            "Jinja2 is required for template rendering. Install with: pip install bambox[templates]"
        )

    class SilentUndefined(Undefined):
        """Return empty string for undefined variables instead of raising."""

        def _fail_with_undefined_error(self, *args: object, **kwargs: object) -> str:  # type: ignore[override]
            return ""

        def __str__(self) -> str:
            return ""

        def __int__(self) -> int:  # type: ignore[override]
            return 0

        def __float__(self) -> float:  # type: ignore[override]
            return 0.0

        def __bool__(self) -> bool:
            return False

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        undefined=SilentUndefined,
        keep_trailing_newline=True,
    )
    tmpl = env.get_template(template_name)
    return tmpl.render(context)

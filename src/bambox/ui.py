"""Rich UI helpers for bambox CLI commands.

Mirrors estampo's ``ui.py`` module for consistent TUI rendering across projects.
"""

from __future__ import annotations

from types import TracebackType

from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm, Prompt
from rich.status import Status
from rich.table import Table
from rich.theme import Theme

_THEME = Theme(
    {
        "info": "cyan",
        "success": "green",
        "warning": "yellow",
        "error": "red bold",
        "heading": "bold cyan",
        "dim": "dim",
    }
)

console = Console(highlight=False, theme=_THEME)
err_console = Console(stderr=True, highlight=False, theme=_THEME)


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------


def heading(text: str) -> None:
    """Print a section heading with a rule line."""
    console.rule(f"[heading]{text}[/heading]", style="dim")


def success(text: str) -> None:
    """Print a success line with green checkmark."""
    console.print(f"  [green]\u2714[/green] {text}", soft_wrap=True)


def warn(text: str) -> None:
    """Print a warning line."""
    err_console.print(f"  [yellow]\u26a0[/yellow] {text}", soft_wrap=True)


def error(text: str) -> None:
    """Print an error line."""
    err_console.print(f"  [red]\u2718[/red] {escape(text)}", soft_wrap=True)


def info(text: str) -> None:
    """Print an info line."""
    console.print(f"  [dim]{text}[/dim]", soft_wrap=True)


# ---------------------------------------------------------------------------
# Spinner / status
# ---------------------------------------------------------------------------

_active_status: Status | None = None


class _StatusContext:
    """Context manager that updates a running spinner or starts a new one.

    Only one Rich Status spinner can render at a time.  If a spinner is
    already active we update its message instead of creating a competing
    spinner that causes flicker.
    """

    def __init__(self, message: str) -> None:
        self._message = f"  {message}"
        self._owned: Status | None = None
        self._prev: str | None = None

    def __enter__(self) -> _StatusContext:
        global _active_status  # noqa: PLW0603
        if _active_status is not None:
            self._prev = str(_active_status.status)
            _active_status.update(self._message)
        else:
            self._owned = console.status(self._message, spinner="dots")
            self._owned.__enter__()
            _active_status = self._owned
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        global _active_status  # noqa: PLW0603
        if self._owned is not None:
            self._owned.__exit__(exc_type, exc_val, exc_tb)
            _active_status = None
        elif self._prev is not None and _active_status is not None:
            _active_status.update(self._prev)


def status(message: str) -> _StatusContext:
    """Return a context manager that shows a Rich Status spinner."""
    return _StatusContext(message)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def prompt_str(prompt: str, default: str | None = None) -> str:
    """Prompt for a string value with optional default."""
    return Prompt.ask(f"  {prompt}", default=default, console=console) or ""


def prompt_yn(prompt: str, default: bool = True) -> bool:
    """Prompt yes/no with a default."""
    return Confirm.ask(f"  {prompt}", default=default, console=console)


def prompt_password(prompt: str) -> str:
    """Prompt for a password (masked input)."""
    return Prompt.ask(f"  {prompt}", password=True, console=console) or ""


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def color_swatch(hex_color: str) -> str:
    """Return a Rich markup string for a colored swatch block.

    *hex_color* should be 6 hex digits (no leading ``#``).
    """
    hex_color = hex_color.lstrip("#").rstrip("F")[:6]
    if len(hex_color) < 6:
        hex_color = hex_color.ljust(6, "0")
    try:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    except ValueError:
        return "  "
    return f"[on rgb({r},{g},{b})]  [/on rgb({r},{g},{b})]"


def choice_table(
    items: list[list[str]],
    columns: list[str],
    *,
    markup: bool = False,
) -> None:
    """Print a numbered selection table."""
    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    table.add_column("#", style="dim", width=4)
    for col in columns:
        table.add_column(col)
    for i, row in enumerate(items, 1):
        cells = row if markup else [escape(c) for c in row]
        table.add_row(str(i), *cells)
    console.print(table)


# ---------------------------------------------------------------------------
# State colors (for status display)
# ---------------------------------------------------------------------------

_STATE_STYLES: dict[str, str] = {
    "IDLE": "dim",
    "RUNNING": "green",
    "PAUSE": "yellow",
    "FINISH": "blue",
    "FAILED": "red bold",
}


def format_state(state: str) -> str:
    """Return Rich-markup-styled printer state string."""
    style = _STATE_STYLES.get(state, "")
    if style:
        return f"[{style}]{escape(state)}[/{style}]"
    return escape(state)

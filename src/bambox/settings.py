"""Generate project_settings.config from machine base + filament profiles.

This module makes bambox slicer-agnostic: instead of requiring a 544-key
blob from OrcaSlicer, it generates the settings from a small machine base
profile and per-filament-type data files.

Usage::

    settings = build_project_settings(
        filaments=["PETG-CF"],      # filament types, one per AMS slot used
        machine="p1s",              # machine base profile
        overrides={"layer_height": "0.2"},  # optional scalar overrides
    )
"""

from __future__ import annotations

import json
from pathlib import Path

from bambox.pack import MIN_SLOTS, pad_to_slots

_DATA_DIR = Path(__file__).parent / "profiles"


def _load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def available_machines() -> list[str]:
    """List available machine base profiles."""
    return [p.stem.removeprefix("base_") for p in _DATA_DIR.glob("base_*.json")]


def available_filaments() -> list[str]:
    """List available filament type profiles."""
    return [
        p.stem.removeprefix("filament_").upper().replace("_", "-")
        for p in _DATA_DIR.glob("filament_*.json")
    ]


def _filament_profile_path(filament_type: str) -> Path:
    """Resolve a filament type name to its profile JSON path."""
    normalized = filament_type.lower().replace("-", "_")
    path = _DATA_DIR / f"filament_{normalized}.json"
    if not path.exists():
        avail = available_filaments()
        raise ValueError(f"Unknown filament type '{filament_type}'. Available: {avail}")
    return path


def _machine_profile_path(machine: str) -> Path:
    """Resolve a machine name to its base profile JSON path."""
    path = _DATA_DIR / f"base_{machine}.json"
    if not path.exists():
        avail = available_machines()
        raise ValueError(f"Unknown machine '{machine}'. Available: {avail}")
    return path


def build_project_settings(
    filaments: list[str],
    *,
    machine: str = "p1s",
    filament_colors: list[str] | None = None,
    filament_ids: list[str] | None = None,
    overrides: dict[str, str] | None = None,
    min_slots: int = MIN_SLOTS,
) -> dict[str, object]:
    """Build the full 544-key project_settings dict.

    Args:
        filaments: Filament type names, one per used slot (e.g. ``["PETG-CF"]``).
            Slots are padded to *min_slots* by repeating the last type.
        machine: Machine base profile name (default ``"p1s"``).
        filament_colors: Hex colors per slot (e.g. ``["#2850E0"]``).
            Padded with last value to *min_slots*. Defaults to ``"#F2754E"``.
        filament_ids: Bambu filament IDs per slot (e.g. ``["GFG98"]``).
            If not provided, taken from the filament profile data.
        overrides: Scalar key overrides applied last.
        min_slots: Minimum slot count for per-filament arrays (default 5).

    Returns:
        Dict with all 544 keys, arrays padded to *min_slots*.
    """
    # Load machine base
    base = _load_json(_machine_profile_path(machine))

    # Load varying keys list
    varying_keys: list[str] = list(_load_json(_DATA_DIR / "_varying_keys.json"))

    # Pad filament list to min_slots
    if not filaments:
        filaments = ["PLA"]
    filaments = pad_to_slots(filaments, min_slots)

    # Load filament profiles
    fil_profiles = [_load_json(_filament_profile_path(ft)) for ft in filaments]

    # Pad colors
    colors = pad_to_slots(list(filament_colors or ["#F2754E"]), min_slots)

    # Build the result: start with base scalars and uniform arrays
    result: dict[str, object] = {}

    for key, value in base.items():
        if isinstance(value, list):
            # Special arrays — keep as-is from base
            result[key] = value
        else:
            # Scalar or uniform-array default — broadcast to array of min_slots
            if key in _UNIFORM_ARRAY_KEYS:
                result[key] = [value] * min_slots
            else:
                result[key] = value

    # Build per-filament arrays from filament profiles
    for key in varying_keys:
        arr = []
        for i in range(min_slots):
            profile = fil_profiles[i]
            if key == "filament_colour":
                arr.append(colors[i])
            elif key in profile:
                arr.append(profile[key])
            else:
                # Fallback: use first filament's value
                arr.append(fil_profiles[0].get(key, ""))
        result[key] = arr

    # Inject filament_colour (always from colors arg, not profiles)
    result["filament_colour"] = colors

    # Override filament_ids if provided
    if filament_ids:
        result["filament_ids"] = pad_to_slots(list(filament_ids), min_slots)

    # Apply scalar overrides last
    if overrides:
        for key, value in overrides.items():
            result[key] = value

    return result


# Keys from the base profile that are stored as single values but must be
# broadcast to per-filament arrays in the output. Built from the reference:
# these are the 113 keys that had len-5 arrays with identical values.
_UNIFORM_ARRAY_KEYS: set[str] = set()


def _init_uniform_keys() -> None:
    """Load the uniform array key set from the base profile + reference."""
    global _UNIFORM_ARRAY_KEYS
    # All keys in the base that map to simple (non-list) values AND also appear
    # as 5-element arrays in the reference are uniform array keys.
    # We store this alongside the varying keys list.
    path = _DATA_DIR / "_uniform_array_keys.json"
    if path.exists():
        _UNIFORM_ARRAY_KEYS = set(_load_json(path))


_init_uniform_keys()

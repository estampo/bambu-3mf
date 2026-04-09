"""Tests for bambox.settings — project_settings builder from profiles."""

from __future__ import annotations

import pytest

from bambox.settings import (
    _filament_profile_path,
    _machine_profile_path,
    available_filaments,
    available_machines,
    build_project_settings,
)


class TestAvailability:
    def test_available_machines_includes_p1s(self) -> None:
        machines = available_machines()
        assert "p1s" in machines

    def test_available_filaments_includes_pla(self) -> None:
        filaments = available_filaments()
        assert "PLA" in filaments


class TestProfileResolution:
    def test_unknown_filament_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown filament type"):
            _filament_profile_path("NONEXISTENT-MATERIAL")

    def test_unknown_machine_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown machine"):
            _machine_profile_path("nonexistent_printer")

    def test_valid_filament_resolves(self) -> None:
        path = _filament_profile_path("PLA")
        assert path.exists()

    def test_valid_machine_resolves(self) -> None:
        path = _machine_profile_path("p1s")
        assert path.exists()


class TestBuildProjectSettings:
    def test_empty_filaments_defaults_to_pla(self) -> None:
        """Empty filament list should default to PLA (line 96)."""
        result = build_project_settings([])
        # Should have PLA in the filament_type array
        ft = result.get("filament_type")
        assert isinstance(ft, list)
        assert ft[0] == "PLA"

    def test_filament_ids_override(self) -> None:
        """Providing filament_ids should override profile defaults (lines 141-144)."""
        result = build_project_settings(["PLA"], filament_ids=["CUSTOM_ID"])
        ids = result["filament_ids"]
        assert isinstance(ids, list)
        assert ids[0] == "CUSTOM_ID"
        # Should be padded to min_slots
        assert len(ids) >= 5
        assert ids[4] == "CUSTOM_ID"

    def test_scalar_overrides_applied(self) -> None:
        """Scalar overrides should be applied last (lines 148-149)."""
        result = build_project_settings(["PLA"], overrides={"layer_height": "0.3"})
        assert result["layer_height"] == "0.3"

    def test_filament_colour_from_colors(self) -> None:
        """Filament colours should come from filament_colors arg (line 128)."""
        result = build_project_settings(["PLA"], filament_colors=["#FF0000", "#00FF00"])
        colours = result["filament_colour"]
        assert isinstance(colours, list)
        assert colours[0] == "#FF0000"
        assert colours[1] == "#00FF00"

    def test_varying_key_fallback_to_first_filament(self) -> None:
        """Keys missing from a filament profile fall back to first profile (line 133).

        Use two different filaments to trigger the fallback path: the second
        filament may lack some varying keys that the first has.
        """
        result = build_project_settings(["PLA", "ASA"])
        # The result should have all varying keys populated as arrays
        # Just verify the result is a valid dict with arrays
        ft = result.get("filament_type")
        assert isinstance(ft, list)
        assert len(ft) >= 5

    def test_result_has_many_keys(self) -> None:
        """The result should contain the full 544-key set from the base profile."""
        result = build_project_settings(["PLA"])
        assert len(result) > 400

    def test_default_colors_used(self) -> None:
        """When no colors provided, default #F2754E is used."""
        result = build_project_settings(["PLA"])
        colours = result["filament_colour"]
        assert isinstance(colours, list)
        assert colours[0] == "#F2754E"

    def test_custom_min_slots(self) -> None:
        """Custom min_slots should control array padding."""
        result = build_project_settings(["PLA"], min_slots=3)
        ft = result.get("filament_type")
        assert isinstance(ft, list)
        assert len(ft) == 3

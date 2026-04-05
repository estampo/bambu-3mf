# Changelog

All notable changes to bambox are documented here.
This changelog is managed by [towncrier](https://towncrier.readthedocs.io/).

<!-- towncrier release notes start -->

## 0.1.0 — 2026-03-15

Initial release of bambox as a standalone library.

### Features

- Core `.gcode.3mf` archive packaging with Bambu Connect compatibility
- Template-driven 544-key `project_settings.config` generation from JSON profiles
- Machine base profile for P1S with filament overlays (PLA, ASA, PETG-CF)
- Automatic array padding and missing-key fixup for Bambu Connect firmware
- Cloud printing via Docker bridge (`estampo/cloud-bridge`) with bind-mount and baked fallback
- AMS tray mapping and printer status querying
- OrcaSlicer-to-Jinja2 template syntax conversion and rendering
- G-code component assembly (start + toolpath + end templates)
- CuraEngine Docker slicer backend prototype
- Synthetic toolpath generator for testing
- G-code-to-PNG thumbnail rendering (top-down view with bounding box)
- CLI with `pack`, `print`, and `status` commands
- MD5 checksum validation matching Bambu Connect requirements
- Support for both OrcaSlicer 2.3.1 and BambuStudio 2.5.0.66 format versions

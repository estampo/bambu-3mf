"""Cloud printing via the Bambu cloud bridge.

Wraps the ``bambox-bridge`` binary (preferred) or falls back to the
``estampo/cloud-bridge`` Docker container for sending prints, querying status,
and managing AMS tray mapping.  No dependency on the estampo package — this
module is self-contained for standalone bambox usage.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)


def _xml_ns(root: ET.Element) -> str:
    """Return the default namespace prefix (e.g. '{http://...}') or empty string."""
    tag = root.tag
    if tag.startswith("{"):
        return tag[: tag.index("}") + 1]
    return ""


DOCKER_IMAGE = "estampo/cloud-bridge:bambu-02.05.00.00"

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def load_credentials(path: Path | None = None) -> dict[str, str]:
    """Load cloud credentials from a TOML file.

    Uses the credentials module for path resolution (bambox path first,
    estampo fallback).  If *path* is given explicitly, reads that file
    directly for backward compatibility.

    Returns dict with keys: token, refresh_token, email, uid.
    """
    if path is not None:
        # Explicit path — read directly (backward compat)
        if not path.exists():
            raise FileNotFoundError(f"Credentials file not found: {path}")
        with open(path, "rb") as f:
            raw = tomllib.load(f)
        cloud = raw.get("cloud")
        if not cloud or not cloud.get("token"):
            raise ValueError(f"No [cloud] credentials in {path}")
        return cloud

    from bambox.credentials import load_cloud_credentials

    cloud = load_cloud_credentials()
    if not cloud:
        raise ValueError("No cloud credentials found.\nRun 'bambox login' to log in.")
    return cloud


def _write_token_json(cloud: dict[str, str], directory: Path | None = None) -> Path:
    """Write a temp JSON token file for the bridge binary.

    Returns the path (caller must clean up).
    """
    bridge_data = {
        "token": cloud["token"],
        "refreshToken": cloud.get("refresh_token", ""),
        "email": cloud.get("email", ""),
        "uid": cloud.get("uid", ""),
    }
    if directory:
        d = directory
        d.mkdir(parents=True, exist_ok=True)
    else:
        from bambox.credentials import _cache_dir

        d = _cache_dir()
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="bambu_token_", dir=d, delete=False
    )
    json.dump(bridge_data, tmp)
    tmp.close()
    Path(tmp.name).chmod(0o600)
    return Path(tmp.name)


# ---------------------------------------------------------------------------
# Bridge runner — local binary first, then Docker fallback
# ---------------------------------------------------------------------------


def _find_local_bridge() -> str | None:
    """Return path to a local ``bambox-bridge`` binary, or *None*."""
    found = shutil.which("bambox-bridge")
    if found:
        return found
    # Check common install locations
    candidates = [
        Path.home() / ".local" / "bin" / "bambox-bridge",
        Path("/usr/local/bin/bambox-bridge"),
    ]
    for p in candidates:
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def _run_bridge(
    args: list[str],
    *,
    timeout: int = 300,
    verbose: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the cloud bridge, trying local binary first then Docker.

    1. Local ``bambox-bridge`` binary (no Docker overhead)
    2. Docker bind-mount mode
    3. Docker baked-image fallback (for sandboxed environments)
    """
    local = _find_local_bridge()
    if local:
        return _run_bridge_local(local, args, timeout=timeout, verbose=verbose)

    log.debug("No local bambox-bridge found, falling back to Docker")
    return _run_bridge_docker(args, timeout=timeout, verbose=verbose)


def _translate_args_for_rust_bridge(args: list[str]) -> list[str]:
    """Translate C++ bridge positional args to Rust bridge CLI shape.

    The C++ bridge uses positional token files:
      status <device_id> <token_file>
      cancel <device_id> <token_file>
      print <3mf> <device_id> <token_file> [--flags...]

    The Rust bridge uses ``-c <token_file>`` as a global flag:
      -c <token_file> status <device_id>
      -c <token_file> cancel <device_id>
      -c <token_file> print <3mf> <device_id> [--flags...]
    """
    if not args:
        return args

    subcmd = args[0]
    if subcmd in ("status", "cancel") and len(args) >= 3:
        # args: [subcmd, device_id, token_file]
        token_file = args[2]
        return ["-c", token_file, subcmd, args[1]] + args[3:]
    elif subcmd == "print" and len(args) >= 4:
        # args: [print, 3mf_path, device_id, token_file, --flags...]
        token_file = args[3]
        return ["-c", token_file, "print", args[1], args[2]] + args[4:]
    else:
        # Unknown shape — pass through unchanged
        return args


def _run_bridge_local(
    binary: str,
    args: list[str],
    *,
    timeout: int = 300,
    verbose: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the bridge via a local binary.

    Translates the C++-style positional arguments to the Rust bridge's
    ``-c/--credentials`` flag format automatically.
    """
    translated = _translate_args_for_rust_bridge(args)
    cmd = [binary]
    if verbose:
        cmd.append("-v")
    cmd.extend(translated)
    log.debug("Running (local): %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _run_bridge_docker(
    args: list[str],
    *,
    timeout: int = 300,
    verbose: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the cloud bridge via Docker.

    First tries bind-mount mode (``-v host:container``). If that fails because
    the container cannot read the mounted file (common in sandboxed environments
    where overlay filesystems break bind mounts), falls back to building a
    temporary Docker image that ``COPY``s the input files.
    """
    install_hint = (
        "Install the bridge: curl -fsSL "
        "https://github.com/estampo/bambox/releases/latest"
        "/download/install.sh | sh"
    )
    try:
        docker_info = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    except FileNotFoundError:
        raise RuntimeError(
            f"bambox-bridge not found and Docker is not installed.\n{install_hint}"
        ) from None
    if docker_info.returncode != 0:
        raise RuntimeError(f"bambox-bridge not found and Docker is not running.\n{install_hint}")

    # Pull image if not present (or if a newer version is available)
    subprocess.run(
        ["docker", "pull", "--quiet", DOCKER_IMAGE],
        capture_output=True,
        timeout=120,
    )

    # Collect local file paths for potential bake fallback
    file_args: dict[str, str] = {}  # host_path -> container_path
    cmd: list[str] = ["docker", "run", "--rm", "--platform", "linux/amd64"]
    docker_args: list[str] = []
    for arg in args:
        if os.path.exists(arg):
            real = os.path.realpath(arg)
            basename = os.path.basename(real)
            container_path = f"/input/{basename}"
            cmd.extend(["-v", f"{real}:{container_path}:ro"])
            docker_args.append(container_path)
            file_args[real] = container_path
        else:
            docker_args.append(arg)

    cmd.append(DOCKER_IMAGE)
    cmd.extend(docker_args)
    if verbose:
        cmd.append("-v")

    log.debug("Running (bind-mount): %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    # Detect bind-mount failure (file appears as dir or unreadable in container)
    if result.returncode != 0 and file_args and "cannot read" in result.stderr:
        log.info("Bind-mount failed, falling back to baked Docker image")
        return _run_bridge_baked(args, file_args, timeout=timeout, verbose=verbose)

    return result


def _run_bridge_baked(
    args: list[str],
    file_args: dict[str, str],
    *,
    timeout: int = 300,
    verbose: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Fallback: build a temp image with COPY instead of bind mounts."""
    import shutil

    tmpdir = Path(tempfile.mkdtemp(prefix="bambu_bridge_"))
    try:
        # Write Dockerfile
        lines = [f"FROM {DOCKER_IMAGE}"]
        for host_path, container_path in file_args.items():
            basename = os.path.basename(host_path)
            shutil.copy2(host_path, tmpdir / basename)
            lines.append(f"COPY {basename} {container_path}")
        (tmpdir / "Dockerfile").write_text("\n".join(lines) + "\n")

        tag = "bambox-bridge-tmp"
        build = subprocess.run(
            ["docker", "build", "-t", tag, "."],
            capture_output=True,
            text=True,
            cwd=str(tmpdir),
            timeout=60,
        )
        if build.returncode != 0:
            raise RuntimeError(f"Docker build failed: {build.stderr[:500]}")

        # Re-build args with container paths
        docker_args: list[str] = []
        for arg in args:
            real = os.path.realpath(arg) if os.path.exists(arg) else ""
            if real in file_args:
                docker_args.append(file_args[real])
            else:
                docker_args.append(arg)

        cmd = ["docker", "run", "--rm", "--platform", "linux/amd64"]
        cmd.append(tag)
        cmd.extend(docker_args)
        if verbose:
            cmd.append("-v")

        log.debug("Running (baked): %s", " ".join(cmd))
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Clean up temp image (best-effort)
        subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=10)


# ---------------------------------------------------------------------------
# AMS tray parsing and mapping
# ---------------------------------------------------------------------------


def parse_ams_trays(status: dict) -> list[dict]:
    """Extract physical AMS tray info from a printer status dict.

    Returns list of dicts with keys: phys_slot, ams_id, slot_id, type, color,
    tray_info_idx.
    """
    trays = []
    ams_data = status.get("ams", {})
    for unit in ams_data.get("ams", []):
        ams_id = int(unit.get("id", 0))
        for tray in unit.get("tray", []):
            slot_id = int(tray.get("id", 0))
            fil_type = tray.get("tray_type", "")
            if not fil_type:
                continue
            color_raw = tray.get("tray_color", "")
            color = color_raw[:6] if len(color_raw) >= 6 else color_raw
            trays.append(
                {
                    "phys_slot": ams_id * 4 + slot_id,
                    "ams_id": ams_id,
                    "slot_id": slot_id,
                    "type": fil_type,
                    "color": color,
                    "tray_info_idx": tray.get("tray_info_idx", ""),
                }
            )
    return trays


def _build_ams_mapping(
    threemf_path: Path,
    ams_trays: list[dict],
) -> dict[str, list]:
    """Build AMS mapping arrays from a 3MF file and live AMS tray state.

    Returns dict with amsMapping and amsMapping2 arrays.
    """
    result: dict[str, list] = {"amsMapping": [], "amsMapping2": []}

    try:
        with zipfile.ZipFile(threemf_path, "r") as z:
            total_slots = 0
            if "Metadata/project_settings.config" in z.namelist():
                ps = json.loads(z.read("Metadata/project_settings.config"))
                total_slots = len(ps.get("filament_colour", []))

            filament_by_id: dict[int, ET.Element] = {}
            if "Metadata/slice_info.config" in z.namelist():
                root = ET.fromstring(z.read("Metadata/slice_info.config"))
                ns = _xml_ns(root)
                plate_el = root.find(f"{ns}plate")
                if plate_el is not None:
                    for f in plate_el.findall(f"{ns}filament"):
                        fid = int(f.get("id", "1"))
                        filament_by_id[fid] = f
                    if not total_slots and filament_by_id:
                        total_slots = max(filament_by_id.keys())
    except (zipfile.BadZipFile, KeyError, ET.ParseError, json.JSONDecodeError) as e:
        log.warning("Failed to parse 3MF for AMS mapping: %s", e)
        return result

    if not filament_by_id:
        return result

    # Match virtual filament slots to physical AMS trays
    mapping = [-1] * total_slots
    used: set[int] = set()
    for filament_id in sorted(filament_by_id.keys()):
        f = filament_by_id[filament_id]
        fil_type = f.get("type", "")
        color = f.get("color", "").lstrip("#").upper()

        best = None
        if ams_trays:
            candidates = [
                (
                    (2 if t["type"] == fil_type else 0) + (1 if t["color"].upper() == color else 0),
                    t,
                )
                for t in ams_trays
                if t["phys_slot"] not in used
            ]
            candidates.sort(key=lambda x: -x[0])
            if candidates and candidates[0][0] > 0:
                best = candidates[0][1]

        idx = filament_id - 1
        if best:
            mapping[idx] = best["phys_slot"]
            used.add(best["phys_slot"])
        else:
            raise RuntimeError(
                f"Filament slot {filament_id} (type={fil_type}, color={color}) "
                f"has no matching AMS tray. Load the correct filament or use "
                f"--skip-ams-mapping to print without AMS."
            )

    mapping2 = []
    for slot in mapping:
        if slot >= 0:
            mapping2.append({"ams_id": slot // 4, "slot_id": slot % 4})
        else:
            mapping2.append({"ams_id": 255, "slot_id": 255})

    result["amsMapping"] = mapping
    result["amsMapping2"] = mapping2
    return result


def _strip_gcode_from_3mf(path: Path) -> bytes:
    """Create a config-only 3MF (no gcode, no images, no MD5)."""
    ALLOWED = {
        "[Content_Types].xml",
        "_rels/.rels",
        "Metadata/slice_info.config",
        "Metadata/model_settings.config",
        "Metadata/project_settings.config",
        "Metadata/_rels/model_settings.config.rels",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in zin.infolist():
            name = item.filename
            if name in ALLOWED or (name.startswith("Metadata/plate_") and name.endswith(".json")):
                zo.writestr(item, zin.read(name))
    return buf.getvalue()


def _patch_config_3mf_colors(
    config_bytes: bytes, source_3mf: Path, ams_trays: list[dict], mapping: list[int]
) -> bytes:
    """Patch filament colors in a config-only 3MF to match AMS tray colors."""
    tray_by_phys = {t["phys_slot"]: t for t in ams_trays}

    with zipfile.ZipFile(io.BytesIO(config_bytes), "r") as z:
        file_data = {name: z.read(name) for name in z.namelist()}

    if "Metadata/slice_info.config" not in file_data:
        return config_bytes

    root = ET.fromstring(file_data["Metadata/slice_info.config"])
    ns = _xml_ns(root)
    plate_el = root.find(f"{ns}plate")
    if plate_el is None:
        return config_bytes

    changed = False
    for f in plate_el.findall(f"{ns}filament"):
        fid = int(f.get("id", "1"))
        idx = fid - 1
        if idx < len(mapping):
            phys_slot = mapping[idx]
            tray = tray_by_phys.get(phys_slot)
            if tray and phys_slot >= 0:
                new_color = "#" + tray["color"]
                if f.get("color", "") != new_color:
                    f.set("color", new_color)
                    changed = True

    if not changed:
        return config_bytes

    file_data["Metadata/slice_info.config"] = ET.tostring(root, encoding="unicode").encode()

    # Also patch project_settings colors
    if "Metadata/project_settings.config" in file_data:
        try:
            ps = json.loads(file_data["Metadata/project_settings.config"])
            colours = list(ps.get("filament_colour", []))
            for f in plate_el.findall("filament"):
                fid = int(f.get("id", "1"))
                idx = fid - 1
                if idx < len(colours) and idx < len(mapping):
                    phys_slot = mapping[idx]
                    tray = tray_by_phys.get(phys_slot)
                    if tray and phys_slot >= 0:
                        colours[idx] = "#" + tray["color"]
            ps["filament_colour"] = colours
            file_data["Metadata/project_settings.config"] = json.dumps(ps).encode()
        except (json.JSONDecodeError, KeyError):
            pass

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in file_data.items():
            zout.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def query_status(
    device_id: str,
    token_file: Path,
    *,
    verbose: bool = False,
) -> dict:
    """Query live printer status via the bridge."""
    result = _run_bridge(
        ["status", device_id, str(token_file.resolve())],
        timeout=120,
        verbose=verbose,
    )
    try:
        data = json.loads(result.stdout.strip())
        return data.get("print", data)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Bridge returned non-JSON (exit {result.returncode}): "
            f"{result.stdout[:200]} | {result.stderr[:200]}"
        )


def cancel_print(
    device_id: str,
    credentials: dict[str, str] | None = None,
    credentials_path: Path | None = None,
    *,
    verbose: bool = False,
) -> dict:
    """Cancel the current print on a Bambu printer via cloud bridge.

    Either *credentials* (dict) or *credentials_path* (TOML file) must be given.

    Returns the bridge response dict.
    """
    if credentials is None:
        credentials = load_credentials(credentials_path)

    token_file = _write_token_json(credentials)
    try:
        result = _run_bridge(
            ["cancel", device_id, str(token_file.resolve())],
            timeout=120,
            verbose=verbose,
        )
    finally:
        try:
            token_file.unlink()
        except OSError:
            pass

    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Bridge returned non-JSON (exit {result.returncode}): "
            f"{result.stdout[:200]} | {result.stderr[:200]}"
        )


def cloud_print(
    threemf_path: Path,
    device_id: str,
    credentials: dict[str, str] | None = None,
    credentials_path: Path | None = None,
    *,
    project_name: str = "bambox",
    timeout: int = 180,
    verbose: bool = False,
    skip_ams_mapping: bool = False,
    ams_trays: list[dict] | None = None,
) -> dict:
    """Send a 3MF to a Bambu printer via cloud bridge.

    Either *credentials* (dict) or *credentials_path* (TOML file) must be given.
    Automatically queries printer AMS state and builds proper mapping unless
    *skip_ams_mapping* is True or *ams_trays* is provided.

    Args:
        ams_trays: Pre-queried AMS tray info (skips live status query if given).
            Each dict should have keys: phys_slot, type, color, tray_info_idx.

    Returns the bridge response dict.
    """
    if credentials is None:
        credentials = load_credentials(credentials_path)

    token_file = _write_token_json(credentials, directory=threemf_path.parent)
    try:
        return _cloud_print_impl(
            threemf_path,
            device_id,
            token_file,
            project_name=project_name,
            timeout=timeout,
            verbose=verbose,
            skip_ams_mapping=skip_ams_mapping,
            ams_trays=ams_trays or [],
        )
    finally:
        try:
            token_file.unlink()
        except OSError:
            pass


def _cloud_print_impl(
    threemf_path: Path,
    device_id: str,
    token_file: Path,
    *,
    project_name: str,
    timeout: int,
    verbose: bool,
    skip_ams_mapping: bool,
    ams_trays: list[dict],
) -> dict:
    """Internal print implementation with an already-written token file."""
    args = [
        "print",
        str(threemf_path.resolve()),
        device_id,
        str(token_file.resolve()),
        "--project",
        project_name,
        "--timeout",
        str(timeout),
    ]

    mapping: list[int] = []
    if not skip_ams_mapping:
        # Use provided AMS trays, or query live
        if not ams_trays:
            try:
                status = query_status(device_id, token_file, verbose=verbose)
                ams_trays = parse_ams_trays(status)
            except Exception:
                log.warning("Could not query AMS state", exc_info=True)
                ams_trays = []

        if ams_trays:
            log.info(
                "AMS trays: %s",
                [(t["phys_slot"], t["type"], t["color"]) for t in ams_trays],
            )
            ams_data = _build_ams_mapping(threemf_path, ams_trays)
            mapping = ams_data["amsMapping"]
            if any(v >= 0 for v in mapping):
                args.extend(["--ams-mapping", json.dumps(mapping)])
                log.info("AMS mapping: %s", mapping)
            raw2 = ams_data["amsMapping2"]
            if raw2:
                args.extend(["--ams-mapping2", json.dumps(raw2)])

    # Generate config-only 3MF
    config_bytes = _strip_gcode_from_3mf(threemf_path)
    if ams_trays and mapping:
        config_bytes = _patch_config_3mf_colors(config_bytes, threemf_path, ams_trays, mapping)

    config_path = threemf_path.parent / (threemf_path.stem + "_config.3mf")
    config_path.write_bytes(config_bytes)
    try:
        args.extend(["--config-3mf", str(config_path.resolve())])
        result = _run_bridge(args, timeout=timeout + 60, verbose=verbose)
    finally:
        try:
            config_path.unlink()
        except OSError:
            pass

    try:
        data = json.loads(result.stdout.strip())
        if result.stderr and data.get("result") not in ("success", "sent"):
            log.warning("Bridge stderr: %s", result.stderr.strip())
        # Attach AMS mapping info for CLI display
        if mapping:
            data["_ams_mapping"] = mapping
        if ams_trays:
            data["_ams_trays"] = ams_trays
        return data
    except json.JSONDecodeError:
        raise RuntimeError(
            f"Bridge returned non-JSON (exit {result.returncode}): "
            f"{result.stdout[:200]} | {result.stderr[:200]}"
        )

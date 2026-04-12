"""Load and manage printer credentials from credentials.toml."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


def _cache_dir() -> Path:
    """Return a writable cache directory for temporary token files.

    Resolution order:
    1. ``XDG_CACHE_HOME/bambox`` (if set)
    2. ``~/.cache/bambox`` (Unix) / ``AppData/Local/bambox/cache`` (Windows)
    3. ``tempfile.gettempdir()/bambox`` (fallback when home is not writable)
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        d = Path(xdg) / "bambox"
    elif sys.platform == "win32":
        d = Path.home() / "AppData" / "Local" / "bambox" / "cache"
    else:
        d = Path.home() / ".cache" / "bambox"

    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "bambox"
        fallback.mkdir(parents=True, exist_ok=True, mode=0o700)
        log.warning("Cannot create cache dir %s — using %s", d, fallback)
        return fallback


def mask_serial(serial: str) -> str:
    """Mask a printer serial, keeping only the last 4 characters visible."""
    if len(serial) <= 4:
        return serial
    return "*" * (len(serial) - 4) + serial[-4:]


def _credentials_path() -> Path:
    """Return the path to the credentials file.

    Resolution order:
    1. ``BAMBOX_CREDENTIALS`` env var
    2. ``ESTAMPO_CREDENTIALS`` env var (backward compat)
    3. ``~/.config/bambox/credentials.toml`` (if exists)
    4. ``~/.config/estampo/credentials.toml`` (fallback for reading)
    5. ``~/Library/Application Support/bambox/credentials.toml`` (macOS only)
    6. ``~/Library/Application Support/estampo/credentials.toml`` (macOS only)
    7. ``~/.config/bambox/credentials.toml`` (default for new installs)
    """
    env = os.environ.get("BAMBOX_CREDENTIALS")
    if env:
        return Path(env)
    env = os.environ.get("ESTAMPO_CREDENTIALS")
    if env:
        return Path(env)

    if sys.platform == "win32":
        bambox_path = Path.home() / "AppData/Roaming/bambox/credentials.toml"
        estampo_path = Path.home() / "AppData/Roaming/estampo/credentials.toml"
    else:
        bambox_path = Path.home() / ".config/bambox/credentials.toml"
        estampo_path = Path.home() / ".config/estampo/credentials.toml"

    candidates = [bambox_path, estampo_path]

    # On macOS, also check ~/Library/Application Support/ (platform-native config dir)
    # to match the Rust bridge's search behavior via the `dirs` crate.
    if sys.platform == "darwin":
        lib_dir = Path.home() / "Library" / "Application Support"
        candidates.append(lib_dir / "bambox" / "credentials.toml")
        candidates.append(lib_dir / "estampo" / "credentials.toml")

    for path in candidates:
        if path.exists():
            return path
    # Default for new installs
    return bambox_path


def _load_raw() -> dict:
    """Load the raw credentials TOML, or return empty dict if not found."""
    path = _credentials_path()
    if not path.exists():
        return {}
    import tomllib

    with open(path, "rb") as f:
        return tomllib.load(f)


def _escape_toml_value(val: str) -> str:
    """Escape a string for use as a TOML basic string value."""
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _quote_toml_key(key: str) -> str:
    """Quote a TOML key if it contains characters that require quoting."""
    if key.isidentifier() and key.isascii():
        return key
    return '"' + _escape_toml_value(key) + '"'


def _write_credentials(data: dict) -> None:
    """Write credentials dict to TOML file with 0o600 permissions.

    Uses ``os.open()`` with explicit mode so the file is never world-readable,
    even briefly.  Manual TOML writer (tomllib is read-only, no tomli_w
    dependency).
    """
    path = _credentials_path()
    if sys.platform != "win32":
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            _write_credentials_toml(f, data)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            _write_credentials_toml(f, data)


def _write_credentials_toml(f, data: dict) -> None:  # noqa: ANN001
    """Write credentials data as TOML to an open file handle."""
    cloud = data.get("cloud", {})
    if cloud:
        f.write("[cloud]\n")
        for key, val in cloud.items():
            f.write(f'{key} = "{_escape_toml_value(str(val))}"\n')
        f.write("\n")

    for printer_name, creds in data.get("printers", {}).items():
        f.write(f"[printers.{_quote_toml_key(printer_name)}]\n")
        for key, val in creds.items():
            f.write(f'{key} = "{_escape_toml_value(str(val))}"\n')
        f.write("\n")


def load_cloud_credentials() -> dict[str, str] | None:
    """Load cloud credentials from the [cloud] section.

    Returns dict with token, refresh_token, email, uid, or None if not set.
    """
    raw = _load_raw()
    cloud = raw.get("cloud")
    if not cloud or not cloud.get("token"):
        return None
    return cloud


def save_cloud_credentials(
    token: str, refresh_token: str, email: str, uid: str, **extra: str
) -> None:
    """Save cloud credentials to the [cloud] section of credentials.toml."""
    raw = _load_raw()
    raw["cloud"] = {
        "token": token,
        "refresh_token": refresh_token,
        "email": email,
        "uid": uid,
        **extra,
    }
    _write_credentials(raw)


def list_printers() -> dict[str, dict[str, str]]:
    """Return all configured printers from credentials.toml.

    Returns dict mapping printer name -> credentials dict (including 'type').
    """
    raw = _load_raw()
    return raw.get("printers", {})


def save_printer(name: str, entry: dict[str, str]) -> None:
    """Save a printer entry to credentials.toml."""
    raw = _load_raw()
    if "printers" not in raw:
        raw["printers"] = {}
    raw["printers"][name] = entry
    _write_credentials(raw)


def load_printer_credentials(name: str) -> dict[str, str]:
    """Load credentials for a named printer.

    Environment variable overrides: BAMBU_SERIAL.
    """
    raw = _load_raw()
    if not raw:
        raise RuntimeError(
            f"Credentials file not found: {_credentials_path()}\nRun 'bambox login' to create it."
        )
    printers = raw.get("printers", {})
    if name not in printers:
        available = list(printers.keys())
        raise RuntimeError(
            f"Printer '{name}' not found in {_credentials_path()}. Available: {available}"
        )
    creds = dict(printers[name])

    # Env var override for serial
    env_serial = os.environ.get("BAMBU_SERIAL")
    if env_serial:
        creds["serial"] = env_serial

    return creds


def write_token_json(cloud: dict[str, str], directory: Path | None = None) -> Path:
    """Write a temp JSON token file for the bridge binary.

    Uses ``mkstemp`` + ``fchmod`` so the file is created with 0o600 from the
    start, avoiding any window where credentials are world-readable.

    Returns the path (caller must clean up).
    """
    bridge_data = {
        "token": cloud["token"],
        "refreshToken": cloud.get("refresh_token", ""),
        "email": cloud.get("email", ""),
        "uid": cloud.get("uid", ""),
    }
    d = directory or _cache_dir()
    if directory:
        d.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".json", prefix="bambu_token_", dir=str(d))
    try:
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(bridge_data, f)
    except BaseException:
        os.close(fd)
        os.unlink(path)
        raise
    return Path(path)


@contextmanager
def cloud_token_json():
    """Context manager that yields a temp JSON file path for the bridge binary.

    The bridge binary expects a JSON file with token, refreshToken, email, uid fields.
    Creates a temp file from credentials.toml [cloud] data, cleans up on exit.
    """
    cloud = load_cloud_credentials()
    if not cloud:
        raise RuntimeError("No cloud credentials found.\nRun 'bambox login' to log in.")

    tmp_path = write_token_json(cloud)
    try:
        yield tmp_path
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

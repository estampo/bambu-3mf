"""Bambu Cloud authentication — login, token caching, and device discovery."""

from __future__ import annotations

import getpass
import json
import logging

from bambox.pack import BAMBU_STUDIO_VERSION

log = logging.getLogger(__name__)

API_BASE = "https://api.bambulab.com"

SLICER_HEADERS = {
    "X-BBL-Client-Name": "OrcaSlicer",
    "X-BBL-Client-Type": "slicer",
    "X-BBL-Client-Version": BAMBU_STUDIO_VERSION,
    "User-Agent": f"bambu_network_agent/{BAMBU_STUDIO_VERSION}",
    "Content-Type": "application/json",
}


def _request_verification_code(email: str) -> None:
    """Request a verification code be sent to the user's email."""
    import requests

    resp = requests.post(
        f"{API_BASE}/v1/user-service/user/sendemail/code",
        headers=SLICER_HEADERS,
        json={"email": email, "type": "codeLogin"},
    )
    resp.raise_for_status()
    print(f"  Verification code sent to {email}")


def _login(email: str, password: str) -> tuple[str, str]:
    """Login and return (access_token, refresh_token). Handles all auth flows."""

    import requests

    # Step 1: Try password login
    print("  Logging in...")
    resp = requests.post(
        f"{API_BASE}/v1/user-service/user/login",
        headers=SLICER_HEADERS,
        json={"account": email, "password": password, "apiError": ""},
    )
    resp.raise_for_status()
    data = resp.json()

    token = data.get("accessToken")
    refresh_token = data.get("refreshToken", "")
    login_type = data.get("loginType", "")

    # Step 2: Handle verification code flow
    if not token and login_type == "verifyCode":
        _request_verification_code(email)
        code = getpass.getpass("  Enter verification code: ")
        resp = requests.post(
            f"{API_BASE}/v1/user-service/user/login",
            headers=SLICER_HEADERS,
            json={"account": email, "code": code},
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("accessToken")
        refresh_token = data.get("refreshToken", "")

    # Step 3: Handle TFA flow
    if not token and data.get("tfaKey"):
        tfa_key = data["tfaKey"]
        print("  Account requires two-factor authentication.")
        tfa_code = getpass.getpass("  Enter 2FA code: ")
        resp = requests.post(
            f"{API_BASE}/v1/user-service/user/tfa",
            headers=SLICER_HEADERS,
            json={"tfaKey": tfa_key, "tfaCode": tfa_code},
        )
        resp.raise_for_status()
        data = resp.json()
        token = data.get("accessToken")
        refresh_token = data.get("refreshToken", "")

    if not token:
        log.debug("Login response: %s", json.dumps(data, indent=2))
        raise RuntimeError("Login failed — no access token in response")

    return token, refresh_token


def _get_user_profile(token: str) -> dict:
    """Fetch user profile (uid, name, avatar)."""
    import requests

    resp = requests.get(
        f"{API_BASE}/v1/design-user-service/my/preference",
        headers={**SLICER_HEADERS, "Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "uid": str(data.get("uid", "")),
        "name": data.get("name", ""),
        "avatar": data.get("avatar", ""),
    }


def _get_devices(token: str) -> list[dict]:
    """List printers bound to the account."""
    import requests

    resp = requests.get(
        f"{API_BASE}/v1/iot-service/api/user/bind",
        headers={**SLICER_HEADERS, "Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json().get("devices", [])

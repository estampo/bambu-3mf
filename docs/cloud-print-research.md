# Bambu Lab Cloud Print API Research

> Consolidated from `estampo/docs/cloud-experiments/` and ongoing bambox bridge
> development. This is the canonical location for all Bambu cloud protocol
> documentation. The estampo copy is frozen and points here.

## Overview

Bambu Lab cloud printing involves two distinct protocol layers:

1. **REST API** — project creation, S3 file upload, task dispatch
2. **MQTT** — real-time printer communication (status, commands, signing)

Both layers have been fully reverse-engineered. The critical constraint is
**X.509 command signing**: since January 2025 firmware, cloud MQTT commands
(print, pause, resume, stop) must be RSA-SHA256 signed with a per-installation
certificate. The `pushing` command family (pushall, get_accessories) is exempt
from signing.

### Working Solutions

| Approach | Status | Limitations |
|----------|--------|-------------|
| SDK bridge (`libbambu_networking.so` via Rust FFI) | **Active** — `bridge/` | `send_message` returns -2 for signed commands (see `sdk-send-message-investigation.md`) |
| SDK `start_print()` | **Working** | Full cloud print flow including signing; but no cancel/pause/resume |
| Pure Python HTTP (steps 1-8) | **Partial** | Task created (200) but printer rejects unsigned MQTT command |
| Pure Python MQTT + X.509 signing | **Planned** | Requires implementing signing ourselves (see solution path below) |
| LAN mode (FTPS + local MQTT) | **Working** | No signing needed; requires same network + Developer Mode |

---

## REST API

### Base URL

`https://api.bambulab.com`

### Authentication

Three login flows, determined by server response:

**1. Direct password:**
```
POST /v1/user-service/user/login
Body: {"account": "user@email.com", "password": "...", "apiError": ""}
Response: {"accessToken": "eyJ..."}
```

**2. Email verification code** (when response has `"loginType": "verifyCode"`):
```
POST /v1/user-service/user/sendemail/code
Body: {"email": "user@email.com", "type": "codeLogin"}

POST /v1/user-service/user/login
Body: {"account": "user@email.com", "code": "123456"}
```

**3. Two-factor auth** (when response has `"tfaKey": "..."`):
```
POST /v1/user-service/user/tfa
Body: {"tfaKey": "...", "tfaCode": "123456"}
```

### Required HTTP Headers

**For POST /my/task — BambuConnect headers required:**
```
Content-Type: application/json
Authorization: Bearer <token>
x-bbl-client-name: BambuConnect
x-bbl-client-type: connect
x-bbl-client-version: v2.2.1-beta.2
x-bbl-device-id: <unique UUID>
x-bbl-language: en-GB
```

**For all other endpoints — slicer headers work:**
```
Content-Type: application/json
Authorization: Bearer <token>
X-BBL-Client-Type: slicer
X-BBL-Client-Name: BambuStudio
X-BBL-Client-Version: 02.05.01.52
X-BBL-OS-Type: linux
X-BBL-OS-Version: 6.8.0
X-BBL-Device-ID: <any unique hex string>
X-BBL-Language: en
```

**For the SDK (`set_extra_http_header`)** — must use slicer headers, NOT
connect headers. Using `bambu_connect`/`device` causes POST /my/task to return
403.

### Endpoints

| Method | Endpoint | Purpose |
|--------|----------|---------|
| POST | `/v1/user-service/user/login` | Login |
| GET | `/v1/design-user-service/my/preference` | Get user ID (uid) |
| GET | `/v1/iot-service/api/user/bind` | List bound devices |
| POST | `/v1/iot-service/api/user/project` | Create project |
| GET | `/v1/iot-service/api/user/project/{id}` | Project detail (poll for profile URL) |
| PATCH | `/v1/iot-service/api/user/project/{id}` | Update project |
| GET | `/v1/iot-service/api/user/upload` | Get signed S3 upload URLs |
| PUT | (presigned S3 URL) | Upload file (NO Content-Type header!) |
| PUT | `/v1/iot-service/api/user/notification` | Notify upload complete |
| GET | `/v1/iot-service/api/user/notification` | Poll upload status |
| GET | `/v1/iot-service/api/user/print` | Device status + access code |
| GET | `/v1/user-service/my/tasks` | List print tasks |
| POST | `/v1/user-service/my/task` | Create print task |

### Cloud Print Flow (8 steps)

Captured from BambuConnect v2.2.1 TLS traffic (March 2026):

1. **Create project** — `POST /project` with `{"name": "file.3mf"}`
   Returns: `project_id`, `model_id`, `profile_id`, `upload_url`, `upload_ticket`

2. **Upload config-only 3MF** — `PUT upload_url` (no Content-Type header)
   Config 3MF = metadata only (slice_info, plate JSON, project_settings — no
   gcode, no geometry, no images)

3. **Notify** — `PUT /notification` with
   `{"action": "upload", "upload": {"ticket": "...", "origin_file_name": "connect_config.3mf"}}`

4. **Poll** — `GET /notification?action=upload&ticket=...` until `message != "running"`
   Required — proceeding early causes PATCH to fail with "Wrong file format"

5. **Get gcode upload URL** — `GET /upload?model_id=X&profile_id=Y&project_id=Z&filename=file.gcode.3mf&md5=<MD5>&size=<size>`

6. **Upload full gcode.3mf** — `PUT <gcode_upload_url>` (no Content-Type header)

7. **PATCH project** — `PATCH /project/{id}` with:
   ```json
   {"profile_id": "<string>",
    "profile_print_3mf": [{"comments": "no_ips", "md5": "<GCODE_3MF_MD5>", "plate_idx": 1, "url": "<gcode_upload_url>"}]}
   ```

8. **Create task** — `POST /my/task` with BambuConnect headers
   ```json
   {"deviceId": "...", "modelId": "...", "profileId": 123456789,
    "plateIndex": 1, "title": "file.3mf", "cover": "", "mode": "cloud_file"}
   ```
   Note: `profileId` must be integer (project creation returns it as string)

### Task Creation Details

**Required fields** (server returns error if missing):
- `deviceId` (string), `modelId` (string), `profileId` (int), `plateIndex` (int),
  `title` (string), `cover` (string)
- `mode` (string) — must be `"cloud_file"` (empty 400 with no error message if missing)

**Header requirement for HTTP-created models:**

| Model source | Headers | Result |
|---|---|---|
| Library-created model | any | 200 OK |
| HTTP-created model | BambuConnect (`x-bbl-client-type: connect`) | 200 OK |
| HTTP-created model | slicer (`x-bbl-client-type: slicer`) | 400 empty |

### S3 URL Format Conversion

API returns path-style URLs; MQTT commands need virtual-hosted dualstack:

```
Path-style:     https://s3.us-west-2.amazonaws.com/bucket/key?params
Dualstack:      https://bucket.s3.dualstack.us-west-2.amazonaws.com/key?params
```

---

## MQTT Protocol

### Cloud MQTT

- **Broker:** `us.mqtt.bambulab.com:8883` (TLS)
- **Username:** `u_{uid}` (uid from `/my/preference`)
- **Password:** Access token
- **Publish:** `device/{device_id}/request`
- **Subscribe:** `device/{device_id}/report`

### LAN MQTT

- **Broker:** `{printer_ip}:8883` (self-signed TLS)
- **Username:** `bblp`
- **Password:** Access code (from GET /user/print)
- **No signing required**

### Commands

**pushall (no signing required):**
```json
{"pushing": {"sequence_id": "1", "command": "pushall", "version": 1, "push_target": 1}}
```

**Print commands (signing required on cloud MQTT):**
```json
{"print": {"sequence_id": "1", "command": "stop", "param": ""}}
{"print": {"sequence_id": "1", "command": "pause", "param": ""}}
{"print": {"sequence_id": "1", "command": "resume", "param": ""}}
```

**project_file (cloud variant, signing required):**
```json
{"print": {"sequence_id": "1", "command": "project_file",
  "param": "Metadata/plate_1.gcode",
  "project_id": "...", "profile_id": "...", "task_id": "...",
  "subtask_id": "0", "subtask_name": "file.3mf",
  "url": "<dualstack S3 URL>", "md5": "...",
  "bed_type": "auto", "use_ams": true, ...}}
```

### Status Messages (Printer → Client)

```json
{"print": {"command": "project_file", "result": "ok",
  "mc_percent": 45, "gcode_state": "RUNNING",
  "upload": {"status": "idle", "progress": 0}}}
```

States: `IDLE`, `RUNNING`, `PAUSED`, `FAILED`, `FINISH`

---

## X.509 Command Signing

Since January 2025 firmware, cloud MQTT commands under the `"print"` key
must be RSA-SHA256 signed. The `"pushing"` key is exempt.

### Signed Message Structure

```json
{
  "print": { ... command payload ... },
  "header": {
    "sign_ver": "v1.0",
    "sign_alg": "RSA_SHA256",
    "sign_string": "<base64 RSA-SHA256 signature>",
    "cert_id": "<md5-fingerprint>CN=<serial>.bambulab.com",
    "payload_len": <byte length of command JSON without header>
  }
}
```

### Signing Process

1. Serialize the command dict (without `header`) to JSON bytes
2. Sign with RSA-SHA256 (PKCS1v15 padding) using the installation's private key
3. Base64-encode the signature
4. Compute `payload_len` = byte length of the JSON from step 1
5. Construct `cert_id` = `{md5_fingerprint}CN={serial}.bambulab.com`
6. Add `header` object alongside the command

### Certificate Architecture

Each Bambu account has an account-level intermediate CA:
- CN: `GLOF{serial}.bambulab.com`
- Signed by `application_root.bambulab.com` → `BBL CA`

Each installation (BambuStudio, BambuConnect, library instance) gets its own
leaf certificate:
- CN: `GLOF{serial}-{installation_id}`
- RSA 2048-bit, ~18 month validity
- Signed by the account-level CA

### Certificate Acquisition

```
GET /v1/iot-service/api/user/applications/{appToken}/cert?aes256={encrypted}
```

Flow:
1. Library generates random AES-256 key
2. RSA-encrypts it with server's embedded public key
3. Sends as `aes256` URL parameter
4. Server generates RSA-2048 keypair, signs cert with account CA
5. AES-256-encrypts private key, returns cert chain + encrypted key
6. Library decrypts with its AES key, stores in `BambuNetworkEngine.conf`

### Cert Registration

The printer broadcasts registered certs via MQTT:
```json
{"command": "app_cert_list",
 "cert_ids": ["<fingerprint>CN=<serial>.bambulab.com", ...]}
```

### Per-Installation Keys

The January 2025 "leaked" Bambu Connect key is NOT a global key — it was
from one specific installation. Each installation has its own RSA keypair.
The publicly-extracted key returns 403 against the current API.

---

## SDK Integration Notes

### Critical Init Sequence

Order matters — getting it wrong causes SSL errors, auth failures, or MQTT
disconnects:

```
 1. setenv("CURL_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt")
 2. create_agent("/tmp/bambu_agent/log")
 3. init_log()
 4. set_config_dir("/tmp/bambu_agent/config")
 5. set_cert_file("/tmp/bambu_agent/cert", "slicer_base64.cer")
 6. set_country_code("US")
 7. start()
 8. set_extra_http_header({7 slicer headers})    ← AFTER start()
 9. Set all callbacks
10. change_user(user_json)                       ← BEFORE connect_server
11. connect_server()  → wait for server_connected callback rc=0
12. set_user_selected_machine(device_id)
13. start_subscribe("device")
14. sleep(3s)  ← subscription must establish
15. send_message(pushall)  → wait ~20s for enc flag
16. start_print(params, callbacks)
```

### Token JSON Format

`change_user()` expects:
```json
{"data":{"token":"...","refresh_token":"...","expires_in":"7200",
 "refresh_expires_in":"2592000","user":{"uid":"...","name":"...",
 "account":"...","avatar":"..."}}}
```

### send_message Signatures

Two versions exist in the SDK:
- `send_message(agent, dev_id, json, qos)` — 4 params
- `send_message_to_printer(agent, dev_id, json, qos, flag)` — 5 params, `flag` uses `MessageFlag` enum

`MessageFlag` values: `MSG_FLAG_NONE=0`, `MSG_SIGN=1<<0`, `MSG_ENCRYPT=1<<1`

**Critical limitation:** Both return -2 for `{"print":...}` commands in headless
mode. See `docs/sdk-send-message-investigation.md` for details.

### Error Codes

| Code | Meaning | Solution |
|------|---------|----------|
| -3140 | ENC flag not ready | Send pushall, wait 20s, retry |
| -3120 | POST task failed (403) | Fix headers + CA bundle |
| -3070 | File not found | Use `.3mf` extension |
| -3010 | SSL verification failed | Set CURL_CA_BUNDLE |
| -2 | JSON content rejected | SDK gates `print` commands; see investigation doc |
| -1 | Generic error | Printer busy or timeout |
| 0 | Success | — |

### Known Gotchas

1. **stdout noise** — SDK prints `use_count = 4` from background threads.
   Redirect stdout via `dup2()` during SDK calls.
2. **Process hang on exit** — `destroy_agent()` blocks on MQTT threads. Use
   `_exit()` or signal-based termination.
3. **Pushall timing** — Must wait 3s after `start_subscribe()`, then ~20s
   after pushall for the encryption flag.
4. **Cert file** — `slicer_base64.cer` is a DigiCert cert for MQTT TLS,
   from BambuStudio's GitHub repo.

---

## LAN Mode

### FTPS Upload

- **Protocol:** Implicit TLS (port 990)
- **Credentials:** `bblp` / access code
- **TLS:** Self-signed cert (`ssl.CERT_NONE`)
- **Upload:** `STOR {filename}` to SD card root
- Requires custom `FTP_TLS` subclass (implicit TLS, not `AUTH TLS`)

### LAN MQTT Commands

Same JSON format as cloud, but:
- `project_id`, `profile_id`, `task_id` all set to `"0"`
- `url` uses `ftp://filename.3mf` format
- No signing required

---

## Private Key Extraction Attempts

Nine different approaches were tried to extract the SDK's per-installation
private key. All failed. Summary:

| # | Approach | Result |
|---|----------|--------|
| 1 | PEM/DER memory scan | 200 certs found, zero private keys |
| 2 | LD_PRELOAD OpenSSL hook | Library statically links OpenSSL |
| 3 | External process `/proc/PID/mem` | Permission denied (ptrace_scope=1) |
| 4 | Library export functions | No key getter exists |
| 5 | BIGNUM scan via Frida | All RSA structures have `d=NULL` |
| 6 | Binary disassembly | VMProtect-style obfuscation |
| 7 | mitmproxy HTTPS interception | Captured signing headers but not key |
| 8 | Frida RSA structure tracing | Custom internal format, not OpenSSL |
| 9 | BambuConnect macOS CDP hooks | V8 bytecode captures refs at startup |

**Conclusion:** The private key is stored in a custom obfuscated format, never
in standard OpenSSL RSA structures or PEM/DER encoding. The library uses its
own crypto implementation. Key extraction is not feasible.

### Library Anti-Debug Protections

- All networking statically linked (VMProtect-obfuscated)
- Anti-debug checks: `/proc/self/status` TracerPid, ptrace(TRACEME)
- All exports use anti-debug trampolines → obfuscated implementations
- Encrypted config: `BambuNetworkEngine.conf` (688 bytes, 7.66 bits/byte entropy)

---

## Solution Path: Pure Python X.509 Signing

Since the SDK's `send_message` cannot deliver signed commands (returns -2), and
the private key cannot be extracted, the solution is to **acquire our own
certificate** and sign commands ourselves.

### Approach

1. **Certificate acquisition** — Call the cert API endpoint directly:
   ```
   GET /v1/iot-service/api/user/applications/{appToken}/cert?aes256={encrypted}
   ```
   Generate our own AES-256 key, RSA-encrypt it with the server's public key,
   receive and decrypt our own cert + private key.

2. **MQTT connection** — Connect to `us.mqtt.bambulab.com:8883` directly using
   `paho-mqtt`, bypassing the SDK entirely for command delivery.

3. **Command signing** — Sign `{"print":...}` payloads with RSA-SHA256 using
   our private key, construct the `header` block, publish to
   `device/{device_id}/request`.

4. **Status** — Continue using the SDK bridge for status/subscription (it works
   fine for `pushing` messages). Only bypass it for signed commands.

### Open Questions

- Server's RSA public key for encrypting the AES key — embedded in the library
  binary; needs extraction (simpler than private key extraction since it's a
  public key)
- Whether `appToken` in the cert endpoint can be any valid token or needs a
  specific format
- Certificate validity period and rotation requirements

---

## External References

- [OpenBambuAPI](https://github.com/Doridian/OpenBambuAPI) — cloud-http.md, cloud-x509-auth.md
- [BambuStudio source](https://github.com/bambulab/BambuStudio)
- [Bambu Connect key extraction (Hackaday, Jan 2025)](https://hackaday.com/2025/01/19/bambu-connects-authentication-x-509-certificate-and-private-key-extracted/)
- [ha-bambulab](https://github.com/greghesp/ha-bambulab) — closed wontfix on cloud command signing
- [coelacant1/Bambu-Lab-Cloud-API](https://github.com/coelacant1/Bambu-Lab-Cloud-API)

---

## History

This document was originally maintained as `estampo/docs/cloud-experiments/cloud-print-research.md`
(~1800 lines). Consolidated into bambox (April 2026) as part of the bridge
migration (ADR-002). The estampo copy is frozen and cross-references this file.

Key milestones:
- **Feb 2026** — Initial HTTP API reverse-engineering, 32 task fields discovered
- **Feb 2026** — C++ bridge working (cloud print via SDK `start_print`)
- **Feb–Mar 2026** — 9 private key extraction attempts, all failed
- **Mar 2026** — mitmproxy capture of signing headers and cert download flow
- **Mar 2026** — Pure Python HTTP flow (8 steps) working except signing
- **Apr 2026** — Rust bridge replaces C++ bridge (ADR-002)
- **Apr 2026** — SDK `send_message` investigation: -2 for print commands (see investigation doc)

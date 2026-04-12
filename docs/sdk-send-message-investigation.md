# SDK `send_message` Investigation

**Date:** 2026-04-12
**Status:** Root cause identified; workaround planned
**Context:** Cancel/pause/resume commands for cloud-connected printers

## Problem

The Rust bridge (`bridge/`) wraps `libbambu_networking.so` via FFI. The SDK's
`send_message` function works for `{"pushing":...}` payloads (returns 0) but
returns **-2** for all `{"print":...}` payloads (stop, pause, resume,
project_file).

This means the bridge can query printer status but cannot send any control
commands over cloud MQTT.

## Root Cause

The SDK inspects the JSON content of messages before sending. Messages are
routed through different internal code paths based on the top-level key:

- `{"pushing": ...}` — passes through directly to MQTT, no signing required
- `{"print": ...}` — enters an internal signing code path that requires X.509
  certificate state

In headless mode (no GUI, no `start_print()` flow), the SDK lacks the
certificate/signing state needed for the `print` code path, and returns -2
immediately.

### Definitive Proof

During testing, the following sequence was executed on the same agent, same
device, same MQTT session:

```
subscribe_and_pushall("DEVICE_ID")  → pushall returns 0 ✅
[400ms later]
send_message("DEVICE_ID", '{"print":{"command":"stop","param":"","sequence_id":"1"}}', qos=1)  → returns -2 ❌
```

Same function, same device, same QoS, same MQTT connection — only the JSON
content differs. The SDK gates on JSON content, not connection state.

### BambuStudio Confirmation

In BambuStudio source (`src/slic3r/GUI/DeviceManager.cpp`), `command_task_abort`
sends stop commands via `publish_json(j, 1)` which calls
`send_message_to_printer(dev_id, json, qos, flag)` — the 5-parameter variant
with a `MessageFlag` argument.

`MessageFlag` enum (`bambu_networking.hpp`):
- `MSG_FLAG_NONE = 0`
- `MSG_SIGN = 1 << 0`
- `MSG_ENCRYPT = 1 << 1`

The SDK's `send_message` (4-param) and `send_message_to_printer` (5-param) are
thin wrappers around the same `.so` function pointer. All JSON inspection
happens inside the closed-source binary.

## Approaches Tried

All approaches were tested against a real P1S printer with an active MQTT
session and confirmed subscription.

### 1. Basic send_message (QoS 0 and 1)

```rust
send_message(dev_id, '{"print":{"command":"stop",...}}', qos=0)  → -2
send_message(dev_id, '{"print":{"command":"stop",...}}', qos=1)  → -2
```

### 2. send_message_to_printer with MSG_SIGN flag

Added the 5-parameter FFI binding with `flag=1` (MSG_SIGN):

```rust
send_message_to_printer(dev_id, json, qos=1, flag=1)  → -2
```

The SDK still rejects it — the signing state is not initialized in headless mode.

### 3. Shim auto-retry with flag

Modified `bambu_shim_send_message` in `shim.cpp` to try flag=1 first, fall
back to flag=0:

```cpp
int ret = fp_send_msg(agent, dev_id, json, qos, 1);  // try signed
if (ret != 0) {
    ret = fp_send_msg(agent, dev_id, json, qos, 0);  // try unsigned
}
```

Both paths return -2 for print commands.

### 4. Subscribe before send

Called `start_subscribe("device")` and waited for `printer_connected` callback
before sending. No effect — -2 is returned regardless of subscription state.

### 5. add_subscribe with device list

Added `bambu_network_add_subscribe(dev_list)` (the per-device subscription
function BambuStudio uses) before sending. No effect.

### 6. Inline with pushall

Stashed the command and sent it immediately after a successful pushall in the
same `subscribe_and_pushall` flow:

```rust
// Inside subscribe_and_pushall, after pushall returns 0:
send_message(dev_id, pending_command, qos=1)  → -2
```

The pushall succeeds (returns 0) but the print command fails 400ms later.

### 7. refresh_connection before send

Called `bambu_network_refresh_connection()` before `send_message`. No effect.

### 8. enable_multi_machine

Called `bambu_network_enable_multi_machine(1)` before the flow. No effect.

### 9. Direct MQTT bypass (paho)

Connected directly to `us.mqtt.bambulab.com:8883` with `paho-mqtt` and
published an unsigned stop command:

```json
{"print": {"sequence_id": "1", "command": "stop", "param": ""}}
```

The message was delivered to the printer (confirmed by MQTT traffic), but the
printer rejected it with:
```
"MQTT Command verification failed"
```

This confirms the firmware-level signing requirement.

## What Works vs What Doesn't

| Payload | send_message | send_message_to_printer | Direct MQTT |
|---------|-------------|------------------------|-------------|
| `{"pushing":{"command":"pushall",...}}` | 0 ✅ | 0 ✅ | Works (no signing) |
| `{"print":{"command":"stop",...}}` | -2 ❌ | -2 ❌ | Delivered but rejected |
| `{"print":{"command":"pause",...}}` | -2 ❌ | -2 ❌ | Delivered but rejected |
| `{"print":{"command":"resume",...}}` | -2 ❌ | -2 ❌ | Delivered but rejected |
| `{"print":{"command":"project_file",...}}` | -2 ❌ | -4 ❌ | Delivered but rejected |

Note: `start_print()` works because it uses an internal code path that bypasses
`send_message` entirely and handles the full signing flow internally.

## Solution

The SDK's `send_message` cannot be used for signed commands. The solution is to
implement X.509 certificate-based signing in Python/Rust and publish directly
to cloud MQTT, bypassing the SDK for command delivery.

See `docs/cloud-print-research.md` § "X.509 Command Signing" and § "Solution
Path: Pure Python X.509 Signing" for the implementation plan.

### Hybrid Architecture

```
Status/subscription:  SDK bridge (works fine for pushing messages)
Signed commands:      Direct MQTT with X.509 signing (bypass SDK)
```

This avoids rewriting the entire MQTT connection management while solving the
command signing gap.

## Experimental Code State

The `bridge/` directory has uncommitted experimental changes from this
investigation. These are diagnostic — not intended for merge:

- `shim/shim.cpp` — Added `add_subscribe`, `enable_multi_machine`,
  `refresh_connection` wrappers; modified `send_message` to try flag=1
- `src/ffi.rs` — Added corresponding FFI declarations
- `src/agent.rs` — Added `pending_command` mechanism for inline sends
- `src/callbacks.rs` — Added `pending_command` / `pending_command_result` fields

These changes should be reverted or cleaned up once the direct MQTT signing
approach is implemented.

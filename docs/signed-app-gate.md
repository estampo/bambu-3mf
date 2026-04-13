# The Signed-App Gate

**Date:** 2026-04-13
**Status:** Root cause definitively identified. Cancel via CLI disabled pending a viable workaround.
**Supersedes (partially):** [`sdk-send-message-investigation.md`](./sdk-send-message-investigation.md) — the earlier "missing cert/signing state" theory is wrong; the real mechanism is described here.

## TL;DR

`libbambu_networking` refuses to emit signed `{"print":...}` MQTT commands
(stop / pause / resume / start / skip_objects / …) when the hosting process
is not an officially signed BambuStudio binary. This is a hard, in-process
gate; no combination of init calls, callbacks, cert installs, or JSON
variations can bypass it. The bridge will never clear it as long as it is
its own process.

Non-restricted operations (`pushall`, subscribe, status reading, message
receive) continue to work fine — that is why the bridge is useful for
observing printers and why it cannot control them.

## Definitive evidence

When the bridge calls `bambu_shim_send_message` with a `{"print":{"command":
"stop",…}}` payload, two things happen within ~1 millisecond, locally, with
no network round trip:

1. `send_message` returns **-2**.
2. The SDK invokes the registered message callback with `dev_id = ""` and
   `msg = "unsigned_studio"`.

The string `unsigned_studio` is handled in BambuStudio itself at
[`src/slic3r/GUI/GUI_App.cpp:5401`](https://github.com/bambulab/BambuStudio/blob/master/src/slic3r/GUI/GUI_App.cpp),
which displays the dialog:

> *Your software is not signed, and some printing functions have been
> restricted. Please use the officially signed software version.*

This is the SDK's own polite notification that it has classified the host
process as an unofficial build.

Reproduced with trace logging of `on_message`:

```
mqtt message dev_id="01P00A4516..." len=4645 head="{\"print\":{\"upgrade_state\"..."
mqtt message dev_id=""             len=15   head="unsigned_studio"
send_message (cloud) ret=-2
```

Observed on macOS with `libbambu_networking.dylib` commit `1e34738` (2026-03-27).

## How the gate actually works

There are two layers of signing stacked on top of each other. Confusing
them was what kept this investigation going in circles for weeks.

### Layer 1 — host binary identity check

Every time `libbambu_networking` is asked to send a print-class command, it
checks *who is hosting it* via the operating system's code-signing APIs
(on macOS: `SecCodeCopySelf` / `SecCodeCopySigningInformation`). The check
almost certainly verifies some combination of Team Identifier
(Bambu Lab's Apple Developer team), bundle identifier
(`com.bambulab.BambuStudio`), and/or CDHash against an allow-list baked into
the .dylib. This check is a property of the *process*, not of any runtime
state we can configure.

The bridge binary is signed with an ad-hoc local identity (or unsigned), so
it fails this check instantly — before any of our JSON ever reaches a
code path that would consider sending it.

### Layer 2 — per-command MQTT signing

The printers themselves reject unsigned `{"print":...}` commands at the
firmware level (since the January 2025 firmware update). The actual wire
format is an MQTT publish containing the JSON *plus* an RSA-SHA256 signature
and cert chain, validated by the printer against a trust root Bambu holds.

The signing private key lives inside the `.dylib`, encrypted at rest. Its
decryption key is derived (at least in part) from Layer 1 attributes —
the OS signature of the host process is almost certainly one of the inputs
to the key-derivation function. When Layer 1 fails, the Layer 2 key is
either never decrypted or the signing code short-circuits before using it,
and `send_message` returns -2 immediately.

## Why every workaround tried so far was a dead end

The preceding investigation (see `sdk-send-message-investigation.md`) tried
nine variants of init ordering, callback registration, cert functions,
HTTP headers, config-dir reuse, and retry logic. None of them moved the
needle because none of them affects the binary-identity check at Layer 1.

Specifically, these changes did **not** and **cannot** help:

- Calling `bambu_network_update_cert` / `bambu_network_install_device_cert`
  — these install the *printer's* cert so the SDK can verify messages
  *from* the printer, not the cert used to sign commands *to* it.
- Registering `bambu_network_set_queue_on_main_fn` — confirmed via fprintf
  debug logging that the `.so` never actually invokes the callback during
  the relevant code paths.
- Reordering init to match BambuStudio's sequence (config_dir → init_log →
  set_cert_file → headers → callbacks → set_country_code → start).
- Implementing an incrementing `sequence_id` counter matching BambuStudio's
  `STUDIO_START_SEQ_ID = 20000`.
- Copying BambuStudio's `BambuNetworkEngine.conf` (the bridge's own is 688
  bytes, BambuStudio's is 720 — the extra 32 bytes are an encrypted blob
  that the `.so` only writes when Layer 1 passes, so overwriting it from
  outside doesn't restore it).
- Any QoS or JSON-field variation (tested both qos=0 and qos=1 for the
  stop payload; both return -2, so it's the JSON *content* not the QoS
  level that's being inspected).

## What still works

The bridge remains useful for everything that isn't a `{"print":...}`
command:

- `pushall` for status snapshots
- MQTT subscription / message receive (including rich push notifications)
- Login, connect, cert refresh
- `start_print` has its own SDK path for uploading and starting a cloud
  print, which interacts with layer 1 differently; that path's status
  under the signed-app gate has not been fully re-verified in light of
  these findings and is out of scope for this note.

## Remaining avenues (none currently implemented)

None of these is implemented and none is without significant caveats.
They are listed roughly in order of "cleanest legitimate path" to
"technically possible but risky."

1. **LAN-mode path with access code.** `bambu_network_send_message_to_printer`
   talks directly to the printer's own MQTT broker using the
   printer-supplied access code as credential. This may not go through the
   Layer 2 signing path at all, since LAN MQTT is end-to-end between the
   client and the printer. Bridge tests so far also return -2 on this path,
   but it is unclear whether that's the same gate or a different failure
   (missing access code, wrong init, LAN mode disabled on the printer).
   Worth a dedicated experiment with a printer in LAN-enabled mode and a
   real access code.

2. **Drive BambuStudio externally.** UI-automate / AppleScript /
   accessibility API the official app to click Stop. Reliable but ugly.
   The hosting process is signed, so it passes Layer 1 trivially.

3. **Bambu Connect as an RPC proxy.** Bambu Connect is an Electron app,
   so its shell is a signed Bambu binary and its JavaScript runs in a V8
   context that can be attached to with Chrome DevTools (via `SIGUSR1` on
   the main process, or `--remote-debugging-port` on the renderer). Once
   attached, the signing function inside the app can in principle be
   called directly from the REPL — the app is signed, so its calls to the
   SDK pass Layer 1. The app.asar is obfuscated but runtime hooks
   (`crypto.sign` wrapping, heap snapshots, event-listener breakpoints on
   the Stop button) sidestep the obfuscation entirely because they
   operate on live objects, not source text.

4. **OrcaSlicer as an oracle.** OrcaSlicer is a community fork that
   successfully talks to Bambu printers through `libbambu_networking` (or
   a fork of it). If OrcaSlicer sends print-class commands successfully
   on Linux or macOS, whatever its hosting process does to clear Layer 1
   is directly inspectable. Worth thirty minutes of grepping its repo
   before any further work on this front.

5. **Reverse the key derivation and sign commands directly.** Extract
   the signing key material from the `.dylib`, replicate the
   canonicalisation, and sign MQTT payloads from Rust, bypassing the
   SDK entirely. Technically possible but reverses a proprietary DRM
   mechanism; research exceptions may apply under DMCA §1201 / Article 6
   EUCD but commercial use or redistribution is legally fraught.

## Status of CLI `cancel` as of this commit

The `cancel` subcommand in `bridge/src/main.rs` is disabled. Invoking it
returns a structured JSON error referencing this document:

```json
{
  "command": "stop",
  "device_id": "...",
  "result": "error",
  "error": "cancel is disabled: libbambu_networking rejects print commands from unsigned hosts (see docs/signed-app-gate.md)"
}
```

The command enum and argument parsing are intentionally kept in place so
that re-enabling the command in a future commit is a small revert once a
viable workaround is implemented. The daemon-mode cancel endpoint in
`server.rs` is unchanged by this commit because it also performs
in-flight upload cancellation, which has value independent of the MQTT
stop send.

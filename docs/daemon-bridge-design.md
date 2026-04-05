# Daemon Bridge Design

## Problem

The current bridge architecture spins up a fresh Docker container for every
operation (status, print, cancel). Each container must:

1. Load `libbambu_networking.so`
2. Authenticate with Bambu Cloud
3. Connect to MQTT broker
4. Subscribe to device topics
5. Wait for data (~5-20 seconds)
6. Do one thing
7. Exit

This is slow (~20s per status query), unreliable (timeouts, unconfirmed
prints), and wastes resources. Bind-mount failures in sandboxed environments
add a second failure mode (baked image fallback adds build time).

## Solution: Persistent Bridge Daemon

Run the bridge container once as a long-lived daemon. Expose a local HTTP API.
Python clients talk to `http://localhost:{port}` instead of `docker run`.

```
┌─────────────────────────────────────────────────────┐
│  Docker container (estampo/cloud-bridge-daemon)     │
│                                                     │
│  ┌──────────┐   ┌──────────┐   ┌────────────────┐  │
│  │ HTTP API │──▶│ Bridge   │──▶│ Bambu Cloud    │  │
│  │ (Python) │   │ Binary   │   │ MQTT + REST    │  │
│  │ :8765    │   │ (watch)  │   │                │  │
│  └──────────┘   └──────────┘   └────────────────┘  │
│       ▲                                             │
└───────│─────────────────────────────────────────────┘
        │ HTTP
┌───────│──────────┐
│  Python client   │
│  (bambox /    │
│   estampo)       │
└──────────────────┘
```

### HTTP API

```
POST   /auth                     # Provide credentials, start MQTT session
GET    /status/{device_id}       # Instant — already subscribed
GET    /ams/{device_id}          # AMS tray info from cached status
POST   /print                    # Upload 3MF, start print job
POST   /cancel/{device_id}       # Cancel current print
GET    /health                   # Daemon health check
```

### Key design decisions

**Files sent via HTTP POST, not bind mounts.** The 3MF is uploaded in the
request body. This eliminates all bind-mount/overlay filesystem issues.

**MQTT connection persists across requests.** Status queries return cached
data (refreshed by MQTT push). No 20-second wait.

**Container lifecycle managed by Docker.** Start with `docker run -d`,
stop with `docker stop`. Restart policy handles crashes.

**Credentials loaded once at startup.** Token JSON passed as environment
variable or mounted once. No per-request token file creation.

## Implementation Plan

### Phase 1: HTTP wrapper inside the existing Docker image

Add a small Python HTTP server (`daemon.py`) to the cloud-bridge Docker
image. It wraps the existing bridge binary's `watch` subcommand.

```
docker/bridge-daemon/
├── Dockerfile           # FROM estampo/cloud-bridge:bambu-02.05.00.00
├── daemon.py            # HTTP server wrapping bridge binary
└── requirements.txt     # None (stdlib only — http.server + subprocess)
```

The daemon:
1. Reads credentials from `/config/credentials.json` (mounted volume)
2. Starts the bridge binary in `watch` mode as a subprocess
3. Waits for `{"ready": true}` handshake
4. Serves HTTP on port 8765
5. Translates HTTP requests to stdin commands / one-shot bridge calls
6. Returns JSON responses

**Status flow:**
```
GET /status/SERIAL → daemon sends "status\n" to bridge stdin
                   → reads JSON from stdout → returns to client
```

**Print flow:**
```
POST /print
  Body: multipart (3mf file + device_id + project_name + ams_mapping)
  → daemon writes 3mf to /tmp
  → runs bridge `print` subcommand (one-shot, but inside same container)
  → returns result JSON
```

### Phase 2: Python client in bambox

New module `bambox/daemon.py`:

```python
class BridgeDaemon:
    """Client for the bridge daemon HTTP API."""

    def __init__(self, url="http://localhost:8765"):
        self.url = url

    def status(self, device_id: str) -> dict: ...
    def ams_trays(self, device_id: str) -> list[dict]: ...
    def print(self, threemf: Path, device_id: str, **kwargs) -> dict: ...
    def cancel(self, device_id: str) -> dict: ...
    def health(self) -> dict: ...
```

Update `bridge.py` to try daemon first, fall back to one-shot Docker:

```python
def cloud_print(...):
    try:
        daemon = BridgeDaemon()
        if daemon.health():
            return daemon.print(threemf_path, device_id, ...)
    except ConnectionError:
        pass
    # Fall back to existing one-shot approach
    return _cloud_print_oneshot(...)
```

### Phase 3: Lifecycle management

```python
# bambox/daemon.py

def start_daemon(credentials_path=None):
    """Start the bridge daemon container."""
    subprocess.run([
        "docker", "run", "-d",
        "--name", "bambu-bridge",
        "--restart", "unless-stopped",
        "-p", "8765:8765",
        "-v", f"{credentials_path}:/config/credentials.json:ro",
        "estampo/cloud-bridge-daemon:latest",
    ])

def stop_daemon():
    subprocess.run(["docker", "stop", "bambu-bridge"])
    subprocess.run(["docker", "rm", "bambu-bridge"])

def ensure_daemon(credentials_path=None):
    """Start daemon if not running."""
    try:
        BridgeDaemon().health()
    except ConnectionError:
        start_daemon(credentials_path)
```

CLI integration:
```bash
bambox daemon start        # Start bridge daemon
bambox daemon stop         # Stop bridge daemon
bambox daemon status       # Health check
bambox print file.3mf ...  # Auto-starts daemon if needed
```

## Docker Image

```dockerfile
FROM estampo/cloud-bridge:bambu-02.05.00.00
COPY daemon.py /opt/daemon/daemon.py
EXPOSE 8765
ENTRYPOINT ["python3", "/opt/daemon/daemon.py"]
```

No additional Python dependencies — `http.server`, `subprocess`, `json`
are all stdlib.

## Migration Path

1. **bambox** adopts daemon client with one-shot fallback
2. **estampo** replaces `PersistentBridge` with daemon client
   (same persistent MQTT benefit, but via HTTP not stdin/stdout)
3. One-shot `docker run` path kept indefinitely as fallback for
   environments where a daemon isn't practical (CI, ephemeral containers)

## Security

- Daemon binds to `localhost:8765` only — not exposed externally
- Credentials stored in mounted volume, not environment variables
- No authentication on HTTP API (localhost-only is sufficient)
- Docker container runs with `--network host` avoided — uses port mapping

## Future: Cloud Service

The daemon HTTP API is the same API a hosted service would expose. When/if
we want `api.estampo.dev`:

- Deploy the same daemon image to a cloud host
- Add authentication layer (API keys / OAuth)
- Python client switches URL from `localhost:8765` to `api.estampo.dev`
- No client code changes beyond URL configuration

Bambu's IP-based restrictions (if any) would block a cloud service from
connecting to their MQTT broker. The local daemon avoids this — the MQTT
connection originates from the user's network, same as OrcaSlicer.

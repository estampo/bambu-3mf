//! axum HTTP server exposing printer status over HTTP.
//!
//! A background task drains MQTT messages every ~1s and updates the device
//! cache. Once a device has been queried via `/status`, its MQTT subscription
//! stays active and the printer pushes updates every ~1-2s, keeping the cache
//! perpetually fresh. Subsequent HTTP requests return instantly from cache.
//!
//! Endpoints:
//!   GET  /health            — daemon health + MQTT connection state
//!   GET  /printers          — list configured printers with cached status summary
//!   GET  /status/:device_id — cached printer status (instant) or live query
//!   GET  /ams/:device_id    — AMS tray info extracted from cached status
//!   POST /cancel/:device_id — cancel current print

use std::collections::{HashMap, HashSet};
use std::sync::{Arc, RwLock};
use std::time::{Duration, Instant};

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::Json;
use axum::routing::get;
use axum::Router;
use serde::{Deserialize, Serialize};

use crate::handle::AgentHandle;

pub const API_VERSION: u32 = 1;

// ---------------------------------------------------------------------------
// Shared state
// ---------------------------------------------------------------------------

/// Cached status for a single device.
#[derive(Clone)]
pub struct DeviceStatus {
    pub payload: serde_json::Value,
    pub updated_at: Instant,
}

/// Configured printer entry from credentials file.
#[derive(Clone, Debug, Serialize)]
pub struct PrinterEntry {
    pub name: String,
    pub serial: String,
}

/// Shared state across all HTTP handlers.
pub struct AppState {
    /// Handle to the agent thread (sends commands via channel).
    pub handle: AgentHandle,
    /// Cached printer status per device_id.
    pub cache: RwLock<HashMap<String, DeviceStatus>>,
    /// Devices with active MQTT subscriptions (cache kept fresh by background task).
    pub subscribed_devices: RwLock<HashSet<String>>,
    /// Configured printers from credentials file.
    pub printers: Vec<PrinterEntry>,
    /// When the daemon started.
    pub started_at: Instant,
    /// Bambu plugin version (e.g. "02.05.00.00").
    pub plugin_version: String,
}

pub type SharedState = Arc<AppState>;

impl AppState {
    pub fn new(
        handle: AgentHandle,
        printers: Vec<(String, String)>,
        plugin_version: String,
    ) -> SharedState {
        Arc::new(Self {
            handle,
            cache: RwLock::new(HashMap::new()),
            subscribed_devices: RwLock::new(HashSet::new()),
            printers: printers
                .into_iter()
                .map(|(name, serial)| PrinterEntry { name, serial })
                .collect(),
            started_at: Instant::now(),
            plugin_version,
        })
    }
}

// ---------------------------------------------------------------------------
// Response types
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize)]
pub struct HealthResponse {
    pub status: String,
    pub pid: u32,
    pub mqtt_connected: bool,
    pub uptime_secs: u64,
    pub cached_devices: Vec<String>,
    pub bridge_version: String,
    pub api_version: u32,
    pub plugin_version: String,
}

#[derive(Serialize, Deserialize)]
pub struct PingResponse {
    pub status: String,
    pub pid: u32,
    pub uptime_secs: u64,
    pub rss_kb: u64,
}

#[derive(Serialize)]
pub struct ErrorResponse {
    pub error: String,
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

/// GET /ping — lightweight process liveness check.
/// No channel, no FFI, no cloud calls. Always fast.
async fn ping(State(state): State<SharedState>) -> Json<PingResponse> {
    Json(PingResponse {
        status: "ok".into(),
        pid: std::process::id(),
        uptime_secs: state.started_at.elapsed().as_secs(),
        rss_kb: read_rss_kb(),
    })
}

/// Read resident set size from /proc/self/statm (Linux).
fn read_rss_kb() -> u64 {
    std::fs::read_to_string("/proc/self/statm")
        .ok()
        .and_then(|s| s.split_whitespace().nth(1)?.parse::<u64>().ok())
        .map(|pages| pages * 4) // page size = 4KB on Linux
        .unwrap_or(0)
}

/// GET /health — reads MQTT connection state directly from shared atomic.
/// No channel round-trip needed.
async fn health(State(state): State<SharedState>) -> Json<HealthResponse> {
    let mqtt_connected = state
        .handle
        .callback_state
        .server_connected
        .load(std::sync::atomic::Ordering::SeqCst);
    let cached_devices: Vec<String> = state
        .cache
        .read()
        .unwrap()
        .keys()
        .cloned()
        .collect();

    Json(HealthResponse {
        status: "ok".into(),
        pid: std::process::id(),
        mqtt_connected,
        uptime_secs: state.started_at.elapsed().as_secs(),
        cached_devices,
        bridge_version: env!("CARGO_PKG_VERSION").into(),
        api_version: API_VERSION,
        plugin_version: state.plugin_version.clone(),
    })
}

async fn get_status(
    State(state): State<SharedState>,
    Path(device_id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ErrorResponse>)> {
    // If this device has an active MQTT subscription, the background task keeps
    // the cache fresh (~1-2s updates from the printer). Return cached if < 10s.
    let is_subscribed = state
        .subscribed_devices
        .read()
        .unwrap()
        .contains(&device_id);

    {
        let cache = state.cache.read().unwrap();
        if let Some(cached) = cache.get(&device_id) {
            let max_age = if is_subscribed {
                Duration::from_secs(10)
            } else {
                Duration::from_secs(30)
            };
            if cached.updated_at.elapsed() < max_age {
                return Ok(Json(cached.payload.clone()));
            }
        }
    }

    // First request for this device (or stale) — subscribe and get initial status
    state
        .handle
        .drain_messages()
        .await
        .map_err(|e| err(StatusCode::INTERNAL_SERVER_ERROR, e))?;

    state
        .handle
        .subscribe_and_pushall(device_id.clone(), Duration::from_secs(10))
        .await
        .map_err(|e| {
            err(
                StatusCode::BAD_GATEWAY,
                format!("MQTT query failed: {e}"),
            )
        })?;

    let messages = state
        .handle
        .drain_messages()
        .await
        .map_err(|e| err(StatusCode::INTERNAL_SERVER_ERROR, e))?;

    let best = messages.iter().max_by_key(|m| m.payload.len());

    let payload = match best {
        Some(msg) => {
            serde_json::from_str(&msg.payload).unwrap_or_else(|_| {
                serde_json::json!({"raw": msg.payload})
            })
        }
        None => {
            return Err(err(
                StatusCode::GATEWAY_TIMEOUT,
                format!("no status received from {device_id}"),
            ));
        }
    };

    // Update cache and mark as subscribed — background task will keep it fresh
    {
        let mut cache = state.cache.write().unwrap();
        cache.insert(
            device_id.clone(),
            DeviceStatus {
                payload: payload.clone(),
                updated_at: Instant::now(),
            },
        );
    }
    {
        let mut subs = state.subscribed_devices.write().unwrap();
        subs.insert(device_id);
    }

    Ok(Json(payload))
}

async fn get_ams(
    State(state): State<SharedState>,
    Path(device_id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ErrorResponse>)> {
    // Get status first (uses cache)
    let status = get_status(State(state), Path(device_id)).await?;
    let value = status.0;

    // Extract AMS data from the status
    let print_data = value.get("print").unwrap_or(&value);
    let ams = print_data.get("ams");
    let vt_tray = print_data.get("vt_tray");

    match ams {
        Some(ams_data) => {
            let mut result = serde_json::json!({ "ams": ams_data });
            if let Some(vt) = vt_tray {
                result["vt_tray"] = vt.clone();
            }
            Ok(Json(result))
        }
        None => Err((
            StatusCode::NOT_FOUND,
            Json(ErrorResponse {
                error: "no AMS data in printer status".into(),
            }),
        )),
    }
}

/// GET /printers — list configured printers with cached status summary.
async fn list_printers(State(state): State<SharedState>) -> Json<serde_json::Value> {
    let cache = state.cache.read().unwrap();
    let printers: Vec<serde_json::Value> = state
        .printers
        .iter()
        .map(|p| {
            let mut entry = serde_json::json!({
                "name": p.name,
                "serial": p.serial,
            });
            if let Some(status) = cache.get(&p.serial) {
                let print_data = status.payload.get("print").unwrap_or(&status.payload);
                entry["gcode_state"] = print_data
                    .get("gcode_state")
                    .cloned()
                    .unwrap_or(serde_json::json!(null));
                entry["nozzle_temper"] = print_data
                    .get("nozzle_temper")
                    .cloned()
                    .unwrap_or(serde_json::json!(null));
                entry["bed_temper"] = print_data
                    .get("bed_temper")
                    .cloned()
                    .unwrap_or(serde_json::json!(null));
                entry["subtask_name"] = print_data
                    .get("subtask_name")
                    .cloned()
                    .unwrap_or(serde_json::json!(null));
                entry["mc_percent"] = print_data
                    .get("mc_percent")
                    .cloned()
                    .unwrap_or(serde_json::json!(null));
                entry["cached"] = serde_json::json!(true);
                entry["cache_age_secs"] = serde_json::json!(status.updated_at.elapsed().as_secs());
            } else {
                entry["cached"] = serde_json::json!(false);
            }
            entry
        })
        .collect();
    Json(serde_json::json!({ "printers": printers }))
}

async fn cancel_print(
    State(state): State<SharedState>,
    Path(device_id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ErrorResponse>)> {
    // Set the atomic cancel flag so any in-flight upload aborts immediately
    let _ = state.handle.cancel_print().await;

    // Ensure the device is selected/subscribed — the SDK requires this
    // before send_message will work (returns -2 otherwise).
    let _ = state
        .handle
        .subscribe_and_pushall(device_id.clone(), std::time::Duration::from_secs(10))
        .await;

    // Send the MQTT stop command matching BambuStudio's command_task_abort()
    let stop_cmd = r#"{"print":{"command":"stop","param":"","sequence_id":"0"}}"#;
    let ret = state
        .handle
        .send_message(device_id.clone(), stop_cmd.to_string())
        .await
        .map_err(|e| {
            err(
                StatusCode::BAD_REQUEST,
                format!("invalid device_id: {e}"),
            )
        })?;

    if ret != 0 {
        return Err(err(
            StatusCode::BAD_GATEWAY,
            format!("send_message returned {ret}"),
        ));
    }

    Ok(Json(serde_json::json!({
        "command": "stop",
        "device_id": device_id,
        "sent": true,
        "upload_cancelled": true,
    })))
}

/// POST /print — multipart upload
///
/// Fields:
///   - `file`: the .3mf file (required)
///   - `params`: JSON string with PrintRequest fields (required)
///
/// The handler automatically:
/// 1. Strips gcode from the 3MF to create a config-only 3MF
/// 2. Queries AMS tray state from the printer (via cache or live MQTT)
/// 3. Builds AMS mapping (matching virtual filaments to physical trays)
/// 4. Patches config 3MF colors to match AMS tray colors
/// 5. Sends both files + mapping to the Bambu cloud API
///
/// This replicates the exact behavior of bridge.py's cloud_print().
async fn start_print(
    State(state): State<SharedState>,
    mut multipart: axum_extra::extract::Multipart,
) -> Result<Json<crate::agent::PrintResult>, (StatusCode, Json<ErrorResponse>)> {
    let mut file_bytes: Option<Vec<u8>> = None;
    let mut params_json: Option<String> = None;

    while let Some(field) = multipart
        .next_field()
        .await
        .map_err(|e| err(StatusCode::BAD_REQUEST, format!("multipart error: {e}")))?
    {
        let name = field.name().unwrap_or("").to_string();
        match name.as_str() {
            "file" => {
                file_bytes = Some(
                    field
                        .bytes()
                        .await
                        .map_err(|e| err(StatusCode::BAD_REQUEST, format!("read file: {e}")))?
                        .to_vec(),
                );
            }
            "params" => {
                params_json = Some(
                    field
                        .text()
                        .await
                        .map_err(|e| err(StatusCode::BAD_REQUEST, format!("read params: {e}")))?
                );
            }
            _ => {} // ignore unknown fields
        }
    }

    let file_data = file_bytes.ok_or_else(|| err(StatusCode::BAD_REQUEST, "missing 'file' field".into()))?;
    let params_str = params_json.ok_or_else(|| err(StatusCode::BAD_REQUEST, "missing 'params' field".into()))?;

    let mut request: crate::agent::PrintRequest = serde_json::from_str(&params_str)
        .map_err(|e| err(StatusCode::BAD_REQUEST, format!("invalid params JSON: {e}")))?;

    // Step 1: Strip gcode from 3MF to create config-only 3MF
    let config_data = crate::print_job::strip_gcode_from_3mf(&file_data)
        .map_err(|e| err(StatusCode::BAD_REQUEST, format!("strip gcode: {e}")))?;

    // Step 2: Query AMS tray state from printer
    // Try cache first, fall back to live query
    let ams_trays = {
        let printer_status = get_printer_status_for_ams(&state, &request.device_id).await;
        match printer_status {
            Some(status) => {
                let print_data = status.get("print").unwrap_or(&status);
                crate::print_job::parse_ams_trays(print_data)
            }
            None => {
                tracing::warn!(device_id = %request.device_id, "no cached status for AMS — using empty mapping");
                Vec::new()
            }
        }
    };

    // Step 3: Build AMS mapping from 3MF filaments vs physical trays
    let ams_mapping = crate::print_job::build_ams_mapping(&file_data, &ams_trays);
    tracing::info!(
        mapping = ?ams_mapping.mapping,
        trays = ams_trays.len(),
        "AMS mapping built"
    );

    // Step 4: Patch config 3MF colors to match AMS tray colors
    let patched_config = if !ams_trays.is_empty() {
        crate::print_job::patch_config_3mf_colors(&config_data, &ams_trays, &ams_mapping.mapping)
            .map_err(|e| err(StatusCode::INTERNAL_SERVER_ERROR, format!("patch colors: {e}")))?
    } else {
        config_data
    };

    // Step 5: Write files to temp directory and set up request
    let tmp_dir = tempfile::tempdir()
        .map_err(|e| err(StatusCode::INTERNAL_SERVER_ERROR, format!("tmpdir: {e}")))?;

    // Write main 3MF
    let basename = std::path::Path::new(&request.filename)
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .into_owned();
    let file_path = tmp_dir.path().join(&basename);
    std::fs::write(&file_path, &file_data)
        .map_err(|e| err(StatusCode::INTERNAL_SERVER_ERROR, format!("write file: {e}")))?;
    request.filename = file_path.to_string_lossy().into_owned();

    // Write config-only 3MF
    let stem = std::path::Path::new(&basename)
        .file_stem()
        .unwrap_or_default()
        .to_string_lossy();
    let cfg_name = format!("{stem}_config.3mf");
    let cfg_path = tmp_dir.path().join(&cfg_name);
    std::fs::write(&cfg_path, &patched_config)
        .map_err(|e| err(StatusCode::INTERNAL_SERVER_ERROR, format!("write config: {e}")))?;
    request.config_filename = Some(cfg_path.to_string_lossy().into_owned());

    // Set AMS mapping from our computed values
    let mapping_json = serde_json::to_string(&ams_mapping.mapping).unwrap_or_else(|_| "[]".into());
    let mapping2_json = serde_json::to_string(&ams_mapping.mapping2).unwrap_or_else(|_| "[]".into());
    request.ams_mapping = Some(mapping_json);
    request.ams_mapping2 = Some(mapping2_json);

    tracing::info!(
        file = %request.filename,
        config = ?request.config_filename,
        ams_mapping = ?request.ams_mapping,
        file_size = file_data.len(),
        config_size = patched_config.len(),
        "print request prepared"
    );

    // Verify files exist before sending to agent
    let file_exists = std::path::Path::new(&request.filename).exists();
    let config_exists = request.config_filename.as_ref()
        .map(|p| std::path::Path::new(p).exists())
        .unwrap_or(false);
    tracing::info!(file_exists, config_exists, "pre-print file check");

    // Send print command via channel — the agent thread does the blocking FFI call
    let result = state
        .handle
        .start_print(request)
        .await
        .map_err(|e| err(StatusCode::BAD_GATEWAY, e))?;

    // tmp_dir is dropped here, cleaning up files
    drop(tmp_dir);

    let is_error = result.return_code != 0 && result.return_code != -1;
    if is_error {
        Err(err(
            StatusCode::BAD_GATEWAY,
            format!(
                "print failed: return_code={}, print_result={}",
                result.return_code, result.print_result
            ),
        ))
    } else {
        Ok(Json(result))
    }
}

/// Get printer status for AMS tray parsing. Checks cache first, then does a live query.
async fn get_printer_status_for_ams(
    state: &SharedState,
    device_id: &str,
) -> Option<serde_json::Value> {
    // Check cache (any age — AMS trays change rarely)
    {
        let cache = state.cache.read().unwrap();
        if let Some(cached) = cache.get(device_id) {
            return Some(cached.payload.clone());
        }
    }

    // No cache — try a live query via agent channel
    let device_id_owned = device_id.to_string();

    if state.handle.drain_messages().await.is_err() {
        return None;
    }

    if state
        .handle
        .subscribe_and_pushall(device_id_owned.clone(), Duration::from_secs(10))
        .await
        .is_err()
    {
        return None;
    }

    let messages = match state.handle.drain_messages().await {
        Ok(m) => m,
        Err(_) => return None,
    };

    let best = messages.iter().max_by_key(|m| m.payload.len());
    let result = best.and_then(|msg| serde_json::from_str::<serde_json::Value>(&msg.payload).ok());

    // Cache the result and mark as subscribed
    if let Some(ref payload) = result {
        {
            let mut cache = state.cache.write().unwrap();
            cache.insert(
                device_id_owned.clone(),
                DeviceStatus {
                    payload: payload.clone(),
                    updated_at: Instant::now(),
                },
            );
        }
        {
            let mut subs = state.subscribed_devices.write().unwrap();
            subs.insert(device_id_owned);
        }
    }

    result
}

fn err(status: StatusCode, msg: String) -> (StatusCode, Json<ErrorResponse>) {
    (status, Json(ErrorResponse { error: msg }))
}

// ---------------------------------------------------------------------------
// Background cache updater
// ---------------------------------------------------------------------------

/// Spawn a background task that drains MQTT messages every second and updates
/// the device cache. Once a device has been subscribed (via `get_status`), the
/// printer pushes status updates every ~1-2s over MQTT. This task picks them
/// up and keeps the cache perpetually fresh so HTTP requests are always instant.
pub fn spawn_cache_updater(state: SharedState) {
    tokio::spawn(async move {
        loop {
            tokio::time::sleep(Duration::from_secs(1)).await;

            // Only drain if there are subscribed devices
            let has_subs = !state.subscribed_devices.read().unwrap().is_empty();
            if !has_subs {
                continue;
            }

            let messages = match state.handle.drain_messages().await {
                Ok(msgs) => msgs,
                Err(_) => continue,
            };

            if messages.is_empty() {
                continue;
            }

            // Group messages by device_id, keep the best (largest) per device
            let mut best_per_device: HashMap<String, &crate::callbacks::MqttMessage> =
                HashMap::new();
            for msg in &messages {
                if msg.dev_id.is_empty() {
                    continue;
                }
                let entry = best_per_device.entry(msg.dev_id.clone()).or_insert(msg);
                if msg.payload.len() > entry.payload.len() {
                    *entry = msg;
                }
            }

            // Update cache for each device that sent messages
            let mut cache = state.cache.write().unwrap();
            for (dev_id, msg) in best_per_device {
                // Full status message — update both payload and timestamp
                if msg.payload.len() >= 100 && msg.payload.contains("gcode_state") {
                    if let Ok(payload) = serde_json::from_str::<serde_json::Value>(&msg.payload) {
                        tracing::trace!(
                            device_id = %dev_id,
                            payload_len = msg.payload.len(),
                            "cache updated by background task"
                        );
                        cache.insert(
                            dev_id,
                            DeviceStatus {
                                payload,
                                updated_at: Instant::now(),
                            },
                        );
                        continue;
                    }
                }
                // Heartbeat or partial message — keep existing payload but refresh
                // the timestamp so the cache doesn't go stale while subscribed.
                if let Some(entry) = cache.get_mut(&dev_id) {
                    tracing::trace!(
                        device_id = %dev_id,
                        payload_len = msg.payload.len(),
                        "cache timestamp refreshed by heartbeat"
                    );
                    entry.updated_at = Instant::now();
                }
            }
        }
    });
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

/// Build an AppState with pre-populated cache (for testing without FFI).
#[cfg(test)]
pub fn mock_state(devices: HashMap<String, serde_json::Value>) -> SharedState {
    mock_state_with_printers(devices, Vec::new())
}

#[cfg(test)]
pub fn mock_state_with_printers(
    devices: HashMap<String, serde_json::Value>,
    printers: Vec<(String, String)>,
) -> SharedState {
    let handle = crate::handle::test_handle();
    let state = Arc::new(AppState {
        handle,
        cache: RwLock::new(
            devices
                .into_iter()
                .map(|(k, v)| {
                    (
                        k,
                        DeviceStatus {
                            payload: v,
                            updated_at: Instant::now(),
                        },
                    )
                })
                .collect(),
        ),
        subscribed_devices: RwLock::new(HashSet::new()),
        printers: printers
            .into_iter()
            .map(|(name, serial)| PrinterEntry { name, serial })
            .collect(),
        started_at: Instant::now(),
        plugin_version: "02.05.00.00".into(),
    });
    state
}

/// POST /shutdown — cleanly stop the daemon.
/// Uses fast_exit to avoid .so MQTT thread cleanup hangs.
async fn shutdown() -> Json<serde_json::Value> {
    tracing::info!("shutdown requested via HTTP");
    // Spawn a task that exits after a short delay so the response can be sent
    tokio::spawn(async {
        tokio::time::sleep(Duration::from_millis(100)).await;
        // flush and fast-exit (same as CLI commands)
        use std::io::Write;
        let _ = std::io::stdout().flush();
        let _ = std::io::stderr().flush();
        unsafe { libc::_exit(0) }
    });
    Json(serde_json::json!({"status": "shutting_down"}))
}

pub fn router(state: SharedState) -> Router {
    Router::new()
        .route("/ping", get(ping))
        .route("/health", get(health))
        .route("/printers", get(list_printers))
        .route("/status/:device_id", get(get_status))
        .route("/ams/:device_id", get(get_ams))
        .route("/print", axum::routing::post(start_print))
        .route("/cancel/:device_id", axum::routing::post(cancel_print))
        .route("/shutdown", axum::routing::post(shutdown))
        .with_state(state)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_status() -> serde_json::Value {
        serde_json::json!({
            "print": {
                "gcode_state": "RUNNING",
                "bed_temper": 60.0,
                "nozzle_temper": 220.0,
                "subtask_name": "test_cube",
                "ams": {
                    "ams": [{
                        "id": "0",
                        "tray": [
                            {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF"},
                            {"id": "1", "tray_type": "ASA", "tray_color": "BCBCBCFF"},
                        ]
                    }]
                },
                "vt_tray": {"id": "254", "tray_type": "TPU"}
            }
        })
    }

    #[tokio::test]
    async fn health_returns_ok() {
        let state = mock_state(HashMap::new());
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/health").await;
        resp.assert_status_ok();
        let body: HealthResponse = resp.json();
        assert_eq!(body.status, "ok");
        assert!(!body.mqtt_connected);
        assert!(body.cached_devices.is_empty());
        assert_eq!(body.bridge_version, env!("CARGO_PKG_VERSION"));
        assert_eq!(body.api_version, API_VERSION);
        assert_eq!(body.plugin_version, "02.05.00.00");
    }

    #[tokio::test]
    async fn health_shows_cached_devices() {
        let mut devices = HashMap::new();
        devices.insert("DEV001".into(), sample_status());
        let state = mock_state(devices);
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/health").await;
        let body: HealthResponse = resp.json();
        assert_eq!(body.cached_devices, vec!["DEV001"]);
    }

    #[tokio::test]
    async fn status_returns_cached() {
        let mut devices = HashMap::new();
        devices.insert("DEV001".into(), sample_status());
        let state = mock_state(devices);
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/status/DEV001").await;
        resp.assert_status_ok();
        let body: serde_json::Value = resp.json();
        assert_eq!(body["print"]["gcode_state"], "RUNNING");
        assert_eq!(body["print"]["subtask_name"], "test_cube");
    }

    #[tokio::test]
    async fn ams_extracts_from_cached_status() {
        let mut devices = HashMap::new();
        devices.insert("DEV001".into(), sample_status());
        let state = mock_state(devices);
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/ams/DEV001").await;
        resp.assert_status_ok();
        let body: serde_json::Value = resp.json();
        let trays = &body["ams"]["ams"][0]["tray"];
        assert_eq!(trays[0]["tray_type"], "PLA");
        assert_eq!(trays[1]["tray_type"], "ASA");
        assert_eq!(body["vt_tray"]["tray_type"], "TPU");
    }

    #[tokio::test]
    async fn printers_lists_configured_printers() {
        let mut devices = HashMap::new();
        devices.insert("DEV001".into(), sample_status());
        let printers = vec![
            ("workshop".to_string(), "DEV001".to_string()),
            ("office".to_string(), "DEV002".to_string()),
        ];
        let state = mock_state_with_printers(devices, printers);
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/printers").await;
        resp.assert_status_ok();
        let body: serde_json::Value = resp.json();
        let printers = body["printers"].as_array().unwrap();
        assert_eq!(printers.len(), 2);
        // First printer has cached status
        assert_eq!(printers[0]["name"], "workshop");
        assert_eq!(printers[0]["serial"], "DEV001");
        assert_eq!(printers[0]["cached"], true);
        assert_eq!(printers[0]["gcode_state"], "RUNNING");
        // Second printer has no cached status
        assert_eq!(printers[1]["name"], "office");
        assert_eq!(printers[1]["serial"], "DEV002");
        assert_eq!(printers[1]["cached"], false);
    }

    #[tokio::test]
    async fn ams_returns_404_when_no_ams_data() {
        let mut devices = HashMap::new();
        devices.insert(
            "DEV002".into(),
            serde_json::json!({"print": {"gcode_state": "IDLE"}}),
        );
        let state = mock_state(devices);
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/ams/DEV002").await;
        resp.assert_status(StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn ping_returns_ok_with_pid_and_uptime() {
        let state = mock_state(HashMap::new());
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/ping").await;
        resp.assert_status_ok();
        let body: PingResponse = resp.json();
        assert_eq!(body.status, "ok");
        assert!(body.pid > 0);
        assert!(body.uptime_secs < 5);
    }

    #[tokio::test]
    async fn health_response_has_all_fields() {
        let state = mock_state(HashMap::new());
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/health").await;
        resp.assert_status_ok();
        let body: serde_json::Value = resp.json();
        assert!(body.get("status").is_some());
        assert!(body.get("pid").is_some());
        assert!(body.get("mqtt_connected").is_some());
        assert!(body.get("uptime_secs").is_some());
        assert!(body.get("cached_devices").is_some());
    }

    #[tokio::test]
    async fn health_mqtt_disconnected_by_default() {
        let state = mock_state(HashMap::new());
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/health").await;
        let body: HealthResponse = resp.json();
        assert!(!body.mqtt_connected);
    }

    #[tokio::test]
    async fn health_mqtt_connected_when_flag_set() {
        let state = mock_state(HashMap::new());
        state
            .handle
            .callback_state
            .server_connected
            .store(true, std::sync::atomic::Ordering::SeqCst);

        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/health").await;
        let body: HealthResponse = resp.json();
        assert!(body.mqtt_connected);
    }

    #[tokio::test]
    async fn health_shows_multiple_cached_devices() {
        let mut devices = HashMap::new();
        devices.insert("DEV001".into(), sample_status());
        devices.insert("DEV002".into(), serde_json::json!({"print": {}}));
        devices.insert("DEV003".into(), serde_json::json!({}));
        let state = mock_state(devices);
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/health").await;
        let body: HealthResponse = resp.json();
        assert_eq!(body.cached_devices.len(), 3);
    }

    #[tokio::test]
    async fn status_uncached_device_returns_error() {
        let state = mock_state(HashMap::new());
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/status/UNKNOWN").await;
        resp.assert_status(StatusCode::INTERNAL_SERVER_ERROR);
    }

    #[tokio::test]
    async fn printers_empty_when_none_configured() {
        let state = mock_state(HashMap::new());
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/printers").await;
        resp.assert_status_ok();
        let body: serde_json::Value = resp.json();
        let printers = body["printers"].as_array().unwrap();
        assert!(printers.is_empty());
    }

    #[tokio::test]
    async fn printers_uncached_shows_cached_false() {
        let printers = vec![("lonely".to_string(), "DEV999".to_string())];
        let state = mock_state_with_printers(HashMap::new(), printers);
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/printers").await;
        let body: serde_json::Value = resp.json();
        let printers = body["printers"].as_array().unwrap();
        assert_eq!(printers.len(), 1);
        assert_eq!(printers[0]["cached"], false);
        assert!(printers[0].get("gcode_state").is_none());
    }

    #[tokio::test]
    async fn cancel_fails_on_disconnected_channel() {
        let state = mock_state(HashMap::new());
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.post("/cancel/DEV001").await;
        resp.assert_status(StatusCode::BAD_REQUEST);
    }

    #[tokio::test]
    async fn nonexistent_route_returns_404() {
        let state = mock_state(HashMap::new());
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/nonexistent").await;
        resp.assert_status(StatusCode::NOT_FOUND);
    }

    #[tokio::test]
    async fn status_cached_preserves_all_fields() {
        let status = serde_json::json!({
            "print": {
                "gcode_state": "IDLE",
                "bed_temper": 25.0,
                "nozzle_temper": 30.0,
                "mc_percent": 0,
            }
        });
        let mut devices = HashMap::new();
        devices.insert("DEV001".into(), status);
        let state = mock_state(devices);
        let app = router(state);
        let server = axum_test::TestServer::new(app).unwrap();

        let resp = server.get("/status/DEV001").await;
        resp.assert_status_ok();
        let body: serde_json::Value = resp.json();
        assert_eq!(body["print"]["gcode_state"], "IDLE");
        assert_eq!(body["print"]["bed_temper"], 25.0);
        assert_eq!(body["print"]["mc_percent"], 0);
    }
}

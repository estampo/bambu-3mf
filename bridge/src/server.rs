//! axum HTTP server exposing printer status over HTTP.
//!
//! Endpoints:
//!   GET  /health            — daemon health + MQTT connection state
//!   GET  /status/:device_id — cached printer status (instant) or live query
//!   GET  /ams/:device_id    — AMS tray info extracted from cached status
//!   POST /cancel/:device_id — cancel current print
//!   WS   /watch/:device_id  — (Phase 2b, not yet implemented)

use std::collections::HashMap;
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant};

use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::Json;
use axum::routing::get;
use axum::Router;
use serde::{Deserialize, Serialize};

use crate::agent::BambuAgent;

// ---------------------------------------------------------------------------
// Shared state
// ---------------------------------------------------------------------------

/// Cached status for a single device.
#[derive(Clone)]
pub struct DeviceStatus {
    pub payload: serde_json::Value,
    pub updated_at: Instant,
}

/// Shared state across all HTTP handlers.
pub struct AppState {
    /// The FFI agent (behind Mutex because it's not Sync).
    pub agent: Mutex<BambuAgent>,
    /// Cached printer status per device_id.
    pub cache: RwLock<HashMap<String, DeviceStatus>>,
    /// When the daemon started.
    pub started_at: Instant,
}

pub type SharedState = Arc<AppState>;

impl AppState {
    pub fn new(agent: BambuAgent) -> SharedState {
        Arc::new(Self {
            agent: Mutex::new(agent),
            cache: RwLock::new(HashMap::new()),
            started_at: Instant::now(),
        })
    }
}

// ---------------------------------------------------------------------------
// Response types
// ---------------------------------------------------------------------------

#[derive(Serialize, Deserialize)]
pub struct HealthResponse {
    pub status: String,
    pub mqtt_connected: bool,
    pub uptime_secs: u64,
    pub cached_devices: Vec<String>,
}

#[derive(Serialize)]
pub struct ErrorResponse {
    pub error: String,
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn health(State(state): State<SharedState>) -> Json<HealthResponse> {
    let agent = state.agent.lock().unwrap();
    let mqtt_connected = agent
        .callback_state()
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
        mqtt_connected,
        uptime_secs: state.started_at.elapsed().as_secs(),
        cached_devices,
    })
}

async fn get_status(
    State(state): State<SharedState>,
    Path(device_id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ErrorResponse>)> {
    // Check cache first — return if fresh (< 30s old)
    {
        let cache = state.cache.read().unwrap();
        if let Some(cached) = cache.get(&device_id) {
            if cached.updated_at.elapsed() < Duration::from_secs(30) {
                return Ok(Json(cached.payload.clone()));
            }
        }
    }

    // Cache miss or stale — do a live query
    let payload = {
        let agent = state.agent.lock().unwrap();

        // Drain stale messages
        agent.drain_messages();

        if let Err(e) = agent.subscribe_and_pushall(&device_id, Duration::from_secs(10)) {
            return Err((
                StatusCode::BAD_GATEWAY,
                Json(ErrorResponse {
                    error: format!("MQTT query failed: {e}"),
                }),
            ));
        }

        let messages = agent.drain_messages();
        let best = messages.iter().max_by_key(|m| m.payload.len());

        match best {
            Some(msg) => {
                let value: serde_json::Value =
                    serde_json::from_str(&msg.payload).unwrap_or_else(|_| {
                        serde_json::json!({"raw": msg.payload})
                    });
                value
            }
            None => {
                return Err((
                    StatusCode::GATEWAY_TIMEOUT,
                    Json(ErrorResponse {
                        error: format!("no status received from {device_id}"),
                    }),
                ));
            }
        }
    };

    // Update cache
    {
        let mut cache = state.cache.write().unwrap();
        cache.insert(
            device_id,
            DeviceStatus {
                payload: payload.clone(),
                updated_at: Instant::now(),
            },
        );
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

async fn cancel_print(
    State(state): State<SharedState>,
    Path(device_id): Path<String>,
) -> Result<Json<serde_json::Value>, (StatusCode, Json<ErrorResponse>)> {
    let agent = state.agent.lock().unwrap();
    let stop_cmd = r#"{"print":{"command":"stop","sequence_id":"0"}}"#;
    let ret = agent.send_message(&device_id, stop_cmd);

    if ret != 0 {
        return Err((
            StatusCode::BAD_GATEWAY,
            Json(ErrorResponse {
                error: format!("send_message returned {ret}"),
            }),
        ));
    }

    Ok(Json(serde_json::json!({
        "command": "stop",
        "device_id": device_id,
        "sent": true,
    })))
}

// ---------------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------------

/// Build an AppState with pre-populated cache (for testing without FFI).
#[cfg(test)]
pub fn mock_state(devices: HashMap<String, serde_json::Value>) -> SharedState {
    // We can't create a real BambuAgent without the .so, so we use a test-only
    // constructor. The agent field won't be accessed in tests that only hit cache.
    let state = Arc::new(AppState {
        agent: Mutex::new(unsafe { BambuAgent::test_null() }),
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
        started_at: Instant::now(),
    });
    state
}

pub fn router(state: SharedState) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/status/:device_id", get(get_status))
        .route("/ams/:device_id", get(get_ams))
        .route("/cancel/:device_id", axum::routing::post(cancel_print))
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
}

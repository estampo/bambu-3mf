//! High-level wrapper around the FFI layer.
//!
//! `BambuAgent` manages the full lifecycle: load library → create agent →
//! configure → login → connect → subscribe → send/receive messages.

use std::ffi::CString;
use std::os::raw::{c_char, c_void};
use std::path::Path;
use std::sync::atomic::Ordering;
use std::time::Duration;

use crate::callbacks::{self, CallbackState};
use crate::ffi;

/// Print job request parameters.
#[derive(Debug, Clone, serde::Deserialize, serde::Serialize)]
pub struct PrintRequest {
    pub device_id: String,
    pub filename: String,
    #[serde(default = "default_project_name")]
    pub project_name: String,
    pub config_filename: Option<String>,
    pub ams_mapping: Option<String>,
    pub ams_mapping2: Option<String>,
    #[serde(default = "default_true")]
    pub bed_leveling: bool,
    #[serde(default = "default_true")]
    pub flow_cali: bool,
    #[serde(default = "default_true")]
    pub vibration_cali: bool,
    #[serde(default)]
    pub timelapse: bool,
    #[serde(default = "default_true")]
    pub use_ams: bool,
}

fn default_project_name() -> String {
    "bambox".into()
}
fn default_true() -> bool {
    true
}

/// Result from a print job.
#[derive(Debug, Clone, serde::Serialize)]
pub struct PrintResult {
    pub result: String,
    pub return_code: i32,
    pub print_result: i32,
    pub device_id: String,
    pub file: String,
}

/// FFI callback for print progress (logged only).
extern "C" fn print_progress_callback(
    stage: std::os::raw::c_int,
    code: std::os::raw::c_int,
    msg: *const std::os::raw::c_char,
    _ctx: *mut std::os::raw::c_void,
) {
    static STAGE_NAMES: &[&str] = &[
        "Create", "Upload", "Waiting", "Sending",
        "Record", "WaitPrinter", "Finished", "ERROR", "Limit",
    ];
    let stage_name = STAGE_NAMES
        .get(stage as usize)
        .unwrap_or(&"?");
    let msg_str = if msg.is_null() {
        ""
    } else {
        unsafe { std::ffi::CStr::from_ptr(msg).to_str().unwrap_or("") }
    };
    tracing::info!(stage = stage_name, code, msg = msg_str, "print progress");
}

/// Credentials loaded from `credentials.toml` or a token JSON file.
#[derive(Debug)]
pub struct Credentials {
    pub token: String,
    pub refresh_token: String,
    pub uid: String,
    pub name: String,
    pub email: String,
}

impl Credentials {
    /// Load from a JSON token file (same format as the C++ bridge).
    pub fn from_token_json(json: &str) -> Result<Self, String> {
        let v: serde_json::Value =
            serde_json::from_str(json).map_err(|e| format!("invalid JSON: {e}"))?;
        let token = v["token"]
            .as_str()
            .unwrap_or_default()
            .to_owned();
        if token.is_empty() {
            return Err("no 'token' field in credentials".into());
        }
        Ok(Self {
            token,
            refresh_token: v["refreshToken"].as_str().unwrap_or_default().to_owned(),
            uid: v["uid"].as_str().unwrap_or_default().to_owned(),
            name: v["name"].as_str().unwrap_or_default().to_owned(),
            email: v["email"].as_str().unwrap_or_default().to_owned(),
        })
    }

    /// Load from a TOML credentials file (`~/.config/estampo/credentials.toml`).
    pub fn from_toml(path: &Path) -> Result<Self, String> {
        let text = std::fs::read_to_string(path)
            .map_err(|e| format!("cannot read {}: {e}", path.display()))?;
        let doc: toml::Value =
            text.parse().map_err(|e| format!("invalid TOML: {e}"))?;
        let cloud = doc
            .get("cloud")
            .ok_or("no [cloud] section in credentials")?;
        let token = cloud["token"]
            .as_str()
            .unwrap_or_default()
            .to_owned();
        if token.is_empty() {
            return Err("no token in [cloud] section".into());
        }
        Ok(Self {
            token,
            refresh_token: cloud
                .get("refresh_token")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_owned(),
            uid: cloud
                .get("uid")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_owned(),
            name: cloud
                .get("name")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_owned(),
            email: cloud
                .get("email")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_owned(),
        })
    }

    /// Build the user JSON blob expected by the .so's `change_user`.
    fn to_user_json(&self) -> String {
        let refresh = if self.refresh_token.is_empty() {
            &self.token
        } else {
            &self.refresh_token
        };
        format!(
            r#"{{"data":{{"token":"{}","refresh_token":"{}","expires_in":"7200","refresh_expires_in":"2592000","user":{{"uid":"{}","name":"{}","account":"{}","avatar":""}}}}}}"#,
            self.token, refresh, self.uid, self.name, self.email,
        )
    }
}

/// High-level agent wrapping the C++ shim + .so library.
pub struct BambuAgent {
    agent: *mut c_void,
    // Box to ensure stable address for callback context pointer
    state: Box<CallbackState>,
}

// The agent pointer is thread-safe (the .so manages its own locking)
unsafe impl Send for BambuAgent {}

impl BambuAgent {
    /// Load the .so library and create an agent.
    pub fn new(lib_path: &str) -> Result<Self, String> {
        let c_path = CString::new(lib_path).map_err(|e| e.to_string())?;
        let ret = unsafe { ffi::bambu_shim_load(c_path.as_ptr()) };
        if ret != 0 {
            let err = unsafe {
                let p = ffi::bambu_shim_load_error();
                if p.is_null() {
                    "unknown error".to_string()
                } else {
                    std::ffi::CStr::from_ptr(p)
                        .to_string_lossy()
                        .into_owned()
                }
            };
            return Err(format!("failed to load library: {err}"));
        }

        // Set SSL cert env vars (same as C++ bridge) — needed for uploads
        if std::env::var("CURL_CA_BUNDLE").is_err() {
            std::env::set_var("CURL_CA_BUNDLE", "/etc/ssl/certs/ca-certificates.crt");
        }
        if std::env::var("SSL_CERT_FILE").is_err() {
            std::env::set_var("SSL_CERT_FILE", "/etc/ssl/certs/ca-certificates.crt");
        }

        // Create directories the .so expects
        let _ = std::fs::create_dir_all("/tmp/bambu_agent/log");
        let _ = std::fs::create_dir_all("/tmp/bambu_agent/config");
        let _ = std::fs::create_dir_all("/tmp/bambu_agent/cert");

        let log_dir = CString::new("/tmp/bambu_agent/log").unwrap();
        let agent = unsafe { ffi::bambu_shim_create_agent(log_dir.as_ptr()) };
        if agent.is_null() {
            return Err("create_agent returned null".into());
        }

        let state = Box::new(CallbackState::new());
        let mut this = Self { agent, state };
        this.configure()?;
        Ok(this)
    }

    /// Set up directories, certs, headers, and register all callbacks.
    fn configure(&mut self) -> Result<(), String> {
        let config_dir = CString::new("/tmp/bambu_agent/config").unwrap();
        let cert_dir = CString::new("/tmp/bambu_agent/cert").unwrap();
        let cert_name = CString::new("slicer_base64.cer").unwrap();
        let country = CString::new("US").unwrap();

        unsafe {
            ffi::bambu_shim_init_log(self.agent);
            ffi::bambu_shim_set_config_dir(self.agent, config_dir.as_ptr());
            ffi::bambu_shim_set_cert_file(self.agent, cert_dir.as_ptr(), cert_name.as_ptr());
            ffi::bambu_shim_set_country_code(self.agent, country.as_ptr());
            ffi::bambu_shim_start(self.agent);
        }

        // Set HTTP headers (BambuStudio slicer identity)
        self.set_http_headers()?;

        // Register callbacks
        let ctx = &*self.state as *const CallbackState as *mut c_void;
        unsafe {
            ffi::bambu_shim_set_on_server_connected_fn(
                self.agent,
                callbacks::on_server_connected,
                ctx,
            );
            ffi::bambu_shim_set_on_message_fn(self.agent, callbacks::on_message, ctx);
            ffi::bambu_shim_set_on_printer_connected_fn(
                self.agent,
                callbacks::on_printer_connected,
                ctx,
            );
            ffi::bambu_shim_set_on_user_login_fn(self.agent, callbacks::on_user_login, ctx);
            ffi::bambu_shim_set_on_http_error_fn(self.agent, callbacks::on_http_error, ctx);

            let country_code = CString::new("US").unwrap();
            ffi::bambu_shim_set_get_country_code_fn(self.agent, country_code.as_ptr());

            ffi::bambu_shim_set_on_subscribe_failure_fn(
                self.agent,
                callbacks::on_subscribe_failure,
                ctx,
            );
        }

        Ok(())
    }

    fn set_http_headers(&self) -> Result<(), String> {
        let keys_owned: Vec<CString> = [
            "X-BBL-Client-Type",
            "X-BBL-Client-Name",
            "X-BBL-Client-Version",
            "X-BBL-OS-Type",
            "X-BBL-OS-Version",
            "X-BBL-Device-ID",
            "X-BBL-Language",
        ]
        .iter()
        .map(|s| CString::new(*s).unwrap())
        .collect();

        let vals_owned: Vec<CString> = [
            "slicer",
            "BambuStudio",
            "02.05.01.52",
            "linux",
            "6.8.0",
            "estampo-headless-001",
            "en",
        ]
        .iter()
        .map(|s| CString::new(*s).unwrap())
        .collect();

        let keys: Vec<*const c_char> = keys_owned.iter().map(|s| s.as_ptr()).collect();
        let vals: Vec<*const c_char> = vals_owned.iter().map(|s| s.as_ptr()).collect();

        unsafe {
            ffi::bambu_shim_set_extra_http_header(
                self.agent,
                keys.as_ptr(),
                vals.as_ptr(),
                keys.len() as i32,
            );
        }
        Ok(())
    }

    /// Log in with credentials and connect to the MQTT server.
    pub fn login_and_connect(&self, creds: &Credentials) -> Result<(), String> {
        let user_json = CString::new(creds.to_user_json()).map_err(|e| e.to_string())?;

        let ret = unsafe { ffi::bambu_shim_change_user(self.agent, user_json.as_ptr()) };
        if ret != 0 {
            return Err(format!("login failed (change_user returned {ret})"));
        }

        // Wait for login callback
        self.poll_flag(&self.state.user_logged_in, Duration::from_secs(2));

        if unsafe { ffi::bambu_shim_is_user_login(self.agent) } == 0 {
            return Err("login did not succeed".into());
        }
        tracing::info!(
            name = creds.name.as_str(),
            email = creds.email.as_str(),
            "logged in"
        );

        // Connect to MQTT
        unsafe { ffi::bambu_shim_connect_server(self.agent) };

        // Wait for server connection
        for _ in 0..150 {
            if self.state.server_connected.load(Ordering::SeqCst) {
                break;
            }
            if unsafe { ffi::bambu_shim_is_server_connected(self.agent) } != 0 {
                self.state.server_connected.store(true, Ordering::SeqCst);
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
        }

        if !self.state.server_connected.load(Ordering::SeqCst) {
            return Err("could not connect to MQTT server".into());
        }
        tracing::info!("MQTT connected");
        Ok(())
    }

    /// Subscribe to a device and send pushall. Returns when the full status
    /// arrives or `timeout` elapses.
    pub fn subscribe_and_pushall(
        &self,
        device_id: &str,
        timeout: Duration,
    ) -> Result<(), String> {
        let dev = CString::new(device_id).map_err(|e| e.to_string())?;
        let module = CString::new("device").unwrap();

        unsafe {
            ffi::bambu_shim_set_user_selected_machine(self.agent, dev.as_ptr());
        }

        self.state.printer_subscribed.store(false, Ordering::SeqCst);
        unsafe {
            ffi::bambu_shim_start_subscribe(self.agent, module.as_ptr());
        }

        // Wait for subscription callback
        self.poll_flag(&self.state.printer_subscribed, Duration::from_secs(3));

        // Send pushall (retry up to 3 times)
        let pushall = CString::new(
            r#"{"pushing":{"sequence_id":"0","command":"pushall","version":1,"push_target":1}}"#,
        )
        .unwrap();

        let mut ret;
        for i in 0..3 {
            if i > 0 {
                std::thread::sleep(Duration::from_secs(1));
            }
            ret = unsafe {
                ffi::bambu_shim_send_message(self.agent, dev.as_ptr(), pushall.as_ptr(), 0)
            };
            if ret == 0 {
                break;
            }
            // Try the other send function
            ret = unsafe {
                ffi::bambu_shim_send_message_to_printer(
                    self.agent,
                    dev.as_ptr(),
                    pushall.as_ptr(),
                    0,
                    0,
                )
            };
            if ret == 0 {
                break;
            }
            tracing::debug!(attempt = i + 1, ret, "pushall retry");
        }

        // Wait for messages
        let start = std::time::Instant::now();
        while start.elapsed() < timeout {
            // Check if we got a full status (large message with gcode_state)
            {
                let msgs = self.state.messages.lock().unwrap();
                if msgs
                    .iter()
                    .any(|m| m.payload.len() > 500 && m.payload.contains("gcode_state"))
                {
                    // Give a brief grace period for remaining messages
                    drop(msgs);
                    std::thread::sleep(Duration::from_millis(300));
                    break;
                }
            }
            std::thread::sleep(Duration::from_millis(100));
        }

        Ok(())
    }

    /// Send an MQTT message to a device. Tries both send functions.
    pub fn send_message(&self, device_id: &str, json: &str) -> i32 {
        let dev = CString::new(device_id).unwrap();
        let msg = CString::new(json).unwrap();
        let mut ret =
            unsafe { ffi::bambu_shim_send_message(self.agent, dev.as_ptr(), msg.as_ptr(), 0) };
        if ret != 0 {
            ret = unsafe {
                ffi::bambu_shim_send_message_to_printer(
                    self.agent,
                    dev.as_ptr(),
                    msg.as_ptr(),
                    0,
                    0,
                )
            };
        }
        ret
    }

    /// Start a cloud print job. Subscribes, sends pushall, then calls start_print.
    /// Retries up to 5 times on enc-flag-not-ready (-3140).
    pub fn start_print(
        &self,
        params: &PrintRequest,
    ) -> Result<PrintResult, String> {
        // Subscribe and wait for MQTT readiness
        self.subscribe_and_pushall(&params.device_id, Duration::from_secs(20))?;

        // Build C-compatible params — keep CStrings alive for the duration
        let dev_id = CString::new(params.device_id.as_str()).unwrap();
        let task_name = CString::new("").unwrap();
        let project_name = CString::new(params.project_name.as_str()).unwrap();
        let preset_name = CString::new("").unwrap();
        let filename = CString::new(params.filename.as_str()).unwrap();
        let config_filename = CString::new(params.config_filename.as_deref().unwrap_or("")).unwrap();
        let ftp_folder = CString::new("sdcard/").unwrap();
        let ams_mapping = CString::new(params.ams_mapping.as_deref().unwrap_or("[0,1,2,3]")).unwrap();
        let ams_mapping2 = CString::new(params.ams_mapping2.as_deref().unwrap_or("")).unwrap();
        let ams_mapping_info = CString::new("").unwrap();
        let connection_type = CString::new("cloud").unwrap();
        let print_type = CString::new("from_normal").unwrap();
        let bed_type = CString::new("auto").unwrap();

        let shim_params = ffi::ShimPrintParams {
            dev_id: dev_id.as_ptr(),
            task_name: task_name.as_ptr(),
            project_name: project_name.as_ptr(),
            preset_name: preset_name.as_ptr(),
            filename: filename.as_ptr(),
            config_filename: config_filename.as_ptr(),
            plate_index: 1,
            ftp_folder: ftp_folder.as_ptr(),
            ams_mapping: ams_mapping.as_ptr(),
            ams_mapping2: ams_mapping2.as_ptr(),
            ams_mapping_info: ams_mapping_info.as_ptr(),
            connection_type: connection_type.as_ptr(),
            print_type: print_type.as_ptr(),
            task_bed_leveling: if params.bed_leveling { 1 } else { 0 },
            task_flow_cali: if params.flow_cali { 1 } else { 0 },
            task_vibration_cali: if params.vibration_cali { 1 } else { 0 },
            task_layer_inspect: 0,
            task_record_timelapse: if params.timelapse { 1 } else { 0 },
            task_use_ams: if params.use_ams { 1 } else { 0 },
            task_bed_type: bed_type.as_ptr(),
        };

        let mut result = ffi::ShimPrintResult {
            return_code: -999,
            print_result: -999,
            finished: 0,
        };

        // Retry on enc flag not ready (-3140)
        for attempt in 0..5 {
            result.print_result = -999;
            result.finished = 0;

            let ret = unsafe {
                ffi::bambu_shim_start_print(
                    self.agent,
                    &shim_params,
                    print_progress_callback,
                    std::ptr::null_mut(),
                    &mut result,
                )
            };

            if ret != 0 {
                return Err(format!("shim_start_print returned {ret}"));
            }

            tracing::info!(attempt = attempt + 1, return_code = result.return_code, "start_print");

            if result.return_code != -3140 {
                break;
            }
            tracing::warn!("enc flag not ready, retrying in 15s...");
            let pushall = r#"{"pushing":{"sequence_id":"0","command":"pushall","version":1,"push_target":1}}"#;
            self.send_message(&params.device_id, pushall);
            std::thread::sleep(Duration::from_secs(15));
        }

        let status = match result.return_code {
            0 => "success",
            -1 => "sent",
            _ => "error",
        };

        Ok(PrintResult {
            result: status.into(),
            return_code: result.return_code,
            print_result: result.print_result,
            device_id: params.device_id.clone(),
            file: params.filename.clone(),
        })
    }

    /// Drain all buffered MQTT messages.
    pub fn drain_messages(&self) -> Vec<callbacks::MqttMessage> {
        self.state.drain_messages()
    }

    /// Access the callback state directly.
    pub fn callback_state(&self) -> &CallbackState {
        &self.state
    }

    /// Raw agent pointer for direct FFI calls.
    pub fn agent_ptr(&self) -> *mut c_void {
        self.agent
    }

    /// Create a null agent for testing (no FFI calls allowed).
    /// # Safety
    /// Only for tests — calling any FFI method on this agent will crash.
    #[cfg(test)]
    pub unsafe fn test_null() -> Self {
        Self {
            agent: std::ptr::null_mut(),
            state: Box::new(CallbackState::new()),
        }
    }

    /// Poll an atomic bool flag until it becomes true or timeout elapses.
    fn poll_flag(&self, flag: &std::sync::atomic::AtomicBool, timeout: Duration) {
        let start = std::time::Instant::now();
        while start.elapsed() < timeout && !flag.load(Ordering::SeqCst) {
            std::thread::sleep(Duration::from_millis(50));
        }
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credentials_from_token_json_valid() {
        let json = r#"{"token":"abc123","refreshToken":"ref456","uid":"42","name":"Test","email":"t@t.com"}"#;
        let c = Credentials::from_token_json(json).unwrap();
        assert_eq!(c.token, "abc123");
        assert_eq!(c.refresh_token, "ref456");
        assert_eq!(c.uid, "42");
        assert_eq!(c.name, "Test");
        assert_eq!(c.email, "t@t.com");
    }

    #[test]
    fn credentials_from_token_json_missing_token() {
        let json = r#"{"uid":"42"}"#;
        let err = Credentials::from_token_json(json).unwrap_err();
        assert!(err.contains("token"), "expected token error, got: {err}");
    }

    #[test]
    fn credentials_from_token_json_invalid() {
        let err = Credentials::from_token_json("not json").unwrap_err();
        assert!(err.contains("invalid JSON"));
    }

    #[test]
    fn credentials_from_toml_valid() {
        let dir = std::env::temp_dir().join("bambu_test_creds");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("credentials.toml");
        std::fs::write(
            &path,
            r#"
[cloud]
token = "tok123"
refresh_token = "ref789"
uid = "99"
email = "user@example.com"
"#,
        )
        .unwrap();

        let c = Credentials::from_toml(&path).unwrap();
        assert_eq!(c.token, "tok123");
        assert_eq!(c.refresh_token, "ref789");
        assert_eq!(c.uid, "99");
        assert_eq!(c.email, "user@example.com");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn credentials_from_toml_no_cloud_section() {
        let dir = std::env::temp_dir().join("bambu_test_creds2");
        let _ = std::fs::create_dir_all(&dir);
        let path = dir.join("bad.toml");
        std::fs::write(&path, "[other]\nfoo = \"bar\"\n").unwrap();

        let err = Credentials::from_toml(&path).unwrap_err();
        assert!(err.contains("cloud"), "expected cloud error, got: {err}");

        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn credentials_to_user_json_structure() {
        let c = Credentials {
            token: "t".into(),
            refresh_token: "r".into(),
            uid: "u".into(),
            name: "n".into(),
            email: "e".into(),
        };
        let json = c.to_user_json();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(v["data"]["token"], "t");
        assert_eq!(v["data"]["refresh_token"], "r");
        assert_eq!(v["data"]["user"]["uid"], "u");
        assert_eq!(v["data"]["user"]["name"], "n");
        assert_eq!(v["data"]["user"]["account"], "e");
    }

    #[test]
    fn print_request_deserialize_minimal() {
        let json = r#"{"device_id":"DEV1","filename":"test.3mf"}"#;
        let req: PrintRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.device_id, "DEV1");
        assert_eq!(req.filename, "test.3mf");
        assert_eq!(req.project_name, "bambox");
        assert!(req.bed_leveling);
        assert!(req.flow_cali);
        assert!(req.vibration_cali);
        assert!(!req.timelapse);
        assert!(req.use_ams);
        assert!(req.ams_mapping.is_none());
        assert!(req.config_filename.is_none());
    }

    #[test]
    fn print_request_deserialize_full() {
        let json = r#"{
            "device_id": "DEV1",
            "filename": "cube.3mf",
            "project_name": "my-project",
            "config_filename": "cube_config.3mf",
            "ams_mapping": "[0,1]",
            "ams_mapping2": "[{\"ams_id\":0,\"slot_id\":0}]",
            "bed_leveling": false,
            "flow_cali": false,
            "vibration_cali": false,
            "timelapse": true,
            "use_ams": false
        }"#;
        let req: PrintRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.project_name, "my-project");
        assert_eq!(req.config_filename.as_deref(), Some("cube_config.3mf"));
        assert_eq!(req.ams_mapping.as_deref(), Some("[0,1]"));
        assert!(!req.bed_leveling);
        assert!(req.timelapse);
        assert!(!req.use_ams);
    }

    #[test]
    fn print_request_serialize_roundtrip() {
        let req = PrintRequest {
            device_id: "DEV".into(),
            filename: "f.3mf".into(),
            project_name: "p".into(),
            config_filename: None,
            ams_mapping: None,
            ams_mapping2: None,
            bed_leveling: true,
            flow_cali: true,
            vibration_cali: true,
            timelapse: false,
            use_ams: true,
        };
        let json = serde_json::to_string(&req).unwrap();
        let req2: PrintRequest = serde_json::from_str(&json).unwrap();
        assert_eq!(req2.device_id, "DEV");
        assert_eq!(req2.filename, "f.3mf");
    }

    #[test]
    fn credentials_to_user_json_empty_refresh_falls_back_to_token() {
        let c = Credentials {
            token: "mytoken".into(),
            refresh_token: "".into(),
            uid: "".into(),
            name: "".into(),
            email: "".into(),
        };
        let json = c.to_user_json();
        let v: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(v["data"]["refresh_token"], "mytoken");
    }
}

impl Drop for BambuAgent {
    fn drop(&mut self) {
        // Note: destroy_agent can hang waiting for MQTT threads.
        // We still try, but the caller may want to use process::exit() instead.
        if !self.agent.is_null() {
            unsafe {
                ffi::bambu_shim_destroy_agent(self.agent);
            }
        }
    }
}

//! Callback state shared between the shim and Rust code.
//!
//! The .so library invokes callbacks on its own threads. We use atomics and
//! a mutex-protected message buffer to safely communicate with the main thread.

use std::ffi::CStr;
use std::os::raw::{c_char, c_int, c_uint, c_void};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

/// Shared state for all callbacks. Allocated on the heap and passed as the
/// `void* ctx` to every shim callback setter.
///
/// # Single-agent constraint
///
/// The underlying C++ shim stores callback function pointers and context
/// pointers in file-scoped globals (`g_message_cb`, `g_server_cb`, etc.).
/// This means only one `CallbackState` (and therefore one `BambuAgent`)
/// can be active in a process at a time. Registering callbacks from a
/// second agent would silently overwrite the first agent's callbacks.
pub struct CallbackState {
    pub server_connected: AtomicBool,
    pub user_logged_in: AtomicBool,
    pub printer_subscribed: AtomicBool,
    pub messages: Mutex<Vec<MqttMessage>>,
}

#[derive(Debug)]
pub struct MqttMessage {
    pub dev_id: String,
    pub payload: String,
}

impl CallbackState {
    pub fn new() -> Self {
        Self {
            server_connected: AtomicBool::new(false),
            user_logged_in: AtomicBool::new(false),
            printer_subscribed: AtomicBool::new(false),
            messages: Mutex::new(Vec::new()),
        }
    }

    /// Take all accumulated messages, leaving the buffer empty.
    pub fn drain_messages(&self) -> Vec<MqttMessage> {
        let mut lock = self.messages.lock().unwrap();
        std::mem::take(&mut *lock)
    }
}

// ---------------------------------------------------------------------------
// extern "C" callback functions passed to the shim
// ---------------------------------------------------------------------------

/// Cast `ctx` back to `&CallbackState`. Caller must guarantee lifetime.
unsafe fn state(ctx: *mut c_void) -> &'static CallbackState {
    &*(ctx as *const CallbackState)
}

unsafe fn cstr_to_string(ptr: *const c_char) -> String {
    if ptr.is_null() {
        return String::new();
    }
    CStr::from_ptr(ptr).to_str().unwrap_or("").to_owned()
}

pub extern "C" fn on_server_connected(rc: c_int, _reason: c_int, ctx: *mut c_void) {
    let s = unsafe { state(ctx) };
    if rc == 0 {
        s.server_connected.store(true, Ordering::SeqCst);
    }
    tracing::debug!(rc, _reason, "server_connected callback");
}

pub extern "C" fn on_message(dev_id: *const c_char, msg: *const c_char, ctx: *mut c_void) {
    let s = unsafe { state(ctx) };
    let dev = unsafe { cstr_to_string(dev_id) };
    let payload = unsafe { cstr_to_string(msg) };
    if payload.is_empty() || payload == "{}" {
        return;
    }
    tracing::trace!(dev_id = &*dev, len = payload.len(), "mqtt message");
    let mut lock = s.messages.lock().unwrap();
    lock.push(MqttMessage {
        dev_id: dev,
        payload,
    });
}

pub extern "C" fn on_printer_connected(topic: *const c_char, ctx: *mut c_void) {
    let s = unsafe { state(ctx) };
    s.printer_subscribed.store(true, Ordering::SeqCst);
    let t = unsafe { cstr_to_string(topic) };
    tracing::debug!(topic = &*t, "printer_connected callback");
}

pub extern "C" fn on_user_login(_online: c_int, login: c_int, ctx: *mut c_void) {
    let s = unsafe { state(ctx) };
    if login != 0 {
        s.user_logged_in.store(true, Ordering::SeqCst);
    }
    tracing::debug!(_online, login, "user_login callback");
}

pub extern "C" fn on_http_error(code: c_uint, body: *const c_char, _ctx: *mut c_void) {
    let b = unsafe { cstr_to_string(body) };
    tracing::warn!(code, body = &b[..b.len().min(200)], "http_error callback");
}

pub extern "C" fn on_subscribe_failure(topic: *const c_char, _ctx: *mut c_void) {
    let t = unsafe { cstr_to_string(topic) };
    tracing::warn!(topic = &*t, "subscribe_failure callback");
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::ffi::CString;

    #[test]
    fn callback_state_new_defaults() {
        let s = CallbackState::new();
        assert!(!s.server_connected.load(Ordering::SeqCst));
        assert!(!s.user_logged_in.load(Ordering::SeqCst));
        assert!(!s.printer_subscribed.load(Ordering::SeqCst));
        assert!(s.drain_messages().is_empty());
    }

    #[test]
    fn drain_messages_empties_buffer() {
        let s = CallbackState::new();
        {
            let mut msgs = s.messages.lock().unwrap();
            msgs.push(MqttMessage {
                dev_id: "dev1".into(),
                payload: "hello".into(),
            });
            msgs.push(MqttMessage {
                dev_id: "dev2".into(),
                payload: "world".into(),
            });
        }
        let drained = s.drain_messages();
        assert_eq!(drained.len(), 2);
        assert_eq!(drained[0].dev_id, "dev1");
        assert_eq!(drained[1].payload, "world");

        // Buffer should be empty now
        assert!(s.drain_messages().is_empty());
    }

    #[test]
    fn on_server_connected_sets_flag() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        on_server_connected(0, 0, ctx);
        assert!(s.server_connected.load(Ordering::SeqCst));
    }

    #[test]
    fn on_server_connected_nonzero_rc_does_not_set() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        on_server_connected(1, 0, ctx);
        assert!(!s.server_connected.load(Ordering::SeqCst));
    }

    #[test]
    fn on_message_stores_payload() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        let dev = CString::new("DEVICE1").unwrap();
        let msg = CString::new(r#"{"gcode_state":"RUNNING"}"#).unwrap();
        on_message(dev.as_ptr(), msg.as_ptr(), ctx);

        let msgs = s.drain_messages();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].dev_id, "DEVICE1");
        assert!(msgs[0].payload.contains("gcode_state"));
    }

    #[test]
    fn on_message_ignores_empty() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        let dev = CString::new("DEV").unwrap();
        let empty = CString::new("").unwrap();
        let braces = CString::new("{}").unwrap();
        on_message(dev.as_ptr(), empty.as_ptr(), ctx);
        on_message(dev.as_ptr(), braces.as_ptr(), ctx);

        assert!(s.drain_messages().is_empty());
    }

    #[test]
    fn on_user_login_sets_flag() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        on_user_login(0, 1, ctx);
        assert!(s.user_logged_in.load(Ordering::SeqCst));
    }

    #[test]
    fn on_printer_connected_sets_flag() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        let topic = CString::new("01P00A451601106").unwrap();
        on_printer_connected(topic.as_ptr(), ctx);
        assert!(s.printer_subscribed.load(Ordering::SeqCst));
    }

    #[test]
    fn cstr_to_string_null_returns_empty() {
        let result = unsafe { cstr_to_string(std::ptr::null()) };
        assert_eq!(result, "");
    }

    #[test]
    fn cstr_to_string_empty_returns_empty() {
        let s = CString::new("").unwrap();
        let result = unsafe { cstr_to_string(s.as_ptr()) };
        assert_eq!(result, "");
    }

    #[test]
    fn cstr_to_string_valid_ascii() {
        let s = CString::new("hello world").unwrap();
        let result = unsafe { cstr_to_string(s.as_ptr()) };
        assert_eq!(result, "hello world");
    }

    #[test]
    fn cstr_to_string_utf8() {
        let s = CString::new("café").unwrap();
        let result = unsafe { cstr_to_string(s.as_ptr()) };
        assert_eq!(result, "café");
    }

    #[test]
    fn on_user_login_zero_login_does_not_set() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        on_user_login(1, 0, ctx); // online=1, login=0
        assert!(!s.user_logged_in.load(Ordering::SeqCst));
    }

    #[test]
    fn on_message_stores_multiple() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        let dev = CString::new("D1").unwrap();
        let msg1 = CString::new(r#"{"a":1}"#).unwrap();
        let msg2 = CString::new(r#"{"b":2}"#).unwrap();
        on_message(dev.as_ptr(), msg1.as_ptr(), ctx);
        on_message(dev.as_ptr(), msg2.as_ptr(), ctx);

        let msgs = s.drain_messages();
        assert_eq!(msgs.len(), 2);
        assert!(msgs[0].payload.contains("\"a\""));
        assert!(msgs[1].payload.contains("\"b\""));
    }

    #[test]
    fn on_message_null_dev_id_produces_empty_string() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        let msg = CString::new(r#"{"data":"ok"}"#).unwrap();
        on_message(std::ptr::null(), msg.as_ptr(), ctx);

        let msgs = s.drain_messages();
        assert_eq!(msgs.len(), 1);
        assert_eq!(msgs[0].dev_id, "");
    }

    #[test]
    fn on_message_null_payload_is_empty_and_skipped() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        let dev = CString::new("DEV").unwrap();
        on_message(dev.as_ptr(), std::ptr::null(), ctx);

        // null -> empty string -> skipped
        assert!(s.drain_messages().is_empty());
    }

    #[test]
    fn callback_state_atomics_toggle() {
        let s = CallbackState::new();
        s.server_connected.store(true, Ordering::SeqCst);
        assert!(s.server_connected.load(Ordering::SeqCst));
        s.server_connected.store(false, Ordering::SeqCst);
        assert!(!s.server_connected.load(Ordering::SeqCst));

        s.user_logged_in.store(true, Ordering::SeqCst);
        assert!(s.user_logged_in.load(Ordering::SeqCst));

        s.printer_subscribed.store(true, Ordering::SeqCst);
        assert!(s.printer_subscribed.load(Ordering::SeqCst));
    }

    #[test]
    fn drain_messages_is_atomic_swap() {
        let s = CallbackState::new();
        {
            let mut msgs = s.messages.lock().unwrap();
            for i in 0..100 {
                msgs.push(MqttMessage {
                    dev_id: format!("dev{i}"),
                    payload: format!("msg{i}"),
                });
            }
        }
        let drained = s.drain_messages();
        assert_eq!(drained.len(), 100);
        // Second drain is empty
        assert!(s.drain_messages().is_empty());
    }

    #[test]
    fn on_http_error_does_not_panic() {
        // Just verify it doesn't crash — it only logs
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        let body = CString::new("error body text").unwrap();
        on_http_error(401, body.as_ptr(), ctx);
        // No panic = pass
    }

    #[test]
    fn on_subscribe_failure_does_not_panic() {
        let s = Box::new(CallbackState::new());
        let ctx = &*s as *const CallbackState as *mut c_void;
        let topic = CString::new("device/01P00A/report").unwrap();
        on_subscribe_failure(topic.as_ptr(), ctx);
        // No panic = pass
    }
}

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
}

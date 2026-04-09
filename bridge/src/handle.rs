//! Command channel handle for the BambuAgent.
//!
//! Instead of wrapping `BambuAgent` in a `Mutex` shared across async handlers,
//! the agent lives on a dedicated `std::thread` that owns it exclusively.
//! HTTP handlers send commands via an `mpsc` channel and receive results via
//! oneshot channels. This avoids blocking the tokio runtime and eliminates
//! lock contention — `/health` and `/ping` never wait for long MQTT queries.

use std::sync::Arc;
use std::time::Duration;

use tokio::sync::{mpsc, oneshot};

use crate::agent::{BambuAgent, PrintRequest, PrintResult};
use crate::callbacks::{CallbackState, MqttMessage};

// ---------------------------------------------------------------------------
// Command enum
// ---------------------------------------------------------------------------

/// Commands dispatched to the agent thread.
pub enum AgentCommand {
    DrainMessages {
        reply: oneshot::Sender<Vec<MqttMessage>>,
    },
    SubscribeAndPushall {
        device_id: String,
        timeout: Duration,
        reply: oneshot::Sender<Result<(), String>>,
    },
    SendMessage {
        device_id: String,
        json: String,
        reply: oneshot::Sender<Result<i32, String>>,
    },
    StartPrint {
        request: PrintRequest,
        reply: oneshot::Sender<Result<PrintResult, String>>,
    },
    CancelUpload {
        reply: oneshot::Sender<()>,
    },
}

// ---------------------------------------------------------------------------
// AgentHandle — async-friendly sender side
// ---------------------------------------------------------------------------

/// Async handle to the agent thread.
///
/// Cloneable and `Send + Sync` — safe to store in `AppState` without a mutex.
#[derive(Clone)]
pub struct AgentHandle {
    tx: mpsc::Sender<AgentCommand>,
    /// Shared callback state for lock-free reads (e.g. `server_connected`).
    pub callback_state: Arc<CallbackState>,
}

impl AgentHandle {
    /// Drain all buffered MQTT messages from the agent.
    pub async fn drain_messages(&self) -> Result<Vec<MqttMessage>, String> {
        let (reply, rx) = oneshot::channel();
        self.tx
            .send(AgentCommand::DrainMessages { reply })
            .await
            .map_err(|_| "agent thread gone".to_string())?;
        rx.await.map_err(|_| "agent thread dropped reply".to_string())
    }

    /// Subscribe to a device and send pushall, waiting up to `timeout`.
    pub async fn subscribe_and_pushall(
        &self,
        device_id: String,
        timeout: Duration,
    ) -> Result<(), String> {
        let (reply, rx) = oneshot::channel();
        self.tx
            .send(AgentCommand::SubscribeAndPushall {
                device_id,
                timeout,
                reply,
            })
            .await
            .map_err(|_| "agent thread gone".to_string())?;
        rx.await.map_err(|_| "agent thread dropped reply".to_string())?
    }

    /// Send an MQTT message to a device.
    pub async fn send_message(
        &self,
        device_id: String,
        json: String,
    ) -> Result<i32, String> {
        let (reply, rx) = oneshot::channel();
        self.tx
            .send(AgentCommand::SendMessage {
                device_id,
                json,
                reply,
            })
            .await
            .map_err(|_| "agent thread gone".to_string())?;
        rx.await.map_err(|_| "agent thread dropped reply".to_string())?
    }

    /// Request cancellation of an in-flight upload.
    pub async fn cancel_upload(&self) -> Result<(), String> {
        let (reply, rx) = oneshot::channel();
        self.tx
            .send(AgentCommand::CancelUpload { reply })
            .await
            .map_err(|_| "agent thread gone".to_string())?;
        rx.await.map_err(|_| "agent thread dropped reply".to_string())
    }

    /// Start a cloud print job.
    pub async fn start_print(
        &self,
        request: PrintRequest,
    ) -> Result<PrintResult, String> {
        let (reply, rx) = oneshot::channel();
        self.tx
            .send(AgentCommand::StartPrint { request, reply })
            .await
            .map_err(|_| "agent thread gone".to_string())?;
        rx.await.map_err(|_| "agent thread dropped reply".to_string())?
    }
}

// ---------------------------------------------------------------------------
// Spawn the agent thread
// ---------------------------------------------------------------------------

/// Spawn a dedicated OS thread that owns the `BambuAgent` and processes
/// commands from the channel. Returns an `AgentHandle` for async callers.
///
/// The thread is a plain `std::thread` (not a tokio task) because FFI calls
/// to the .so are blocking and must not run on the tokio executor.
pub fn spawn_agent_thread(agent: BambuAgent) -> AgentHandle {
    let callback_state = Arc::new(CallbackState::new());

    // Copy atomic state from the agent's own CallbackState so the shared Arc
    // reflects the current connection status. The agent's CallbackState is
    // updated by FFI callbacks on the .so's threads — we share the *same*
    // Arc with the agent thread and the handle.
    //
    // However, BambuAgent owns its CallbackState in a Box and the FFI
    // callbacks write to *that* instance. We cannot replace it, so we read
    // from the agent's state on the agent thread (for commands) and also
    // expose a separate Arc<CallbackState> for lock-free reads. The agent
    // thread periodically syncs the shared arc from the agent's state.
    //
    // Actually, simpler: we expose the agent's own CallbackState via Arc.
    // But the agent stores it as Box<CallbackState>. We cannot convert
    // Box → Arc without moving the pointee, which would invalidate the raw
    // pointer the .so holds. So instead we just sync the one atomic we need.
    let shared_state = callback_state.clone();

    let (tx, mut rx) = mpsc::channel::<AgentCommand>(64);

    std::thread::Builder::new()
        .name("bambu-agent".into())
        .spawn(move || {
            // Sync initial connection state
            let connected = agent
                .callback_state()
                .server_connected
                .load(std::sync::atomic::Ordering::SeqCst);
            shared_state
                .server_connected
                .store(connected, std::sync::atomic::Ordering::SeqCst);

            while let Some(cmd) = rx.blocking_recv() {
                // Sync connection state before each command
                let connected = agent
                    .callback_state()
                    .server_connected
                    .load(std::sync::atomic::Ordering::SeqCst);
                shared_state
                    .server_connected
                    .store(connected, std::sync::atomic::Ordering::SeqCst);

                match cmd {
                    AgentCommand::DrainMessages { reply } => {
                        let msgs = agent.drain_messages();
                        let _ = reply.send(msgs);
                    }
                    AgentCommand::SubscribeAndPushall {
                        device_id,
                        timeout,
                        reply,
                    } => {
                        let result = agent.subscribe_and_pushall(&device_id, timeout);
                        let _ = reply.send(result);
                    }
                    AgentCommand::SendMessage {
                        device_id,
                        json,
                        reply,
                    } => {
                        let result = agent.send_message(&device_id, &json);
                        let _ = reply.send(result);
                    }
                    AgentCommand::StartPrint { request, reply } => {
                        let result = agent.start_print(&request);
                        let _ = reply.send(result);
                    }
                    AgentCommand::CancelUpload { reply } => {
                        unsafe { crate::ffi::bambu_shim_request_cancel() };
                        tracing::info!("upload cancellation requested");
                        let _ = reply.send(());
                    }
                }

                // Sync connection state after each command
                let connected = agent
                    .callback_state()
                    .server_connected
                    .load(std::sync::atomic::Ordering::SeqCst);
                shared_state
                    .server_connected
                    .store(connected, std::sync::atomic::Ordering::SeqCst);
            }

            tracing::info!("agent thread exiting — channel closed");
            // agent is dropped here (may hang on .so cleanup; caller should fast_exit)
        })
        .expect("failed to spawn agent thread");

    AgentHandle {
        tx,
        callback_state,
    }
}

/// Create a test handle with a disconnected channel.
/// Commands sent will fail, but `/health`, `/ping`, and cached endpoints work.
#[cfg(test)]
pub fn test_handle() -> AgentHandle {
    let (tx, _rx) = mpsc::channel(1);
    AgentHandle {
        tx,
        callback_state: Arc::new(CallbackState::new()),
    }
}

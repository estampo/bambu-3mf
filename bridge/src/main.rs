//! bambu-bridge — Rust CLI for Bambu Lab printer status and monitoring.
//!
//! Phase 1: `status` and `watch` subcommands (one-shot / stdin-driven)
//! Phase 2: `daemon` subcommand — axum HTTP API on localhost

mod agent;
mod callbacks;
mod fetch;
mod ffi;
mod print_job;
mod server;

use std::io::{self, BufRead, Write};
use std::net::SocketAddr;
use std::path::PathBuf;
use std::process;
use std::time::Duration;

use clap::{Parser, Subcommand};

use agent::{BambuAgent, Credentials};

/// Candidate credentials paths, checked in order.
/// On macOS dirs::config_dir() returns ~/Library/Application Support/
/// but estampo uses ~/.config/ (XDG style), so we check both.
fn credentials_search_paths() -> Vec<PathBuf> {
    let mut paths = Vec::new();
    // Always check XDG-style ~/.config first (cross-platform, estampo default)
    if let Some(home) = dirs::home_dir() {
        paths.push(home.join(".config").join("estampo").join("credentials.toml"));
    }
    // Also check platform config dir (~/Library/Application Support/ on macOS)
    if let Some(config) = dirs::config_dir() {
        let platform_path = config.join("estampo").join("credentials.toml");
        if !paths.contains(&platform_path) {
            paths.push(platform_path);
        }
    }
    paths
}

#[derive(Parser)]
#[command(name = "bambox-bridge", about = "Bambu Lab printer bridge")]
struct Cli {
    #[command(subcommand)]
    command: Command,

    /// Path to libbambu_networking.so
    #[arg(
        long,
        env = "BAMBU_LIB_PATH",
        default_value_t = fetch::default_lib_path(),
    )]
    lib_path: String,

    /// Path to credentials file (TOML or JSON)
    #[arg(long, short, global = true, env = "BAMBU_CREDENTIALS")]
    credentials: Option<PathBuf>,

    /// Disable auto-download of the networking library
    #[arg(long, global = true)]
    no_fetch: bool,

    /// Bambu plugin version for auto-download
    #[arg(long, default_value = "02.05.00.00", global = true)]
    plugin_version: String,

    /// Verbose debug output
    #[arg(short, long, global = true)]
    verbose: bool,
}

#[derive(Subcommand)]
enum Command {
    /// Query live printer state via MQTT (JSON output)
    Status {
        /// Bambu device ID (omit to show all printers)
        device_id: Option<String>,
    },
    /// Long-lived mode: login once, accept commands on stdin
    Watch {
        /// Bambu device ID
        device_id: String,
    },
    /// Start HTTP API daemon on localhost
    Daemon {
        /// Port to listen on
        #[arg(short, long, default_value = "8765")]
        port: u16,
        /// Bind address
        #[arg(long, default_value = "127.0.0.1")]
        bind: String,
    },
}

/// Saved original stdout fd, used to restore after suppressing library noise.
static mut SAVED_STDOUT: i32 = -1;

/// Suppress stdout to hide library noise (e.g. "use_count = 4").
/// Logs (tracing) go to stderr and are unaffected.
fn suppress_stdout() {
    unsafe {
        SAVED_STDOUT = libc::dup(1);
        let devnull = libc::open(b"/dev/null\0".as_ptr() as *const _, libc::O_WRONLY);
        if devnull >= 0 {
            libc::dup2(devnull, 1);
            libc::close(devnull);
        }
    }
}

/// Restore stdout after suppression.
fn restore_stdout() {
    unsafe {
        if SAVED_STDOUT >= 0 {
            libc::dup2(SAVED_STDOUT, 1);
        }
    }
}

/// Fast exit that skips atexit handlers, avoiding .so MQTT thread cleanup hangs.
fn fast_exit(code: i32) -> ! {
    use std::io::Write;
    let _ = io::stdout().flush();
    let _ = io::stderr().flush();
    unsafe { libc::_exit(code) }
}

/// Resolve the credentials path: explicit flag > env var > search defaults.
fn resolve_credentials_path(explicit: &Option<PathBuf>) -> PathBuf {
    if let Some(p) = explicit {
        return p.clone();
    }
    let candidates = credentials_search_paths();
    for path in &candidates {
        if path.is_file() {
            return path.clone();
        }
    }
    let hint = candidates.first()
        .map(|p| p.display().to_string())
        .unwrap_or_else(|| "~/.config/estampo/credentials.toml".to_string());
    eprintln!(
        "error: no credentials file found. Pass --credentials or create {}",
        hint
    );
    process::exit(1);
}

fn load_credentials(path: &PathBuf) -> Credentials {
    if let Some(ext) = path.extension() {
        if ext == "toml" {
            match Credentials::from_toml(path) {
                Ok(c) => return c,
                Err(e) => {
                    eprintln!("error: {e}");
                    process::exit(1);
                }
            }
        }
    }
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(e) => {
            eprintln!("error: cannot read {}: {e}", path.display());
            process::exit(1);
        }
    };
    match Credentials::from_token_json(&text) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("error: {e}");
            process::exit(1);
        }
    }
}

/// Load printer entries from a TOML credentials file.
/// Returns a list of (name, serial) pairs from `[printers.*]` sections.
fn load_printers_from_toml(path: &PathBuf) -> Vec<(String, String)> {
    let text = match std::fs::read_to_string(path) {
        Ok(t) => t,
        Err(_) => return Vec::new(),
    };
    let doc: toml::Value = match text.parse() {
        Ok(v) => v,
        Err(_) => return Vec::new(),
    };
    let printers = match doc.get("printers").and_then(|p| p.as_table()) {
        Some(t) => t,
        None => return Vec::new(),
    };
    printers
        .iter()
        .filter_map(|(name, val)| {
            let serial = val.get("serial")?.as_str()?;
            Some((name.clone(), serial.to_string()))
        })
        .collect()
}

fn init_agent(lib_path: &str, creds: &Credentials) -> BambuAgent {
    let agent = match BambuAgent::new(lib_path) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("error: {e}");
            process::exit(1);
        }
    };
    if let Err(e) = agent.login_and_connect(creds) {
        eprintln!("error: {e}");
        process::exit(1);
    }
    agent
}

/// Find the best (largest, most complete) message from a set.
fn best_message(messages: &[callbacks::MqttMessage]) -> Option<&callbacks::MqttMessage> {
    messages.iter().max_by_key(|m| m.payload.len())
}

fn cmd_watch(agent: &BambuAgent, device_id: &str) -> Result<(), String> {
    let dev_c = std::ffi::CString::new(device_id)
        .map_err(|e| format!("null byte in device_id: {e}"))?;
    let module = std::ffi::CString::new("device").expect("literal contains no NUL");

    unsafe {
        ffi::bambu_shim_set_user_selected_machine(agent.agent_ptr(), dev_c.as_ptr());
    }
    agent
        .callback_state()
        .printer_subscribed
        .store(false, std::sync::atomic::Ordering::SeqCst);
    unsafe {
        ffi::bambu_shim_start_subscribe(agent.agent_ptr(), module.as_ptr());
    }

    let start = std::time::Instant::now();
    while start.elapsed() < Duration::from_secs(3)
        && !agent
            .callback_state()
            .printer_subscribed
            .load(std::sync::atomic::Ordering::SeqCst)
    {
        std::thread::sleep(Duration::from_millis(100));
    }

    restore_stdout();
    println!("{{\"ready\":true}}");
    io::stdout().flush().unwrap();

    let stdin = io::stdin();
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        let line = line.trim().to_string();
        if line.is_empty() {
            continue;
        }
        if line == "quit" || line == "exit" {
            break;
        }

        if line == "status" {
            agent.drain_messages();

            let pushall = r#"{"pushing":{"sequence_id":"0","command":"pushall","version":1,"push_target":1}}"#;
            if let Err(e) = agent.send_message(device_id, pushall) {
                println!("{{\"error\":\"{e}\"}}");
                io::stdout().flush().unwrap();
                continue;
            }

            let start = std::time::Instant::now();
            let timeout = Duration::from_secs(10);
            loop {
                if start.elapsed() >= timeout {
                    break;
                }
                {
                    let msgs = agent.callback_state().messages.lock().unwrap();
                    if msgs
                        .iter()
                        .any(|m| m.payload.len() > 500 && m.payload.contains("gcode_state"))
                    {
                        drop(msgs);
                        std::thread::sleep(Duration::from_millis(300));
                        break;
                    }
                }
                std::thread::sleep(Duration::from_millis(100));
            }

            let messages = agent.drain_messages();
            match best_message(&messages) {
                Some(msg) => println!("{}", msg.payload),
                None => println!("{{\"error\":\"no status received\"}}"),
            }
            io::stdout().flush().unwrap();
        } else {
            println!("{{\"error\":\"unknown command\"}}");
            io::stdout().flush().unwrap();
        }
    }
    Ok(())
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();

    let level = if cli.verbose { "debug" } else { "info" };
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new(level)),
        )
        .with_writer(io::stderr)
        .init();

    // Resolve library path (auto-download if needed).
    let lib_path =
        match fetch::ensure_library(&cli.lib_path, cli.no_fetch, &cli.plugin_version).await {
            Ok(p) => p,
            Err(e) => {
                eprintln!("error: {e}");
                process::exit(1);
            }
        };

    let creds_path = resolve_credentials_path(&cli.credentials);

    match &cli.command {
        Command::Status { device_id } => {
            // If no device_id, list all printers from credentials
            let device_ids: Vec<(String, String)> = match device_id {
                Some(id) => vec![("".to_string(), id.clone())],
                None => {
                    let printers = load_printers_from_toml(&creds_path);
                    if printers.is_empty() {
                        eprintln!("error: no device_id provided and no [printers.*] sections in credentials");
                        process::exit(1);
                    }
                    printers
                }
            };

            suppress_stdout();
            let creds = load_credentials(&creds_path);
            let agent = init_agent(&lib_path, &creds);

            let mut results = Vec::new();
            for (name, dev_id) in &device_ids {
                if let Err(e) =
                    agent.subscribe_and_pushall(dev_id, Duration::from_secs(10))
                {
                    restore_stdout();
                    eprintln!("error: {e}");
                    fast_exit(1);
                }
                let messages = agent.drain_messages();
                if let Some(msg) = best_message(&messages) {
                    let mut val: serde_json::Value =
                        serde_json::from_str(&msg.payload).unwrap_or_else(|_| {
                            serde_json::json!({"raw": msg.payload})
                        });
                    if !name.is_empty() {
                        val["_printer_name"] = serde_json::json!(name);
                    }
                    val["_device_id"] = serde_json::json!(dev_id);
                    results.push(val);
                } else {
                    results.push(serde_json::json!({
                        "_device_id": dev_id,
                        "_printer_name": name,
                        "error": "no status received"
                    }));
                }
            }

            restore_stdout();
            if results.len() == 1 {
                println!("{}", serde_json::to_string(&results[0]).unwrap());
            } else {
                println!("{}", serde_json::to_string(&results).unwrap());
            }
            fast_exit(0);
        }
        Command::Watch { device_id } => {
            suppress_stdout();
            let creds = load_credentials(&creds_path);
            let agent = init_agent(&lib_path, &creds);
            if let Err(e) = cmd_watch(&agent, device_id) {
                eprintln!("error: {e}");
                fast_exit(1);
            }
            fast_exit(0);
        }
        Command::Daemon { port, bind } => {
            let printers = load_printers_from_toml(&creds_path);
            suppress_stdout();
            let creds = load_credentials(&creds_path);
            let agent = init_agent(&lib_path, &creds);
            restore_stdout();

            let state = server::AppState::new(agent, printers);
            let app = server::router(state);

            let addr: SocketAddr = format!("{bind}:{port}")
                .parse()
                .unwrap_or_else(|e| {
                    eprintln!("error: invalid bind address: {e}");
                    process::exit(1);
                });

            tracing::info!("listening on http://{addr}");

            let listener = tokio::net::TcpListener::bind(addr).await.unwrap_or_else(|e| {
                eprintln!("error: cannot bind {addr}: {e}");
                process::exit(1);
            });
            axum::serve(listener, app)
                .await
                .unwrap_or_else(|e| {
                    eprintln!("error: server failed: {e}");
                    process::exit(1);
                });
        }
    }
}

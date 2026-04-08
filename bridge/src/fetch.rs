//! Auto-fetch Bambu Lab's `libbambu_networking` shared library.
//!
//! On first run (when the .so is not found at the configured path), this module
//! downloads it from Bambu's CDN and caches it in a platform-aware directory.

use std::io::Read;
use std::path::{Path, PathBuf};

/// Library filename for the current platform.
#[cfg(target_os = "linux")]
const LIB_FILENAME: &str = "libbambu_networking.so";
#[cfg(target_os = "macos")]
const LIB_FILENAME: &str = "libbambu_networking.dylib";
#[cfg(not(any(target_os = "linux", target_os = "macos")))]
const LIB_FILENAME: &str = "libbambu_networking.so";

/// Default Docker path (unchanged for backwards compatibility).
const DOCKER_LIB_PATH: &str = "/tmp/bambu_plugin/libbambu_networking.so";

/// TLS cert URL from BambuStudio repository.
const CERT_URL: &str =
    "https://raw.githubusercontent.com/bambulab/BambuStudio/master/resources/cert/slicer_base64.cer";

/// Return the platform-specific cache directory for bambox.
pub fn cache_dir() -> Option<PathBuf> {
    dirs::data_dir().map(|d| d.join("bambox"))
}

/// Ensure the networking library is available, downloading if needed.
///
/// Returns the resolved path to the library file.
pub async fn ensure_library(
    lib_path: &str,
    no_fetch: bool,
    plugin_version: &str,
) -> Result<String, String> {
    // 1. If the explicit path exists, use it directly.
    if Path::new(lib_path).is_file() {
        tracing::debug!(path = lib_path, "library found at configured path");
        return Ok(lib_path.to_string());
    }

    // 2. Check platform cache directory.
    if let Some(cache) = cache_dir() {
        let cached_lib = cache.join(LIB_FILENAME);
        if cached_lib.is_file() {
            tracing::info!(path = %cached_lib.display(), "using cached library");
            ensure_cert(&cache).await;
            ensure_subdirs(&cache);
            return Ok(cached_lib.to_string_lossy().into_owned());
        }
    }

    // 3. Nothing found — download or fail.
    if no_fetch {
        return Err(format!(
            "library not found at '{}' and auto-fetch is disabled (--no-fetch)",
            lib_path
        ));
    }

    let cache = cache_dir().ok_or("cannot determine cache directory")?;
    std::fs::create_dir_all(&cache)
        .map_err(|e| format!("cannot create cache dir {}: {e}", cache.display()))?;

    tracing::info!(
        version = plugin_version,
        cache = %cache.display(),
        "downloading Bambu networking library"
    );

    let lib_dest = download_library(&cache, plugin_version).await?;
    ensure_cert(&cache).await;
    ensure_subdirs(&cache);

    Ok(lib_dest)
}

/// Create subdirectories the agent expects (log, config, cert).
fn ensure_subdirs(base: &Path) {
    for sub in &["log", "config", "cert"] {
        let _ = std::fs::create_dir_all(base.join(sub));
    }
}

/// Download the TLS cert if not already cached.
async fn ensure_cert(cache: &Path) {
    let cert_dir = cache.join("cert");
    let _ = std::fs::create_dir_all(&cert_dir);
    let cert_path = cert_dir.join("slicer_base64.cer");
    if cert_path.is_file() {
        return;
    }

    tracing::info!("downloading TLS certificate");
    match download_file(CERT_URL).await {
        Ok(bytes) => {
            if let Err(e) = std::fs::write(&cert_path, &bytes) {
                tracing::warn!(error = %e, "failed to write cert file");
            }
        }
        Err(e) => {
            tracing::warn!(error = %e, "failed to download TLS cert (non-fatal)");
        }
    }
}

/// OS type string for the Bambu API `X-BBL-OS-Type` header.
fn bbl_os_type() -> &'static str {
    #[cfg(target_os = "linux")]
    { "linux" }
    #[cfg(target_os = "macos")]
    { "macos" }
    #[cfg(target_os = "windows")]
    { "windows" }
    #[cfg(not(any(target_os = "linux", target_os = "macos", target_os = "windows")))]
    { "linux" }
}

/// Download the library from Bambu's CDN and extract the .so/.dylib.
async fn download_library(cache: &Path, plugin_version: &str) -> Result<String, String> {
    // Step 1: Query the slicer resource API for the CDN URL.
    let api_url = format!(
        "https://api.bambulab.com/v1/iot-service/api/slicer/resource?slicer/plugins/cloud={}",
        plugin_version
    );

    let client = reqwest::Client::builder()
        .user_agent(format!("BambuStudio/{}", plugin_version))
        .build()
        .map_err(|e| format!("HTTP client error: {e}"))?;

    tracing::debug!(url = %api_url, os_type = bbl_os_type(), "querying plugin resource API");
    let resp = client
        .get(&api_url)
        .header("X-BBL-OS-Type", bbl_os_type())
        .send()
        .await
        .map_err(|e| format!("API request failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("API returned status {}", resp.status()));
    }

    let body: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("invalid API response: {e}"))?;

    // Find the resource entry whose "type" contains "plugins".
    let cdn_url = find_plugin_url(&body)?;
    tracing::debug!(url = %cdn_url, "downloading plugin ZIP");

    // Step 2: Download the ZIP.
    let zip_bytes = download_file(&cdn_url).await?;
    tracing::info!(bytes = zip_bytes.len(), "downloaded plugin ZIP");

    // Step 3: Extract the library from the ZIP.
    let lib_dest = cache.join(LIB_FILENAME);
    extract_library(&zip_bytes, &lib_dest)?;

    tracing::info!(path = %lib_dest.display(), "library extracted");
    Ok(lib_dest.to_string_lossy().into_owned())
}

/// Parse the API response JSON and find the CDN download URL.
fn find_plugin_url(json: &serde_json::Value) -> Result<String, String> {
    // The response can be either:
    //   { "resources": [ { "type": "..plugins..", "url": "..." }, ... ] }
    // or:
    //   { "software": { ... }, "plugins": { "url": "..." } }

    // Try array format first.
    if let Some(resources) = json.get("resources").and_then(|r| r.as_array()) {
        for entry in resources {
            let entry_type = entry.get("type").and_then(|t| t.as_str()).unwrap_or("");
            if entry_type.contains("plugins") {
                if let Some(url) = entry.get("url").and_then(|u| u.as_str()) {
                    return Ok(url.to_string());
                }
            }
        }
    }

    // Try the plugins key directly.
    if let Some(plugins) = json.get("plugins") {
        if let Some(url) = plugins.get("url").and_then(|u| u.as_str()) {
            return Ok(url.to_string());
        }
    }

    Err(format!(
        "no plugin download URL found in API response: {}",
        serde_json::to_string_pretty(json).unwrap_or_default()
    ))
}

/// Download a URL and return the bytes.
async fn download_file(url: &str) -> Result<Vec<u8>, String> {
    let client = reqwest::Client::builder()
        .user_agent("bambu-bridge/0.1")
        .build()
        .map_err(|e| format!("HTTP client error: {e}"))?;

    let resp = client
        .get(url)
        .send()
        .await
        .map_err(|e| format!("download failed: {e}"))?;

    if !resp.status().is_success() {
        return Err(format!("download returned status {}", resp.status()));
    }

    resp.bytes()
        .await
        .map(|b| b.to_vec())
        .map_err(|e| format!("failed to read response body: {e}"))
}

/// Extract the library file from a ZIP archive.
fn extract_library(zip_bytes: &[u8], dest: &Path) -> Result<(), String> {
    let cursor = std::io::Cursor::new(zip_bytes);
    let mut archive =
        zip::ZipArchive::new(cursor).map_err(|e| format!("invalid ZIP archive: {e}"))?;

    for i in 0..archive.len() {
        let mut file = archive
            .by_index(i)
            .map_err(|e| format!("ZIP entry error: {e}"))?;
        let name = file.name().to_string();

        // Look for the library file anywhere in the archive.
        if name.ends_with(LIB_FILENAME) {
            let mut buf = Vec::new();
            file.read_to_end(&mut buf)
                .map_err(|e| format!("failed to read {name} from ZIP: {e}"))?;
            std::fs::write(dest, &buf)
                .map_err(|e| format!("failed to write {}: {e}", dest.display()))?;

            // Make executable on Unix.
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = std::fs::set_permissions(dest, std::fs::Permissions::from_mode(0o755));
            }

            return Ok(());
        }
    }

    // List what we found for debugging.
    let names: Vec<String> = (0..archive.len())
        .filter_map(|i| archive.by_index(i).ok().map(|f| f.name().to_string()))
        .collect();
    Err(format!(
        "{} not found in ZIP archive. Contents: {:?}",
        LIB_FILENAME, names
    ))
}

/// Resolve the default library path: prefer cache dir, fall back to Docker path.
pub fn default_lib_path() -> String {
    if let Some(cache) = cache_dir() {
        let cached = cache.join(LIB_FILENAME);
        if cached.is_file() {
            return cached.to_string_lossy().into_owned();
        }
    }
    // Fall back to Docker path (works inside the container).
    DOCKER_LIB_PATH.to_string()
}

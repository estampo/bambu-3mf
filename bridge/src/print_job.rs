//! Print job preparation — replicates the battle-tested Python bridge.py logic.
//!
//! This module handles:
//! - Config-only 3MF generation (strip gcode, images, MD5)
//! - AMS tray parsing from printer status
//! - AMS mapping (match virtual filament slots to physical trays)
//! - Config 3MF color patching to match AMS tray colors
//!
//! Ported from `src/bambox/bridge.py` — preserving exact behavior.

use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::io::{Cursor, Read, Write};

// ---------------------------------------------------------------------------
// AMS tray parsing (mirrors parse_ams_trays in bridge.py)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AmsTray {
    pub phys_slot: i32,
    pub ams_id: i32,
    pub slot_id: i32,
    #[serde(rename = "type")]
    pub tray_type: String,
    pub color: String,
    pub tray_info_idx: String,
}

/// Extract physical AMS tray info from a printer status dict.
/// Mirrors `parse_ams_trays()` in bridge.py.
pub fn parse_ams_trays(status: &serde_json::Value) -> Vec<AmsTray> {
    let mut trays = Vec::new();
    let ams_data = match status.get("ams") {
        Some(v) => v,
        None => return trays,
    };
    let units = match ams_data.get("ams").and_then(|v| v.as_array()) {
        Some(a) => a,
        None => return trays,
    };

    for unit in units {
        let ams_id = unit
            .get("id")
            .and_then(|v| v.as_str())
            .and_then(|s| s.parse::<i32>().ok())
            .or_else(|| unit.get("id").and_then(|v| v.as_i64()).map(|n| n as i32))
            .unwrap_or(0);

        let unit_trays = match unit.get("tray").and_then(|v| v.as_array()) {
            Some(a) => a,
            None => continue,
        };

        for tray in unit_trays {
            let slot_id = tray
                .get("id")
                .and_then(|v| v.as_str())
                .and_then(|s| s.parse::<i32>().ok())
                .or_else(|| tray.get("id").and_then(|v| v.as_i64()).map(|n| n as i32))
                .unwrap_or(0);

            let fil_type = tray
                .get("tray_type")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            if fil_type.is_empty() {
                continue;
            }

            let color_raw = tray
                .get("tray_color")
                .and_then(|v| v.as_str())
                .unwrap_or("");
            let color = if color_raw.len() >= 6 {
                &color_raw[..6]
            } else {
                color_raw
            };

            trays.push(AmsTray {
                phys_slot: ams_id * 4 + slot_id,
                ams_id,
                slot_id,
                tray_type: fil_type.to_string(),
                color: color.to_string(),
                tray_info_idx: tray
                    .get("tray_info_idx")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
            });
        }
    }

    trays
}

// ---------------------------------------------------------------------------
// AMS mapping (mirrors _build_ams_mapping in bridge.py)
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize)]
pub struct AmsMapping {
    #[serde(rename = "amsMapping")]
    pub mapping: Vec<i32>,
    #[serde(rename = "amsMapping2")]
    pub mapping2: Vec<AmsMapping2Entry>,
}

#[derive(Debug, Serialize)]
pub struct AmsMapping2Entry {
    pub ams_id: i32,
    pub slot_id: i32,
}

/// Build AMS mapping arrays from a 3MF file and live AMS tray state.
/// Mirrors `_build_ams_mapping()` in bridge.py.
pub fn build_ams_mapping(
    threemf_data: &[u8],
    ams_trays: &[AmsTray],
) -> AmsMapping {
    let mut total_slots = 0;
    let mut filament_by_id: HashMap<i32, FilamentInfo> = HashMap::new();

    // Parse 3MF to find filament info
    if let Ok(mut archive) = zip::ZipArchive::new(Cursor::new(threemf_data)) {
        // Read project_settings into a string (separate scope to release borrow)
        let ps_text = {
            let mut buf = String::new();
            if let Ok(mut entry) = archive.by_name("Metadata/project_settings.config") {
                let _ = entry.read_to_string(&mut buf);
            }
            buf
        };
        if !ps_text.is_empty() {
            if let Ok(ps) = serde_json::from_str::<serde_json::Value>(&ps_text) {
                if let Some(colours) = ps.get("filament_colour").and_then(|v| v.as_array()) {
                    total_slots = colours.len();
                }
            }
        }

        // Read slice_info into a string (separate scope to release borrow)
        let si_text = {
            let mut buf = String::new();
            if let Ok(mut entry) = archive.by_name("Metadata/slice_info.config") {
                let _ = entry.read_to_string(&mut buf);
            }
            buf
        };
        if !si_text.is_empty() {
            if let Ok(doc) = roxmltree::Document::parse(&si_text) {
                if let Some(plate_el) = doc
                    .descendants()
                    .find(|n| n.has_tag_name("plate"))
                {
                    for f in plate_el.children().filter(|n| n.has_tag_name("filament")) {
                        let fid: i32 = f
                            .attribute("id")
                            .and_then(|s| s.parse().ok())
                            .unwrap_or(1);
                        let fil_type =
                            f.attribute("type").unwrap_or("").to_string();
                        let color = f.attribute("color").unwrap_or("").to_string();
                        filament_by_id.insert(fid, FilamentInfo { fil_type, color });
                    }
                    if total_slots == 0 && !filament_by_id.is_empty() {
                        total_slots = *filament_by_id.keys().max().unwrap_or(&0) as usize;
                    }
                }
            }
        }
    }

    if filament_by_id.is_empty() {
        return AmsMapping {
            mapping: Vec::new(),
            mapping2: Vec::new(),
        };
    }

    // Match virtual filament slots to physical AMS trays
    let mut mapping = vec![-1i32; total_slots];
    let mut used: HashSet<i32> = HashSet::new();

    let mut sorted_ids: Vec<i32> = filament_by_id.keys().cloned().collect();
    sorted_ids.sort();

    for filament_id in sorted_ids {
        let f = &filament_by_id[&filament_id];
        let idx = (filament_id - 1) as usize;

        let mut best: Option<&AmsTray> = None;
        let mut best_score = 0;

        if !ams_trays.is_empty() {
            for tray in ams_trays {
                if used.contains(&tray.phys_slot) {
                    continue;
                }
                let mut score = 0;
                if tray.tray_type == f.fil_type {
                    score += 2;
                }
                let f_color = f.color.trim_start_matches('#').to_uppercase();
                if tray.color.to_uppercase() == f_color {
                    score += 1;
                }
                if score > best_score {
                    best_score = score;
                    best = Some(tray);
                }
            }
            // Only use if we found a match with score > 0
            if best_score == 0 {
                best = None;
            }
        }

        if idx < mapping.len() {
            if let Some(tray) = best {
                mapping[idx] = tray.phys_slot;
                used.insert(tray.phys_slot);
            } else {
                mapping[idx] = idx as i32; // fallback: identity
            }
        }
    }

    let mapping2: Vec<AmsMapping2Entry> = mapping
        .iter()
        .map(|&slot| {
            if slot >= 0 {
                AmsMapping2Entry {
                    ams_id: slot / 4,
                    slot_id: slot % 4,
                }
            } else {
                AmsMapping2Entry {
                    ams_id: 255,
                    slot_id: 255,
                }
            }
        })
        .collect();

    AmsMapping { mapping, mapping2 }
}

struct FilamentInfo {
    fil_type: String,
    color: String,
}

// ---------------------------------------------------------------------------
// Config-only 3MF generation (mirrors _strip_gcode_from_3mf in bridge.py)
// ---------------------------------------------------------------------------

/// Allowed files in config-only 3MF — exact list from Python bridge.
const ALLOWED_CONFIG_FILES: &[&str] = &[
    "[Content_Types].xml",
    "_rels/.rels",
    "Metadata/slice_info.config",
    "Metadata/model_settings.config",
    "Metadata/project_settings.config",
    "Metadata/_rels/model_settings.config.rels",
];

/// Create a config-only 3MF (no gcode, no images, no MD5).
/// Mirrors `_strip_gcode_from_3mf()` in bridge.py.
pub fn strip_gcode_from_3mf(threemf_data: &[u8]) -> Result<Vec<u8>, String> {
    let reader = Cursor::new(threemf_data);
    let mut archive =
        zip::ZipArchive::new(reader).map_err(|e| format!("bad zip: {e}"))?;

    let mut buf = Vec::new();
    {
        let writer = Cursor::new(&mut buf);
        let mut out = zip::ZipWriter::new(writer);
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);

        for i in 0..archive.len() {
            let mut entry = archive.by_index(i).map_err(|e| format!("zip entry: {e}"))?;
            let name = entry.name().to_string();

            let dominated = ALLOWED_CONFIG_FILES.contains(&name.as_str())
                || (name.starts_with("Metadata/plate_") && name.ends_with(".json"));

            if dominated {
                let mut data = Vec::new();
                entry
                    .read_to_end(&mut data)
                    .map_err(|e| format!("read {name}: {e}"))?;
                out.start_file(&name, options)
                    .map_err(|e| format!("write {name}: {e}"))?;
                out.write_all(&data)
                    .map_err(|e| format!("write {name}: {e}"))?;
            }
        }

        out.finish().map_err(|e| format!("finalize zip: {e}"))?;
    }
    Ok(buf)
}

// ---------------------------------------------------------------------------
// Config 3MF color patching (mirrors _patch_config_3mf_colors in bridge.py)
// ---------------------------------------------------------------------------

/// Patch filament colors in a config-only 3MF to match AMS tray colors.
/// Mirrors `_patch_config_3mf_colors()` in bridge.py.
pub fn patch_config_3mf_colors(
    config_data: &[u8],
    ams_trays: &[AmsTray],
    mapping: &[i32],
) -> Result<Vec<u8>, String> {
    let tray_by_phys: HashMap<i32, &AmsTray> =
        ams_trays.iter().map(|t| (t.phys_slot, t)).collect();

    let reader = Cursor::new(config_data);
    let mut archive =
        zip::ZipArchive::new(reader).map_err(|e| format!("bad zip: {e}"))?;

    // Read all files into memory
    let mut file_data: HashMap<String, Vec<u8>> = HashMap::new();
    for i in 0..archive.len() {
        let mut entry = archive.by_index(i).map_err(|e| format!("zip entry: {e}"))?;
        let name = entry.name().to_string();
        let mut data = Vec::new();
        entry.read_to_end(&mut data).map_err(|e| format!("read: {e}"))?;
        file_data.insert(name, data);
    }

    // Parse and patch slice_info.config (XML)
    let slice_info_key = "Metadata/slice_info.config";
    let slice_info = match file_data.get(slice_info_key) {
        Some(data) => data.clone(),
        None => return Ok(config_data.to_vec()), // no slice info, nothing to patch
    };

    let xml_str = String::from_utf8_lossy(&slice_info);
    let mut changed = false;

    // We need to parse and modify XML. Since roxmltree is read-only, we'll do
    // string-based patching like the Python code does with ET.
    // Parse to find filament elements and their colors, then replace in string.
    let doc = roxmltree::Document::parse(&xml_str)
        .map_err(|e| format!("parse XML: {e}"))?;

    let plate_el = match doc.descendants().find(|n| n.has_tag_name("plate")) {
        Some(el) => el,
        None => return Ok(config_data.to_vec()),
    };

    // Build replacement map: for each filament, check if color needs updating
    let mut color_replacements: Vec<(i32, String)> = Vec::new(); // (filament_id, new_color)
    for f in plate_el.children().filter(|n| n.has_tag_name("filament")) {
        let fid: i32 = f.attribute("id").and_then(|s| s.parse().ok()).unwrap_or(1);
        let idx = (fid - 1) as usize;
        if idx < mapping.len() {
            let phys_slot = mapping[idx];
            if let Some(tray) = tray_by_phys.get(&phys_slot) {
                if phys_slot >= 0 {
                    let new_color = format!("#{}", tray.color);
                    let old_color = f.attribute("color").unwrap_or("");
                    if old_color != new_color {
                        color_replacements.push((fid, new_color));
                        changed = true;
                    }
                }
            }
        }
    }

    if !changed {
        return Ok(config_data.to_vec());
    }

    // Apply replacements to the XML string
    let mut patched_xml = xml_str.to_string();
    for (fid, new_color) in &color_replacements {
        // Find the filament element with this id and replace its color attribute
        // This is a targeted replacement matching the Python ET behavior
        let doc = roxmltree::Document::parse(&patched_xml)
            .map_err(|e| format!("reparse XML: {e}"))?;
        if let Some(plate) = doc.descendants().find(|n| n.has_tag_name("plate")) {
            for f in plate.children().filter(|n| n.has_tag_name("filament")) {
                let this_fid: i32 =
                    f.attribute("id").and_then(|s| s.parse().ok()).unwrap_or(0);
                if this_fid == *fid {
                    if let Some(old_color) = f.attribute("color") {
                        // Replace color="OLD" with color="NEW" in this specific element
                        let range = f.range();
                        let element_str = &patched_xml[range.clone()];
                        let updated = element_str.replace(
                            &format!("color=\"{}\"", old_color),
                            &format!("color=\"{}\"", new_color),
                        );
                        patched_xml.replace_range(range, &updated);
                    }
                    break;
                }
            }
        }
    }
    file_data.insert(slice_info_key.to_string(), patched_xml.into_bytes());

    // Also patch project_settings filament_colour (mirrors Python)
    if let Some(ps_data) = file_data.get("Metadata/project_settings.config").cloned() {
        if let Ok(mut ps) = serde_json::from_slice::<serde_json::Value>(&ps_data) {
            if let Some(colours) = ps.get_mut("filament_colour").and_then(|v| v.as_array_mut()) {
                for (fid, new_color) in &color_replacements {
                    let idx = (*fid - 1) as usize;
                    if idx < colours.len() {
                        colours[idx] = serde_json::Value::String(new_color.clone());
                    }
                }
                if let Ok(ps_bytes) = serde_json::to_vec(&ps) {
                    file_data.insert(
                        "Metadata/project_settings.config".to_string(),
                        ps_bytes,
                    );
                }
            }
        }
    }

    // Rewrite zip
    let mut buf = Vec::new();
    {
        let writer = Cursor::new(&mut buf);
        let mut out = zip::ZipWriter::new(writer);
        let options = zip::write::SimpleFileOptions::default()
            .compression_method(zip::CompressionMethod::Deflated);
        for (name, data) in &file_data {
            out.start_file(name, options)
                .map_err(|e| format!("write {name}: {e}"))?;
            out.write_all(data)
                .map_err(|e| format!("write {name}: {e}"))?;
        }
        out.finish().map_err(|e| format!("finalize: {e}"))?;
    }
    Ok(buf)
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_ams_trays_from_real_status() {
        let status = serde_json::json!({
            "ams": {
                "ams": [{
                    "id": "0",
                    "tray": [
                        {"id": "0", "tray_type": "PLA", "tray_color": "FFFFFFFF", "tray_info_idx": "GFL99"},
                        {"id": "1", "tray_type": "ASA", "tray_color": "BCBCBCFF", "tray_info_idx": "GFB98"},
                        {"id": "2", "tray_type": "PETG-CF", "tray_color": "2850E0FF", "tray_info_idx": "GFG98"},
                        {"id": "3", "tray_type": "PLA", "tray_color": "161616FF", "tray_info_idx": "GFL99"},
                    ]
                }]
            }
        });
        let trays = parse_ams_trays(&status);
        assert_eq!(trays.len(), 4);
        assert_eq!(trays[0].phys_slot, 0);
        assert_eq!(trays[0].tray_type, "PLA");
        assert_eq!(trays[0].color, "FFFFFF");
        assert_eq!(trays[1].phys_slot, 1);
        assert_eq!(trays[1].tray_type, "ASA");
        assert_eq!(trays[2].phys_slot, 2);
        assert_eq!(trays[2].tray_type, "PETG-CF");
        assert_eq!(trays[3].phys_slot, 3);
        assert_eq!(trays[3].color, "161616");
    }

    #[test]
    fn parse_ams_trays_empty_type_skipped() {
        let status = serde_json::json!({
            "ams": {
                "ams": [{
                    "id": "0",
                    "tray": [
                        {"id": "0", "tray_type": "", "tray_color": "FFFFFF"},
                        {"id": "1", "tray_type": "PLA", "tray_color": "000000FF"},
                    ]
                }]
            }
        });
        let trays = parse_ams_trays(&status);
        assert_eq!(trays.len(), 1);
        assert_eq!(trays[0].slot_id, 1);
    }

    #[test]
    fn parse_ams_trays_no_ams_data() {
        let status = serde_json::json!({"gcode_state": "IDLE"});
        let trays = parse_ams_trays(&status);
        assert!(trays.is_empty());
    }

    #[test]
    fn parse_ams_trays_multi_unit() {
        let status = serde_json::json!({
            "ams": {
                "ams": [
                    {"id": "0", "tray": [{"id": "0", "tray_type": "PLA", "tray_color": "FFFFFF", "tray_info_idx": ""}]},
                    {"id": "1", "tray": [{"id": "0", "tray_type": "ABS", "tray_color": "000000", "tray_info_idx": ""}]},
                ]
            }
        });
        let trays = parse_ams_trays(&status);
        assert_eq!(trays.len(), 2);
        assert_eq!(trays[0].phys_slot, 0); // unit 0, slot 0
        assert_eq!(trays[1].phys_slot, 4); // unit 1, slot 0
    }

    #[test]
    fn strip_gcode_from_3mf_basic() {
        // Create a minimal 3MF zip in memory
        let mut buf = Vec::new();
        {
            let writer = Cursor::new(&mut buf);
            let mut zip = zip::ZipWriter::new(writer);
            let opts = zip::write::SimpleFileOptions::default();

            zip.start_file("[Content_Types].xml", opts).unwrap();
            zip.write_all(b"<Types/>").unwrap();

            zip.start_file("_rels/.rels", opts).unwrap();
            zip.write_all(b"<Relationships/>").unwrap();

            zip.start_file("Metadata/slice_info.config", opts).unwrap();
            zip.write_all(b"<config/>").unwrap();

            zip.start_file("Metadata/plate_1.json", opts).unwrap();
            zip.write_all(b"{}").unwrap();

            // These should be stripped:
            zip.start_file("Metadata/plate_1.gcode", opts).unwrap();
            zip.write_all(b"G28\nG1 X10\n").unwrap();

            zip.start_file("Metadata/plate_1.gcode.md5", opts).unwrap();
            zip.write_all(b"abc123").unwrap();

            zip.start_file("Metadata/plate_1.png", opts).unwrap();
            zip.write_all(b"PNG_DATA").unwrap();

            zip.finish().unwrap();
        }

        let config = strip_gcode_from_3mf(&buf).unwrap();
        let reader = Cursor::new(&config);
        let archive = zip::ZipArchive::new(reader).unwrap();
        let names: Vec<&str> = archive.file_names().collect();

        assert!(names.contains(&"[Content_Types].xml"));
        assert!(names.contains(&"_rels/.rels"));
        assert!(names.contains(&"Metadata/slice_info.config"));
        assert!(names.contains(&"Metadata/plate_1.json"));
        // Stripped:
        assert!(!names.contains(&"Metadata/plate_1.gcode"));
        assert!(!names.contains(&"Metadata/plate_1.gcode.md5"));
        assert!(!names.contains(&"Metadata/plate_1.png"));
    }

    #[test]
    fn strip_gcode_from_3mf_invalid_zip() {
        let result = strip_gcode_from_3mf(b"not a zip file");
        assert!(result.is_err());
        assert!(result.unwrap_err().contains("bad zip"));
    }

    #[test]
    fn strip_gcode_from_3mf_empty_zip() {
        // Create a valid but empty zip
        let mut buf = Vec::new();
        {
            let writer = Cursor::new(&mut buf);
            let zip = zip::ZipWriter::new(writer);
            zip.finish().unwrap();
        }
        let result = strip_gcode_from_3mf(&buf).unwrap();
        let reader = Cursor::new(&result);
        let archive = zip::ZipArchive::new(reader).unwrap();
        assert_eq!(archive.len(), 0);
    }

    #[test]
    fn parse_ams_trays_integer_ids() {
        // Some firmware sends numeric IDs rather than string IDs
        let status = serde_json::json!({
            "ams": {
                "ams": [{
                    "id": 0,
                    "tray": [
                        {"id": 0, "tray_type": "PLA", "tray_color": "FF0000FF", "tray_info_idx": ""},
                    ]
                }]
            }
        });
        let trays = parse_ams_trays(&status);
        assert_eq!(trays.len(), 1);
        assert_eq!(trays[0].ams_id, 0);
        assert_eq!(trays[0].slot_id, 0);
        assert_eq!(trays[0].color, "FF0000");
    }

    #[test]
    fn parse_ams_trays_short_color() {
        let status = serde_json::json!({
            "ams": {
                "ams": [{
                    "id": "0",
                    "tray": [
                        {"id": "0", "tray_type": "PLA", "tray_color": "FFF", "tray_info_idx": ""},
                    ]
                }]
            }
        });
        let trays = parse_ams_trays(&status);
        assert_eq!(trays.len(), 1);
        assert_eq!(trays[0].color, "FFF"); // short color returned as-is
    }

    #[test]
    fn parse_ams_trays_missing_tray_array() {
        let status = serde_json::json!({
            "ams": {
                "ams": [{
                    "id": "0"
                    // no "tray" key
                }]
            }
        });
        let trays = parse_ams_trays(&status);
        assert!(trays.is_empty());
    }

    #[test]
    fn build_ams_mapping_empty_trays() {
        let mapping = build_ams_mapping(b"not a zip", &[]);
        assert!(mapping.mapping.is_empty());
        assert!(mapping.mapping2.is_empty());
    }

    #[test]
    fn ams_mapping2_entry_structure() {
        let entry = AmsMapping2Entry {
            ams_id: 1,
            slot_id: 2,
        };
        let json = serde_json::to_string(&entry).unwrap();
        assert!(json.contains("\"ams_id\":1"));
        assert!(json.contains("\"slot_id\":2"));
    }

    #[test]
    fn ams_tray_serialization() {
        let tray = AmsTray {
            phys_slot: 5,
            ams_id: 1,
            slot_id: 1,
            tray_type: "PLA".into(),
            color: "FFFFFF".into(),
            tray_info_idx: "GFL99".into(),
        };
        let json = serde_json::to_string(&tray).unwrap();
        let parsed: serde_json::Value = serde_json::from_str(&json).unwrap();
        assert_eq!(parsed["phys_slot"], 5);
        assert_eq!(parsed["type"], "PLA"); // serde rename
        assert_eq!(parsed["color"], "FFFFFF");
    }

    #[test]
    fn ams_tray_deserialization() {
        let json = r#"{"phys_slot":0,"ams_id":0,"slot_id":0,"type":"ASA","color":"BCBCBC","tray_info_idx":"GFB98"}"#;
        let tray: AmsTray = serde_json::from_str(json).unwrap();
        assert_eq!(tray.tray_type, "ASA");
        assert_eq!(tray.phys_slot, 0);
    }

    #[test]
    fn phys_slot_calculation() {
        // ams_id * 4 + slot_id
        // Unit 0, slot 3 -> 3
        // Unit 1, slot 0 -> 4
        // Unit 2, slot 2 -> 10
        let status = serde_json::json!({
            "ams": {
                "ams": [
                    {"id": "0", "tray": [{"id": "3", "tray_type": "PLA", "tray_color": "000000", "tray_info_idx": ""}]},
                    {"id": "1", "tray": [{"id": "0", "tray_type": "ABS", "tray_color": "111111", "tray_info_idx": ""}]},
                    {"id": "2", "tray": [{"id": "2", "tray_type": "TPU", "tray_color": "222222", "tray_info_idx": ""}]},
                ]
            }
        });
        let trays = parse_ams_trays(&status);
        assert_eq!(trays.len(), 3);
        assert_eq!(trays[0].phys_slot, 3);
        assert_eq!(trays[1].phys_slot, 4);
        assert_eq!(trays[2].phys_slot, 10);
    }
}

//! 配置持久化。
//!
//! 参数存到用户目录下的 `adb_bridge_rs.json`，下次启动自动加载。
//!
//! 位置选择：`%APPDATA%\adb_bridge_rs\config.json`（Windows）
//! 或 `~/.config/adb_bridge_rs/config.json`（其他平台）——
//! 遵循各平台的惯例，不污染程序目录。

use std::path::PathBuf;

use crate::Config;

/// 配置文件名（不含目录）。
pub const CONFIG_FILE: &str = "config.json";

/// 配置目录路径。
pub fn config_dir() -> PathBuf {
    if let Ok(appdata) = std::env::var("APPDATA") {
        if !appdata.is_empty() {
            return PathBuf::from(appdata).join("adb_bridge_rs");
        }
    }
    if let Ok(home) = std::env::var("HOME") {
        if !home.is_empty() {
            return PathBuf::from(home).join(".config").join("adb_bridge_rs");
        }
    }
    PathBuf::from(".")
}

/// 配置文件完整路径。
pub fn config_path() -> PathBuf {
    config_dir().join(CONFIG_FILE)
}

/// 保存配置。
///
/// ⚠️ 端口不能用 5555~5585（会被 adb server 当成模拟器），保存时自动纠正，
/// 避免手工编辑配置文件后出现幽灵设备。
pub fn save(cfg: &Config) -> std::io::Result<PathBuf> {
    let mut c = cfg.clone();
    if crate::is_emulator_port(c.listen_port) {
        c.listen_port = crate::recommended_port();
    }
    let dir = config_dir();
    std::fs::create_dir_all(&dir)?;
    let p = dir.join(CONFIG_FILE);
    let json = to_json(&c);
    std::fs::write(&p, json)?;
    Ok(p)
}

/// 加载配置。
///
/// 文件不存在时返回默认配置；**内容损坏时不 panic**，退回默认并保留原文件，
/// 避免用户手动改坏后程序无法启动。
pub fn load() -> Config {
    let p = config_path();
    match std::fs::read_to_string(&p) {
        Ok(s) => from_json(&s).unwrap_or_else(|| {
            eprintln!(
                "[config] {} 内容无法解析，改用默认配置（原文件已保留）",
                p.display()
            );
            let _ = std::fs::rename(&p, p.with_extension("json.bad"));
            Config::default()
        }),
        Err(_) => Config::default(),
    }
}

/// 配置 → JSON（手写序列化，避免引入 serde 依赖影响命令行版构建）。
fn to_json(c: &Config) -> String {
    format!(
        "{{\n  \"listen_addr\": \"{}\",\n  \"listen_port\": {},\n  \"serial\": {},\n  \"server_port\": {},\n  \"no_kill_port\": {},\n  \"debug_packets\": {}\n}}\n",
        c.listen_addr,
        c.listen_port,
        match &c.serial {
            Some(s) => format!("\"{}\"", s.replace('"', "\\\"")),
            None => "null".to_string(),
        },
        c.server_port,
        c.no_kill_port,
        c.debug_packets
    )
}

/// JSON → 配置。
///
/// 解析失败返回 None（由调用方决定退回默认）。字段缺失时用默认值补齐，
/// 这样旧版本配置文件也能加载。
pub fn from_json(s: &str) -> Option<Config> {
    let mut c = Config::default();

    if let Some(v) = json_str(s, "listen_addr") {
        c.listen_addr = v;
    }
    if let Some(v) = json_num(s, "listen_port") {
        c.listen_port = v as u16;
    }
    if let Some(v) = json_num(s, "server_port") {
        c.server_port = v as u16;
    }
    // serial 可为 null
    if let Some(v) = json_str(s, "serial") {
        c.serial = Some(v);
    }
    if let Some(v) = json_bool(s, "no_kill_port") {
        c.no_kill_port = v;
    }
    if let Some(v) = json_bool(s, "debug_packets") {
        c.debug_packets = v;
    }
    Some(c)
}

/// 极简 JSON 字符串字段提取。
///
/// 只处理 `"key": "value"` 与 `"key": null` 两种形式 ——
/// 配置是我们自己写出的，格式受控，不需要完整的 JSON 解析器。
fn json_str(s: &str, key: &str) -> Option<String> {
    let pat = format!("\"{}\"", key);
    let i = s.find(&pat)? + pat.len();
    let rest = &s[i..];
    let rest = rest.trim_start().strip_prefix(':')?.trim_start();
    if rest.starts_with("null") {
        return None;
    }
    let rest = rest.strip_prefix('"')?;
    let end = rest.find('"')?;
    Some(rest[..end].replace("\\\"", "\""))
}

/// 极简 JSON 数值字段提取。
fn json_num(s: &str, key: &str) -> Option<i64> {
    let pat = format!("\"{}\"", key);
    let i = s.find(&pat)? + pat.len();
    let rest = &s[i..];
    let rest = rest.trim_start().strip_prefix(':')?.trim_start();
    let end = rest
        .find(|c: char| !c.is_ascii_digit() && c != '-')
        .unwrap_or(rest.len());
    rest[..end].parse().ok()
}

/// 极简 JSON 布尔字段提取。
fn json_bool(s: &str, key: &str) -> Option<bool> {
    let pat = format!("\"{}\"", key);
    let i = s.find(&pat)? + pat.len();
    let rest = &s[i..];
    let rest = rest.trim_start().strip_prefix(':')?.trim_start();
    if rest.starts_with("true") {
        Some(true)
    } else if rest.starts_with("false") {
        Some(false)
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn roundtrip_config() {
        let c = Config {
            listen_addr: "0.0.0.0".into(),
            listen_port: 15555,
            serial: Some("abc123".into()),
            server_port: 5037,
            no_kill_port: true,
            debug_packets: false,
        };
        let s = to_json(&c);
        let back = from_json(&s).expect("应能解析");
        assert_eq!(back.listen_port, 15555);
        assert_eq!(back.listen_addr, "0.0.0.0");
        assert_eq!(back.serial.as_deref(), Some("abc123"));
        assert_eq!(back.server_port, 5037);
        assert!(back.no_kill_port);
        assert!(!back.debug_packets);
    }

    #[test]
    fn serial_null_becomes_none() {
        let c = Config {
            serial: None,
            ..Config::default()
        };
        let s = to_json(&c);
        assert!(s.contains("\"serial\": null"));
        let back = from_json(&s).unwrap();
        assert_eq!(back.serial, None);
    }

    #[test]
    fn missing_fields_fall_back_to_defaults() {
        // 旧版本配置文件只有部分字段
        let s = r#"{"listen_port": 16666}"#;
        let c = from_json(s).unwrap();
        assert_eq!(c.listen_port, 16666);
        assert_eq!(c.server_port, 5037, "缺失字段应用默认值");
        assert_eq!(c.listen_addr, "0.0.0.0");
    }

    #[test]
    fn dangerous_port_is_corrected_on_save() {
        let c = Config {
            listen_port: 5555, // 危险值
            ..Config::default()
        };
        let mut corrected = c.clone();
        if crate::is_emulator_port(corrected.listen_port) {
            corrected.listen_port = crate::recommended_port();
        }
        assert_eq!(corrected.listen_port, 15555);
        assert!(!crate::is_emulator_port(corrected.listen_port));
    }

    #[test]
    fn parses_all_fields() {
        let s = r#"{
  "listen_addr": "127.0.0.1",
  "listen_port": 15555,
  "serial": "xyz",
  "server_port": 5038,
  "no_kill_port": true,
  "debug_packets": true
}"#;
        let c = from_json(s).unwrap();
        assert_eq!(c.listen_addr, "127.0.0.1");
        assert_eq!(c.listen_port, 15555);
        assert_eq!(c.serial.as_deref(), Some("xyz"));
        assert_eq!(c.server_port, 5038);
        assert!(c.no_kill_port);
        assert!(c.debug_packets);
    }

    #[test]
    fn json_helpers() {
        let s = r#"{"a": 1, "b": "x", "c": true, "d": false, "e": null}"#;
        assert_eq!(json_num(s, "a"), Some(1));
        assert_eq!(json_str(s, "b").as_deref(), Some("x"));
        assert_eq!(json_bool(s, "c"), Some(true));
        assert_eq!(json_bool(s, "d"), Some(false));
        assert_eq!(json_str(s, "e"), None);
        assert_eq!(json_num(s, "nope"), None);
    }

    #[test]
    fn config_path_is_absolute_or_fallback() {
        let p = config_path();
        assert!(p.ends_with(CONFIG_FILE));
    }
}
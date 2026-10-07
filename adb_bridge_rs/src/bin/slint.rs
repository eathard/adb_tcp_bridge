//! Slint 界面入口。
//!
//! 构建：`cargo build --release --features slint-ui`
//! 产物：`target/release/adb_bridge_slint.exe`
//!
//! 与 `adb_bridge_rs` 主入口的区别：本入口直接打开 Slint 图形界面，
//! 且在 release 下隐藏控制台窗口。

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let cfg = adb_bridge_rs::parse_args();
    if cfg.debug_packets {
        adb_bridge_rs::log::set_debug(true);
    }
    adb_bridge_rs::slint_ui::run(cfg);
}

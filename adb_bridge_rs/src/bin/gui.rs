//! 图形界面专用入口。
//!
//! 与主入口 `adb_bridge_rs` 的区别只有一个：本入口把子系统设为
//! `windows`，**双击运行时不弹出控制台黑窗**。
//!
//! 主入口保持控制台子系统，以便命令行参数、帮助信息、以及
//! 现有测试套件按原样工作。
//!
//! 构建：`cargo build --release --features gui`
//! 产物：`target/release/adb_bridge_rs_gui.exe`

// 只在 release 下隐藏控制台：debug 构建仍希望看到 panic 信息
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    let cfg = adb_bridge_rs::parse_args();
    if cfg.debug_packets {
        adb_bridge_rs::log::set_debug(true);
    }
    adb_bridge_rs::gui::run(cfg);
}

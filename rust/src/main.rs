//! ADB TCP 桥 —— Rust 重构版入口。
//!
//! 把本机 USB 设备 C 暴露成标准 TCP adb 设备，使电脑 B 只需
//! `adb connect <A的局域网IP>:15555` 就能像普通网络设备一样使用。
//!
//! # 启动方式
//!
//! 本入口**只提供命令行模式**，便于脚本与测试套件调用（参数照原样解析，
//! 结果只走 stdout/退出码）。
//!
//! 图形界面是一个**独立入口** `adb_bridge_slint.exe`
//! （`cargo build --release --features slint-ui`），双击即开，
//! 与本入口的启动方式互不影响。
//!
//! ```text
//! adb_bridge_rs [选项]
//!   --listen-port <端口>    监听端口（默认 15555）
//!   --listen-addr <地址>    监听地址（默认 0.0.0.0）
//!   --serial <serial>       目标设备（默认第一台在线设备）
//!   --server-port <端口>    adb server 端口（默认 5037）
//!   --no-kill-port          端口被占用时不结束占用进程
//!   --debug-packets         打印每个包的收发（排障用）
//!   --save-config           把当前参数保存到配置文件
//! ```
//!
//! ⚠️ **端口必须避开 5555~5585**：本机 adb server 会扫描该段查找模拟器，
//! 桥若占用其中之一，`adb devices` 会出现幽灵设备 emulator-5554。

use adb_bridge_rs::{config, log, start_bridge};
use adb_bridge_rs::parse_args;

/// 把 Windows 控制台切到 UTF-8。
///
/// ⚠️ 控制台的默认代码页是 GBK(936)，而 Rust 的 stdout 写出的是 **UTF-8 字节流**，
/// 不做处理时中文日志会整片显示成乱码／方块 ——
/// 「程序本身没问题但一跑就乱码」的典型原因就在这里。
///
/// 必须在**任何输出之前**调用，所以放在 `main` 的第一行。
#[cfg(windows)]
fn init_console_utf8() {
    // 只声明需要的最小 API，不引入额外 crate
    extern "system" {
        fn SetConsoleOutputCP(code_page: u32) -> i32;
        fn SetConsoleCP(code_page: u32) -> i32;
    }
    const CP_UTF8: u32 = 65001;
    // SAFETY: 两个函数只改当前进程控制台的代码页，无内存安全影响；
    // 程序未附着控制台时（纯 GUI 启动）调用它们是无害的空操作。
    unsafe {
        SetConsoleOutputCP(CP_UTF8);
        SetConsoleCP(CP_UTF8);
    }
}

#[cfg(not(windows))]
fn init_console_utf8() {}

fn main() {
    init_console_utf8();

    let raw: Vec<String> = std::env::args().skip(1).collect();
    let want_save = raw.iter().any(|a| a == "--save-config");

    let cfg = parse_args();
    if cfg.debug_packets {
        log::set_debug(true);
    }

    // --save-config：只保存参数后退出，便于脚本化配置
    if want_save {
        match config::save(&cfg) {
            Ok(p) => log::info("bridge", format!("配置已保存到 {}", p.display())),
            Err(e) => {
                log::error("bridge", format!("保存配置失败: {}", e));
                std::process::exit(1);
            }
        }
        return;
    }

    log::info("bridge", "ADB TCP 桥启动（Rust 版）");
    match start_bridge(&cfg) {
        Ok(p) => log::info("bridge", format!("桥已停止（端口 {}）", p)),
        Err(e) => log::error("bridge", format!("启动失败: {}", e)),
    }
}

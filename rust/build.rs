// Copyright © 2026 ADB TCP 桥项目
// SPDX-License-Identifier: GPL-3.0-only
//
// 编译 .slint 界面描述文件。
//
// 只在启用 `slint-ui` 特性时做实际工作，否则直接返回 ——
// 这样核心桥（无界面）构建完全不受影响。

fn main() {
    #[cfg(feature = "slint-ui")]
    {
        // 把 ui/main.slint 编译成 Rust 代码，产物通过 include_modules! 引入
        slint_build::compile("ui/main.slint").expect("Slint 界面编译失败");
        println!("cargo:rerun-if-changed=ui/main.slint");
    }

    // 未启用界面特性时也要声明，否则改了 .slint 不会触发重编
    #[cfg(not(feature = "slint-ui"))]
    {
        println!("cargo:rerun-if-changed=ui/main.slint");
    }
}

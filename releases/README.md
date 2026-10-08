# 编译产物

本目录存放**构建出来的可执行文件**，用于上传到 GitHub Release。

> ⚠️ 本目录下的二进制**不入库**（见根目录 `.gitignore`），
> 只有这个 README 会进仓库。仓库里只放源码，避免 clone 变慢。

## 产物清单

| 文件 | 来源 | 说明 |
|---|---|---|
| `adb_bridge_slint.exe` | `rust/` · `--features slint-ui` | **推荐**。Slint 图形界面版，6 个页面（控制台 / 会话 / 运行日志 / 参数设置 / 诊断工具 / 关于软件） |
| `adb_bridge_rs_gui.exe` | `rust/` · `--features gui` | egui 图形界面版（旧实现，保留用于回退对比） |
| `adb_bridge_rs.exe` | `rust/` · 默认 | 命令行版，无界面 |
| `python/adb_tcp_bridge.dist/` | `python/` · Nuitka | Python 版的免环境打包产物（含 python313.dll） |

## 自己构建

```bash
# Rust 版（默认就落在 rust/target/release/ 下，需要时复制到本目录）
cd rust
cargo build --release --features slint-ui --offline
cp target/release/adb_bridge_slint.exe ../releases/

# Python 版（产物自动输出到本目录）
cd python
python build_nuitka.py
```

## 上传到 GitHub Release

```bash
git tag -a v1.0.0 -m "v1.0.0：首个归档版本"
git push origin v1.0.0

gh release create v1.0.0 \
  releases/adb_bridge_slint.exe \
  releases/adb_bridge_rs_gui.exe \
  releases/adb_bridge_rs.exe \
  --title "v1.0.0" \
  --notes "见 CHANGELOG.md"
```

Python 版是**目录**形式的产物，上传前先打包：

```bash
cd releases/python && zip -qr ../adb_tcp_bridge-windows-x64.zip adb_tcp_bridge.dist && cd ..
gh release upload v1.0.0 adb_tcp_bridge-windows-x64.zip
```

## 运行要求

- 电脑 A 上已安装 **adb** 且在 `PATH` 中
- 设备 C 的 adb 驱动由厂商定制（Rockchip 改版），**必须用厂商提供的 adb**
- 防火墙需放行监听端口（默认 **15555**）的入站 TCP

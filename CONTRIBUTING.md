# 参与贡献

感谢你愿意花时间在这个项目上。

## 先读这些

动手之前，建议先读两篇文档，能省掉很多来回：

- [`docs/软件说明书.md`](docs/软件说明书.md) —— 两侧协议为什么不对称、参数含义、技术规格
- [`rust/README.md`](rust/README.md) —— 模块划分，**协议实现的 7 条硬约束**、
  界面实现约束（这些约束都是踩过的坑，改代码前务必知道）

## 项目结构

```
python/    Python 版（参考实现与对照基线）
rust/      Rust 版（推荐，含图形界面）
tests/     回归测试（Python 脚本，全部平铺在一个目录）
docs/      文档与界面原型
releases/  编译产物（不入库，用于 GitHub Release）
```

Python 版与 Rust 版**协议字节级兼容**。Rust 版有 7 项单元测试是与 Python 版的
逐字节交叉校验，改协议相关代码后请务必跑一遍。

## 开发环境

### Python 版

```bash
cd python
python adb_tcp_bridge.py --listen-port 15555
```

只需要标准库，无第三方依赖（打包 Nuitka 时才需要额外装包）。

### Rust 版

```bash
cd rust
cargo build --release --offline     # 命令行版，无外部依赖
cargo test  --release --offline     # 93 项单元测试
```

> ⚠️ **图形界面版需要先改 `Cargo.toml`**：Slint 依赖在开发机上写成了
> `path = "D:/slint-ui/slint/..."`，换机器直接编译会失败。
> 把 `slint` 与 `slint-build` 两处改成版本号 `slint = "1.16"` /
> `slint-build = "1.16"` 即可（首次编译需联网下载依赖，约 16 分钟）。
> 只编译命令行版不受影响。

## 跑测试

单元测试不需要真实设备：

```bash
cd rust && cargo test --release --offline
```

`tests/` 下的脚本**需要真实设备**（电脑 A + USB 设备 C + 局域网电脑 B），
桥也要处于运行状态。详见 [`tests/README.md`](tests/README.md)。

改动协议行为后至少要跑：

```bash
cd tests
python consistency_test.py          # 第一批 50 条
python consistency_test2.py         # 第二批 50 条
python stress_test.py               # 基础压测
```

## 提交规范

- 提交信息用 [Conventional Commits](https://www.conventionalcommits.org/zh-hans/)：
  `feat:` / `fix:` / `docs:` / `refactor:` / `test:` / `chore:`
- 一个提交只做一件事
- 改了行为就同步更新 `CHANGELOG.md` 与相关文档

## 版本号改动清单

发版时版本号散落在多处，且 **Slint 界面读不到 `Cargo.toml`**（版本号是
硬编码的字符串），漏改就会显示成旧版本。按下面清单逐处同步：

| # | 位置 | 写法 |
|---|---|---|
| 1 | `rust/Cargo.toml` 的 `version` | `1.0.0` |
| 2 | `rust/Cargo.lock` 里本包的 `version` | 紧跟上一条，由 cargo 自动同步 |
| 3 | **`rust/ui/main.slint`** 的底部版权行 | `adb_bridge_rs v1.0.0 · GPL-3.0-only` |
| 4 | `CHANGELOG.md` 的版本标题与「命名约定」 | `## [1.0.0]` |
| 5 | `docs/软件说明书.md` 开头的「版本」 | `1.0.0` |
| 6 | `docs/部署指南.md` 开头的「适用版本」 | `1.0.0` |
| 7 | `releases/README.md` 的 tag / Release 命令 | `v1.0.0` |
| 8 | Git 附注标签 | `git tag -a v1.0.0 -m "..."` |

注意事项：

- 版本号**必须是三段式**。`Cargo.toml` 写 `1.0` 会直接报
  `unexpected end of input while parsing minor version number`（semver 要求）。
- 改了 `ui/main.slint` 后**必须重新编译** Slint 版才能让界面显示新版本号：
  `cargo build --release --features slint-ui --offline`（约 16 分钟）。
- 改完提交后，把标签移到最新提交：`git tag -f v1.0.0 <commit>`，
  否则标签会落在旧版本上。

## 提 Issue

提 bug 时请附上：

- 现象与复现步骤
- 用的是 Python 版还是 Rust 版（哪个 exe / 哪个提交）
- 电脑 A 与 B 的系统、adb 版本
- 桥的日志（用 `--debug-packets` 跑一次最有价值）
- 桥报的连接超时 / 连接被拒绝，这两者含义完全不同，请写清楚是哪种

## 安全

本项目会在局域网内开放一个可执行 **root shell** 的端口。
如果你发现安全问题，请**不要公开提 Issue**，直接联系维护者。

## 许可证

贡献即表示你同意你的代码以 **GPL-3.0-only** 授权发布。

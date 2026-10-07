"""把 adb_tcp_bridge.py 编译为 Windows 独立可执行文件（Nuitka + MSVC）。

用法
----
    python build_nuitka.py            # 编译，产物在 adb_tcp_bridge.dist/
    python build_nuitka.py --debug    # 附带 --debug --show-scons，排查用

前置条件
--------
1. Visual Studio Build Tools 2022（含 C++ 生成工具），例如装在
   C:\\Program Files (x86)\\Microsoft Visual Studio\\2022\\BuildTools
2. Python 依赖：nuitka、scons、pywin32、ordered-set、zstandard
       pip install nuitka scons pywin32 ordered-set zstandard

关于 --msvc=latest 与Windows SDK
--------------------------------
Nuitka 的 Scons 后端需要知道 Windows SDK 版本。若本机 reg.exe 被安全策略拦截，
vcvars64.bat 采集到的 SDK 信息会残缺（WindowsSDKVersion 只剩一个反斜杠），
Nuitka 会误判"未安装 SDK"并把 CC 置空，报：

    FATAL: scons environment variable 'CC' is not set

本脚本在编译前自动调用 fix_nuitka_sdk_cache.py 修补该缓存。
"""
import glob
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SDK = r"C:\Program Files (x86)\Windows Kits\10"
VSWHERE = os.path.join(os.environ["ProgramFiles(x86)"],
                       "Microsoft Visual Studio", "Installer", "vswhere.exe")


def fail(msg):
    print("[错误] " + msg, flush=True)
    sys.exit(1)


def build_env():
    """定位 MSVC 与 Windows SDK，组装编译所需的环境变量。"""
    if not os.path.isfile(VSWHERE):
        fail("未找到 vswhere.exe，无法定位 Visual Studio")

    vsp = subprocess.run(
        [VSWHERE, "-latest", "-products", "*", "-requires",
         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
         "-property", "installationPath"],
        capture_output=True, text=True).stdout.strip()
    if not vsp:
        fail("未找到 Visual Studio Build Tools（含 C++ 生成工具）")

    candidates = glob.glob(os.path.join(vsp, "VC", "Tools", "MSVC", "*"))
    if not candidates:
        fail("未找到 MSVC 工具链: " + os.path.join(vsp, "VC", "Tools", "MSVC"))
    vc = sorted(candidates, key=os.path.basename, reverse=True)[0]
    host64 = os.path.join(vc, "bin", "Hostx64", "x64")
    cl = os.path.join(host64, "cl.exe")
    if not os.path.isfile(cl):
        fail("未找到 cl.exe: " + cl)

    sdks = [d for d in glob.glob(os.path.join(SDK, "Include", "*"))
            if os.path.isdir(d) and os.path.basename(d).replace(".", "").isdigit()]
    if not sdks:
        fail("未找到 Windows SDK: " + SDK)
    sdk_dir = max(sdks, key=os.path.basename)
    sdkv = os.path.basename(sdk_dir)

    env = os.environ.copy()
    # Nuitka 的 _detectWindowsSDK() 硬性读取 WindowsSDKVersion
    env["WindowsSDKVersion"] = sdkv
    env["WindowsSdkDir"] = SDK + "\\"
    env["VCToolsInstallDir"] = vc + "\\"
    env["VCINSTALLDIR"] = os.path.join(vsp, "VC") + "\\"
    env["VisualStudioVersion"] = "17.0"
    env["VisualStudioInstallationPath"] = vsp
    env["CC"] = cl
    env["CXX"] = cl
    env["VSCMD_ARG_TGT_ARCH"] = "x64"
    env["INCLUDE"] = ";".join([
        os.path.join(vc, "include"),
        os.path.join(sdk_dir, "ucrt"),
        os.path.join(sdk_dir, "shared"),
        os.path.join(sdk_dir, "um"),
        os.path.join(sdk_dir, "cppwinrt"),
    ])
    env["LIB"] = ";".join([
        os.path.join(vc, "lib", "x64"),
        os.path.join(SDK, "Lib", sdkv, "ucrt", "x64"),
        os.path.join(SDK, "Lib", sdkv, "um", "x64"),
    ])
    env["PATH"] = host64 + os.pathsep + env.get("PATH", "")

    return env, cl, vsp, vc, sdkv


def main():
    debug = "--debug" in sys.argv

    # ---- 1. 先修补 Scons 的 MSVC 缓存（关键前置）----
    fixer = os.path.join(ROOT, "fix_nuitka_sdk_cache.py")
    if os.path.isfile(fixer):
        print("==> 修补 Nuitka MSVC 缓存", flush=True)
        subprocess.call([sys.executable, fixer])

    # ---- 2. 组装环境 ----
    env, cl, vsp, vc, sdkv = build_env()
    print()
    print("==> 编译环境", flush=True)
    print("    MSVC  = %s" % vc, flush=True)
    print("    cl    = %s" % cl, flush=True)
    print("    SDK   = %s" % sdkv, flush=True)

    # ---- 3. 清理旧产物 ----
    for name in ("adb_tcp_bridge.dist", "adb_tcp_bridge.build"):
        path = os.path.join(ROOT, name)
        if os.path.isdir(path):
            subprocess.call(["rmdir", "/s", "/q", path], shell=True)
    old_exe = os.path.join(ROOT, "adb_tcp_bridge.exe")
    if os.path.isfile(old_exe):
        os.remove(old_exe)

    # ---- 4. 编译 ----
    cmd = [sys.executable, "-m", "nuitka",
           "--standalone",
           "--msvc=latest",
           "--assume-yes-for-downloads",
           "--remove-output",
           "--output-filename=adb_tcp_bridge.exe"]
    if debug:
        cmd += ["--debug", "--show-scons"]
    cmd.append("adb_tcp_bridge.py")

    print()
    print("==> 开始编译（首次约 1-3 分钟）...", flush=True)
    rc = subprocess.call(cmd, env=env, cwd=ROOT)

    print()
    if rc != 0:
        print("==> 编译失败，退出码 %d" % rc, flush=True)
        print("    崩溃报告: nuitka-crash-report.xml", flush=True)
        return rc

    exe = os.path.join(ROOT, "adb_tcp_bridge.dist", "adb_tcp_bridge.exe")
    if not os.path.isfile(exe):
        print("==> 未找到产物", flush=True)
        return 1

    print("==> 编译成功", flush=True)
    print("    可执行文件: %s" % exe, flush=True)
    print("    大小      : %.2f MB" % (os.path.getsize(exe) / 1048576.0),
          flush=True)
    print()
    print("    运行要求: 本机需已安装 adb 并在 PATH 中", flush=True)
    print("    启动方式: adb_tcp_bridge.exe --listen-port 15555", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""修复 Nuitka/Scons 的 MSVC 配置缓存（编译 adb TCP 桥的前置步骤）。

为什么需要这一步
----------------
Nuitka 用 Scons做 C 后端编译，Scons 通过 vcvars64.bat 采集 MSVC 环境并缓存到
    %LOCALAPPDATA%\\Nuitka\\Nuitka\\Cache\\scons-msvc-config\\content-<ver>.json
但 vcvars64.bat 内部会调用 reg.exe 读取注册表来确定 Windows SDK 路径。若
reg.exe 不可用（某些安全策略会拦截），采集到的 SDK 信息是残缺的：

    WindowsSDKVersion = ['\\\\']<- 只有一个反斜杠
    WindowsSdkDir     = []           <- 完全为空
    INCLUDE           只有 2 条（缺 SDK 的 ucrt / shared / um）

Nuitka 的 _detectWindowsSDK() 解析这个残值失败，于是判定"未安装 Windows SDK"，
并在checkWindowsCompilerFound() 里主动把 env['CC'] 置为 None，最终报：

    FATAL: Error, scons environment variable 'CC' is not set, this ought to never happen.

这个脚本把"本应由 vcvars64 采集到、但因 reg.exe 被拦而丢失"的信息补回缓存：
修正 WindowsSDKVersion / WindowsSdkDir，并补齐 INCLUDE 与 LIB。

安全说明：只修改 Nuitka 自己的缓存文件（先备份），不触碰注册表、不修改系统
配置、不绕过任何安全策略。若reg.exe 可用，本脚本不会重复修补（会检测并跳过）。
"""
import glob
import json
import os
import shutil
import subprocess
import sys

SDK = r"C:\Program Files (x86)\Windows Kits\10"


def find_msvc():
    vswhere = os.path.join(os.environ["ProgramFiles(x86)"],
                           "Microsoft Visual Studio", "Installer", "vswhere.exe")
    if not os.path.isfile(vswhere):
        return None, None
    vsp = subprocess.run(
        [vswhere, "-latest", "-products", "*", "-requires",
         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
         "-property", "installationPath"],
        capture_output=True, text=True).stdout.strip()
    if not vsp:
        return None, None
    candidates = glob.glob(os.path.join(vsp, "VC", "Tools", "MSVC", "*"))
    if not candidates:
        return vsp, None
    vc = sorted(candidates, key=os.path.basename, reverse=True)[0]
    return vsp, vc


def find_sdk():
    sdks = [d for d in glob.glob(os.path.join(SDK, "Include", "*"))
            if os.path.isdir(d) and os.path.basename(d).replace(".", "").isdigit()]
    if not sdks:
        return None, None
    sdk_dir = max(sdks, key=os.path.basename)
    return sdk_dir, os.path.basename(sdk_dir)


def main():
    cache_dir = os.path.join(os.environ["LOCALAPPDATA"], "Nuitka", "Nuitka",
                             "Cache", "scons-msvc-config")
    targets = glob.glob(os.path.join(cache_dir, "content-*.json"))
    if not targets:
        print("[跳过] 未找到 MSVC 配置缓存: %s" % cache_dir)
        print("       首次用 Nuitka 编译时会自动生成，届时再运行本脚本。")
        return 0

    vsp, vc = find_msvc()
    sdk_dir, sdkv = find_sdk()
    if not vc or not sdkv:
        print("[错误] 未能定位 MSVC 或 Windows SDK，无法修补缓存。")
        return 1

    print("MSVC : %s" % vc)
    print("SDK  : %s" % sdkv)
    print()

    for path in targets:
        with open(path, encoding="utf-8") as fh:
            entries = json.load(fh)

        changed = False
        for entry in entries:
            data = entry["data"]

            if data.get("WindowsSDKVersion") in (None, [], ["\\"], "", ["\\\\"]):
                data["WindowsSDKVersion"] = [sdkv]
                changed = True
            if not data.get("WindowsSdkDir"):
                data["WindowsSdkDir"] = [SDK + "\\"]
                changed = True

            include = list(data.get("INCLUDE") or [])
            for sub in ("ucrt", "shared", "um", "cppwinrt", "winrt"):
                p = os.path.join(sdk_dir, sub)
                if os.path.isdir(p) and p not in include:
                    include.append(p)
            data["INCLUDE"] = include

            lib = list(data.get("LIB") or [])
            for sub in (os.path.join("ucrt", "x64"), os.path.join("um", "x64")):
                p = os.path.join(SDK, "Lib", sdkv, sub)
                if os.path.isdir(p) and p not in lib:
                    lib.append(p)
            data["LIB"] = lib

        if not changed:
            print("[无需修改] %s" % os.path.basename(path))
            continue

        backup = path + ".bak"
        if not os.path.exists(backup):
            shutil.copy2(path, backup)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, indent=2)
        print("[已修补] %s  (备份: %s)"
              % (os.path.basename(path), os.path.basename(backup)))

        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)[0]["data"]
        print("WindowsSDKVersion = %s" % data.get("WindowsSDKVersion"))
        print("WindowsSdkDir     = %s" % data.get("WindowsSdkDir"))
        print("INCLUDE 条目      = %d" % len(data.get("INCLUDE") or []))

    return 0


if __name__ == "__main__":
    sys.exit(main())
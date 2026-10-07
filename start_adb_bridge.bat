@echo off
chcp 65001 >nul
title adb TCP 桥 - 把 USB 设备共享到局域网
echo.
echo  把本机 USB 设备暴露给局域网内的电脑 B 使用 adb
echo.
echo  电脑 B 执行： adb connect ^<本机IP^>:15555
echo.
echo  端口说明：15555 不能改成 5555-5585，否则本机 adb devices
echo            会出现幽灵设备 emulator-5554（adb server 会扫描这段找模拟器）。
echo.

REM 优先使用编译好的二进制版（无需 Python），否则回退到 Python 脚本
set "EXE=%~dp0adb_tcp_bridge.dist\adb_tcp_bridge.exe"
if exist "%EXE%" (
    echo  运行方式：二进制版
    echo.
    "%EXE%" --listen-port 15555
    goto :done
)

where python >nul 2>&1
if not errorlevel 1 (
    echo  运行方式：Python 版（未找到已编译的二进制）
    echo  如需编译为免 Python 的exe：python build_nuitka.py
    echo.
    python "%~dp0adb_tcp_bridge.py" --listen-port 15555
    goto :done
)

echo  [错误] 未找到可用的运行方式：
echo         既没有 adb_tcp_bridge.dist\adb_tcp_bridge.exe，也没有 python。
echo.
echo         解决：安装 Python，或先编译二进制版
echo                python -m pip install nuitka scons pywin32 ordered-set zstandard
echo                python build_nuitka.py
echo.

:done
echo.
echo 桥已退出，按任意键关闭...
pause >nul
@echo off
chcp 65001 >nul
title adb TCP bridge - 把 USB 设备共享到局域网
echo.
echo  启动 adb TCP 桥：本机 USB 设备 -- 局域网 --> 电脑 B
echo  电脑 B 执行： adb connect ^<本机IP^>:15555
echo.
echo  注意：端口不要用 5555-5585，adb server 会把这段端口当成模拟器扫描，
echo        否则本机 adb devices 里会出现幽灵设备 emulator-5554。
echo.

python "%~dp0adb_tcp_bridge.py" --listen-port 15555

echo.
echo 桥已退出，按任意键关闭...
pause >nul

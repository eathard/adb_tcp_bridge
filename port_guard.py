"""端口占用检查与清理。

独立成模块便于单独测试，也便于 build_nuitka.py 在编译前调用同样的逻辑。

Windows 上查询端口占用有两条路：
  1. netstat -ano    —— 通用，但需解析文本，且慢（数百行）
  2. psutil         —— 需第三方依赖
这里优先用 netstat（系统自带，无需安装），并在可用时回退到 socket 探测
（只能判断"有没有人监听"，拿不到 PID）。
"""
import os
import re
import socket
import subprocess
import sys
import time

# netstat -ano 的一行示例：
#   TCP    0.0.0.0:15555          0.0.0.0:0              LISTENING       66380
#   TCP    [::]:15555             [::]:0                 LISTENING       66380
_NETSTAT_RE = re.compile(
    r"^\s*TCP\s+(\S+):(\d+)\s+(\S+):(\d+)\s+LISTENING\s+(\d+)\s*$",
    re.IGNORECASE)

# 这些进程名即使占着端口也不应被杀（属于系统或关键服务）
_PROTECTED_NAMES = {
    "system", "system idle process", "services.exe", "lsass.exe",
    "csrss.exe", "wininit.exe", "winlogon.exe", "svchost.exe",
    "explorer.exe", "audiodg.exe", "dwm.exe",
}


def _run(cmd, timeout=20):
    """执行命令并返回 stdout 文本。

    Windows 上 netstat / tasklist 输出是本地代码页（简体中文系统为 GBK），
    不是 UTF-8，必须用 errors="replace" 容错解码，否则会抛 UnicodeDecodeError。
    """
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=_creationflags(),
        )
    except Exception as exc:
        log_msg("执行 %s 失败: %s" % (cmd[0], exc))
        return None
    return proc.stdout.decode("utf-8", errors="replace")


def _creationflags():
    """Windows 上隐藏子进程的控制台窗口。"""
    if os.name == "nt":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _netstat_listeners(port):
    """返回 [(pid, local_addr), ...]，即监听指定端口的进程。"""
    out = _run(["netstat", "-ano", "-p", "TCP"])
    if out is None:
        return None  # None 表示"查不了"，区别于"没人占用"

    found = []
    for line in out.splitlines():
        m = _NETSTAT_RE.match(line)
        if not m:
            continue
        local, lport = m.group(1), int(m.group(2))
        if lport != port:
            continue
        found.append((int(m.group(5)), local))
    return found


def _process_name(pid):
    """取进程名，查不到返回 None。"""
    out = _run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"])
    if not out:
        return None
    out = out.strip()
    if not out or "没有运行的任务" in out or "No tasks" in out:
        return None
    # CSV 形如 "python.exe","1234","Console","1","50,000 K"
    first = out.splitlines()[0].strip()
    if first.startswith('"'):
        parts = first.split('"')
        if len(parts) >= 2:
            return parts[1]
    return first.split(",")[0]


def _pid_alive(pid):
    """进程是否还活着。"""
    out = _run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"])
    if out is None:
        return False
    return str(pid) in out


def log_msg(msg):
    sys.stdout.write("%s\n" % msg)
    sys.stdout.flush()


def kill_pid(pid, timeout=5.0):
    """结束进程。

    顺序：先试 taskkill（不带 /F）。对有窗口的程序相当于请求关闭；
    对无窗口的后台进程（Python / 打包后的 exe）通常无效，随即升级为 /F 强杀。

    保留"温和优先"是为了避免误杀 GUI 程序时连带丢掉用户未保存的工作——
    那些程序通常会弹窗询问或 orderly 退出。
    """
    log_msg("[cleanup] 结束进程 PID=%d ..." % pid)

    if os.name == "nt":
        _run(["taskkill", "/PID", str(pid)], timeout=10)
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            return True

    if _wait_gone(pid, timeout):
        log_msg("[cleanup] 进程 PID=%d 已退出（温和终止）" % pid)
        return True

    log_msg("[cleanup] 进程 PID=%d 未响应，强制结束" % pid)
    if os.name == "nt":
        _run(["taskkill", "/F", "/PID", str(pid)], timeout=10)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            return True

    if _wait_gone(pid, 5):
        log_msg("[cleanup] 进程 PID=%d 已强制结束" % pid)
        return True
    return not _pid_alive(pid)


def _wait_gone(pid, timeout):
    """等待进程消失。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.25)
    return not _pid_alive(pid)


def port_in_use(port, host="0.0.0.0"):
    """只判断端口有没有被监听（拿不到 PID 时的回退方案）。"""
    for family, addr in ((socket.AF_INET, (host, port)),
                         (socket.AF_INET6, ("::", port))):
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(addr)
            s.close()
            return False
        except OSError:
            try:
                s.close()
            except OSError:
                pass
            continue
    return True


def free_port(port, host="0.0.0.0", attempts=15, delay=0.4):
    """等待端口释放。"""
    for _ in range(attempts):
        if not port_in_use(port, host):
            return True
        time.sleep(delay)
    return not port_in_use(port, host)


def ensure_port_free(port, host="0.0.0.0", skip_pids=(), auto_kill=True):
    """确保监听端口可用。

    返回 (ok, killed_pids)：
      ok=True  端口已可用（或被跳过而保留）
      ok=False 端口仍被占用且未能清理

    auto_kill=False 时只报告不杀进程。
    skip_pids 里的 PID 不会被杀（本进程自身 PID 放进来可避免自杀）。
    """
    skip = set(p for p in skip_pids if p)

    listeners = _netstat_listeners(port)
    if listeners is None:
        # 查不到 PID，退化为"能否 bind"判断
        if not port_in_use(port, host):
            return True, []
        if not auto_kill:
            log_msg("[cleanup] 端口 %d 已被占用，但无法定位占用进程" % port)
            return False, []
        log_msg("[cleanup] 端口 %d 被占用，尝试等待其释放..." % port)
        if free_port(port, host):
            return True, []
        log_msg("[cleanup] 端口 %d 仍被占用，且无法定位进程，无法清理" % port)
        return False, []

    if not listeners:
        # netstat 说没人监听，但可能因权限等原因看不到，再 bind 确认
        if not port_in_use(port, host):
            return True, []
        log_msg("[cleanup] 端口 %d 仍无法绑定，可能存在 TIME_WAIT 或"
                "权限问题" % port)
        return False, []

    killed = []
    for pid, local in listeners:
        name = _process_name(pid)
        shown = name or "未知进程"
        if pid in skip:
            log_msg("[cleanup] 端口 %d 被本进程占用(PID=%d %s)，跳过"
                    % (port, pid, shown))
            continue
        if (name or "").lower() in _PROTECTED_NAMES:
            log_msg("[cleanup] 端口 %d 被系统进程占用(PID=%d %s)，"
                    "出于安全不自动结束" % (port, pid, shown))
            continue

        log_msg("[cleanup] 端口 %d 已被占用: PID=%d 进程=%s 监听=%s"
                % (port, pid, shown, local))
        if not auto_kill:
            continue
        if kill_pid(pid):
            killed.append(pid)

    # 判定端口是否真的可用：以"清理后是否还有残留占用者"为准。
    # 不用 port_in_use()，因为它只反映"能否 bind"——同端口存在半关闭/TIME_WAIT
    # 残留时 bind 可能成功，但 bind() 随后仍会失败。
    remaining = _netstat_listeners(port) or []
    if remaining:
        log_msg("[cleanup] 端口 %d 仍被以下进程占用: %s"
                % (port, ", ".join("PID=%d(%s)" % (p, _process_name(p) or "?")
                                   for p, _ in remaining)))
        return False, killed

    if killed:
        log_msg("[cleanup] 端口 %d 已释放（清理了 %d 个进程）"
                % (port, len(killed)))
    return True, killed


if __name__ == "__main__":
    # 自测：python port_guard.py [端口...]
    targets = [int(a) for a in sys.argv[1:]] or [15555]
    for p in targets:
        ok, killed = ensure_port_free(p, skip_pids={os.getpid()})
        print("端口 %d: %s  (清理 %s)"
              % (p, "可用" if ok else "仍被占用", killed or "无"))
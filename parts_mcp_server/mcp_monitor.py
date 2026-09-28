"""Live, read-only monitor for the Xiaozhi MCP bridge."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "logs" / "mcp_bridge.log"
STATUS_FILE = BASE_DIR / "logs" / "mcp_status.json"
STOP_SCRIPT = BASE_DIR.parent / "stop_all.ps1"
POLL_SECONDS = 0.5
SUMMARY_SECONDS = 15

STATE_LABELS = {
    "starting": "正在启动",
    "connecting": "正在连接小智",
    "connected": "已连接",
    "reconnecting": "连接中断，正在重连",
    "stopped": "已停止",
}


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def _read_status() -> dict | None:
    try:
        value = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _pid_exists(pid: object) -> bool:
    try:
        numeric_pid = int(pid)
        if numeric_pid <= 0:
            return False
    except (TypeError, ValueError):
        return False

    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL

            handle = open_process(
                process_query_limited_information, False, numeric_pid
            )
            if handle:
                close_handle(handle)
                return True
            # Access denied also means the process exists but cannot be queried.
            return ctypes.get_last_error() == 5
        except (AttributeError, OSError):
            return False

    try:
        os.kill(numeric_pid, 0)
        return True
    except OSError:
        return False


def _find_bridge_pids() -> list[int]:
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'python.exe' -and "
        "$_.CommandLine -like '*parts_mcp_server*' -and "
        "$_.CommandLine -like '*xiaozhi_bridge.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return [int(line) for line in completed.stdout.splitlines() if line.strip().isdigit()]
    except (OSError, subprocess.SubprocessError, ValueError):
        return []


def _status_signature(status: dict | None) -> tuple:
    if not status:
        return ()
    return (
        status.get("state"),
        status.get("bridge_pid"),
        status.get("child_pid"),
        status.get("received_messages"),
        status.get("sent_messages"),
        status.get("tool_calls"),
        status.get("last_tool"),
        status.get("last_result"),
        status.get("last_error"),
    )


def _print_status(status: dict | None) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    if not status:
        legacy_pids = _find_bridge_pids()
        if legacy_pids:
            print(
                f"[{timestamp}] 状态：检测到桥接进程 PID {', '.join(map(str, legacy_pids))}，"
                "但它尚未生成监控状态；请重新启动一次 start_all.bat。"
            )
        else:
            print(f"[{timestamp}] 状态：未检测到 MCP 桥接服务。")
        return

    state = str(status.get("state", "unknown"))
    if not _pid_exists(status.get("bridge_pid")) and state != "stopped":
        state_text = "状态文件存在，但桥接进程已经退出"
    else:
        state_text = STATE_LABELS.get(state, state)

    print(
        f"[{timestamp}] 状态：{state_text} | "
        f"桥接 PID {status.get('bridge_pid') or '-'} | "
        f"本地 MCP PID {status.get('child_pid') or '-'}"
    )
    print(
        "           统计："
        f"接收 {status.get('received_messages', 0)} | "
        f"发送 {status.get('sent_messages', 0)} | "
        f"工具调用 {status.get('tool_calls', 0)}"
    )
    if status.get("last_tool"):
        result_labels = {"running": "执行中", "success": "成功", "error": "失败"}
        result = result_labels.get(status.get("last_result"), status.get("last_result"))
        print(f"           最近工具：{status['last_tool']} | 结果：{result or '-'}")
    if status.get("last_error"):
        print(f"           最近错误：{status['last_error']}")


def _show_recent_log(lines: int = 10) -> int:
    try:
        content = LOG_FILE.read_text(encoding="utf-8", errors="replace")
        recent = content.splitlines()[-lines:]
        if recent:
            print("\n最近事件：")
            for line in recent:
                print(f"  {line}")
        return LOG_FILE.stat().st_size
    except OSError:
        return 0


def _read_new_log(position: int) -> tuple[int, list[str]]:
    try:
        size = LOG_FILE.stat().st_size
        if size < position:
            position = 0
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(position)
            lines = handle.read().splitlines()
            return handle.tell(), lines
    except OSError:
        return position, []


def _quit_requested() -> bool:
    if os.name != "nt":
        return False
    try:
        import msvcrt

        if msvcrt.kbhit():
            return msvcrt.getwch().lower() == "q"
    except (ImportError, OSError):
        return False
    return False


def _stop_services() -> int:
    print("\n正在停止零件网站和 MCP 服务……")
    if not STOP_SCRIPT.is_file():
        print(f"停止失败：找不到 {STOP_SCRIPT}")
        return 1
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(STOP_SCRIPT),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        print(f"停止失败：{error}")
        return 1

    output = "\n".join(
        line for line in (completed.stdout + completed.stderr).splitlines() if line.strip()
    )
    if output:
        print(output)
    if completed.returncode == 0:
        print("网站和 MCP 服务已停止。")
    else:
        print("部分服务未能停止，请查看上面的错误信息。")
    return completed.returncode


def monitor(once: bool = False) -> int:
    print("=" * 64)
    print(" 小智零件管家 MCP 实时监控")
    print(" 仅显示连接状态、方法名和工具结果；参数、库存内容和密钥不会记录。")
    print("=" * 64)

    status = _read_status()
    _print_status(status)
    position = _show_recent_log()
    if once:
        return 0

    print("\n正在监控。按 Q 同时停止网站、MCP 服务和监控。\n")
    signature = _status_signature(status)
    last_summary = time.monotonic()

    try:
        while True:
            if _quit_requested():
                return _stop_services()

            position, lines = _read_new_log(position)
            for line in lines:
                print(f"  {line}")

            status = _read_status()
            current_signature = _status_signature(status)
            now = time.monotonic()
            if current_signature != signature or now - last_summary >= SUMMARY_SECONDS:
                _print_status(status)
                signature = current_signature
                last_summary = now
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        print("\n监控已关闭；网站和 MCP 服务仍在后台运行。")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="监控小智 MCP 桥接连接与工具调用。")
    parser.add_argument("--once", action="store_true", help="输出一次状态后退出。")
    args = parser.parse_args()
    return monitor(once=args.once)


if __name__ == "__main__":
    _configure_console()
    raise SystemExit(main())

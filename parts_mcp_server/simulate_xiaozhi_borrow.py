"""通过 MCP stdio 协议模拟小智发起一次零件借用。

默认只执行库存查询并打印将要发送的请求。实际登记借用需要显式加
``--execute``，以免测试时误触发树莓派摄像头、云台和借用记录写入。
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_REMOTE_HOST = "192.168.0.105"
DEFAULT_REMOTE_USER = "xiao"
DEFAULT_REMOTE_HOSTKEY = "ssh-ed25519 255 SHA256:ugfywNbbLkUcbEdT+1mVawLLspiLekHKpXqwTbT0PXo"


def _mcp_python() -> str:
    candidates = [
        BASE_DIR / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"),
        Path(sys.executable),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _run_on_raspberry_pi(args: argparse.Namespace) -> int:
    """从 Windows 通过 SSH 让树莓派在本地执行 MCP 模拟客户端。"""

    plink = os.environ.get("PARTS_PLINK", r"C:\Program Files\PuTTY\plink.exe")
    host = args.remote_host or os.environ.get("PARTS_REMOTE_HOST", DEFAULT_REMOTE_HOST)
    user = args.remote_user or os.environ.get("PARTS_REMOTE_USER", DEFAULT_REMOTE_USER)
    remote_path = args.remote_path or os.environ.get(
        "PARTS_REMOTE_SCRIPT",
        "/home/xiao/face/parts_mcp_server/simulate_xiaozhi_borrow.py",
    )
    if not host:
        raise RuntimeError("远程模式需要 --remote-host，或设置 PARTS_REMOTE_HOST。")
    if not Path(plink).is_file():
        raise RuntimeError(f"找不到 Plink：{plink}")

    # remote_path 是树莓派上的 Linux 路径，不能用 Windows pathlib 处理，
    # 否则会被转换成 \home\xiao\face。
    remote_root = posixpath.dirname(posixpath.dirname(remote_path))
    remote_command = [
        "cd",
        remote_root,
        "&&",
        ".venv/bin/python",
        remote_path,
        "--keyword",
        args.keyword,
        "--quantity",
        str(args.quantity),
    ]
    if args.execute:
        remote_command.append("--execute")
    command_text = " ".join(
        item if item in {"&&"} else shlex.quote(item) for item in remote_command
    )
    # 树莓派当前 SSH 登录会先显示“Access granted. Press Return to begin
    # session.”，需要伪终端才能继续进入 shell 并执行后面的命令。
    command = [plink, "-t"]
    hostkey = args.remote_hostkey or os.environ.get("PARTS_REMOTE_HOSTKEY", DEFAULT_REMOTE_HOSTKEY)
    if hostkey:
        command.extend(["-hostkey", hostkey])
    command.append(f"{user}@{host}")
    command.append(command_text)
    print(f"通过 SSH 请求树莓派执行 MCP 模拟：{user}@{host}")
    completed = subprocess.run(command, cwd=BASE_DIR.parent, check=False)
    return completed.returncode


def _read_response(process: subprocess.Popen[str], message_id: int) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("MCP 子进程没有可读取的标准输出。")
    while True:
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f"MCP 子进程提前退出，退出码：{process.poll()}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            # MCP 的日志必须走 stderr；为兼容调试输出，忽略 stdout 上的非 JSON 行。
            continue
        if message.get("id") != message_id:
            continue
        if "error" in message:
            raise RuntimeError(f"MCP 返回协议错误：{message['error']}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("MCP 返回的数据格式无效。")
        return result


def _request(process: subprocess.Popen[str], message_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    if process.stdin is None:
        raise RuntimeError("MCP 子进程没有可写入的标准输入。")
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        message["params"] = params
    process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    process.stdin.flush()
    return _read_response(process, message_id)


def _notify(process: subprocess.Popen[str], method: str, params: dict[str, Any] | None = None) -> None:
    if process.stdin is None:
        raise RuntimeError("MCP 子进程没有可写入的标准输入。")
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
    process.stdin.flush()


def _tool_data(result: dict[str, Any]) -> dict[str, Any]:
    """兼容 FastMCP 的 structuredContent 和 text content 两种返回形式。"""

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                decoded = json.loads(item.get("text", ""))
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(decoded, dict):
                return decoded
    raise RuntimeError(f"无法解析 MCP 工具返回值：{result}")


def _call_tool(process: subprocess.Popen[str], message_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = _request(
        process,
        message_id,
        "tools/call",
        {"name": name, "arguments": arguments},
    )
    return _tool_data(result)


def _choose_component(data: dict[str, Any], keyword: str) -> dict[str, Any]:
    if not data.get("success"):
        raise RuntimeError(data.get("message", "库存查询失败。"))
    items = data.get("items") or []
    exact = [item for item in items if str(item.get("name", "")).strip() == keyword]
    candidates = exact or [item for item in items if keyword in str(item.get("name", ""))]
    if len(candidates) != 1:
        print(json.dumps({"keyword": keyword, "candidates": candidates}, ensure_ascii=False, indent=2))
        raise RuntimeError("匹配到的零件不是唯一一项，请先确认具体型号。")
    return candidates[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟小智通过 MCP 借用一个锂电池")
    parser.add_argument("--keyword", default="锂电池", help="库存查询关键词，默认：锂电池")
    parser.add_argument("--quantity", type=int, default=1, help="借用数量，默认：1")
    parser.add_argument("--execute", action="store_true", help="实际调用借用工具并写入借用记录")
    parser.add_argument("--remote", action="store_true", help="在配置好的树莓派上执行 MCP，而不是启动本机 MCP")
    parser.add_argument("--remote-host", help="通过 SSH 让树莓派执行，例如 192.168.0.105")
    parser.add_argument("--remote-user", help="树莓派 SSH 用户名，默认读取 PARTS_REMOTE_USER 或 xiao")
    parser.add_argument("--remote-path", help="树莓派上的脚本路径")
    parser.add_argument("--remote-hostkey", help="Plink 主机指纹；也可设置 PARTS_REMOTE_HOSTKEY")
    args = parser.parse_args()

    if args.quantity <= 0:
        parser.error("--quantity 必须大于 0")

    if args.remote or args.remote_host or os.environ.get("PARTS_REMOTE_HOST"):
        return _run_on_raspberry_pi(args)

    process = subprocess.Popen(
        [_mcp_python(), str(BASE_DIR / "parts_mcp.py")],
        cwd=BASE_DIR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
    )
    try:
        _request(
            process,
            1,
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "simulate-xiaozhi-borrow", "version": "1.0"},
            },
        )
        _notify(process, "notifications/initialized")
        search = _call_tool(process, 2, "search_part", {"keyword": args.keyword, "limit": 20})
        component = _choose_component(search, args.keyword)
        request = {
            "component_id": int(component["component_id"]),
            "quantity": args.quantity,
            "request_id": f"simulate-{uuid.uuid4().hex[:12]}",
        }
        print("已通过 MCP 查询到唯一零件：")
        print(json.dumps(component, ensure_ascii=False, indent=2))
        print("将发送的借用参数：")
        print(json.dumps(request, ensure_ascii=False, indent=2))
        if not args.execute:
            print("当前为演练模式，未写入借用记录。需要实际申请时加 --execute。")
            return 0

        borrow = _call_tool(process, 3, "request_part_borrow", request)
        print("MCP 借用返回：")
        print(json.dumps(borrow, ensure_ascii=False, indent=2))
        return 0 if borrow.get("success") else 1
    finally:
        if process.stdin:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as error:
        print(f"模拟失败：{error}", file=sys.stderr)
        raise SystemExit(1)

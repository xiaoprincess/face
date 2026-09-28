#!/usr/bin/env python
"""模拟「小智云端 MCP 客户端 → 树莓派 MCP 桥接 → 零件管家网站」整条链路。

流程：
  1. 检查零件管家网站是否在线、设备令牌是否与 MCP 一致；
  2. 在本机开一个 WebSocket 服务端，扮演小智云端（MCP Client）；
  3. 启动真实的 xiaozhi_bridge.py（树莓派侧），让它主动连上来；
  4. 依次发送 initialize / tools/list / tools/call（search_part、request_part_borrow），
     打印双向 JSON-RPC 报文，最后回数据库核对借用记录是否真的落库。

本地仿真，不连接真实小智云端，也不会改动 parts_mcp_server/.env 中的真实令牌。
网站本身请由配套的启动脚本负责（本脚本不会去杀或起网站）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

MCP_ROOT = Path(__file__).resolve().parent
WEB_ROOT = MCP_ROOT.parent / "unified_web_app"
MCP_PYTHON = MCP_ROOT / ".venv" / "Scripts" / "python.exe"
LOG_DIR = MCP_ROOT / "logs"
BRIDGE_OUT = LOG_DIR / "sim_bridge.out.log"

DATABASE = WEB_ROOT / "instance" / "unified.db"
DEFAULT_TOKEN = "local-sim-20260921-device-token"
WEB_BASE = "http://127.0.0.1:5050"
WS_HOST, WS_PORT = "127.0.0.1", 8765


def log(message: str = "") -> None:
    print(message, flush=True)


def rule(title: str) -> None:
    log()
    log("=" * 74)
    log(title)
    log("=" * 74)


# ------------------------------------------------------------------ 前置检查


def http_json(url, payload=None, token=None, timeout=8):
    """返回 (状态码, JSON 或原文)；连不上时返回 (None, None)。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("X-Device-Token", token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        status = error.code
    except (urllib.error.URLError, OSError):
        return None, None
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def check_website(token: str) -> bool:
    """确认网站在线且设备令牌匹配。"""
    status, body = http_json(f"{WEB_BASE}/health", timeout=4)
    if status != 200:
        log(f"  × 网站没有在 {WEB_BASE} 运行。")
        log("    请先用配套的「启动零件管家网站.bat」把网站起起来（它已配好设备令牌）。")
        return False
    log(f"  √ 网站在线：{WEB_BASE}（{body}）")

    # 用一个不存在的零件编号探测：令牌通过时会走到业务逻辑并返回 404「未找到指定零件」。
    probe_status, probe_body = http_json(
        f"{WEB_BASE}/parts/api/device/borrow",
        {"component_id": 999999, "quantity": 1, "request_id": "token-probe"},
        token=token,
    )
    message = probe_body.get("message") if isinstance(probe_body, dict) else probe_body

    if probe_status in (401, 503):
        log(f"  × 设备令牌不匹配，借用接口返回 HTTP {probe_status}：{message}")
        if probe_status == 503:
            log("    说明网站启动时没有注入 PARTS_DEVICE_TOKEN。")
        else:
            log(f"    说明网站用的令牌与本脚本的 `{token}` 不一致。")
        log("    请用配套的「启动零件管家网站.bat」重启网站后再运行本脚本。")
        return False

    log(f"  √ 设备令牌匹配（借用接口已放行到业务逻辑，HTTP {probe_status}：{message}）。")
    return True


# ------------------------------------------------------------------ 数据库核对


def lookup_record(request_id: str):
    if not DATABASE.is_file():
        return None
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            """
            SELECT pa.id, pa.equipment_name, pa.quantity, pa.purpose, pa.status,
                   pa.created_at, u.username, c.cabinet_number
            FROM part_applications AS pa
            LEFT JOIN users AS u ON u.id = pa.user_id
            LEFT JOIN cabinets AS c ON c.id = pa.cabinet_id
            WHERE pa.purpose LIKE ?
            ORDER BY pa.id DESC LIMIT 1
            """,
            (f"%[MCP 借用请求:{request_id}]%",),
        ).fetchone()
        return dict(row) if row else None
    finally:
        connection.close()


def record_exists(request_id: str) -> bool:
    return lookup_record(request_id) is not None


# ------------------------------------------------------------------ MCP 客户端


class McpClient:
    """按 JSON-RPC id 配对请求与响应，兼容服务端主动下发的通知。"""

    def __init__(self, websocket):
        self.ws = websocket
        self.pending: dict[object, asyncio.Future] = {}
        self.reader = asyncio.create_task(self._read_loop())

    async def _read_loop(self):
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                log(f"[树莓派 → 小智云端] {raw}")
                try:
                    decoded = json.loads(raw)
                except ValueError:
                    continue
                for item in decoded if isinstance(decoded, list) else [decoded]:
                    if not isinstance(item, dict):
                        continue
                    if "id" in item and ("result" in item or "error" in item):
                        future = self.pending.pop(item["id"], None)
                        if future and not future.done():
                            future.set_result(item)
        except Exception as error:  # 连接关闭属正常收尾
            log(f"[读取循环结束] {error!r}")

    async def call(self, message_id, method, params=None, timeout=90):
        payload = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        log()
        log(f"[小智云端 → 树莓派] {json.dumps(payload, ensure_ascii=False)}")
        future = asyncio.get_running_loop().create_future()
        self.pending[message_id] = future
        await self.ws.send(json.dumps(payload, ensure_ascii=False))
        return await asyncio.wait_for(future, timeout=timeout)

    async def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        log(f"[小智云端 → 树莓派]（通知）{json.dumps(payload, ensure_ascii=False)}")
        await self.ws.send(json.dumps(payload, ensure_ascii=False))


def tool_payload(response):
    result = (response or {}).get("result") or {}
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except ValueError:
                return {"text": block["text"]}
    return {}


async def drive(websocket, request_id: str, keyword: str, quantity: int) -> bool:
    """在一条已建立的连接上跑完整的 MCP 调用序列。"""
    client = McpClient(websocket)
    log()
    log(f"[桥接已连接] {websocket.remote_address} —— 树莓派侧上线")

    init = await client.call(
        1,
        "initialize",
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "xiaozhi-cloud-sim", "version": "1.0"},
        },
    )
    info = ((init.get("result") or {}).get("serverInfo")) or {}
    log(f">> 握手成功：{info.get('name')} {info.get('version', '')}")
    await client.notify("notifications/initialized")

    listing = await client.call(2, "tools/list", {})
    tools = ((listing.get("result") or {}).get("tools")) or []
    log(f">> 树莓派暴露的工具（{len(tools)} 个）：" + "、".join(t["name"] for t in tools))

    search = await client.call(
        3, "tools/call", {"name": "search_part", "arguments": {"keyword": keyword, "limit": 5}}
    )
    items = (tool_payload(search).get("items")) or []
    log(f">> search_part('{keyword}') 命中 {len(items)} 条：")
    for item in items:
        log(
            "   - id={component_id} | {name} × {quantity}{unit} | {cabinet_number} 号柜 | 箱号 {box_number}".format(
                **item
            )
        )
    if not items:
        log(">> 没查到零件，借用信号终止。")
        return False
    component = items[0]

    log()
    log(
        f">> 发送借用信号：request_part_borrow(component_id={component['component_id']}, "
        f"quantity={quantity}, request_id={request_id})"
    )
    borrow = await client.call(
        4,
        "tools/call",
        {
            "name": "request_part_borrow",
            "arguments": {
                "component_id": component["component_id"],
                "quantity": quantity,
                "request_id": request_id,
            },
        },
    )
    outcome = tool_payload(borrow)
    log(">> 借用信号返回：" + json.dumps(outcome, ensure_ascii=False))
    success = bool(outcome.get("success"))

    log()
    log(">> 幂等复测（同一个 request_id 再发一次）")
    repeat = await client.call(
        5,
        "tools/call",
        {
            "name": "request_part_borrow",
            "arguments": {
                "component_id": component["component_id"],
                "quantity": quantity,
                "request_id": request_id,
            },
        },
    )
    log(">> 复测返回：" + json.dumps(tool_payload(repeat), ensure_ascii=False))
    return success


def start_bridge(token: str) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "XIAOZHI_MCP_ENDPOINT": f"ws://{WS_HOST}:{WS_PORT}",
            "PARTS_WEB_BASE_URL": WEB_BASE,
            "PARTS_DEVICE_TOKEN": token,
            "PYTHONIOENCODING": "utf-8",
        }
    )
    handle = open(BRIDGE_OUT, "ab")
    return subprocess.Popen(
        [str(MCP_PYTHON), "-X", "utf8", "xiaozhi_bridge.py"],
        cwd=str(MCP_ROOT),
        env=environment,
        stdout=handle,
        stderr=handle,
    )


def stop_bridge(bridge: subprocess.Popen) -> None:
    if bridge.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(bridge.pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )


async def run(args) -> int:
    rule("步骤 1/4　检查零件管家网站")
    if not check_website(args.token):
        return 2

    rule("步骤 2/4　启动树莓派 MCP 桥接（连向本机仿真的小智云端）")
    bridge = start_bridge(args.token)
    log(f"  桥接进程 PID {bridge.pid}，日志写到 {BRIDGE_OUT.relative_to(MCP_ROOT.parent)}")
    await asyncio.sleep(1.5)

    rule("步骤 3/4　以「小智云端 MCP 客户端」身份发送信号")
    success = False
    connected = asyncio.get_running_loop().create_future()

    async def handler(websocket):
        try:
            nonlocal success
            success = await drive(websocket, args.request_id, args.keyword, args.quantity)
        except Exception as error:
            log(f"!! 调用出错：{error!r}")
        finally:
            if not connected.done():
                connected.set_result(True)

    try:
        async with websockets.serve(handler, WS_HOST, WS_PORT, ping_interval=None):
            log(f"  模拟小智云端已就绪：ws://{WS_HOST}:{WS_PORT}，等待树莓派桥接连接…")
            try:
                await asyncio.wait_for(connected, timeout=args.timeout)
            except asyncio.TimeoutError:
                log(f"!! {args.timeout} 秒内没有收到桥接连接，请看 {BRIDGE_OUT.name}。")
    finally:
        stop_bridge(bridge)
        log("  桥接进程已停止。")

    rule("步骤 4/4　回数据库核对这条借用记录")
    record = lookup_record(args.request_id)
    if record:
        def unpack(value):
            """equipment_name / quantity 列里存的是 JSON 数组原文，这里还原成人看的形式。"""
            try:
                parsed = json.loads(value)
            except (TypeError, ValueError):
                return str(value or "")
            if isinstance(parsed, list):
                return "、".join(str(item) for item in parsed)
            return str(parsed)

        status_label = {"approved": "recorded（已登记）"}.get(record["status"], record["status"])
        log("  已落库：")
        log(f"    记录号   #{record['id']}")
        log(f"    借用人   {record['username']}")
        log(f"    器材     {unpack(record['equipment_name'])} × {unpack(record['quantity'])}")
        log(f"    柜号     {record['cabinet_number']} 号柜")
        log(f"    状态     {status_label}")
        log(f"    时间     {record['created_at']}")
        log(f"    用途     {record['purpose']}")
    else:
        log("  没有查到记录（借用可能失败，或本次是幂等复测）。")

    rule("结果")
    if success and record:
        log("  √ 链路打通：小智云端 MCP 客户端 → 树莓派桥接 → stdio MCP 服务 → 网站接口 → 数据库")
        log(f"  网站：{WEB_BASE}（登录后可在「借用记录」里看到这条）")
        log("  演示账号：demo / demo12345（管理员）　user / user12345（普通用户）")
        return 0
    log("  × 本次没有完整成功，请查看上面的报文与桥接日志定位问题。")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="模拟小智云端 MCP 客户端给树莓派发信号")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="两端一致的设备令牌")
    parser.add_argument(
        "--request-id",
        default="",
        help="幂等请求编号（留空则用 sim-时间戳）",
    )
    parser.add_argument("--keyword", default="18650", help="要查询的零件关键词")
    parser.add_argument("--quantity", type=int, default=1, help="借用数量")
    parser.add_argument("--timeout", type=int, default=45, help="等待桥接连接的秒数")
    args = parser.parse_args()
    if not args.request_id:
        args.request_id = "sim-" + time.strftime("%Y%m%d-%H%M%S")

    log("小智 MCP 信号仿真（本地）")
    log(f"  request_id = {args.request_id}")
    log(f"  关键词 = {args.keyword}，数量 = {args.quantity}")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

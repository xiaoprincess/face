"""Bridge a local stdio MCP server to the Xiaozhi MCP WebSocket endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import websockets
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
LOGGER = logging.getLogger("xiaozhi_bridge")
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "mcp_bridge.log"
STATUS_FILE = LOG_DIR / "mcp_status.json"
INITIAL_BACKOFF_SECONDS = 1
MAX_BACKOFF_SECONDS = 60


def _safe_endpoint(endpoint: str) -> str:
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console_handler, file_handler])


def _new_status() -> dict:
    return {
        "state": "starting",
        "bridge_pid": os.getpid(),
        "child_pid": None,
        "connected_since": None,
        "received_messages": 0,
        "sent_messages": 0,
        "tool_calls": 0,
        "last_method": None,
        "last_tool": None,
        "last_result": None,
        "last_error": None,
    }


def _write_status(status: dict) -> None:
    """Atomically publish non-sensitive bridge state for the monitor."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload = dict(status)
        payload["updated_at"] = _now()
        temporary = STATUS_FILE.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, STATUS_FILE)
    except OSError as error:
        LOGGER.debug("无法更新 MCP 状态文件：%s", error)


def _message_key(message_id: object) -> str:
    return json.dumps(message_id, ensure_ascii=False, sort_keys=True)


def _business_result_succeeded(message: dict) -> bool | None:
    """Read a tool's business-level success flag from common MCP result shapes."""

    result = message.get("result")
    if not isinstance(result, dict):
        return None
    if result.get("isError") is True:
        return False

    candidates = [result.get("structuredContent"), result]
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            try:
                candidates.append(json.loads(str(item.get("text", ""))))
            except (TypeError, ValueError):
                continue

    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(candidate.get("success"), bool):
            return candidate["success"]
    return None


def _observe_message(
    raw_message: str,
    source: str,
    status: dict,
    pending_calls: dict[str, str],
) -> None:
    """Record protocol metadata without logging parameters, results, or secrets."""
    try:
        decoded = json.loads(raw_message)
    except (TypeError, ValueError):
        LOGGER.warning("收到无法解析的 MCP 消息（内容已隐藏）。")
        return

    messages = decoded if isinstance(decoded, list) else [decoded]
    for message in messages:
        if not isinstance(message, dict):
            continue

        if source == "xiaozhi":
            status["received_messages"] += 1
            method = message.get("method")
            if isinstance(method, str):
                status["last_method"] = method
                if method == "tools/call":
                    params = message.get("params")
                    tool_name = (
                        params.get("name", "未知工具")
                        if isinstance(params, dict)
                        else "未知工具"
                    )
                    tool_name = str(tool_name)
                    status["tool_calls"] += 1
                    status["last_tool"] = tool_name
                    status["last_result"] = "running"
                    if "id" in message:
                        pending_calls[_message_key(message["id"])] = tool_name
                    LOGGER.info("MCP 工具调用：%s", tool_name)
                else:
                    LOGGER.info("MCP 请求：%s", method)
        else:
            status["sent_messages"] += 1
            if "id" in message and ("result" in message or "error" in message):
                tool_name = pending_calls.pop(_message_key(message["id"]), None)
                if tool_name:
                    business_success = _business_result_succeeded(message)
                    succeeded = "error" not in message and business_success is not False
                    status["last_tool"] = tool_name
                    status["last_result"] = "success" if succeeded else "error"
                    if succeeded:
                        LOGGER.info("MCP 工具返回：%s（成功）", tool_name)
                    else:
                        LOGGER.warning("MCP 工具返回：%s（失败，详情已隐藏）", tool_name)
            method = message.get("method")
            if isinstance(method, str):
                status["last_method"] = method
                LOGGER.info("本地 MCP 消息：%s", method)

    _write_status(status)


async def _websocket_to_process(
    websocket,
    process: subprocess.Popen[str],
    status: dict,
    pending_calls: dict[str, str],
) -> None:
    while True:
        message = await websocket.recv()
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        _observe_message(message, "xiaozhi", status, pending_calls)
        if process.stdin is None:
            raise RuntimeError("MCP 子进程标准输入不可用。")
        process.stdin.write(message.rstrip("\n") + "\n")
        process.stdin.flush()


async def _process_to_websocket(
    process: subprocess.Popen[str],
    websocket,
    status: dict,
    pending_calls: dict[str, str],
) -> None:
    if process.stdout is None:
        raise RuntimeError("MCP 子进程标准输出不可用。")
    while True:
        line = await asyncio.to_thread(process.stdout.readline)
        if not line:
            raise RuntimeError(f"MCP 子进程已退出，退出码：{process.poll()}")
        _observe_message(line, "local", status, pending_calls)
        await websocket.send(line.rstrip("\n"))


async def _stream_stderr(process: subprocess.Popen[str]) -> None:
    if process.stderr is None:
        return
    while True:
        line = await asyncio.to_thread(process.stderr.readline)
        if not line:
            return
        LOGGER.info("本地 MCP：%s", line.rstrip())


async def _status_heartbeat(status: dict) -> None:
    while True:
        _write_status(status)
        await asyncio.sleep(5)


async def _connect_once(endpoint: str, status: dict) -> None:
    command = [sys.executable, "-X", "utf8", str(BASE_DIR / "parts_mcp.py")]
    status.update(
        state="connecting",
        child_pid=None,
        connected_since=None,
        last_error=None,
    )
    _write_status(status)
    LOGGER.info("正在连接小智 MCP：%s", _safe_endpoint(endpoint))
    async with websockets.connect(
        endpoint, open_timeout=20, ping_interval=20, ping_timeout=20
    ) as websocket:
        process = subprocess.Popen(
            command,
            cwd=BASE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        pending_calls: dict[str, str] = {}
        status.update(
            state="connected",
            child_pid=process.pid,
            connected_since=_now(),
            last_error=None,
        )
        _write_status(status)
        LOGGER.info("MCP bridge connected; local inventory tools are online.")
        tasks = [
            asyncio.create_task(
                _websocket_to_process(websocket, process, status, pending_calls)
            ),
            asyncio.create_task(
                _process_to_websocket(process, websocket, status, pending_calls)
            ),
            asyncio.create_task(_stream_stderr(process)),
            asyncio.create_task(_status_heartbeat(status)),
        ]
        try:
            done, _ = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done:
                error = task.exception()
                if error is not None:
                    raise error
            raise RuntimeError("MCP 通信任务意外结束。")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 5)
                except subprocess.TimeoutExpired:
                    process.kill()
            status["child_pid"] = None
            _write_status(status)


async def main() -> None:
    endpoint = os.environ.get("XIAOZHI_MCP_ENDPOINT", "").strip()
    if not endpoint.startswith(("ws://", "wss://")):
        raise RuntimeError("请在 .env 中配置有效的 XIAOZHI_MCP_ENDPOINT。")

    status = _new_status()
    _write_status(status)
    delay = INITIAL_BACKOFF_SECONDS
    try:
        while True:
            try:
                await _connect_once(endpoint, status)
                delay = INITIAL_BACKOFF_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as error:
                safe_error = str(error).replace(endpoint, _safe_endpoint(endpoint))
                status.update(
                    state="reconnecting",
                    child_pid=None,
                    connected_since=None,
                    last_error=safe_error,
                    retry_in_seconds=delay,
                )
                _write_status(status)
                LOGGER.warning("连接断开：%s；将在 %s 秒后重试。", safe_error, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
    finally:
        status.update(state="stopped", child_pid=None, connected_since=None)
        _write_status(status)


if __name__ == "__main__":
    _configure_logging()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        LOGGER.info("已停止 MCP 桥接服务。")

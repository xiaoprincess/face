"""Inventory query tools and authenticated hardware tools for Parts Manager."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastmcp import Context, FastMCP

from inventory_store import (
    InventoryStoreError,
    cabinet_inventory,
    database_path,
    inventory_summary,
    low_stock,
    search_inventory,
)


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# MCP stdio messages contain Chinese inventory names. Keep the JSON stream UTF-8 on Windows.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

mcp = FastMCP("零件管家库存")


def _db_path() -> str:
    return str(database_path(os.environ.get("PARTS_DB_PATH", "").strip() or None))


def _failure(error: Exception) -> dict:
    return {"success": False, "message": str(error), "items": []}


def _web_base_url() -> str:
    """Return the local unified website URL without exposing credentials."""

    return os.environ.get("PARTS_WEB_BASE_URL", "http://127.0.0.1:5050").strip().rstrip("/")


def _borrow_timeout() -> float:
    try:
        return min(max(float(os.environ.get("PARTS_BORROW_TIMEOUT_SECONDS", "45")), 5.0), 90.0)
    except ValueError:
        return 45.0


def _post_borrow_request(payload: dict) -> dict:
    """Call the local website, which owns the camera, face check and database write."""

    import json

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    device_token = os.environ.get("PARTS_DEVICE_TOKEN", "").strip()
    if device_token:
        headers["X-Device-Token"] = device_token

    request = Request(
        f"{_web_base_url()}/parts/api/device/borrow",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=_borrow_timeout()) as response:
            raw = response.read().decode("utf-8")
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise ValueError("网站返回的数据格式无效")
            return result
    except HTTPError as error:
        try:
            raw = error.read().decode("utf-8")
            result = json.loads(raw) if raw else {}
            if isinstance(result, dict):
                return result
        except (UnicodeDecodeError, ValueError):
            pass
        return {"success": False, "message": f"借用请求被网站拒绝（HTTP {error.code}）"}
    except (URLError, TimeoutError, OSError):
        return {"success": False, "message": "无法连接零件网站，请确认网站和树莓派服务在线。"}
    except (UnicodeDecodeError, ValueError):
        return {"success": False, "message": "零件网站返回的数据格式无效。"}



APRILTAG_FAMILY = "tag36h11"
APRILTAG_MAX_ID = 586
TAG_TEST_MIN_TIMEOUT = 10.0
TAG_TEST_MAX_TIMEOUT = 180.0
TAG_TEST_DEFAULT_TIMEOUT = 45.0
TAG_TEST_EXTRA_WAIT = 90.0
TAG_TEST_POLL_INTERVAL = 0.5


class TagTestTransportError(RuntimeError):
    """Raised when the local website cannot be reached or returns junk."""


def _device_headers() -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    device_token = os.environ.get("PARTS_DEVICE_TOKEN", "").strip()
    if device_token:
        headers["X-Device-Token"] = device_token
    return headers


def _http_json(method: str, url: str, payload: dict | None = None, timeout: float = 15.0):
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=body, headers=_device_headers(), method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            if not isinstance(data, dict):
                raise TagTestTransportError("网站返回的数据格式无效")
            return response.status, data
    except HTTPError as error:
        try:
            raw = error.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
        except (UnicodeDecodeError, ValueError):
            data = {}
        if isinstance(data, dict) and data:
            return error.code, data
        return error.code, {"success": False, "message": f"网站拒绝请求（HTTP {error.code}）"}
    except (URLError, TimeoutError, OSError) as error:
        raise TagTestTransportError(f"无法连接零件网站：{error}") from error
    except (UnicodeDecodeError, ValueError) as error:
        raise TagTestTransportError("零件网站返回的数据不是有效 JSON") from error


def _tag_test_url(request_id: str = "") -> str:
    base = f"{_web_base_url()}/parts/api/device/tag-test"
    return f"{base}/{request_id}" if request_id else base


def _start_tag_test(tag_id: int, request_id: str, timeout_seconds: float):
    return _http_json(
        "POST",
        _tag_test_url(),
        {
            "tag_id": tag_id,
            "request_id": request_id,
            "timeout_seconds": timeout_seconds,
        },
    )


def _tag_test_status(request_id: str):
    return _http_json("GET", _tag_test_url(request_id))


def _cancel_tag_test(request_id: str):
    return _http_json("POST", f"{_tag_test_url(request_id)}/cancel", {})


def _parse_tag_id(raw_value) -> int:
    try:
        tag_id = int(raw_value)
    except (TypeError, ValueError) as error:
        raise ValueError("AprilTag ID 必须是整数") from error
    if tag_id < 0 or tag_id > APRILTAG_MAX_ID:
        raise ValueError(f"AprilTag ID 必须在 0 到 {APRILTAG_MAX_ID} 之间（{APRILTAG_FAMILY}）")
    return tag_id


def _parse_timeout(raw_value) -> float:
    try:
        timeout = float(raw_value)
    except (TypeError, ValueError) as error:
        raise ValueError("定位时限必须是数字") from error
    return min(max(timeout, TAG_TEST_MIN_TIMEOUT), TAG_TEST_MAX_TIMEOUT)


def _parse_request_id(raw_value) -> str:
    request_id = str(raw_value or "").strip()
    if not request_id:
        return uuid.uuid4().hex
    if len(request_id) > 80 or not all(char.isalnum() or char in "-_" for char in request_id):
        raise ValueError("请求编号只能包含字母、数字、连字符或下划线")
    return request_id


def _tool_result(snapshot: dict) -> dict:
    state = str(snapshot.get("state") or "unknown")
    result = snapshot.get("result") or {}
    payload = {
        "success": False,
        "state": state,
        "status": result.get("status") or snapshot.get("error_code") or state,
        "tag_id": snapshot.get("tag_id"),
        "request_id": snapshot.get("request_id"),
        "username": snapshot.get("username"),
        "face_id": snapshot.get("face_id"),
        "user_id": snapshot.get("user_id"),
        "face_score": snapshot.get("face_score"),
        "face_attempts": snapshot.get("face_attempts"),
        "stages": snapshot.get("stages") or [],
        "message": snapshot.get("error") or result.get("message") or "",
        "borrow_record_created": False,
    }
    if state == "done" and isinstance(result, dict):
        payload.update(result)
        payload["success"] = bool(result.get("success"))
        payload["state"] = "done"
    return payload


async def _report_progress(ctx, snapshot: dict) -> None:
    if ctx is None:
        return
    stages = snapshot.get("stages") or []
    finished = sum(1 for stage in stages if stage.get("status") != "running")
    label = ""
    if stages:
        current = stages[-1]
        label = current.get("label") or current.get("name") or ""
        detail = str(current.get("message") or "").strip()
        if detail:
            label = f"{label}（{detail}）"
    # The matched stage may be shorter than one polling interval. Carry the
    # identity on later progress updates as well, including search/cleanup.
    if snapshot.get('face_id') is not None:
        label += f"；人脸 ID：{snapshot['face_id']}，用户：{snapshot.get('username') or '—'}"
    try:
        await ctx.report_progress(finished, max(len(stages), 1), label)
    except Exception:
        # Progress is informational; a client that does not support it must not fail the test.
        pass

@mcp.tool()
def search_part(keyword: str, limit: int = 10) -> dict:
    """按器材名称、型号或柜号查询库存和位置。用户问“某个零件在哪”时使用。"""
    try:
        items = search_inventory(keyword, limit, _db_path())
        return {
            "success": True,
            "keyword": keyword,
            "count": len(items),
            "items": items,
            "message": "已找到库存记录。" if items else "库存中没有找到匹配的器材。",
        }
    except (InventoryStoreError, ValueError) as error:
        return _failure(error)


@mcp.tool()
def get_cabinet_parts(cabinet_number: str) -> dict:
    """查询指定柜号内的全部器材、箱号和剩余库存。"""
    try:
        items = cabinet_inventory(cabinet_number, _db_path())
        return {
            "success": True,
            "cabinet_number": cabinet_number,
            "count": len(items),
            "items": items,
            "message": "已找到该柜子的库存。" if items else "没有找到该柜号或该柜暂无库存。",
        }
    except (InventoryStoreError, ValueError) as error:
        return _failure(error)


@mcp.tool()
def get_low_stock(threshold: int = 5) -> dict:
    """查询库存数量小于或等于阈值的器材，用于回答缺货或补货问题。"""
    try:
        items = low_stock(threshold, _db_path())
        return {
            "success": True,
            "threshold": threshold,
            "count": len(items),
            "items": items,
            "message": "已列出低库存器材。" if items else "当前没有低于该阈值的器材。",
        }
    except (InventoryStoreError, ValueError) as error:
        return _failure(error)


@mcp.tool()
def get_inventory_overview() -> dict:
    """获取库存柜、器材记录和库存总数的概览。"""
    try:
        summary = inventory_summary(_db_path())
        return {"success": True, **summary}
    except InventoryStoreError as error:
        return _failure(error)


@mcp.tool()
def get_part_borrow_status(request_id: str) -> dict:
    """只读查询已有借用和云台指引状态。使用借用结果中的 request_id，不重复借用。"""
    request_id = str(request_id or "").strip()
    if not request_id or len(request_id) > 80 or not all(char.isalnum() or char in "-_" for char in request_id):
        return {"success": False, "message": "请提供有效的借用请求编号。"}
    try:
        _status, result = _http_json("GET", f"{_web_base_url()}/parts/api/device/borrow/{request_id}")
        return result
    except TagTestTransportError as error:
        return _failure(error)


@mcp.tool()
def request_part_borrow(component_id: int, quantity: int = 1, request_id: str = "") -> dict:
    """登记零件借用记录，并在树莓派上执行云台人脸识别。

    仅当用户已明确表示要借用、且此前 search_part 已返回唯一且准确的
    component_id 时调用。数量不明确、查询结果有多个相似型号、或用户只是
    问位置/库存时，必须先继续询问，不能调用此工具。成功与否及借用状态
    必须以工具返回为准；成功时借用已登记，无需审批。
    """

    try:
        component_id = int(component_id)
        quantity = int(quantity)
    except (TypeError, ValueError):
        return {"success": False, "message": "零件编号和借用数量必须是整数。"}

    if component_id <= 0:
        return {"success": False, "message": "零件编号无效，请先使用 search_part 获取 component_id。"}
    if quantity <= 0 or quantity > 100000:
        return {"success": False, "message": "借用数量必须在 1 到 100000 之间。"}

    request_id = str(request_id or "").strip() or uuid.uuid4().hex
    if len(request_id) > 80 or not all(char.isalnum() or char in "-_" for char in request_id):
        return {"success": False, "message": "请求编号只能包含字母、数字、连字符或下划线。"}

    result = _post_borrow_request(
        {
            "component_id": component_id,
            "quantity": quantity,
            "request_id": request_id,
            "source": "xiaozhi-mcp",
        }
    )
    result.setdefault("request_id", request_id)
    return result



@mcp.tool()
async def test_face_and_point(
    tag_id: int,
    request_id: str = "",
    timeout_seconds: float = TAG_TEST_DEFAULT_TIMEOUT,
    ctx: Context = None,
) -> dict:
    """测试用：转向人脸位置、完成人脸识别，并让云台对准指定 AprilTag。

    仅用于硬件联调，不会创建借用记录、不会改动库存，也不代表用户已经借走零件。
    人脸识别失败时不会继续寻柜。最终结果以返回的 status 为准：
    aligned（已对准）、lost（视野内找不到标签）、timeout（定位超时）、
    cancelled（用户取消）、face_failed（人脸未通过）、hardware_unavailable（硬件不可用）、
    error（其他异常）。
    """
    try:
        tag_id = _parse_tag_id(tag_id)
        timeout = _parse_timeout(timeout_seconds)
        request_id = _parse_request_id(request_id)
    except ValueError as error:
        return {"success": False, "message": str(error), "stages": []}

    try:
        status, body = await asyncio.to_thread(
            _start_tag_test, tag_id, request_id, timeout
        )
    except TagTestTransportError as error:
        return {
            "success": False,
            "message": str(error),
            "request_id": request_id,
            "stages": [],
        }

    if status not in (200, 202) or not body.get("success"):
        return {
            "success": False,
            "state": body.get("state", "failed"),
            "status": body.get("error_code") or "rejected",
            "tag_id": tag_id,
            "request_id": request_id,
            "message": body.get("message") or body.get("error") or "网站拒绝启动测试",
            "stages": body.get("stages") or [],
            "borrow_record_created": False,
        }

    deadline = time.monotonic() + timeout + TAG_TEST_EXTRA_WAIT
    last_key = None
    try:
        while True:
            stages = body.get("stages") or []
            current = stages[-1] if stages else {}
            key = (body.get("stage"), current.get("message"), body.get('face_id'))
            if key != last_key:
                last_key = key
                await _report_progress(ctx, body)
            if body.get("terminal") or body.get("state") in {
                "done",
                "failed",
                "cancelled",
                "timeout",
            }:
                return _tool_result(body)
            if time.monotonic() > deadline:
                try:
                    await asyncio.to_thread(_cancel_tag_test, request_id)
                except TagTestTransportError:
                    pass
                payload = _tool_result(body)
                payload.update(
                    {
                        "success": False,
                        "state": "timeout",
                        "status": "timeout",
                        "message": "等待树莓派硬件测试超时，已请求停止",
                    }
                )
                return payload

            await asyncio.sleep(TAG_TEST_POLL_INTERVAL)
            try:
                _, body = await asyncio.to_thread(_tag_test_status, request_id)
            except TagTestTransportError as error:
                try:
                    await asyncio.to_thread(_cancel_tag_test, request_id)
                except TagTestTransportError:
                    pass
                return {
                    "success": False,
                    "state": "network_error",
                    "status": "network_error",
                    "tag_id": tag_id,
                    "request_id": request_id,
                    "message": str(error),
                    "stages": [],
                    "borrow_record_created": False,
                }
    except asyncio.CancelledError:
        # The MCP client sent notifications/cancelled or the SSH session closed.
        # The tool task is about to disappear, so explicitly stop the website
        # job instead of leaving the motors/camera running.
        try:
            await asyncio.to_thread(_cancel_tag_test, request_id)
        except TagTestTransportError:
            pass
        raise


if __name__ == "__main__":
    mcp.run(transport="stdio")

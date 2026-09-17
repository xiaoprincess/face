"""Inventory query tools and an authenticated borrow-request tool for Parts Manager."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastmcp import FastMCP

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
def request_part_borrow(component_id: int, quantity: int = 1, request_id: str = "") -> dict:
    """为用户提交零件借用申请，并在树莓派上执行云台人脸识别。

    仅当用户已明确表示要借用、且此前 search_part 已返回唯一且准确的
    component_id 时调用。数量不明确、查询结果有多个相似型号、或用户只是
    问位置/库存时，必须先继续询问，不能调用此工具。工具成功代表人脸已
    通过并已创建待审批申请；失败时不要声称申请成功。
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


if __name__ == "__main__":
    mcp.run(transport="stdio")

"""Read and import the shared inventory database used by the website and MCP."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE = BASE_DIR.parent / "unified_web_app" / "instance" / "unified.db"
REQUIRED_COLUMNS = ("柜号", "箱号", "器材名称", "剩余库存数量")


class InventoryStoreError(RuntimeError):
    """Raised when the shared inventory database cannot be used safely."""


def database_path(value: str | None = None) -> Path:
    return Path(value).expanduser().resolve() if value else DEFAULT_DATABASE.resolve()


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise InventoryStoreError(
            f"未找到库存数据库：{path}。请先启动一次统一网站，或检查 PARTS_DB_PATH。"
        )
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _assert_schema(connection: sqlite3.Connection) -> None:
    existing = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    required = {"cabinets", "cabinet_components"}
    if not required.issubset(existing):
        raise InventoryStoreError("库存表尚未初始化。请先启动一次 unified_web_app。")


def _box_number(notes: str | None) -> str:
    if not notes:
        return ""
    prefix = "箱号 "
    value = str(notes).strip()
    if value.startswith(prefix):
        return value[len(prefix) :].split("（", 1)[0].strip()
    return value


def _record(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "component_id": row["component_id"],
        "name": row["component_name"],
        "quantity": row["quantity"],
        "unit": row["unit"],
        "cabinet_number": row["cabinet_number"],
        "cabinet_category": row["equipment_name"],
        "box_number": _box_number(row["notes"]),
        "notes": row["notes"] or "",
    }


def inventory_summary(path: str | None = None) -> dict[str, int]:
    with _connect(database_path(path)) as connection:
        _assert_schema(connection)
        row = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM cabinets) AS cabinet_count,
                (SELECT COUNT(*) FROM cabinet_components) AS component_count,
                COALESCE((SELECT SUM(quantity) FROM cabinet_components), 0) AS total_quantity
            """
        ).fetchone()
    return dict(row)


def search_inventory(keyword: str, limit: int = 10, path: str | None = None) -> list[dict[str, Any]]:
    keyword = str(keyword or "").strip()
    if not keyword:
        raise InventoryStoreError("请输入要查询的器材名称或型号。")
    limit = max(1, min(int(limit), 30))
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    with _connect(database_path(path)) as connection:
        _assert_schema(connection)
        rows = connection.execute(
            """
            SELECT cc.id AS component_id, cc.component_name, cc.quantity, cc.unit, cc.notes,
                   c.cabinet_number, c.equipment_name
            FROM cabinet_components AS cc
            JOIN cabinets AS c ON c.id = cc.cabinet_id
            WHERE cc.component_name LIKE ? ESCAPE '\\'
               OR c.cabinet_number LIKE ? ESCAPE '\\'
            ORDER BY c.cabinet_number, cc.component_name, cc.id
            LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()
    return [_record(row) for row in rows]


def cabinet_inventory(cabinet_number: str, path: str | None = None) -> list[dict[str, Any]]:
    cabinet_number = str(cabinet_number or "").strip()
    if not cabinet_number:
        raise InventoryStoreError("请输入柜号。")
    with _connect(database_path(path)) as connection:
        _assert_schema(connection)
        rows = connection.execute(
            """
            SELECT cc.id AS component_id, cc.component_name, cc.quantity, cc.unit, cc.notes,
                   c.cabinet_number, c.equipment_name
            FROM cabinet_components AS cc
            JOIN cabinets AS c ON c.id = cc.cabinet_id
            WHERE c.cabinet_number = ?
            ORDER BY cc.component_name, cc.id
            """,
            (cabinet_number,),
        ).fetchall()
    return [_record(row) for row in rows]


def low_stock(threshold: int = 5, path: str | None = None) -> list[dict[str, Any]]:
    threshold = max(0, min(int(threshold), 100000))
    with _connect(database_path(path)) as connection:
        _assert_schema(connection)
        rows = connection.execute(
            """
            SELECT cc.id AS component_id, cc.component_name, cc.quantity, cc.unit, cc.notes,
                   c.cabinet_number, c.equipment_name
            FROM cabinet_components AS cc
            JOIN cabinets AS c ON c.id = cc.cabinet_id
            WHERE cc.quantity <= ?
            ORDER BY cc.quantity ASC, c.cabinet_number, cc.component_name
            """,
            (threshold,),
        ).fetchall()
    return [_record(row) for row in rows]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _quantity(value: Any, row_number: int) -> int:
    try:
        quantity = int(float(value))
    except (TypeError, ValueError) as error:
        raise InventoryStoreError(f"第 {row_number} 行的库存数量无效。") from error
    if quantity < 0:
        raise InventoryStoreError(f"第 {row_number} 行的库存数量不能小于 0。")
    return quantity


def import_inventory_xlsx(xlsx_path: str | Path, path: str | None = None) -> dict[str, int]:
    """Upsert inventory rows from the supplied workbook without deleting website-only data."""

    try:
        from openpyxl import load_workbook
    except ImportError as error:  # pragma: no cover - installation guidance
        raise InventoryStoreError("缺少 openpyxl，请先运行 install.bat 或 pip install -r requirements.txt。") from error

    workbook_path = Path(xlsx_path).expanduser().resolve()
    if not workbook_path.is_file():
        raise InventoryStoreError(f"未找到库存表：{workbook_path}")

    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    worksheet = workbook.active
    headers = [_text(cell) for cell in next(worksheet.iter_rows(min_row=1, max_row=1, values_only=True))]
    missing = [name for name in REQUIRED_COLUMNS if name not in headers]
    if missing:
        raise InventoryStoreError(f"库存表缺少列：{'、'.join(missing)}")
    positions = {name: headers.index(name) for name in REQUIRED_COLUMNS}

    imported = 0
    created_cabinets = 0
    updated = 0
    with _connect(database_path(path)) as connection:
        _assert_schema(connection)
        for row_number, values in enumerate(worksheet.iter_rows(min_row=2, values_only=True), start=2):
            cabinet_number = _text(values[positions["柜号"]])
            box_number = _text(values[positions["箱号"]])
            component_name = _text(values[positions["器材名称"]])
            if not any((cabinet_number, box_number, component_name)):
                continue
            if not cabinet_number or not component_name:
                raise InventoryStoreError(f"第 {row_number} 行缺少柜号或器材名称。")
            quantity = _quantity(values[positions["剩余库存数量"]], row_number)
            note = f"箱号 {box_number}（来自库存清单）" if box_number else "来自库存清单"

            cabinet = connection.execute(
                "SELECT id FROM cabinets WHERE cabinet_number = ?", (cabinet_number,)
            ).fetchone()
            if cabinet is None:
                cursor = connection.execute(
                    """
                    INSERT INTO cabinets (cabinet_number, equipment_name, description, created_at, updated_at)
                    VALUES (?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (cabinet_number, "零件库存", "由零件管家器材库存清单导入"),
                )
                cabinet_id = cursor.lastrowid
                created_cabinets += 1
            else:
                cabinet_id = cabinet["id"]

            component = connection.execute(
                """
                SELECT id FROM cabinet_components
                WHERE cabinet_id = ? AND component_name = ? AND notes = ?
                """,
                (cabinet_id, component_name, note),
            ).fetchone()
            if component is None:
                connection.execute(
                    """
                    INSERT INTO cabinet_components
                    (cabinet_id, component_name, quantity, unit, notes, created_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    (cabinet_id, component_name, quantity, "件", note),
                )
                imported += 1
            else:
                connection.execute(
                    "UPDATE cabinet_components SET quantity = ? WHERE id = ?",
                    (quantity, component["id"]),
                )
                updated += 1
    workbook.close()
    return {
        "created_cabinets": created_cabinets,
        "created_components": imported,
        "updated_components": updated,
    }

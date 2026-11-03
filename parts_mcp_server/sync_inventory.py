"""Import an Excel inventory sheet into the database shared with unified_web_app."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # 仅导入库存时不必依赖 MCP 运行环境
    def load_dotenv(*_args, **_kwargs):
        return False

from inventory_store import InventoryStoreError, database_path, import_inventory_xlsx


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def main() -> int:
    parser = argparse.ArgumentParser(description="将库存 Excel 同步至零件管家网站数据库")
    parser.add_argument("xlsx", type=Path, help="库存清单 xlsx 文件")
    parser.add_argument("--db", help="网站 SQLite 数据库路径，默认读取 PARTS_DB_PATH")
    args = parser.parse_args()
    db_value = args.db or os.environ.get("PARTS_DB_PATH", "").strip() or None
    try:
        result = import_inventory_xlsx(args.xlsx, str(database_path(db_value)))
    except InventoryStoreError as error:
        parser.error(str(error))
    print(
        "同步完成："
        f"新建柜子 {result['created_cabinets']} 个，"
        f"新建器材 {result['created_components']} 条，"
        f"更新器材 {result['updated_components']} 条。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

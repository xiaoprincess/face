"""Inventory access for the AprilTag simulation client.

Online first (the Raspberry Pi website), local repository fallback second.  The
fallback is always opened read-only and is clearly labelled as a copy whose
freshness cannot be guaranteed.
"""

from __future__ import annotations

import json
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

REQUIRED_TABLES = {"cabinets", "cabinet_components"}
REQUIRED_CABINET_COLUMNS = {"id", "cabinet_number", "equipment_name", "description", "updated_at"}
REQUIRED_COMPONENT_COLUMNS = {"id", "cabinet_id", "component_name", "quantity", "unit", "notes"}


class InventoryError(RuntimeError):
    """Raised when no usable inventory can be produced."""


class OnlineInventoryError(InventoryError):
    """The Raspberry Pi website could not provide inventory."""


class LocalInventoryError(InventoryError):
    """A local database or export could not be used."""


@dataclass
class ComponentRecord:
    component_id: int
    component_name: str
    quantity: int
    unit: str
    notes: str


@dataclass
class CabinetRecord:
    cabinet_id: int
    cabinet_number: str
    equipment_name: str
    description: str
    updated_at: str
    components: list = field(default_factory=list)


@dataclass
class InventorySnapshot:
    source_kind: str
    source_label: str
    address: str
    fetched_at: datetime
    data_time: str
    cabinets: list
    warnings: list = field(default_factory=list)
    path: str = ""
    generated_at: str = ""
    file_mtime: str = ""

    @property
    def cabinet_count(self):
        return len(self.cabinets)

    @property
    def component_count(self):
        return sum(len(cabinet.components) for cabinet in self.cabinets)

    def describe(self):
        parts = [self.source_label]
        if self.address:
            parts.append(self.address)
        parts.append(f"{self.cabinet_count} 个柜子 / {self.component_count} 条零件")
        return " · ".join(parts)


@dataclass
class LocalCandidate:
    path: str
    kind: str
    valid: bool
    reason: str = ""
    cabinet_count: int = 0
    component_count: int = 0
    data_time: str = ""
    file_mtime: str = ""
    size: int = 0

    def describe(self):
        if not self.valid:
            return f"{self.path}（不可用：{self.reason}）"
        return (
            f"{self.path}（{self.cabinet_count} 柜 / {self.component_count} 条，"
            f"库存记录 {self.data_time or '未知'}，"
            f"文件修改 {self.file_mtime or '未知'}）"
        )


def _ssl_context(config):
    """HTTPS context; the Pi uses a self-signed certificate by default."""

    if not str(config.web_base_url).lower().startswith("https://"):
        return None
    if getattr(config, "ca_file", ""):
        return ssl.create_default_context(cafile=config.ca_file)
    if not getattr(config, "verify_tls", True):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    return ssl.create_default_context()


def _request_json(url, token, timeout, retries, ssl_context=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Device-Token"] = token
    last_error = None
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(
                request, timeout=timeout, context=ssl_context
            ) as response:
                raw = response.read().decode("utf-8")
                data = json.loads(raw) if raw else {}
                if not isinstance(data, dict):
                    raise OnlineInventoryError("网站返回的数据格式无效")
                return data
        except urllib.error.HTTPError as error:
            body = ""
            try:
                body = error.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if error.code in (401, 403):
                raise OnlineInventoryError(
                    f"设备令牌被网站拒绝（HTTP {error.code}），请检查 PARTS_DEVICE_TOKEN"
                ) from error
            last_error = OnlineInventoryError(f"网站返回 HTTP {error.code} {body}".strip())
        except urllib.error.URLError as error:
            reason = getattr(error, "reason", error)
            if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
                last_error = OnlineInventoryError(
                    "HTTPS 证书校验失败：请在设置中填写树莓派证书（CA 文件），"
                    "或临时关闭“校验 HTTPS 证书”"
                )
            else:
                last_error = OnlineInventoryError(f"无法连接树莓派网站：{reason}")
        except (TimeoutError, OSError) as error:
            last_error = OnlineInventoryError(f"连接树莓派网站超时或被拒绝：{error}")
        except (UnicodeDecodeError, ValueError) as error:
            last_error = OnlineInventoryError(f"网站返回的数据不是有效 JSON：{error}")
        if attempt < retries:
            time.sleep(0.6 * (attempt + 1))
    raise last_error or OnlineInventoryError("无法读取线上库存")


def fetch_online(config):
    """Read the complete cabinet list from the authenticated device endpoint."""

    if not config.device_token:
        raise OnlineInventoryError("未配置设备令牌，无法读取线上库存")
    payload = _request_json(
        config.cabinets_url,
        config.device_token,
        config.http_timeout,
        config.retries,
        _ssl_context(config),
    )
    if not payload.get("success"):
        raise OnlineInventoryError(payload.get("message") or "网站未返回有效库存")
    cabinets = []
    for item in payload.get("cabinets") or []:
        components = []
        for part in item.get("components") or []:
            components.append(
                ComponentRecord(
                    component_id=int(part.get("id") or 0),
                    component_name=str(part.get("component_name") or ""),
                    quantity=int(part.get("quantity") or 0),
                    unit=str(part.get("unit") or ""),
                    notes=str(part.get("notes") or ""),
                )
            )
        cabinets.append(
            CabinetRecord(
                cabinet_id=int(item.get("id") or 0),
                cabinet_number=str(item.get("cabinet_number") or ""),
                equipment_name=str(item.get("equipment_name") or ""),
                description=str(item.get("description") or ""),
                updated_at=str(item.get("updated_at") or ""),
                components=components,
            )
        )
    if not cabinets:
        raise OnlineInventoryError("线上库存为空（没有任何柜子），请先在网站后台录入库存")
    return InventorySnapshot(
        source_kind="online",
        source_label="树莓派实时库存",
        address=config.web_base_url,
        fetched_at=datetime.now(),
        data_time=str(payload.get("generated_at") or ""),
        generated_at=str(payload.get("generated_at") or ""),
        cabinets=cabinets,
        warnings=[],
    )


def _connect_readonly(path):
    uri = "file:" + quote(Path(path).as_posix()) + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _table_columns(connection, table):
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def probe_sqlite(path):
    """Return (valid, reason, counts, data_time).  Never writes to the file."""

    path = Path(path)
    if not path.is_file():
        return False, "文件不存在", 0, 0, ""
    try:
        connection = _connect_readonly(path)
    except sqlite3.Error as error:
        return False, f"无法以只读方式打开：{error}", 0, 0, ""
    try:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = REQUIRED_TABLES - tables
        if missing:
            return False, f"缺少数据表：{'、'.join(sorted(missing))}", 0, 0, ""
        cabinet_columns = _table_columns(connection, "cabinets")
        component_columns = _table_columns(connection, "cabinet_components")
        missing_cabinet = REQUIRED_CABINET_COLUMNS - cabinet_columns
        missing_component = REQUIRED_COMPONENT_COLUMNS - component_columns
        if missing_cabinet or missing_component:
            missing_columns = sorted(missing_cabinet | missing_component)
            return (
                False,
                "缺少库存字段：" + "、".join(missing_columns) + "（可能是旧版站点数据库）",
                0,
                0,
                "",
            )
        cabinet_count = connection.execute("SELECT COUNT(*) FROM cabinets").fetchone()[0]
        component_count = connection.execute(
            "SELECT COUNT(*) FROM cabinet_components"
        ).fetchone()[0]
        if cabinet_count == 0:
            return False, "数据库中没有柜子数据（可能是空库或样本库）", 0, 0, ""
        updated = connection.execute(
            "SELECT MAX(updated_at) FROM cabinets"
        ).fetchone()[0]
        return True, "", cabinet_count, component_count, str(updated or "")
    except sqlite3.Error as error:
        return False, f"读取失败：{error}", 0, 0, ""
    finally:
        connection.close()


def probe_json_export(path):
    path = Path(path)
    if not path.is_file():
        return False, "文件不存在", 0, 0, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return False, f"不是有效 JSON：{error}", 0, 0, ""
    cabinets = payload.get("cabinets") if isinstance(payload, dict) else None
    if not isinstance(cabinets, list) or not cabinets:
        return False, "JSON 中没有 cabinets 列表", 0, 0, ""
    component_count = sum(len(item.get("components") or []) for item in cabinets)
    return True, "", len(cabinets), component_count, str(payload.get("generated_at") or "")


def candidate_from_path(path):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".json":
        valid, reason, cabinets, components, data_time = probe_json_export(path)
        kind = "json"
    elif suffix in {".db", ".sqlite", ".sqlite3"}:
        valid, reason, cabinets, components, data_time = probe_sqlite(path)
        kind = "sqlite"
    else:
        return LocalCandidate(str(path), suffix or "unknown", False, "不支持的文件类型")
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    file_mtime = ""
    if valid:
        try:
            file_mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        except OSError:
            file_mtime = ""
    return LocalCandidate(
        path=str(path),
        kind=kind,
        valid=valid,
        reason=reason,
        cabinet_count=cabinets,
        component_count=components,
        # data_time is the latest inventory record change; file mtime is shown
        # separately so copying an old database does not look freshly updated.
        data_time=data_time,
        file_mtime=file_mtime,
        size=size,
    )


def default_scan_dirs():
    from config import REPO_DIR

    return [
        REPO_DIR / "unified_web_app" / "instance",
        REPO_DIR,
        REPO_DIR / "instance",
    ]


def scan_local_candidates(config):
    """Collect local database/export candidates, valid ones first."""

    seen = set()
    candidates = []
    paths = []
    if config.local_db_path:
        paths.append(Path(config.local_db_path))
    for directory in default_scan_dirs():
        if not directory.is_dir():
            continue
        for pattern in ("*.db", "*.sqlite", "*.sqlite3", "*.json"):
            paths.extend(sorted(directory.glob(pattern)))
    for path in paths:
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.name in {"equipment_system.db"}:
            candidates.append(
                LocalCandidate(
                    path=str(path),
                    kind="sqlite",
                    valid=False,
                    reason="旧版站点数据库，不是当前库存",
                )
            )
            continue
        candidates.append(candidate_from_path(path))
    candidates.sort(key=lambda item: (not item.valid, item.path))
    return candidates


def load_local(path):
    """Load a validated local sqlite database or JSON export, read-only."""

    candidate = candidate_from_path(path)
    if not candidate.valid:
        raise LocalInventoryError(candidate.reason or "本地库存不可用")
    if candidate.kind == "json":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        items = payload.get("cabinets") or []
    else:
        connection = _connect_readonly(path)
        try:
            rows = connection.execute(
                """
                SELECT id, cabinet_number, equipment_name,
                       COALESCE(description, '') AS description,
                       COALESCE(updated_at, '') AS updated_at
                FROM cabinets
                ORDER BY cabinet_number
                """
            ).fetchall()
            parts = connection.execute(
                """
                SELECT id, cabinet_id, component_name, quantity, unit, notes
                FROM cabinet_components
                ORDER BY id
                """
            ).fetchall()
        finally:
            connection.close()
        grouped = {}
        for row in parts:
            grouped.setdefault(row["cabinet_id"], []).append(
                {
                    "id": row["id"],
                    "component_name": row["component_name"],
                    "quantity": row["quantity"],
                    "unit": row["unit"],
                    "notes": row["notes"] or "",
                }
            )
        items = [
            {
                "id": row["id"],
                "cabinet_number": row["cabinet_number"],
                "equipment_name": row["equipment_name"],
                "description": row["description"],
                "updated_at": row["updated_at"],
                "components": grouped.get(row["id"], []),
            }
            for row in rows
        ]

    cabinets = []
    for item in items:
        cabinets.append(
            CabinetRecord(
                cabinet_id=int(item.get("id") or 0),
                cabinet_number=str(item.get("cabinet_number") or ""),
                equipment_name=str(item.get("equipment_name") or ""),
                description=str(item.get("description") or ""),
                updated_at=str(item.get("updated_at") or ""),
                components=[
                    ComponentRecord(
                        component_id=int(part.get("id") or 0),
                        component_name=str(part.get("component_name") or ""),
                        quantity=int(part.get("quantity") or 0),
                        unit=str(part.get("unit") or ""),
                        notes=str(part.get("notes") or ""),
                    )
                    for part in item.get("components") or []
                ],
            )
        )
    return InventorySnapshot(
        source_kind="local",
        source_label="本地副本，可能过期",
        address=str(path),
        fetched_at=datetime.now(),
        data_time=candidate.data_time,
        cabinets=cabinets,
        warnings=["本地副本只作参考，实际执行仍依赖树莓派在线。"],
        path=str(path),
        file_mtime=candidate.file_mtime,
    )

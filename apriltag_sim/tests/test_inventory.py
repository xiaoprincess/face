"""Tests for online inventory access and the local read-only fallback."""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import SimConfig  # noqa: E402
from inventory import (  # noqa: E402
    OnlineInventoryError,
    candidate_from_path,
    fetch_online,
    load_local,
    probe_sqlite,
)


SCHEMA = """
CREATE TABLE cabinets (
    id INTEGER PRIMARY KEY,
    cabinet_number TEXT UNIQUE NOT NULL,
    equipment_name TEXT NOT NULL,
    description TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE cabinet_components (
    id INTEGER PRIMARY KEY,
    cabinet_id INTEGER NOT NULL,
    component_name TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    unit TEXT NOT NULL,
    notes TEXT,
    created_at TEXT
);
"""


def make_database(path, empty_cabinet=True):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO cabinets VALUES (1, '11', '零件库存', '常用零件', '2026-01-01', '2026-11-01 10:00:00')"
    )
    connection.execute(
        "INSERT INTO cabinet_components VALUES (1, 1, 'AO3401A', 20, '个', '箱号 A1（来自库存清单）', '2026-01-01')"
    )
    if empty_cabinet:
        connection.execute(
            "INSERT INTO cabinets VALUES (2, '20', '空柜', '', '2026-01-01', '2026-10-30 09:00:00')"
        )
    connection.commit()
    connection.close()


class StubHandler(BaseHTTPRequestHandler):
    token = "good-token"
    payload = {}

    def do_GET(self):
        if self.path != "/parts/api/device/cabinets":
            self.send_response(404)
            self.end_headers()
            return
        if self.headers.get("X-Device-Token") != self.token:
            body = json.dumps({"success": False, "message": "设备认证失败"}).encode()
            self.send_response(401)
        else:
            body = json.dumps(self.payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class InventoryTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        # Do not use mkdtemp: its chmod(0o700) creates a directory the Windows
        # sandbox denies to child processes.  A normal inherited-ACL dir works.
        self.temp_dir = base / ("case-" + str(time.time_ns()))
        self.temp_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, str(self.temp_dir), True)

    def test_local_database_is_read_only_and_complete(self):
        path = Path(self.temp_dir) / "inventory.db"
        make_database(path)
        valid, reason, cabinets, components, updated = probe_sqlite(path)
        self.assertTrue(valid, reason)
        self.assertEqual((cabinets, components), (2, 1))
        self.assertTrue(updated)

        snapshot = load_local(path)
        self.assertEqual(snapshot.source_kind, "local")
        self.assertIn("可能过期", snapshot.source_label)
        self.assertEqual(snapshot.data_time, "2026-11-01 10:00:00")
        self.assertTrue(snapshot.file_mtime)
        self.assertEqual(snapshot.cabinet_count, 2)
        self.assertEqual(snapshot.component_count, 1)
        empty = [item for item in snapshot.cabinets if item.cabinet_number == "20"][0]
        self.assertEqual(empty.components, [])

    def test_empty_database_is_rejected(self):
        path = Path(self.temp_dir) / "empty.db"
        connection = sqlite3.connect(path)
        connection.executescript(SCHEMA)
        connection.commit()
        connection.close()
        candidate = candidate_from_path(path)
        self.assertFalse(candidate.valid)
        self.assertIn("没有柜子", candidate.reason)

    def test_legacy_schema_is_rejected_with_reason(self):
        path = Path(self.temp_dir) / "legacy.db"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE cabinets (id INTEGER PRIMARY KEY, cabinet_number TEXT, equipment_name TEXT, created_at TEXT);
            CREATE TABLE cabinet_components (id INTEGER PRIMARY KEY, cabinet_id INTEGER, component_name TEXT);
            """
        )
        connection.commit()
        connection.close()
        candidate = candidate_from_path(path)
        self.assertFalse(candidate.valid)
        self.assertIn("缺少库存字段", candidate.reason)

    def test_online_inventory_parses_full_list(self):
        StubHandler.payload = {
            "success": True,
            "generated_at": "2026-11-04T12:00:00",
            "cabinets": [
                {
                    "id": 3,
                    "cabinet_number": "11",
                    "equipment_name": "零件库存",
                    "description": "",
                    "updated_at": "2026-11-01 10:00:00",
                    "components": [
                        {"id": 9, "component_name": "AO3401A", "quantity": 7, "unit": "个", "notes": "箱号 B2"}
                    ],
                },
                {
                    "id": 4,
                    "cabinet_number": "20",
                    "equipment_name": "空柜",
                    "description": "",
                    "components": [],
                },
            ],
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        config = SimConfig(
            web_base_url=f"http://127.0.0.1:{server.server_address[1]}",
            device_token="good-token",
            retries=0,
            http_timeout=5,
        )
        snapshot = fetch_online(config)
        self.assertEqual(snapshot.source_kind, "online")
        self.assertEqual(snapshot.source_label, "树莓派实时库存")
        self.assertEqual(snapshot.cabinet_count, 2)
        self.assertEqual(snapshot.component_count, 1)
        self.assertEqual(snapshot.cabinets[0].components[0].quantity, 7)

    def test_bad_token_is_reported_not_swallowed(self):
        StubHandler.payload = {"success": True, "cabinets": []}
        server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        config = SimConfig(
            web_base_url=f"http://127.0.0.1:{server.server_address[1]}",
            device_token="wrong",
            retries=0,
            http_timeout=5,
        )
        with self.assertRaises(OnlineInventoryError) as raised:
            fetch_online(config)
        self.assertIn("令牌", str(raised.exception))

    def test_unreachable_website_raises(self):
        config = SimConfig(web_base_url="http://127.0.0.1:1", device_token="t", retries=0, http_timeout=2)
        with self.assertRaises(OnlineInventoryError):
            fetch_online(config)

    def test_missing_token_fails_before_network(self):
        config = SimConfig(web_base_url="http://127.0.0.1:1", device_token="", retries=0)
        with self.assertRaises(OnlineInventoryError):
            fetch_online(config)


if __name__ == "__main__":
    unittest.main()

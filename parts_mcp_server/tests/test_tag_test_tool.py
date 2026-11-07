"""Integration test for the test_face_and_point MCP tool.

The website is replaced by a local HTTP stub: no Raspberry Pi, camera or Baidu
call is involved.  The test drives the real FastMCP server object through the
in-memory client, so tool registration, polling, progress and result mapping
are all exercised.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from fastmcp import Client  # noqa: E402

import parts_mcp  # noqa: E402


class StubSite(BaseHTTPRequestHandler):
    calls = []
    polls = 0

    def _json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.headers.get("X-Device-Token") != "stub-token":
            self._json(401, {"success": False, "message": "设备认证失败"})
            return
        if self.path == "/parts/api/device/tag-test":
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
            StubSite.calls.append(payload)
            StubSite.polls = 0
            self._json(
                202,
                {
                    "success": True,
                    "created": True,
                    "request_id": payload.get("request_id"),
                    "tag_id": payload.get("tag_id"),
                    "state": "running",
                    "stage": "queued",
                    "terminal": False,
                    "stages": [
                        {"name": "queued", "label": "等待执行", "status": "done", "message": "", "elapsed": 0.01}
                    ],
                },
            )
            return
        self._json(404, {"success": False, "message": "not found"})

    def do_GET(self):
        if self.headers.get("X-Device-Token") != "stub-token":
            self._json(401, {"success": False, "message": "设备认证失败"})
            return
        if self.path.startswith("/parts/api/device/tag-test/"):
            StubSite.polls += 1
            if StubSite.polls < 2:
                self._json(
                    200,
                    {
                        "success": True,
                        "request_id": "stub1",
                        "tag_id": 11,
                        "state": "running",
                        "stage": "align",
                        "terminal": False,
                        "username": "alice",
                        "face_score": 91.2,
                        "stages": [
                            {"name": "face_verify", "label": "人脸识别", "status": "ok", "message": "", "elapsed": 1.0},
                            {"name": "align", "label": "对准标签", "status": "running", "message": "", "elapsed": None},
                        ],
                    },
                )
                return
            self._json(
                200,
                {
                    "success": True,
                    "request_id": "stub1",
                    "tag_id": 11,
                    "state": "done",
                    "stage": "done",
                    "terminal": True,
                    "username": "alice",
                    "face_score": 91.2,
                    "stages": [
                        {"name": "face_verify", "label": "人脸识别", "status": "ok", "message": "", "elapsed": 1.0},
                        {"name": "align", "label": "对准标签", "status": "ok", "message": "", "elapsed": 2.5},
                        {"name": "done", "label": "测试完成", "status": "ok", "message": "", "elapsed": 0.0},
                    ],
                    "result": {
                        "success": True,
                        "status": "aligned",
                        "tag_id": 11,
                        "username": "alice",
                        "face_score": 91.2,
                        "message": "已通过人脸识别并对准标签 11",
                        "borrow_record_created": False,
                    },
                },
            )
            return
        self._json(404, {"success": False, "message": "not found"})

    def log_message(self, *args):
        pass


class TagTestToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubSite)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "PARTS_WEB_BASE_URL": f"http://127.0.0.1:{self.server.server_address[1]}",
                "PARTS_DEVICE_TOKEN": "stub-token",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_tool_returns_aligned_result_and_stages(self):
        async def run():
            async with Client(parts_mcp.mcp) as client:
                return await client.call_tool(
                    "test_face_and_point", {"tag_id": 11, "timeout_seconds": 20}
                )

        result = asyncio.run(run())
        data = result.structured_content or result.data
        self.assertTrue(data.get("success"), data)
        self.assertEqual(data.get("status"), "aligned")
        self.assertEqual(data.get("username"), "alice")
        self.assertFalse(data.get("borrow_record_created"))
        self.assertGreaterEqual(len(data.get("stages") or []), 2)

    def test_tool_rejects_out_of_range_tag(self):
        async def run():
            async with Client(parts_mcp.mcp) as client:
                return await client.call_tool("test_face_and_point", {"tag_id": 900})

        result = asyncio.run(run())
        data = result.structured_content or result.data
        self.assertFalse(data.get("success"))
        self.assertIn("586", data.get("message", ""))

    def test_identity_survives_skipped_matched_progress_stage(self):
        messages = []
        class Context:
            async def report_progress(self, progress, total, message):
                messages.append(message)
        asyncio.run(parts_mcp._report_progress(Context(), {
            'face_id': '007', 'username': 'alice',
            'stages': [{'name': 'tag_search', 'label': '搜索目标标签', 'status': 'running'}],
        }))
        self.assertIn('007', messages[0])
        self.assertIn('alice', messages[0])


if __name__ == "__main__":
    unittest.main()

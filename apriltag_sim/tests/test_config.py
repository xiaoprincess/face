"""Tests for configuration loading (BOM, bad JSON, defaults)."""

from __future__ import annotations

import json
import shutil
import sys
import time
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import config as config_module  # noqa: E402
from config import DEFAULT_REMOTE_HOST, DEFAULT_WEB_BASE_URL, SimConfig  # noqa: E402


class ConfigLoadTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parent / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.dir = base / ("cfg-" + str(time.time_ns()))
        self.dir.mkdir()
        self.addCleanup(shutil.rmtree, str(self.dir), True)
        self.original_path = config_module.CONFIG_PATH

    def tearDown(self):
        config_module.CONFIG_PATH = self.original_path

    def write(self, text, bom=False):
        path = self.dir / "config.json"
        data = text.encode("utf-8")
        if bom:
            data = b"\xef\xbb\xbf" + data
        path.write_bytes(data)
        config_module.CONFIG_PATH = path
        return path

    def test_defaults_point_to_current_pi(self):
        config_module.CONFIG_PATH = self.dir / "missing.json"
        loaded = SimConfig.load()
        self.assertEqual(loaded.web_base_url, DEFAULT_WEB_BASE_URL)
        self.assertEqual(loaded.ssh.host, DEFAULT_REMOTE_HOST)
        self.assertEqual(loaded.load_error, "")

    def test_bom_prefixed_config_is_accepted(self):
        payload = {
            "web_base_url": "https://192.168.0.100",
            "device_token": "abc",
            "verify_tls": False,
            "ssh": {"host": "192.168.0.100", "user": "xiao"},
        }
        self.write(json.dumps(payload), bom=True)
        loaded = SimConfig.load()
        self.assertEqual(loaded.load_error, "")
        self.assertEqual(loaded.ssh.host, "192.168.0.100")
        self.assertFalse(loaded.verify_tls)
        self.assertEqual(loaded.device_token, "abc")

    def test_broken_json_reports_error_and_uses_defaults(self):
        self.write("{not json", bom=False)
        loaded = SimConfig.load()
        self.assertIn("配置文件读取失败", loaded.load_error)
        self.assertEqual(loaded.web_base_url, DEFAULT_WEB_BASE_URL)


if __name__ == "__main__":
    unittest.main()

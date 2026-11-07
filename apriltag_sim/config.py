"""Configuration for the desktop AprilTag simulation client.

The file lives next to this package (config.json) and is git-ignored.  Values
can also come from environment variables so the program never needs secrets in
source control.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
CONFIG_PATH = PACKAGE_DIR / "config.json"

DEFAULT_WEB_BASE_URL = "https://192.168.0.100"
DEFAULT_REMOTE_HOST = "192.168.0.100"
DEFAULT_REMOTE_USER = "xiao"
DEFAULT_REMOTE_DIR = "/home/xiao/face/parts_mcp_server"
DEFAULT_PLINK = r"C:\Program Files\PuTTY\plink.exe"
DEFAULT_TAG_TIMEOUT = 45
DEFAULT_HTTP_TIMEOUT = 12


@dataclass
class SshConfig:
    tool: str = "plink"
    host: str = DEFAULT_REMOTE_HOST
    port: int = 22
    user: str = DEFAULT_REMOTE_USER
    hostkey: str = ""
    key_file: str = ""
    plink_path: str = DEFAULT_PLINK
    ssh_path: str = "ssh"
    remote_dir: str = DEFAULT_REMOTE_DIR
    remote_python: str = ""
    connect_timeout: int = 20

    @property
    def remote_python_path(self) -> str:
        if self.remote_python.strip():
            return self.remote_python.strip()
        return self.remote_dir.rstrip("/") + "/.venv/bin/python"

    @classmethod
    def from_dict(cls, data):
        data = data or {}
        return cls(
            tool=str(data.get("tool") or "plink"),
            host=str(data.get("host") or DEFAULT_REMOTE_HOST),
            port=int(data.get("port") or 22),
            user=str(data.get("user") or DEFAULT_REMOTE_USER),
            hostkey=str(data.get("hostkey") or ""),
            key_file=str(data.get("key_file") or ""),
            plink_path=str(data.get("plink_path") or DEFAULT_PLINK),
            ssh_path=str(data.get("ssh_path") or "ssh"),
            remote_dir=str(data.get("remote_dir") or DEFAULT_REMOTE_DIR),
            remote_python=str(data.get("remote_python") or ""),
            connect_timeout=int(data.get("connect_timeout") or 20),
        )


@dataclass
class SimConfig:
    web_base_url: str = DEFAULT_WEB_BASE_URL
    device_token: str = ""
    http_timeout: int = DEFAULT_HTTP_TIMEOUT
    retries: int = 2
    verify_tls: bool = True
    ca_file: str = ""
    local_db_path: str = ""
    last_local_db_path: str = ""
    tag_test_timeout: int = DEFAULT_TAG_TIMEOUT
    ssh: SshConfig = field(default_factory=SshConfig)
    # Set when config.json exists but cannot be parsed (BOM, typo, locked file).
    load_error: str = field(default="", repr=False)

    @property
    def cabinets_url(self) -> str:
        return self.web_base_url.rstrip("/") + "/parts/api/device/cabinets"

    @classmethod
    def load(cls) -> "SimConfig":
        data = {}
        load_error = ""
        if CONFIG_PATH.is_file():
            try:
                # utf-8-sig tolerates a BOM left by Windows editors/PowerShell.
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    raise ValueError("顶层不是 JSON 对象")
            except (OSError, ValueError) as error:
                load_error = (
                    f"配置文件读取失败（{error}），本次使用默认地址 "
                    f"{DEFAULT_WEB_BASE_URL}"
                )
                data = {}
        config = cls(
            web_base_url=str(data.get("web_base_url") or DEFAULT_WEB_BASE_URL),
            device_token=str(data.get("device_token") or ""),
            http_timeout=int(data.get("http_timeout") or DEFAULT_HTTP_TIMEOUT),
            retries=int(data.get("retries") if data.get("retries") is not None else 2),
            verify_tls=bool(data.get("verify_tls", True)),
            ca_file=str(data.get("ca_file") or ""),
            local_db_path=str(data.get("local_db_path") or ""),
            last_local_db_path=str(data.get("last_local_db_path") or ""),
            tag_test_timeout=int(data.get("tag_test_timeout") or DEFAULT_TAG_TIMEOUT),
            ssh=SshConfig.from_dict(data.get("ssh")),
            load_error=load_error,
        )
        return config.with_environment()

    def with_environment(self) -> "SimConfig":
        env = os.environ
        if env.get("PARTS_WEB_BASE_URL"):
            self.web_base_url = env["PARTS_WEB_BASE_URL"].strip()
        if env.get("PARTS_DEVICE_TOKEN"):
            self.device_token = env["PARTS_DEVICE_TOKEN"].strip()
        if env.get("PARTS_REMOTE_HOST"):
            self.ssh.host = env["PARTS_REMOTE_HOST"].strip()
        if env.get("PARTS_REMOTE_USER"):
            self.ssh.user = env["PARTS_REMOTE_USER"].strip()
        if env.get("PARTS_REMOTE_HOSTKEY"):
            self.ssh.hostkey = env["PARTS_REMOTE_HOSTKEY"].strip()
        if env.get("PARTS_REMOTE_DIR"):
            self.ssh.remote_dir = env["PARTS_REMOTE_DIR"].strip()
        if env.get("PARTS_REMOTE_PYTHON"):
            self.ssh.remote_python = env["PARTS_REMOTE_PYTHON"].strip()
        if env.get("PARTS_PLINK"):
            self.ssh.plink_path = env["PARTS_PLINK"].strip()
        if env.get("PARTS_VERIFY_TLS"):
            self.verify_tls = env["PARTS_VERIFY_TLS"].strip().lower() not in {"0", "false", "no", "off"}
        if env.get("PARTS_CA_FILE"):
            self.ca_file = env["PARTS_CA_FILE"].strip()
        if env.get("PARTS_TAG_TEST_TIMEOUT"):
            try:
                self.tag_test_timeout = int(env["PARTS_TAG_TEST_TIMEOUT"])
            except ValueError:
                pass
        self.tag_test_timeout = max(10, min(180, int(self.tag_test_timeout)))
        self.http_timeout = max(3, min(60, int(self.http_timeout)))
        self.retries = max(0, min(5, int(self.retries)))
        return self

    def save(self) -> None:
        data = {
            "web_base_url": self.web_base_url,
            "device_token": self.device_token,
            "http_timeout": self.http_timeout,
            "retries": self.retries,
            "verify_tls": self.verify_tls,
            "ca_file": self.ca_file,
            "local_db_path": self.local_db_path,
            "last_local_db_path": self.last_local_db_path,
            "tag_test_timeout": self.tag_test_timeout,
            "ssh": {
                "tool": self.ssh.tool,
                "host": self.ssh.host,
                "port": self.ssh.port,
                "user": self.ssh.user,
                "hostkey": self.ssh.hostkey,
                "key_file": self.ssh.key_file,
                "plink_path": self.ssh.plink_path,
                "ssh_path": self.ssh.ssh_path,
                "remote_dir": self.ssh.remote_dir,
                "remote_python": self.ssh.remote_python,
                "connect_timeout": self.ssh.connect_timeout,
            },
        }
        CONFIG_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def masked(self) -> str:
        token = self.device_token
        if not token:
            return "未设置"
        if len(token) <= 6:
            return "***"
        return token[:2] + "***" + token[-2:]

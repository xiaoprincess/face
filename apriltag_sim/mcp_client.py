"""Minimal MCP stdio client that drives the Raspberry Pi through SSH.

The desktop program opens one SSH session, starts parts_mcp.py on the Pi and
speaks JSON-RPC with it:

    initialize -> notifications/initialized -> tools/list -> tools/call

Only the tool test_face_and_point is used here; the browser/short-voice borrow
flow is untouched.  Progress notifications are forwarded to the UI.
"""

from __future__ import annotations

import json
import re
import shlex
import ssl
import subprocess
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "apriltag-sim"
CLIENT_VERSION = "1.0"

INIT_TIMEOUT = 30.0
CALL_EXTRA_TIMEOUT = 90.0


class MCPConnectionError(RuntimeError):
    """SSH or MCP handshake failed."""


@dataclass
class MCPOutcome:
    ok: bool
    data: dict = field(default_factory=dict)
    progress: list = field(default_factory=list)
    stderr: str = ""
    duration: float = 0.0
    error: str = ""
    cancelled: bool = False
    call_started: bool = False
    worker_finished: bool = False
    stop_confirmed: bool | None = None


def _parse_json_line(line):
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        pass
    # Plink or a login banner may prefix the first JSON message with text.
    index = line.find("{")
    if index > 0:
        try:
            return json.loads(line[index:])
        except ValueError:
            return None
    return None


def _tool_payload(result):
    payload = result.get("structuredContent")
    texts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if not isinstance(text, str):
                continue
            texts.append(text)
            try:
                decoded = json.loads(text)
            except ValueError:
                continue
            if not isinstance(payload, dict) and isinstance(decoded, dict):
                payload = decoded
    payload = dict(payload) if isinstance(payload, dict) else {"raw": result}
    if result.get("isError"):
        # The MCP error flag takes precedence over contradictory tool data.
        payload["success"] = False
        payload["message"] = "\n".join(texts) or payload.get("message") or "MCP 工具执行失败"
    return payload


class MCPTestClient:
    def __init__(
        self,
        config,
        on_progress=None,
        on_log=None,
        cancel_event=None,
        password=None,
        popen_factory=None,
        on_hostkey_confirm=None,
    ):
        self.config = config
        self.on_progress = on_progress
        self.on_log = on_log or (lambda message: None)
        self.cancel_event = cancel_event or threading.Event()
        self.password = password or ""
        self.popen_factory = popen_factory or subprocess.Popen
        self.on_hostkey_confirm = on_hostkey_confirm
        self.process = None
        self._responses = {}
        self._lock = threading.Lock()
        self._notified = threading.Event()
        self._stderr_lines = []
        self._threads = []
        self._progress = []
        self._closed = False
        self.request_id = None
        self._call_started = False

    # -- process and protocol ------------------------------------------------
    def build_command(self):
        ssh = self.config.ssh
        if ssh.tool == "plink":
            plink = Path(ssh.plink_path)
            if not plink.is_file():
                raise MCPConnectionError(f"找不到 Plink：{plink}")
            command = [str(plink), "-T", "-batch", "-P", str(ssh.port)]
            if ssh.hostkey:
                command.extend(["-hostkey", ssh.hostkey])
            if ssh.key_file:
                command.extend(["-i", ssh.key_file])
            if self.password:
                command.extend(["-pw", self.password])
            command.append(f"{ssh.user}@{ssh.host}")
        elif ssh.tool == "ssh":
            command = [
                ssh.ssh_path or "ssh",
                "-T",
                "-p",
                str(ssh.port),
                "-o",
                "BatchMode=yes",
                "-o",
                f"ConnectTimeout={ssh.connect_timeout}",
            ]
            if ssh.key_file:
                command.extend(["-i", ssh.key_file])
            command.append(f"{ssh.user}@{ssh.host}")
        else:
            raise MCPConnectionError(f"不支持的 SSH 工具：{ssh.tool}")

        remote = (
            "cd "
            + shlex.quote(ssh.remote_dir)
            + " && exec "
            + shlex.quote(ssh.remote_python_path)
            + " parts_mcp.py"
        )
        command.append(remote)
        return command

    def _spawn(self):
        self._prepare_hostkey()
        self._check_cancelled()
        command = self.build_command()
        self._safe_log("执行：" + " ".join(command[:-1]) + " <远程 MCP 命令>")
        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        try:
            self.process = self.popen_factory(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creationflags,
            )
        except OSError as error:
            raise MCPConnectionError(f"无法启动 SSH 进程：{error}") from error

        self._threads = [
            threading.Thread(target=self._read_stdout, name="mcp-stdout", daemon=True),
            threading.Thread(target=self._read_stderr, name="mcp-stderr", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _prepare_hostkey(self):
        """Discover an unconfigured Plink key before sending credentials/MCP data."""
        ssh = self.config.ssh
        if ssh.tool != "plink" or ssh.hostkey or self.on_hostkey_confirm is None:
            return
        if not Path(ssh.plink_path).is_file():
            raise MCPConnectionError(f"找不到 Plink：{ssh.plink_path}")
        self._safe_log("正在读取 SSH 主机指纹…")
        # No password, private key or agent: this probe only discovers the key.
        command = [ssh.plink_path, "-v", "-T", "-batch", "-noagent",
                   "-P", str(ssh.port), f"{ssh.user}@{ssh.host}", "exit"]
        try:
            probe = subprocess.run(
                command, input="", capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=max(3, min(60, ssh.connect_timeout)),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MCPConnectionError(f"无法读取 SSH 主机指纹：{error}") from error
        self._check_cancelled()
        output = probe.stderr or ""
        match = re.search(
            r"(?m)^\s*((?:ssh-|ecdsa-|rsa-sha2-)[\w@.+-]+\s+\d+\s+SHA256:[A-Za-z0-9+/=]+)\s*$",
            output,
        )
        if not match:
            raise MCPConnectionError(
                "无法读取 SSH 主机指纹：" + (output.strip()[-1200:] or "Plink 未返回指纹")
            )
        fingerprint = match.group(1).strip()
        if not self.on_hostkey_confirm(fingerprint):
            raise MCPConnectionError("未接受 SSH 主机指纹，连接已终止")
        self._check_cancelled()
        ssh.hostkey = fingerprint
        self._safe_log("已确认 SSH 主机指纹：" + fingerprint)

    def _safe_log(self, text):
        try:
            self.on_log(text)
        except Exception:
            pass

    def _read_stdout(self):
        stream = self.process.stdout
        if stream is None:
            return
        for line in stream:
            message = _parse_json_line(line)
            if message is None:
                if line.strip():
                    self._safe_log("MCP 非 JSON 输出：" + line.strip()[:300])
                continue
            self._handle(message)

    def _read_stderr(self):
        stream = self.process.stderr
        if stream is None:
            return
        for line in stream:
            text = line.rstrip()
            if not text:
                continue
            self._stderr_lines.append(text)
            del self._stderr_lines[:-80]
            self._safe_log("SSH: " + text[:300])

    def _handle(self, message):
        if not isinstance(message, dict):
            return
        if "id" in message and ("result" in message or "error" in message):
            with self._lock:
                self._responses[message["id"]] = message
            self._notified.set()
            return
        method = message.get("method")
        if method == "notifications/progress":
            params = message.get("params") or {}
            entry = {
                "progress": params.get("progress"),
                "total": params.get("total"),
                "message": params.get("message") or "",
            }
            self._progress.append(entry)
            try:
                if self.on_progress is not None:
                    self.on_progress(entry)
            except Exception:
                pass
        elif method:
            self._safe_log("MCP 通知：" + str(method))

    def _send(self, payload):
        if self.process is None or self.process.stdin is None:
            raise MCPConnectionError("SSH 会话已关闭")
        text = json.dumps(payload, ensure_ascii=False) + "\n"
        try:
            self.process.stdin.write(text)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise MCPConnectionError(f"写入 MCP 请求失败：{error}") from error

    def _request(self, message_id, method, params, deadline):
        payload = {"jsonrpc": "2.0", "id": message_id, "method": method}
        if params is not None:
            payload["params"] = params
        self._check_cancelled()
        if method == "tools/call":
            # A failed/partial write can still have reached the remote worker.
            self._call_started = True
        self._send(payload)
        while time.monotonic() < deadline:
            if self.cancel_event.is_set():
                raise MCPConnectionError("已取消")
            with self._lock:
                response = self._responses.pop(message_id, None)
            if response is not None:
                if "error" in response:
                    raise MCPConnectionError(f"MCP 协议错误：{response['error']}")
                result = response.get("result")
                if not isinstance(result, dict):
                    raise MCPConnectionError("MCP 返回的数据格式无效")
                return result
            if self.process.poll() is not None and message_id not in self._responses:
                raise MCPConnectionError(
                    "SSH/MCP 进程提前退出，退出码："
                    + str(self.process.returncode)
                    + self._stderr_tail()
                )
            time.sleep(0.05)
        raise MCPConnectionError("等待 MCP 响应超时")

    def _stderr_tail(self, limit=3):
        tail = [line for line in self._stderr_lines[-limit:] if line.strip()]
        return ("；" + " / ".join(tail)) if tail else ""

    # -- public API ----------------------------------------------------------
    def _check_cancelled(self):
        if self.cancel_event.is_set():
            raise MCPConnectionError("已取消")

    def _ssl_context(self):
        base = str(getattr(self.config, "web_base_url", "")).rstrip("/")
        if not base.lower().startswith("https"):
            return None
        ca_file = str(getattr(self.config, "ca_file", "") or "")
        try:
            if ca_file:
                return ssl.create_default_context(cafile=ca_file)
            if not getattr(self.config, "verify_tls", True):
                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                return context
            return ssl.create_default_context()
        except Exception:
            return None

    def _job_url(self):
        base = str(getattr(self.config, "web_base_url", "")).rstrip("/")
        if not base or not self.request_id:
            return None
        return f"{base}/parts/api/device/tag-test/{self.request_id}"

    def _server_job_state(self):
        url = self._job_url()
        token = str(getattr(self.config, "device_token", "")).strip()
        if not url or not token:
            return None
        request = urllib.request.Request(
            url, headers={"Accept": "application/json", "X-Device-Token": token}
        )
        try:
            with urllib.request.urlopen(
                request, timeout=5, context=self._ssl_context()
            ) as response:
                data = json.loads(response.read() or b"{}")
                return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _wait_server_terminal(self, timeout=6.0):
        """Wait for owner cleanup; a terminal state alone is insufficient."""

        if not self._job_url() or not str(getattr(self.config, "device_token", "")).strip():
            return None
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            snapshot = self._server_job_state()
            if snapshot is not None:
                state = snapshot
            if state is not None and state.get("worker_finished") is True:
                return state
            time.sleep(0.3)
        return state

    def _cancel_server_job(self):
        """Stop the website job directly, independent of MCP cancellation."""

        url = self._job_url()
        token = str(getattr(self.config, "device_token", "")).strip()
        if not url or not token:
            return
        request = urllib.request.Request(
            url + "/cancel",
            data=b"{}",
            headers={
                "Content-Type": "application/json",
                "X-Device-Token": token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=5, context=self._ssl_context()
            ) as response:
                response.read(200)
            self._safe_log("已直接请求网站取消任务")
        except Exception as error:
            self._safe_log(f"直接取消网站任务失败：{error}")

    def run(self, tag_id, timeout_seconds):
        started = time.monotonic()
        deadline = started + float(timeout_seconds) + CALL_EXTRA_TIMEOUT
        self.request_id = "sim-" + uuid.uuid4().hex[:12]
        progress_token = "tagtest-" + uuid.uuid4().hex[:12]
        try:
            self._check_cancelled()
            self._spawn()
            init = self._request(
                1,
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                },
                time.monotonic() + INIT_TIMEOUT,
            )
            server_info = (init.get("serverInfo") or {}).get("name", "unknown")
            self._safe_log(f"握手成功：{server_info}")
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

            listing = self._request(2, "tools/list", {}, time.monotonic() + INIT_TIMEOUT)
            names = [tool.get("name") for tool in listing.get("tools") or []]
            if "test_face_and_point" not in names:
                raise MCPConnectionError(
                    "树莓派 MCP 未提供 test_face_and_point，请先部署更新后的 parts_mcp.py"
                )
            self._safe_log("工具列表已就绪，开始调用 test_face_and_point")

            call = self._request(
                3,
                "tools/call",
                {
                    "name": "test_face_and_point",
                    "arguments": {
                        "tag_id": int(tag_id),
                        "request_id": self.request_id,
                        "timeout_seconds": float(timeout_seconds),
                    },
                    "_meta": {"progressToken": progress_token},
                },
                deadline,
            )
            self._check_cancelled()
            data = _tool_payload(call)
            return MCPOutcome(
                ok=bool(data.get("success")),
                data=data,
                progress=list(self._progress),
                stderr="\n".join(self._stderr_lines[-20:]),
                duration=round(time.monotonic() - started, 2),
                error="" if data.get("success") else str(data.get("message") or ""),
                call_started=self._call_started,
            )
        except MCPConnectionError as error:
            cancelled = self.cancel_event.is_set()
            state = None
            if self._call_started and (cancelled or "超时" in str(error)):
                try:
                    self._send({
                        "jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": 3, "reason": str(error)},
                    })
                except Exception:
                    pass
                # Do not rely on the MCP notification alone: the websocket may
                # be gone, but the website endpoint can still stop the job.
                self._cancel_server_job()
                state = self._wait_server_terminal(6.0)
                if state is not None:
                    self._safe_log(
                        "网站任务状态：" + str(state.get("state")) + " / "
                        + str(state.get("stage"))
                    )
                if state is None or state.get("worker_finished") is not True:
                    detail = (
                        "停止命令已发送，任务清理尚未确认"
                        if state and state.get("stop_confirmed") is True
                        else "任务清理及停机确认未知"
                    )
                    error = MCPConnectionError(str(error) + "；" + detail)
            return MCPOutcome(
                ok=False,
                progress=list(self._progress),
                stderr="\n".join(self._stderr_lines[-20:]),
                duration=round(time.monotonic() - started, 2),
                error=str(error),
                cancelled=cancelled,
                data={key: state.get(key) for key in (
                    'face_id', 'user_id', 'username', 'face_score', 'stages',
                    'state', 'stage', 'tag_id', 'request_id',
                )} if state else {},
                call_started=self._call_started,
                worker_finished=bool(state and state.get("worker_finished") is True),
                stop_confirmed=(state.get("stop_confirmed") if state else None),
            )
        finally:
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        if process.poll() is None:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        for thread in self._threads:
            thread.join(timeout=2)

    def cancel(self):
        """Signal only; run() owns all cancellation I/O and confirmation."""

        self.cancel_event.set()

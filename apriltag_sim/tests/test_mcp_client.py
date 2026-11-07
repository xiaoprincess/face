"""Tests for the SSH MCP client using an in-process fake MCP server.

Spawning real SSH processes is deliberately avoided: the fake process speaks
the same JSON-RPC lines, which is enough to verify handshake, tool check,
progress forwarding and cancellation without any hardware or network.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from config import SimConfig, SshConfig  # noqa: E402
from mcp_client import MCPConnectionError, MCPOutcome, MCPTestClient, _parse_json_line, _tool_payload  # noqa: E402
from ui import ProgressWindow, SimApp, _cancel_status  # noqa: E402


class LineStream:
    def __init__(self):
        self._queue = queue.Queue()

    def put(self, text):
        self._queue.put(text)

    def close(self):
        self._queue.put(None)

    def __iter__(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            yield item


class FakeProcess:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.stdin = self
        self.stdout = LineStream()
        self.stderr = LineStream()
        self.returncode = None
        self.messages = []
        self._terminated = threading.Event()
        self._inbox = queue.Queue()
        self._worker = threading.Thread(target=self._loop, daemon=True)
        self._worker.start()

    def write(self, text):
        self.messages.append(json.loads(text))
        self._inbox.put(text)

    def flush(self):
        pass

    def close(self):
        self._inbox.put(None)

    def poll(self):
        return self.returncode

    def terminate(self):
        self._terminated.set()
        if self.returncode is None:
            self.returncode = -15
        self.stdout.close()
        self.stderr.close()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode

    def _send(self, payload):
        if self.returncode is None:
            self.stdout.put(json.dumps(payload, ensure_ascii=False) + "\n")

    def _loop(self):
        while True:
            item = self._inbox.get()
            if item is None:
                return
            try:
                message = json.loads(item)
            except ValueError:
                continue
            self._handle(message)

    def _handle(self, message):
        message_id = message.get("id")
        method = message.get("method")
        if method == "initialize":
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "fake-parts", "version": "1"},
                    },
                }
            )
        elif method == "notifications/initialized":
            return
        elif method == "tools/list":
            tools = [{"name": "search_part"}]
            if self.mode != "missing":
                tools.append({"name": "test_face_and_point"})
            self._send({"jsonrpc": "2.0", "id": message_id, "result": {"tools": tools}})
        elif method == "tools/call":
            token = ((message.get("params") or {}).get("_meta") or {}).get("progressToken")
            for index, label in enumerate(["转向人脸位置", "人脸识别", "对准标签"]):
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {
                            "progressToken": token,
                            "progress": index + 1,
                            "total": 3,
                            "message": label,
                        },
                    }
                )
                time.sleep(0.02)
            if self.mode == "slow":
                self._terminated.wait(30)
            if self.mode.startswith("error_"):
                result = {"isError": True, "content": [{"type": "text", "text": "模拟工具失败"}]}
                if self.mode == "error_structured":
                    result["structuredContent"] = {"success": True, "status": "aligned"}
                if self.mode == "error_json":
                    result["content"][0]["text"] = json.dumps({"success": True, "message": "矛盾成功"})
                self._send({"jsonrpc": "2.0", "id": message_id, "result": result})
                return
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "result": {
                        "structuredContent": {
                            "success": True,
                            "status": "aligned",
                            "tag_id": 11,
                            "message": "已对准标签 11",
                        }
                    },
                }
            )


def make_factory(mode):
    def factory(command, **kwargs):
        return FakeProcess(mode)

    return factory


class MCPClientTests(unittest.TestCase):
    def setUp(self):
        # Fail closed: every test must use fake transport, regardless of config.
        self.urlopen = self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("禁止网络")))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("禁止外部进程")))

    def make_config(self):
        return SimConfig(
            ssh=SshConfig(tool="ssh", ssh_path="ssh", host="127.0.0.1", user="tester"),
            tag_test_timeout=20,
        )

    def test_first_plink_connection_confirms_and_pins_discovered_key(self):
        config = SimConfig(ssh=SshConfig(hostkey=""))
        fingerprint = "ssh-ed25519 255 SHA256:AbCdEf0123456789+/abc"
        confirm = Mock(return_value=True)
        client = MCPTestClient(config, password="secret", on_hostkey_confirm=confirm)
        with patch("mcp_client.Path.is_file", return_value=True), patch(
            "mcp_client.subprocess.run", return_value=Mock(
                stderr=f"The host key is not cached for this server:\n{fingerprint}\nFATAL ERROR: Cannot confirm a host key in batch mode",
                returncode=1,
            ),
        ) as probe:
            client._prepare_hostkey()
            command = client.build_command()
        confirm.assert_called_once_with(fingerprint)
        self.assertEqual(config.ssh.hostkey, fingerprint)
        self.assertEqual(command[command.index("-hostkey") + 1], fingerprint)
        self.assertNotIn("secret", probe.call_args.args[0])
        self.assertNotIn("-pw", probe.call_args.args[0])

    def test_hostkey_rejection_does_not_start_mcp_or_save_key(self):
        config = SimConfig(ssh=SshConfig(hostkey=""))
        factory = Mock()
        client = MCPTestClient(config, on_hostkey_confirm=lambda _: False, popen_factory=factory)
        with patch("mcp_client.Path.is_file", return_value=True), patch(
            "mcp_client.subprocess.run", return_value=Mock(stderr="ssh-ed25519 255 SHA256:AbCd\n"),
        ):
            outcome = client.run(11, 20)
        self.assertFalse(outcome.ok)
        self.assertIn("未接受", outcome.error)
        self.assertEqual(config.ssh.hostkey, "")
        factory.assert_not_called()

    def test_pinned_key_does_not_require_putty_cache_or_another_prompt(self):
        client = MCPTestClient(SimConfig(ssh=SshConfig(hostkey="SHA256:AbCd")),
                               on_hostkey_confirm=Mock())
        with patch("mcp_client.subprocess.run") as probe:
            client._prepare_hostkey()
        probe.assert_not_called()
        client.on_hostkey_confirm.assert_not_called()

    def test_probe_connection_failure_is_not_presented_as_a_fingerprint(self):
        confirm = Mock()
        client = MCPTestClient(SimConfig(), on_hostkey_confirm=confirm)
        with patch("mcp_client.Path.is_file", return_value=True), patch(
            "mcp_client.subprocess.run", return_value=Mock(stderr="Network error: Connection refused"),
        ):
            with self.assertRaisesRegex(MCPConnectionError, "Connection refused"):
                client._prepare_hostkey()
        confirm.assert_not_called()

    def test_ui_hostkey_confirmation_saves_only_after_acceptance(self):
        for accepted in (False, True):
            with self.subTest(accepted=accepted):
                app = SimApp.__new__(SimApp)
                app.config = SimConfig()
                app.root = Mock()
                app._closing = False
                app._cancel_event = threading.Event()
                payload = {"fingerprint": "ssh-ed25519 255 SHA256:AbCd",
                           "ready": threading.Event(), "accepted": False}
                with patch("ui.messagebox.askyesno", return_value=accepted), \
                        patch.object(app.config, "save") as save:
                    app._handle_event("hostkey_confirm", payload)
                self.assertTrue(payload["ready"].is_set())
                self.assertEqual(payload["accepted"], accepted)
                self.assertEqual(save.call_count, int(accepted))
                self.assertEqual(app.config.ssh.hostkey, payload["fingerprint"] if accepted else "")

    def test_successful_call_collects_stages(self):
        progress = []
        client = MCPTestClient(
            self.make_config(),
            on_progress=progress.append,
            popen_factory=make_factory("ok"),
        )
        outcome = client.run(11, 20)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.data.get("status"), "aligned")
        self.assertGreaterEqual(len(outcome.progress), 3)
        self.assertEqual(outcome.progress[-1]["message"], "对准标签")

    def test_missing_tool_is_reported(self):
        client = MCPTestClient(
            self.make_config(),
            popen_factory=make_factory("missing"),
        )
        outcome = client.run(11, 20)
        self.assertFalse(outcome.ok)
        self.assertIn("test_face_and_point", outcome.error)

    def test_cancel_stops_the_call(self):
        cancel = threading.Event()
        client = MCPTestClient(
            self.make_config(),
            cancel_event=cancel,
            on_progress=lambda entry: cancel.set(),
            popen_factory=make_factory("slow"),
        )
        started = time.monotonic()
        outcome = client.run(11, 20)
        self.assertTrue(outcome.cancelled, outcome.error)
        self.assertTrue(outcome.call_started)
        self.assertFalse(outcome.worker_finished)
        self.assertIsNone(outcome.stop_confirmed)
        self.assertLess(time.monotonic() - started, 10)
        self.urlopen.assert_not_called()

    def test_cancel_keeps_recognized_face_identity(self):
        client = MCPTestClient(self.make_config(), popen_factory=make_factory('slow'))
        client.on_progress = lambda entry: client.cancel()
        client._cancel_server_job = Mock()
        client._wait_server_terminal = Mock(return_value={
            'terminal': True, 'worker_finished': True, 'state': 'cancelled',
            'face_id': '007', 'user_id': 7, 'username': 'alice', 'face_score': 89,
        })
        outcome = client.run(14, 20)
        self.assertTrue(outcome.cancelled)
        self.assertEqual(outcome.data['face_id'], '007')
        self.assertEqual(outcome.data['username'], 'alice')

    def test_is_error_overrides_plain_text_and_contradictory_success(self):
        for mode in ("error_text", "error_structured", "error_json"):
            with self.subTest(mode=mode):
                outcome = MCPTestClient(self.make_config(), popen_factory=make_factory(mode)).run(11, 20)
                self.assertFalse(outcome.ok)
                self.assertFalse(outcome.data["success"])
                self.assertTrue(outcome.error)
        result = {"isError": True, "structuredContent": {"success": True}}
        self.assertFalse(_tool_payload(result)["success"])
        self.assertTrue(result["structuredContent"]["success"], "不能改写原始响应")

    def test_cancel_only_signals_without_io(self):
        client = MCPTestClient(self.make_config())
        client._send = Mock(side_effect=AssertionError("取消不能同步写管道"))
        client._cancel_server_job = Mock(side_effect=AssertionError("取消不能同步联网"))
        client.cancel()
        self.assertTrue(client.cancel_event.is_set())
        client._send.assert_not_called()
        client._cancel_server_job.assert_not_called()

    def test_cancel_before_run_does_not_spawn_or_dispatch(self):
        factory = Mock(side_effect=AssertionError("提前取消不能启动 SSH"))
        client = MCPTestClient(self.make_config(), popen_factory=factory)
        client.cancel()
        outcome = client.run(11, 20)
        self.assertTrue(outcome.cancelled)
        self.assertFalse(outcome.call_started)
        factory.assert_not_called()
        self.urlopen.assert_not_called()

    def test_cancel_between_tool_listing_and_dispatch_does_not_call(self):
        process = FakeProcess()
        client = MCPTestClient(self.make_config(), popen_factory=lambda *a, **kw: process)
        client.on_log = lambda text: client.cancel() if "工具列表已就绪" in text else None
        outcome = client.run(11, 20)
        self.assertTrue(outcome.cancelled)
        self.assertFalse(outcome.call_started)
        self.assertNotIn("tools/call", [message.get("method") for message in process.messages])
        self.urlopen.assert_not_called()

    def test_cancel_confirmation_preserves_independent_snapshot_fields(self):
        snapshots = [None,
                     {"terminal": True, "worker_finished": False, "stop_confirmed": None},
                     {"terminal": True, "worker_finished": False, "stop_confirmed": False},
                     {"terminal": True, "worker_finished": False, "stop_confirmed": True},
                     {"terminal": True, "worker_finished": True, "stop_confirmed": False},
                     {"terminal": True, "worker_finished": True, "stop_confirmed": True}]
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                client = MCPTestClient(self.make_config(), popen_factory=make_factory("slow"))
                client.on_progress = lambda entry: client.cancel()
                client._cancel_server_job = Mock()
                client._wait_server_terminal = Mock(return_value=snapshot)
                outcome = client.run(11, 20)
                self.assertTrue(outcome.cancelled)
                self.assertEqual(outcome.worker_finished, bool(snapshot and snapshot["worker_finished"]))
                self.assertIs(outcome.stop_confirmed, snapshot["stop_confirmed"] if snapshot else None)
                client._cancel_server_job.assert_called_once_with()
                client._wait_server_terminal.assert_called_once_with(6.0)
                if not outcome.worker_finished:
                    self.assertIn("尚未确认" if outcome.stop_confirmed else "确认未知", outcome.error)

    def test_timeout_terminal_does_not_end_cleanup_polling(self):
        client = MCPTestClient(self.make_config())
        client.config.device_token = "fake-token"
        client.request_id = "fake-job"
        pending = {"terminal": True, "state": "timeout", "worker_finished": False, "stop_confirmed": True}
        finished = dict(pending, worker_finished=True)
        client._server_job_state = Mock(side_effect=[pending, finished])
        with patch("mcp_client.time.monotonic", side_effect=[0, 0, 1]), patch("mcp_client.time.sleep"):
            self.assertEqual(client._wait_server_terminal(), finished)
        self.assertEqual(client._server_job_state.call_count, 2)
        client._server_job_state = Mock(return_value=pending)
        with patch("mcp_client.time.monotonic", side_effect=[0, 0, 7]), patch("mcp_client.time.sleep"):
            self.assertEqual(client._wait_server_terminal(), pending)

    def test_call_timeout_requests_cancel_and_reports_unknown(self):
        client = MCPTestClient(self.make_config(), popen_factory=make_factory("ok"))
        original = client._request

        def request(message_id, method, params, deadline):
            if method == "tools/call":
                client._call_started = True
                raise MCPConnectionError("等待 MCP 响应超时")
            return original(message_id, method, params, deadline)

        client._request = request
        client._cancel_server_job = Mock()
        client._wait_server_terminal = Mock(return_value={"terminal": True, "worker_finished": False})
        outcome = client.run(11, 20)
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.cancelled)
        self.assertFalse(outcome.worker_finished)
        self.assertIn("确认未知", outcome.error)
        client._cancel_server_job.assert_called_once_with()

    def test_parse_json_line_tolerates_banner_prefix(self):
        self.assertEqual(_parse_json_line('Access granted. {"jsonrpc":"2.0","id":1}')["id"], 1)
        self.assertIsNone(_parse_json_line("not json"))

    def test_command_contains_remote_entry(self):
        config = self.make_config()
        client = MCPTestClient(config)
        command = client.build_command()
        self.assertIn("tester@127.0.0.1", command)
        self.assertIn("parts_mcp.py", command[-1])
        self.assertIn("/.venv/bin/python", command[-1])


class CancellationUITests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("禁止网络")))
        self.enterContext(patch("subprocess.Popen", side_effect=AssertionError("禁止外部进程")))

    def make_app(self):
        # Exercise real controller methods without creating any Tk windows.
        app = SimApp.__new__(SimApp)
        app.root = Mock()
        app.config = SimConfig(ssh=SshConfig(tool="ssh"))
        app.busy = False
        app.loading = False
        app._closing = False
        app._test_thread = None
        app._events = queue.Queue()
        app._client = None
        app._cancel_event = None
        app._progress_window = None
        app._password = ""
        app.target_tag = 11
        app.target_cabinet = None
        app.target_source = "手动输入"
        app.snapshot = None
        app.btn_cancel = Mock()
        app._set_status = Mock()
        app._set_data_enabled = Mock()
        return app

    def test_ui_status_respects_worker_and_stop_command_semantics(self):
        cases = [
            ({"call_started": False}, "调用前已取消"),
            ({"call_started": True}, "停机确认未知"),
            ({"call_started": True, "stop_confirmed": False}, "停机确认未知"),
            ({"call_started": True, "stop_confirmed": True}, "停止命令已发送"),
            ({"call_started": True, "worker_finished": True, "stop_confirmed": False}, "任务已结束"),
            ({"call_started": True, "worker_finished": True, "stop_confirmed": True}, "任务已结束"),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                outcome = MCPOutcome(ok=False, cancelled=True, **fields)
                text = _cancel_status(outcome)
                self.assertIn(expected, text)
                self.assertNotIn("硬件应已停止", text)
                self.assertNotIn("物理已停", text)
                app = self.make_app()
                app._on_done({"outcome": outcome, "elapsed": 1, "tag_id": 11})
                app._set_status.assert_called_with(text)
                window = ProgressWindow.__new__(ProgressWindow)
                window.closed = False
                window.progress = Mock()
                window.cancel_button = Mock()
                window.close_button = Mock()
                window.result_label = Mock()
                window.add_log = Mock()
                window._last_stage = None
                window.finish(outcome, 1)
                window.result_label.configure.assert_called_with(text=text)

    def test_cancel_and_window_close_wait_for_background_confirmation(self):
        app = self.make_app()
        dispatched = threading.Event()
        confirming = threading.Event()
        release = threading.Event()
        client = MCPTestClient(app.config, popen_factory=make_factory("slow"),
                               on_progress=lambda entry: dispatched.set())

        def confirm_cancel():
            confirming.set()
            if not release.wait(3):
                raise AssertionError("测试未释放模拟取消确认")

        client._cancel_server_job = Mock(side_effect=confirm_cancel)
        client._wait_server_terminal = Mock(return_value={"worker_finished": True, "stop_confirmed": True})
        with patch("ui.MCPTestClient", return_value=client), patch("ui.ProgressWindow"), \
                patch("ui.messagebox.askyesno", return_value=True) as ask:
            try:
                app.run_test()
                self.assertFalse(app._test_thread.daemon)
                self.assertTrue(dispatched.wait(2), "模拟工具未调用")
                started = time.monotonic()
                app.cancel_test()
                self.assertLess(time.monotonic() - started, 0.2)
                self.assertTrue(confirming.wait(2), "后台未进入取消确认")
                app.on_close()
                app.root.destroy.assert_not_called()
                app.root.after.assert_called_with(100, app._wait_for_close)
                count = ask.call_count
                app.on_close()
                self.assertEqual(ask.call_count, count, "重复关闭不应重复询问")
                app.run_test()
                self.assertEqual(ask.call_count, count, "关闭等待中不能开始新测试")
                release.set()
                app._test_thread.join(2)
                self.assertFalse(app._test_thread.is_alive())
                # Even after thread exit, consume the completion result before destroying Tk.
                app._wait_for_close()
                app.root.destroy.assert_not_called()
                event, payload = app._events.get(timeout=1)
                self.assertEqual(event, "done")
                app._handle_event(event, payload)
                self.assertTrue(payload["outcome"].worker_finished)
                app._wait_for_close()
                app.root.destroy.assert_called_once_with()
                client._cancel_server_job.assert_called_once_with()
            finally:
                release.set()
                if app._test_thread is not None:
                    app._test_thread.join(3)

    def test_close_waits_for_thread_exit_after_done_event(self):
        app = self.make_app()
        app._test_thread = Mock()
        app._test_thread.is_alive.side_effect = [True, False]
        app._closing = True
        app._wait_for_close()
        app.root.destroy.assert_not_called()
        app.root.after.assert_called_once_with(100, app._wait_for_close)
        app._wait_for_close()
        app.root.destroy.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

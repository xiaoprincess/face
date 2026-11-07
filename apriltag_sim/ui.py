"""Tkinter desktop client for the AprilTag full-flow simulation.

Start flow: inventory window first, then pick a tag (or type one), confirm the
target and only then drive the Raspberry Pi through MCP.  Browsing never touches
the camera, the motors or the database.
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk

from config import SimConfig
from inventory import fetch_online, load_local, scan_local_candidates
from mcp_client import MCPTestClient
from tags import TAG_FAMILY, TAG_MAX_ID, build_entries, find_by_tag, summary

def _box_number(notes):
    text = str(notes or "").strip()
    if not text:
        return ""
    if text.startswith("箱号 "):
        return text[3:].split("（", 1)[0].strip()
    return text


def _now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _cancel_status(outcome):
    if outcome.worker_finished:
        return "任务已结束（服务端已确认）"
    if not outcome.call_started:
        return "调用前已取消，未启动测试任务"
    if outcome.stop_confirmed is True:
        return "已取消；停止命令已发送，任务清理尚未确认"
    return "已取消；任务清理及停机确认未知"


class SimApp:
    def __init__(self, root, config):
        self.root = root
        self.config = config
        self.snapshot = None
        self.entries = []
        self.target_tag = None
        self.target_cabinet = None
        self.target_source = ""
        self.busy = False
        self.loading = False
        self._events = queue.Queue()
        self._password = ""
        self._client = None
        self._cancel_event = None
        self._progress_window = None
        self._test_thread = None
        self._closing = False

        root.title("AprilTag 完整流程模拟 · 零件管家")
        root.geometry("1180x760")
        root.minsize(980, 640)
        self._build_style()
        self._build_ui()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        if config.load_error:
            self._set_status(config.load_error)
            root.after(
                400,
                lambda: messagebox.showwarning("配置读取失败", config.load_error),
            )
        root.after(100, self._drain_events)
        root.after(250, self.refresh_online)

    # -- layout -------------------------------------------------------------
    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=24, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Warn.TLabel", foreground="#a15c00")

    def _build_ui(self):
        header = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        header.pack(fill="x")
        ttk.Label(header, text="零件管家 · AprilTag 全流程模拟", style="Title.TLabel").pack(side="left")
        self.source_label = ttk.Label(header, text="数据来源：正在读取…")
        self.source_label.pack(side="left", padx=16)
        ttk.Button(header, text="退出", command=self.on_close).pack(side="right")
        ttk.Button(header, text="修改地址与令牌", command=self.open_settings).pack(side="right", padx=6)
        ttk.Button(header, text="读取本地副本", command=self.choose_local).pack(side="right", padx=6)
        ttk.Button(header, text="刷新线上数据", command=self.refresh_online).pack(side="right", padx=6)

        toolbar = ttk.Frame(self.root, padding=(10, 2, 10, 6))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="搜索：").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.populate_tree())
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search_var, width=42)
        self.search_entry.pack(side="left")
        ttk.Label(toolbar, text="（柜号 / 柜子名称 / 零件 / 箱号 / 备注）").pack(side="left", padx=6)
        self.count_label = ttk.Label(toolbar, text="")
        self.count_label.pack(side="right")

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        left = ttk.Frame(body)
        columns = ("tag", "cabinet", "cabinet_name", "part", "qty", "unit", "box", "notes")
        headings = {
            "tag": "AprilTag ID",
            "cabinet": "柜号",
            "cabinet_name": "柜子名称/类别",
            "part": "零件名称",
            "qty": "库存",
            "unit": "单位",
            "box": "箱号",
            "notes": "备注/状态",
        }
        widths = {"tag": 90, "cabinet": 70, "cabinet_name": 160, "part": 190, "qty": 60, "unit": 50, "box": 90, "notes": 240}
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        for key in columns:
            self.tree.heading(key, text=headings[key])
            self.tree.column(key, width=widths[key], anchor="w", stretch=(key in {"part", "notes", "cabinet_name"}))
        yscroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=yscroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        self.tree.tag_configure("invalid", foreground="#888888")
        self.tree.tag_configure("ambiguous", foreground="#b26a00")
        self.tree.tag_configure("empty", foreground="#666666")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _event: self.choose_selected())
        body.add(left, weight=3)

        right = ttk.LabelFrame(body, text="详情", padding=8)
        self.detail = tk.Text(right, width=42, height=20, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        detail_scroll = ttk.Scrollbar(right, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        body.add(right, weight=2)

        actions = ttk.Frame(self.root, padding=(10, 4, 10, 8))
        actions.pack(fill="x")
        self.btn_pick = ttk.Button(actions, text="选择此标签", command=self.choose_selected)
        self.btn_pick.pack(side="left")
        self.btn_manual = ttk.Button(actions, text="手动输入标签 ID", command=self.manual_tag)
        self.btn_manual.pack(side="left", padx=6)
        self.btn_run = ttk.Button(actions, text="执行测试", command=self.run_test)
        self.btn_run.pack(side="left", padx=6)
        self.btn_cancel = ttk.Button(actions, text="取消测试", command=self.cancel_test, state="disabled")
        self.btn_cancel.pack(side="left", padx=6)
        self.status = ttk.Label(actions, text="准备就绪")
        self.status.pack(side="right")

        self._set_data_enabled(False)

    # -- helpers ------------------------------------------------------------
    def _set_data_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        self.tree.configure(selectmode="browse" if enabled else "none")
        for button in (self.btn_pick, self.btn_manual, self.btn_run):
            button.configure(state=state)
        if enabled:
            self.btn_run.configure(state="normal")
        else:
            self.btn_run.configure(state="disabled")

    def _set_status(self, text, warn=False):
        self.status.configure(text=text)

    def _set_source(self, text):
        self.source_label.configure(text="数据来源：" + text)

    def _write_detail(self, text):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def _drain_events(self):
        try:
            while True:
                event, payload = self._events.get_nowait()
                self._handle_event(event, payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _handle_event(self, event, payload):
        if event == "hostkey_confirm":
            try:
                if not self._closing and not self._cancel_event.is_set():
                    fingerprint = payload["fingerprint"]
                    accepted = messagebox.askyesno(
                        "首次连接：接受 SSH 主机指纹",
                        f"目标：{self.config.ssh.host}:{self.config.ssh.port}\n\n"
                        f"主机指纹：\n{fingerprint}\n\n"
                        "确认这是你的树莓派后，点击“是”接受并保存指纹。\n"
                        "后续连接将使用此指纹核验主机。",
                        parent=self.root,
                    )
                    if accepted and not self._cancel_event.is_set():
                        self.config.ssh.hostkey = fingerprint
                        try:
                            self.config.save()
                        except OSError as error:
                            messagebox.showwarning(
                                "指纹未能保存", f"本次连接可继续，但下次需重新确认。\n{error}",
                                parent=self.root,
                            )
                        payload["accepted"] = True
            finally:
                payload["ready"].set()
        elif event == "online_ok":
            self._apply_snapshot(payload)
        elif event == "online_fail":
            self._set_status("线上库存读取失败，尝试本地副本…")
            self._scan_local_fallback(payload)
        elif event == "candidates":
            self._on_candidates(payload["message"], payload["candidates"])
        elif event == "local_ok":
            self._apply_snapshot(payload)
        elif event == "local_fail":
            self.loading = False
            self._set_data_enabled(False)
            self._set_status("本地副本不可用")
            messagebox.showerror("读取失败", payload)
        elif event == "progress":
            self._on_progress(payload)
        elif event == "log":
            self._on_log(payload)
        elif event == "done":
            self._on_done(payload)

    # -- inventory loading ---------------------------------------------------
    def refresh_online(self):
        if self.loading or self.busy:
            return
        self.loading = True
        self._set_data_enabled(False)
        self._set_status("正在读取树莓派实时库存…")
        self._set_source("读取中 " + self.config.web_base_url)
        threading.Thread(target=self._online_worker, daemon=True).start()

    def _online_worker(self):
        try:
            snapshot = fetch_online(self.config)
            self._events.put(("online_ok", snapshot))
        except Exception as error:  # surface every failure reason to the UI
            self._events.put(("online_fail", str(error)))

    def _scan_local_fallback(self, online_message):
        threading.Thread(
            target=self._scan_worker, args=(online_message,), daemon=True
        ).start()

    def _scan_worker(self, online_message):
        candidates = scan_local_candidates(self.config)
        self._events.put(("candidates", {"message": online_message, "candidates": candidates}))

    def _on_candidates(self, message, candidates):
        valid = [item for item in candidates if item.valid]
        if not valid:
            self.loading = False
            self._set_data_enabled(False)
            self._show_no_data(message, candidates)
            return
        if len(valid) == 1 and not self.config.local_db_path:
            self._load_local_async(valid[0].path)
            return
        choice = LocalChooser(self.root, candidates, self.config.last_local_db_path).show()
        if choice is None:
            self.loading = False
            self._set_status("未选择本地副本")
            return
        self._load_local_async(choice)

    def choose_local(self):
        if self.loading or self.busy:
            return
        candidates = scan_local_candidates(self.config)
        if not [item for item in candidates if item.valid]:
            self._show_no_data("没有可用的线上或本地库存。", candidates)
            return
        choice = LocalChooser(self.root, candidates, self.config.last_local_db_path).show()
        if choice:
            self._load_local_async(choice)

    def _load_local_async(self, path):
        self.loading = True
        self._set_data_enabled(False)
        self._set_status("正在读取本地副本…")
        threading.Thread(target=self._local_worker, args=(path,), daemon=True).start()

    def _local_worker(self, path):
        try:
            snapshot = load_local(path)
            self._events.put(("local_ok", snapshot))
        except Exception as error:
            self._events.put(("local_fail", str(error)))

    def _apply_snapshot(self, snapshot):
        self.snapshot = snapshot
        self.entries = build_entries(snapshot.cabinets)
        # A new data source invalidates the previously chosen cabinet/tag.
        self.target_tag = None
        self.target_cabinet = None
        self.target_source = ""
        self._write_detail("库存已刷新，请重新选择标签（旧选择已清除）。")
        self.loading = False
        self._set_source(snapshot.describe())
        self._set_status(
            f"已加载 {snapshot.cabinet_count} 个柜子，"
            f"{summary(self.entries)['valid']} 个有效标签映射"
        )
        self.populate_tree()
        self._set_data_enabled(True)
        if snapshot.source_kind == "local":
            warn = "本地副本可能过期"
            self.source_label.configure(text="数据来源：" + snapshot.describe() + "（" + warn + "）")

    # -- tree ----------------------------------------------------------------
    def _visible_entries(self):
        text = self.search_var.get().strip().lower()
        if not text:
            return self.entries
        result = []
        for entry in self.entries:
            cabinet = entry.cabinet
            haystack = " ".join(
                [
                    str(cabinet.cabinet_number),
                    cabinet.equipment_name,
                    cabinet.description,
                    entry.status_text(),
                    " ".join(
                        f"{part.component_name} {part.notes} {part.unit}" for part in cabinet.components
                    ),
                ]
            ).lower()
            if text in haystack:
                result.append(entry)
        return result

    def populate_tree(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        entries = self._visible_entries()
        cabinets = 0
        parts = 0
        for index, entry in enumerate(entries):
            cabinets += 1
            cabinet = entry.cabinet
            tags = []
            if not entry.valid:
                tags.append("invalid")
            elif entry.ambiguous:
                tags.append("ambiguous")
            parent = self.tree.insert(
                "",
                "end",
                iid=f"entry-{index}",
                values=(
                    entry.tag_id if entry.valid else "—",
                    cabinet.cabinet_number,
                    cabinet.equipment_name or "未命名",
                    f"共 {len(cabinet.components)} 项",
                    "",
                    "",
                    "",
                    entry.status_text() if not entry.valid or entry.ambiguous else "有效",
                ),
                tags=tuple(tags),
                open=True,
            )
            if not cabinet.components:
                self.tree.insert(
                    parent,
                    "end",
                    iid=f"entry-{index}-empty",
                    values=("", "", "", "（柜内暂无零件）", "", "", "", ""),
                    tags=("empty",),
                )
            for part_index, part in enumerate(cabinet.components):
                parts += 1
                self.tree.insert(
                    parent,
                    "end",
                    iid=f"entry-{index}-part-{part_index}",
                    values=(
                        "",
                        "",
                        "",
                        part.component_name,
                        part.quantity,
                        part.unit,
                        _box_number(part.notes),
                        part.notes,
                    ),
                )
        if self.snapshot is not None:
            self.count_label.configure(
                text=f"显示 {cabinets} 个柜子 / {parts} 条零件，共 {len(self.entries)} 个标签映射"
            )
        if selected:
            for iid in selected:
                if self.tree.exists(iid):
                    self.tree.selection_set(iid)
                    break

    def _entry_for_iid(self, iid):
        if not iid or not iid.startswith("entry-"):
            return None
        try:
            index = int(iid.split("-")[1])
        except (IndexError, ValueError):
            return None
        entries = self._visible_entries()
        if 0 <= index < len(entries):
            return entries[index]
        return None

    def _on_select(self, _event=None):
        selection = self.tree.selection()
        entry = self._entry_for_iid(selection[0]) if selection else None
        if entry is None:
            return
        self._write_detail(self._detail_text(entry, selection[0]))

    def _detail_text(self, entry, iid=""):
        cabinet = entry.cabinet
        lines = [
            f"AprilTag ID：{entry.tag_id if entry.valid else '无'}（{TAG_FAMILY}，范围 0-{TAG_MAX_ID}）",
            f"解析方式：柜号 {cabinet.cabinet_number!r} → int(柜号)"
            if entry.valid
            else f"解析方式：不可用（{entry.reason}）",
            f"柜号：{cabinet.cabinet_number}",
            f"数据库柜子 ID：{cabinet.cabinet_id}",
            f"柜子名称：{cabinet.equipment_name or '未提供'}",
            f"柜子说明：{cabinet.description or '未提供'}",
            f"标签状态：{entry.status_text()}",
            "",
            "数据来源：" + (self.snapshot.source_label if self.snapshot else "未提供"),
            "来源地址：" + (self.snapshot.address if self.snapshot else "未提供"),
            "本次读取时间：" + (self.snapshot.fetched_at.strftime("%Y-%m-%d %H:%M:%S") if self.snapshot else "未提供"),
            "库存记录时间：" + ((self.snapshot.data_time or "未提供") if self.snapshot else "未提供"),
            "文件修改时间："
            + (
                (self.snapshot.file_mtime or "未提供")
                if self.snapshot is not None and self.snapshot.source_kind == "local"
                else "不适用（线上接口）"
            ),
            "",
            f"零件条目（{len(cabinet.components)}）：",
        ]
        if not cabinet.components:
            lines.append("  （柜内暂无零件）")
        for part in cabinet.components:
            lines.append(
                f"  · 零件编号 {part.component_id}｜{part.component_name} × {part.quantity}{part.unit}"
                f"｜箱号 {_box_number(part.notes) or '未提供'}｜备注 {part.notes or '未提供'}"
            )
        lines.append("")
        lines.append("说明：列表来自库存中可推导的标签映射，不代表摄像头已看见这些标签。")
        lines.append("浏览与选择阶段不会驱动电机或摄像头。")
        return "\n".join(lines)

    # -- target selection ----------------------------------------------------
    def choose_selected(self):
        selection = self.tree.selection()
        entry = self._entry_for_iid(selection[0]) if selection else None
        if entry is None:
            messagebox.showinfo("请选择标签", "请先在左侧列表中点击一个柜子。")
            return
        if not entry.valid:
            messagebox.showwarning("无有效标签", "该柜子没有有效标签映射：" + entry.reason)
            return
        if entry.ambiguous:
            messagebox.showwarning(
                "重复映射",
                "标签 " + str(entry.tag_id) + " 同时映射到多个柜号：" + "、".join(entry.duplicates)
                + "。程序不会自动选择柜子，请先修正库存柜号，或使用手动输入并核对。",
            )
            return
        self.target_tag = entry.tag_id
        self.target_cabinet = entry.cabinet
        self.target_source = "库存映射"
        self._set_status(f"已选择标签 {self.target_tag}（柜号 {entry.cabinet.cabinet_number}），可执行测试")
        self._write_detail(self._detail_text(entry) + "\n\n已选择该标签，等待执行。")

    def manual_tag(self):
        if self.busy or self.loading:
            return
        value = simpledialog.askstring(
            "手动输入标签 ID",
            f"请输入 {TAG_FAMILY} 标签 ID（0-{TAG_MAX_ID}）：",
            parent=self.root,
        )
        if value is None:
            return
        text = str(value).strip()
        if not text.isdigit():
            messagebox.showerror("输入无效", "标签 ID 必须是整数。")
            return
        tag_id = int(text)
        if tag_id < 0 or tag_id > TAG_MAX_ID:
            messagebox.showerror("输入无效", f"标签 ID 必须在 0 到 {TAG_MAX_ID} 之间。")
            return
        matches = [entry for entry in find_by_tag(self.entries, tag_id)]
        if len(matches) > 1:
            messagebox.showwarning(
                "重复映射",
                f"标签 {tag_id} 在库存中对应多个柜子："
                + "、".join(m.cabinet.cabinet_number for m in matches)
                + "。执行前请核对，程序不会替您选择柜子。",
            )
            self.target_cabinet = None
            self.target_source = "手动输入（映射歧义）"
        elif len(matches) == 1:
            self.target_cabinet = matches[0].cabinet
            self.target_source = "手动输入（库存匹配）"
        else:
            self.target_cabinet = None
            self.target_source = "手动输入（库存未收录，未核实）"
        self.target_tag = tag_id
        self.btn_run.configure(state="normal")
        cabinet_text = (
            f"柜号 {self.target_cabinet.cabinet_number}"
            if self.target_cabinet is not None
            else "未匹配到柜子（库存未核实的标签测试）"
        )
        self._set_status(f"已手动选择标签 {tag_id}，{cabinet_text}")
        self._write_detail(
            f"手动输入标签 ID：{tag_id}\n目标柜子：{cabinet_text}\n来源：{self.target_source}\n\n"
            "注意：该标签可能不在库存映射中，列表仅供核对。"
        )

    # -- test execution ------------------------------------------------------
    def run_test(self):
        if self.busy or self.loading or self._closing:
            return
        if self.target_tag is None:
            messagebox.showinfo("请先选择标签", "请从列表选择标签，或使用“手动输入标签 ID”。")
            return
        if self.snapshot is None and not self.target_source.startswith("手动输入"):
            messagebox.showwarning("没有库存数据", "当前没有库存数据，无法执行测试。")
            return
        cabinet = self.target_cabinet
        cabinet_text = (
            f"柜号 {cabinet.cabinet_number}（{cabinet.equipment_name}）"
            if cabinet is not None
            else "未匹配到柜子"
        )
        source = (
            self.snapshot.source_label
            if self.snapshot is not None
            else "未核实库存（线上/本地均不可用）"
        )
        detail = (
            f"目标标签：{self.target_tag}（{TAG_FAMILY}）\n"
            f"匹配柜子：{cabinet_text}\n"
            f"数据来源：{source}\n"
            f"SSH 目标：{self.config.ssh.user}@{self.config.ssh.host}:{self.config.ssh.port}\n"
            f"选择方式：{self.target_source}\n\n"
            "将执行：转向人脸 → 拍照 → 人脸识别 → 对准标签。\n"
            "本次测试不会创建借用记录，也不会扣减库存。\n\n"
            "确认开始？"
        )
        if not messagebox.askyesno("确认执行硬件测试", detail, parent=self.root):
            return
        if self.config.ssh.tool == "plink" and not self.config.ssh.key_file and not self._password:
            password = simpledialog.askstring(
                "SSH 密码",
                f"{self.config.ssh.user}@{self.config.ssh.host} 的 SSH 密码（仅本次使用，不保存）：",
                show="*",
                parent=self.root,
            )
            if password is None:
                return
            self._password = password

        self.busy = True
        self._set_data_enabled(False)
        self.btn_cancel.configure(state="normal")
        self._set_status("正在执行测试…")

        self._cancel_event = threading.Event()
        self._client = MCPTestClient(
            self.config,
            on_progress=lambda entry: self._events.put(("progress", entry)),
            on_log=lambda line: self._events.put(("log", line)),
            cancel_event=self._cancel_event,
            password=self._password,
            on_hostkey_confirm=self._confirm_hostkey,
        )
        self._progress_window = ProgressWindow(
            self.root,
            tag_id=self.target_tag,
            cabinet_text=cabinet_text,
            source=source,
            on_cancel=self.cancel_test,
        )
        self._test_thread = threading.Thread(target=self._run_worker, daemon=False)
        self._test_thread.start()

    def _confirm_hostkey(self, fingerprint):
        # Worker waits; only the Tk main thread creates or handles the dialog.
        payload = {"fingerprint": fingerprint, "ready": threading.Event(), "accepted": False}
        self._events.put(("hostkey_confirm", payload))
        while not payload["ready"].wait(0.1):
            if self._cancel_event.is_set():
                return False
        return payload["accepted"]

    def _run_worker(self):
        started = time.monotonic()
        outcome = self._client.run(self.target_tag, self.config.tag_test_timeout)
        self._events.put(
            (
                "done",
                {
                    "outcome": outcome,
                    "elapsed": round(time.monotonic() - started, 2),
                    "tag_id": self.target_tag,
                },
            )
        )

    def cancel_test(self):
        if not self.busy:
            return
        self._set_status("正在取消…")
        if self._cancel_event is not None:
            self._cancel_event.set()
        if self._client is not None:
            self._client.cancel()
        if self._progress_window is not None:
            self._progress_window.mark_cancelling()

    def _on_progress(self, entry):
        if self._progress_window is not None:
            self._progress_window.add_progress(entry)

    def _on_log(self, line):
        if self._progress_window is not None:
            self._progress_window.add_log(line)

    def _on_done(self, payload):
        outcome = payload["outcome"]
        self.busy = False
        self.btn_cancel.configure(state="disabled")
        self._set_data_enabled(True)
        if self._progress_window is not None:
            self._progress_window.finish(outcome, payload["elapsed"])
        if outcome.ok:
            data = outcome.data or {}
            self._set_status(
                f"测试完成：标签 {payload['tag_id']} 已对准（{payload['elapsed']} 秒）"
            )
        elif (outcome.data or {}).get("status") == "tag_not_found":
            self._set_status(
                "人脸已通过，但目标标签不在当前视野：请调整云台朝向或把对应柜子移入画面后重试。"
            )
        elif outcome.cancelled:
            self._set_status(_cancel_status(outcome))
        else:
            error = outcome.error or "未知错误"
            if "unexpectedly closed" in error.lower() or "提前退出" in error:
                hint = (
                    f"SSH 连接被对端关闭：请确认树莓派 IP 是否正确"
                    f"（当前 {self.config.ssh.user}@{self.config.ssh.host}:{self.config.ssh.port}）、"
                    "设备是否开机、22 端口可达，以及 Plink 主机指纹是否变化。"
                )
                self._set_status(hint)
                if self._progress_window is not None:
                    self._progress_window.add_log(hint)
            else:
                self._set_status("测试失败：" + error)
        self._client = None
        self._cancel_event = None

    # -- settings and failure dialogs ---------------------------------------
    def open_settings(self):
        dialog = SettingsDialog(self.root, self.config)
        if dialog.show():
            self.config = dialog.config
            try:
                self.config.save()
            except OSError as error:
                messagebox.showerror("保存失败", str(error))
            self._set_source(self.config.web_base_url)
            self.refresh_online()

    def _show_no_data(self, online_message, candidates):
        lines = [online_message, "", "本地候选："]
        if not candidates:
            lines.append("  未找到任何库存数据库或导出文件。")
        for item in candidates[:8]:
            lines.append("  · " + item.describe())
        lines.append("")
        lines.append("可以重试线上、修改地址与令牌，或使用“未核实库存”的手动标签测试。")
        self._set_status("没有可用库存")
        dialog = NoDataDialog(self.root, "\n".join(lines), self)
        action = dialog.show()
        if action == "retry":
            self.refresh_online()
        elif action == "settings":
            self.open_settings()
        elif action == "manual":
            self.manual_tag()
        elif action == "local":
            self.choose_local()

    def on_close(self):
        if self._closing:
            return
        if self.busy and not messagebox.askyesno("正在测试", "测试仍在进行，确定取消并等待服务端确认后退出吗？"):
            return
        self._closing = True
        if self.busy:
            self.cancel_test()
        self._wait_for_close()

    def _wait_for_close(self):
        # Keep Tk's event queue alive until confirmation and process cleanup
        # finish, including the small gap between the done event and thread exit.
        if self.busy or (self._test_thread is not None and self._test_thread.is_alive()):
            self.root.after(100, self._wait_for_close)
        else:
            self.root.destroy()


class LocalChooser:
    def __init__(self, parent, candidates, preferred=""):
        self.parent = parent
        self.candidates = candidates
        self.preferred = preferred
        self.result = None

    def show(self):
        window = tk.Toplevel(self.parent)
        window.title("选择本地库存来源")
        window.geometry("760x360")
        window.transient(self.parent)
        window.grab_set()
        ttk.Label(
            window,
            text="检测到多个本地库存来源，请选择本次使用哪一个（不会修改文件）：",
            padding=8,
        ).pack(anchor="w")
        columns = ("path", "state", "counts", "time")
        tree = ttk.Treeview(window, columns=columns, show="headings", selectmode="browse")
        for key, text, width in (
            ("path", "文件", 380),
            ("state", "状态", 140),
            ("counts", "柜子/零件", 110),
            ("time", "更新时间", 150),
        ):
            tree.heading(key, text=text)
            tree.column(key, width=width, anchor="w")
        for item in self.candidates:
            tree.insert(
                "",
                "end",
                values=(
                    item.path,
                    "可用" if item.valid else "不可用：" + item.reason,
                    f"{item.cabinet_count}/{item.component_count}" if item.valid else "—",
                    item.data_time or "未知",
                ),
            )
        tree.pack(fill="both", expand=True, padx=8)
        first = None
        for index, item in enumerate(self.candidates):
            if item.valid and (not self.preferred or item.path == self.preferred):
                first = index
                break
        if first is None:
            for index, item in enumerate(self.candidates):
                if item.valid:
                    first = index
                    break
        if first is not None:
            tree.selection_set(tree.get_children()[first])

        buttons = ttk.Frame(window, padding=8)
        buttons.pack(fill="x")

        def use():
            selection = tree.selection()
            if not selection:
                messagebox.showinfo("请选择", "请选择一个可用的库存文件。", parent=window)
                return
            index = tree.index(selection[0])
            item = self.candidates[index]
            if not item.valid:
                messagebox.showerror("不可用", item.reason, parent=window)
                return
            self.result = item.path
            window.destroy()

        ttk.Button(buttons, text="使用所选文件", command=use).pack(side="right")
        ttk.Button(buttons, text="取消", command=window.destroy).pack(side="right", padx=6)
        window.wait_window()
        return self.result


class NoDataDialog:
    def __init__(self, parent, message, app):
        self.parent = parent
        self.message = message
        self.app = app
        self.action = None

    def show(self):
        window = tk.Toplevel(self.parent)
        window.title("无法获取库存")
        window.geometry("680x420")
        window.transient(self.parent)
        window.grab_set()
        text = tk.Text(window, wrap="word", font=("Microsoft YaHei UI", 9))
        text.insert("1.0", self.message)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=10, pady=10)
        buttons = ttk.Frame(window, padding=8)
        buttons.pack(fill="x")

        def choose(action):
            self.action = action
            window.destroy()

        ttk.Button(buttons, text="重试线上", command=lambda: choose("retry")).pack(side="left")
        ttk.Button(buttons, text="修改地址与令牌", command=lambda: choose("settings")).pack(side="left", padx=6)
        ttk.Button(buttons, text="选择本地文件", command=lambda: choose("local")).pack(side="left", padx=6)
        ttk.Button(buttons, text="手动标签测试（未核实库存）", command=lambda: choose("manual")).pack(side="left", padx=6)
        ttk.Button(buttons, text="退出", command=lambda: choose("quit")).pack(side="right")
        window.wait_window()
        if self.action == "quit":
            self.parent.destroy()
        return self.action


class SettingsDialog:
    def __init__(self, parent, config):
        self.parent = parent
        self.config = config
        self.result = False

    def show(self):
        import copy

        config = copy.deepcopy(self.config)
        window = tk.Toplevel(self.parent)
        window.title("地址与连接设置")
        window.geometry("620x520")
        window.transient(self.parent)
        window.grab_set()

        frame = ttk.Frame(window, padding=12)
        frame.pack(fill="both", expand=True)
        fields = {}

        def add_row(row, label, value, show=None, width=46):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=3)
            entry = ttk.Entry(frame, width=width, show=show)
            entry.insert(0, "" if value is None else str(value))
            entry.grid(row=row, column=1, sticky="we", pady=3)
            fields[label] = entry
            return entry

        frame.columnconfigure(1, weight=1)
        add_row(0, "网站地址", config.web_base_url)
        add_row(1, "设备令牌", config.device_token, show="*")
        add_row(2, "SSH 主机", config.ssh.host)
        add_row(3, "SSH 端口", config.ssh.port)
        add_row(4, "SSH 用户", config.ssh.user)
        add_row(5, "远程目录", config.ssh.remote_dir)
        add_row(6, "远程 Python（空=目录/.venv/bin/python）", config.ssh.remote_python)
        add_row(7, "主机指纹 -hostkey（可空）", config.ssh.hostkey)
        add_row(8, "私钥文件（可空）", config.ssh.key_file)
        add_row(9, "Plink 路径", config.ssh.plink_path)
        add_row(10, "定位时限（秒）", config.tag_test_timeout)
        add_row(11, "校验 HTTPS 证书（1=校验，0=忽略）", 1 if config.verify_tls else 0)
        add_row(12, "CA 证书文件（可空）", config.ca_file)
        ttk.Label(
            frame,
            text="提示：优先使用私钥或 Pageant；密码只在本次运行的内存中使用，不写入配置文件。"
            "树莓派使用自签名 HTTPS 时可关闭证书校验，或填写 CA 证书。",
            wraplength=560,
        ).grid(row=13, column=0, columnspan=2, sticky="w", pady=(10, 0))

        buttons = ttk.Frame(window, padding=8)
        buttons.pack(fill="x")

        def save():
            try:
                config.web_base_url = fields["网站地址"].get().strip() or config.web_base_url
                config.device_token = fields["设备令牌"].get().strip()
                config.ssh.host = fields["SSH 主机"].get().strip()
                config.ssh.port = int(fields["SSH 端口"].get().strip() or 22)
                config.ssh.user = fields["SSH 用户"].get().strip()
                config.ssh.remote_dir = fields["远程目录"].get().strip()
                config.ssh.remote_python = fields["远程 Python（空=目录/.venv/bin/python）"].get().strip()
                config.ssh.hostkey = fields["主机指纹 -hostkey（可空）"].get().strip()
                config.ssh.key_file = fields["私钥文件（可空）"].get().strip()
                config.ssh.plink_path = fields["Plink 路径"].get().strip()
                config.tag_test_timeout = max(10, min(180, int(fields["定位时限（秒）"].get().strip() or 45)))
                config.verify_tls = fields["校验 HTTPS 证书（1=校验，0=忽略）"].get().strip() not in {"0", "false", "no", "off", ""}
                config.ca_file = fields["CA 证书文件（可空）"].get().strip()
            except ValueError:
                messagebox.showerror("输入无效", "端口和定位时限必须是整数。", parent=window)
                return
            self.config = config
            self.result = True
            window.destroy()

        ttk.Button(buttons, text="保存", command=save).pack(side="right")
        ttk.Button(buttons, text="取消", command=window.destroy).pack(side="right", padx=6)
        window.wait_window()
        return self.result


class ProgressWindow:
    def __init__(self, parent, tag_id, cabinet_text, source, on_cancel):
        self.on_cancel = on_cancel
        self.window = tk.Toplevel(parent)
        self.window.title(f"测试进行中 · 标签 {tag_id}")
        self.window.geometry("760x560")
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self._close_requested)
        self.closed = False

        header = ttk.Frame(self.window, padding=10)
        header.pack(fill="x")
        ttk.Label(
            header,
            text=f"目标标签 {tag_id} · {cabinet_text}",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w")
        ttk.Label(header, text="数据来源：" + source).pack(anchor="w")

        self.result_label = ttk.Label(header, text="正在执行…", font=("Microsoft YaHei UI", 10))
        self.result_label.pack(anchor="w", pady=(6, 0))

        stage_frame = ttk.LabelFrame(self.window, text="阶段", padding=6)
        stage_frame.pack(fill="both", expand=False, padx=10)
        self.stage_tree = ttk.Treeview(
            stage_frame, columns=("stage", "state", "detail"), show="headings", height=6
        )
        self.stage_tree.heading("stage", text="阶段")
        self.stage_tree.heading("state", text="状态")
        self.stage_tree.heading("detail", text="说明")
        self.stage_tree.column("stage", width=200)
        self.stage_tree.column("state", width=90)
        self.stage_tree.column("detail", width=420)
        self.stage_tree.pack(fill="x")
        self._last_stage = None

        log_frame = ttk.LabelFrame(self.window, text="日志", padding=6)
        log_frame.pack(fill="both", expand=True, padx=10, pady=6)
        self.log = tk.Text(log_frame, height=10, wrap="word", font=("Consolas", 9))
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        buttons = ttk.Frame(self.window, padding=8)
        buttons.pack(fill="x")
        self.cancel_button = ttk.Button(buttons, text="取消测试", command=self._cancel)
        self.cancel_button.pack(side="right")
        self.close_button = ttk.Button(buttons, text="关闭", command=self._close, state="disabled")
        self.close_button.pack(side="right", padx=6)
        self.progress = ttk.Progressbar(buttons, mode="indeterminate", length=220)
        self.progress.pack(side="left")
        self.progress.start(12)

    def _cancel(self):
        self.result_label.configure(text="正在取消…")
        self.on_cancel()

    def _close_requested(self):
        if self.close_button["state"] == "normal":
            self._close()
        else:
            self._cancel()

    def _close(self):
        self.closed = True
        self.window.destroy()

    def add_log(self, line):
        if self.closed:
            return
        self.log.insert("end", time.strftime("%H:%M:%S ") + str(line) + "\n")
        self.log.see("end")

    def add_progress(self, entry):
        if self.closed:
            return
        label = str(entry.get("message") or "").strip() or "处理中"
        total = entry.get("total")
        done = entry.get("progress")
        detail = ""
        if isinstance(total, (int, float)) and isinstance(done, (int, float)) and total:
            detail = f"{done}/{total}"
        if label != self._last_stage:
            self.stage_tree.insert(
                "", "end", values=(label, "进行中", detail)
            )
            if self._last_stage is not None:
                children = self.stage_tree.get_children()
                if children:
                    previous = children[-2]
                    self.stage_tree.set(previous, "state", "完成")
            self._last_stage = label
        self.add_log("阶段：" + label)

    def mark_cancelling(self):
        if not self.closed:
            self.result_label.configure(text="正在取消，等待服务端确认…")
            self.cancel_button.configure(state="disabled")

    def finish(self, outcome, elapsed):
        if self.closed:
            return
        self.progress.stop()
        self.cancel_button.configure(state="disabled")
        self.close_button.configure(state="normal")
        data = outcome.data or {}
        if data.get('face_id') is not None:
            self.add_log(f"识别人脸 ID：{data['face_id']}；用户：{data.get('username', '—')}")
        if outcome.ok:
            status = data.get("status", "aligned")
            self.result_label.configure(
                text=f"完成：{status}（耗时 {elapsed} 秒，人脸分数 {data.get('face_score', '—')}）"
            )
            self.add_log("结果：" + str(data.get("message") or "已对准"))
        elif outcome.cancelled:
            status = _cancel_status(outcome)
            self.result_label.configure(text=status)
            self.add_log("结果：" + status)
        else:
            self.result_label.configure(text="失败：" + (outcome.error or "未知错误"))
            self.add_log("失败：" + (outcome.error or "未知错误"))
        for stage in data.get("stages") or []:
            self.add_log(
                f"阶段记录：{stage.get('label')}｜{stage.get('status')}｜{stage.get('elapsed')}s｜{stage.get('message')}"
            )
        if outcome.stderr:
            self.add_log("SSH 输出（末尾）：\n" + outcome.stderr)
        if self._last_stage is not None:
            children = self.stage_tree.get_children()
            if children:
                self.stage_tree.set(children[-1], "state", "完成" if outcome.ok else "结束")


def run(config_path=None):
    config = SimConfig.load()
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.2)
    except tk.TclError:
        pass
    SimApp(root, config)
    root.mainloop()

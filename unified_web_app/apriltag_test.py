"""In-memory registry for AprilTag hardware test jobs.

A tag test is long running (pan/tilt, camera, Baidu request), so the HTTP
endpoint starts it in a worker thread and returns 202 immediately.  The MCP
tool polls the status endpoint, forwards the stage changes to its client and
finally returns the stored result.

Only in-process state is kept: jobs are never persisted and never write to the
inventory or borrow tables.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

MAX_JOBS = 32
JOB_TTL_SECONDS = 15 * 60

STAGE_LABELS = {
    "queued": "等待执行",
    "face_position": "转向人脸位置",
    "capture": "摄像头拍照",
    "face_verify": "人脸识别",
    "face_matched": "人脸识别通过",
    "return_position": "云台返回",
    "tag_search": "搜索目标标签",
    "align": "对准标签",
    "hold": "保持激光指示",
    "cleanup": "释放相机与指示灯",
    "done": "测试完成",
}

TERMINAL_STATES = {"done", "failed", "cancelled", "timeout"}


class JobCapacityError(RuntimeError):
    """No worker slot is available; the HTTP caller should return 429."""


class JobConflictError(ValueError):
    """A request id already belongs to another tag; return HTTP 409."""


def _iso(timestamp):
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


class TagTestJob:
    """Thread-safe state for one tag test."""

    def __init__(self, request_id, tag_id):
        self.request_id = str(request_id)
        self.tag_id = int(tag_id)
        self.state = "running"
        self.error = None
        self.error_code = None
        self.result = None
        self.username = None
        self.face_id = None
        self.user_id = None
        self.face_score = None
        self.created_at = time.time()
        self.finished_at = None
        self.face_attempts = 0
        self.cancel_event = threading.Event()
        self.cancel_requested_at = None
        self._lock = threading.RLock()
        self._timers = []
        self._worker_finished = False
        self._worker = None
        self._timers_closed = False
        self.stop_confirmed = None
        self._stages = []
        self._current = None
        self._stage("queued")

    # -- stage bookkeeping -------------------------------------------------
    def schedule_timer(self, seconds, callback, args):
        with self._lock:
            if self._timers_closed or self.state in TERMINAL_STATES:
                return
            timer = threading.Timer(seconds, callback, args=args)
            timer.daemon = True
            self._timers.append(timer)
            timer.start()

    def cancel_timers(self):
        with self._lock:
            self._timers_closed = True
            for timer in self._timers:
                timer.cancel()
            self._timers.clear()

    def attach_worker(self, worker):
        """Bind the reserved slot before Thread.start(), including queued work."""
        with self._lock:
            if self._worker is not None or self._worker_finished:
                raise RuntimeError("任务已绑定线程或已释放")
            self._worker = worker

    bind_worker = attach_worker

    def finish_worker(self):
        """Call only in the outermost worker finally, or after start() fails.

        A bound live thread still occupies its slot, even after this call.
        Cancellation, timeout and cancel_timers() never release worker slots.
        """
        with self._lock:
            self.cancel_timers()
            self._worker_finished = True

    def _worker_exited(self):
        # Caller holds self._lock. An unbound reservation remains occupied
        # until explicitly released (e.g. when thread construction fails).
        return self._worker_finished and (
            self._worker is None or not self._worker.is_alive()
        )

    def _stage(self, name, message=None):
        now = time.time()
        if self._current is not None and self._current["status"] == "running":
            self._current["status"] = "ok"
            self._current["finished_at"] = now
            self._current["elapsed"] = round(now - self._current["started_at"], 3)
        entry = {
            "name": name,
            "label": STAGE_LABELS.get(name, name),
            "status": "running",
            "message": message or "",
            "started_at": now,
            "finished_at": None,
            "elapsed": None,
        }
        self._stages.append(entry)
        self._current = entry

    def stage(self, name, message=None):
        with self._lock:
            if self.state != "running":
                return
            if self._current is not None and self._current["name"] == name:
                if message:
                    # Keep the newest attempt text, e.g. "第 4 次人脸识别".
                    self._current["message"] = message
                return
            self._stage(name, message)

    def mark_stage_failed(self, name, message):
        with self._lock:
            self.stage(name, message)
            if self._current is not None:
                self._current["status"] = "failed"
                self._current["finished_at"] = time.time()

    def set_user(self, username, score=None, face_id=None, user_id=None):
        with self._lock:
            self.username = username
            self.face_id = face_id
            self.user_id = user_id
            self.face_score = score

    def set_face_attempts(self, attempts):
        with self._lock:
            try:
                self.face_attempts = int(attempts)
            except (TypeError, ValueError):
                self.face_attempts = 0

    def _mark_named_stage(self, name, status):
        """Mark the stage that actually failed, not whatever runs last."""

        if not name:
            return
        for entry in reversed(self._stages):
            if entry["name"] != name:
                continue
            if entry["status"] in {"running", "ok"}:
                now = time.time()
                entry["status"] = status
                entry["finished_at"] = entry["finished_at"] or now
                entry["elapsed"] = entry["elapsed"] or round(
                    now - entry["started_at"], 3
                )
            return

    # -- terminal states ----------------------------------------------------
    def _close_current(self, status="ok"):
        if self._current is not None and self._current["status"] == "running":
            now = time.time()
            self._current["status"] = status
            self._current["finished_at"] = now
            self._current["elapsed"] = round(now - self._current["started_at"], 3)

    def succeed(self, result):
        with self._lock:
            if self.state != "running":
                return
            self.result = result
            self._close_current("ok")
            self.state = "done"
            self.finished_at = time.time()

    def fail(self, code, message, stage=None):
        with self._lock:
            if self.state != "running":
                return
            self.error_code = code
            self.error = message
            if stage:
                self._mark_named_stage(stage, "failed")
                self._close_current("ok")
            else:
                self._close_current("failed")
            self.state = "failed"
            self.finished_at = time.time()

    def cancelled(self, message="用户取消", stage=None):
        with self._lock:
            if self.state != "running":
                return
            self.error_code = "cancelled"
            self.error = message
            if stage:
                self._mark_named_stage(stage, "cancelled")
                self._close_current("ok")
            else:
                self._close_current("cancelled")
            self.state = "cancelled"
            self.finished_at = time.time()

    def timed_out(self, message="测试超时", stage=None):
        with self._lock:
            if self.state != "running":
                return
            self.error_code = "timeout"
            self.error = message
            if stage:
                self._mark_named_stage(stage, "failed")
                self._close_current("ok")
            else:
                self._close_current("failed")
            self.state = "timeout"
            self.finished_at = time.time()

    def request_cancel(self):
        with self._lock:
            if self.state == "running" and self.cancel_requested_at is None:
                self.cancel_requested_at = time.time()
        self.cancel_event.set()

    # -- read model ---------------------------------------------------------
    def snapshot(self):
        with self._lock:
            stages = [
                {
                    "name": item["name"],
                    "label": item["label"],
                    "status": item["status"],
                    "message": item["message"],
                    "elapsed": item["elapsed"],
                }
                for item in self._stages
            ]
            current = self._current["name"] if self._current else None
            return {
                "request_id": self.request_id,
                "tag_id": self.tag_id,
                "state": self.state,
                "stage": current,
                "stages": stages,
                "result": self.result,
                "error_code": self.error_code,
                "error": self.error,
                "username": self.username,
                "face_id": self.face_id,
                "user_id": self.user_id,
                "face_score": self.face_score,
                "face_attempts": self.face_attempts,
                "cancel_requested": self.cancel_requested_at is not None,
                "started_at": _iso(self.created_at),
                "finished_at": _iso(self.finished_at),
                "terminal": self.state in TERMINAL_STATES,
                "worker_finished": self._worker_exited(),
                "stop_confirmed": self.stop_confirmed,
            }


class JobRegistry:
    """Atomic hard cap on reservations, including logically finished workers."""

    def __init__(self, max_jobs=MAX_JOBS, ttl_seconds=JOB_TTL_SECONDS):
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or max_jobs < 1:
            raise ValueError("max_jobs must be a positive integer")
        self._jobs = {}
        self._lock = threading.Lock()
        self._max_jobs = max_jobs
        self._ttl = ttl_seconds

    def _finished_locked(self):
        finished = []
        for job in self._jobs.values():
            with job._lock:
                if job.state in TERMINAL_STATES and job._worker_exited():
                    finished.append((job.finished_at, job.request_id))
        return sorted(finished)

    def _prune_locked(self):
        now = time.time()
        for finished_at, request_id in self._finished_locked():
            if now - finished_at > self._ttl:
                self._jobs.pop(request_id, None)

    def prune(self):
        with self._lock:
            self._prune_locked()

    def create(self, request_id, tag_id):
        """Reserve a slot or raise JobCapacityError/JobConflictError.

        An identical request is idempotent even when full. Only terminal jobs
        whose real workers exited may be evicted for TTL or capacity.
        """

        request_id, tag_id = str(request_id), int(tag_id)
        with self._lock:
            self._prune_locked()
            existing = self._jobs.get(request_id)
            if existing is not None:
                if existing.tag_id != tag_id:
                    raise JobConflictError("request_id 已被其他标签任务使用")
                return existing, False
            if len(self._jobs) >= self._max_jobs:
                finished = self._finished_locked()
                if not finished:
                    raise JobCapacityError("活动测试任务已达上限，请稍后重试")
                self._jobs.pop(finished[0][1])
            job = TagTestJob(request_id, tag_id)
            self._jobs[request_id] = job
            return job, True

    def get(self, request_id):
        with self._lock:
            return self._jobs.get(str(request_id))

    def snapshot(self, request_id):
        job = self.get(request_id)
        return job.snapshot() if job else None


registry = JobRegistry()

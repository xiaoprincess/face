"""Optional hardware and Baidu Face adapters.

Imports are intentionally lazy so a development machine can run the website
without Raspberry Pi packages, a camera, or Baidu credentials.
"""

import base64
import io
import logging
import math
import os
import threading
import time
from contextlib import contextmanager


logger = logging.getLogger(__name__)


class HardwareUnavailable(RuntimeError):
    """Raised when an optional capability is not configured or installed."""


class _OperationInterrupted(Exception):
    def __init__(self, status):
        self.status = status


class _Cancellation:
    def __init__(self, external):
        self.external = external
        self.stopped = threading.Event()

    def is_set(self):
        return self.stopped.is_set() or (
            self.external is not None and self.external.is_set()
        )


class HardwareService:
    def __init__(self):
        self._lock = threading.RLock()
        # Serialize all physical camera and pan/tilt operations.  The tuned
        # motorDrive module remains an implementation detail behind this
        # adapter and is never modified by the website.
        self._operation_lock = threading.RLock()
        # Motor/LED commands and emergency stop share a short critical section.
        # Never hold this lock during camera capture or remote recognition.
        self._command_lock = threading.RLock()
        self._active_cancel = None
        self._face_client = None
        self._face_error = None
        self._pan_tilt = None
        self._pan_tilt_error = None

    @staticmethod
    def _check_operation(deadline, cancel_event):
        if time.monotonic() >= deadline:
            raise _OperationInterrupted("timeout")
        if cancel_event is not None and cancel_event.is_set():
            raise _OperationInterrupted("cancelled")

    @contextmanager
    def _guarded_operation(self, deadline, cancel_event):
        """Bound queue waits and stop hardware even if a driver blocks.

        The worker keeps the operation lock until its actual cleanup completes.
        The monitor exits on stop/deadline; it never launches replacement work.
        """
        while True:
            self._check_operation(deadline, cancel_event)
            if self._operation_lock.acquire(timeout=min(0.05, max(0, deadline - time.monotonic()))):
                break
        finished = threading.Event()
        cancellation = _Cancellation(cancel_event)
        monitor = None
        installed = False
        try:
            self._check_operation(deadline, cancellation)
            with self._command_lock:
                self._active_cancel = cancellation
                installed = True

            def watch():
                while not finished.is_set():
                    remaining = deadline - time.monotonic()
                    if cancellation.is_set() or remaining <= 0:
                        with self._command_lock:
                            if self._active_cancel is cancellation:
                                self.emergency_stop()
                        return
                    finished.wait(min(0.05, remaining))

            monitor = threading.Thread(target=watch, name="hardware-stop", daemon=True)
            monitor.start()
            yield cancellation
        finally:
            finished.set()
            if installed:
                with self._command_lock:
                    if self._active_cancel is cancellation:
                        self._stop_hardware()
                        self._active_cancel = None
            if monitor is not None and monitor.ident is not None:
                monitor.join(timeout=0.2)
            self._operation_lock.release()

    @staticmethod
    def _config_value(config, name, default=""):
        if config is not None and config.get(name) not in (None, ""):
            return str(config[name]).strip()
        return os.environ.get(name, default).strip()

    def _get_face_client(self, config=None):
        with self._lock:
            if self._face_client is not None:
                return self._face_client

            app_id = self._config_value(config, "BAIDU_APP_ID")
            api_key = self._config_value(config, "BAIDU_API_KEY")
            secret_key = self._config_value(config, "BAIDU_SECRET_KEY")
            if not all((app_id, api_key, secret_key)):
                self._face_error = "未配置百度人脸服务环境变量"
                raise HardwareUnavailable(self._face_error)

            try:
                from aip import AipFace
            except (ImportError, OSError) as exc:
                self._face_error = f"百度 AIP SDK 不可用: {exc}"
                raise HardwareUnavailable(self._face_error) from exc

            try:
                self._face_client = AipFace(app_id, api_key, secret_key)
                # Bound the blocking Baidu call so cancellation/timeouts cannot
                # hang the worker indefinitely.
                for setter, millis in (
                    ("setConnectionTimeoutInMillis", 5000),
                    ("setSocketTimeoutInMillis", 8000),
                ):
                    method = getattr(self._face_client, setter, None)
                    if method:
                        try:
                            method(millis)
                        except Exception:
                            logger.warning("设置百度超时失败: %s", setter, exc_info=True)
            except Exception as exc:
                self._face_error = f"百度人脸客户端初始化失败: {exc}"
                raise HardwareUnavailable(self._face_error) from exc
            self._face_error = None
            return self._face_client

    def _get_pan_tilt(self):
        with self._lock:
            if self._pan_tilt is not None:
                return self._pan_tilt
            try:
                from agent_yuntai.panTilt import lookAt, turnLED, x_motor, y_motor
            except (ImportError, OSError) as exc:
                self._pan_tilt_error = f"树莓派云台库不可用: {exc}"
                raise HardwareUnavailable(self._pan_tilt_error) from exc
            self._pan_tilt = {
                "look_at": lookAt,
                "turn_led": turnLED,
                "x_motor": x_motor,
                "y_motor": y_motor,
            }
            self._pan_tilt_error = None
            return self._pan_tilt

    def status(self, config=None):
        face_configured = all(
            self._config_value(config, key)
            for key in ("BAIDU_APP_ID", "BAIDU_API_KEY", "BAIDU_SECRET_KEY")
        )
        def module_available(name):
            try:
                import importlib.util

                return importlib.util.find_spec(name) is not None
            except (ImportError, ModuleNotFoundError, ValueError):
                return False

        aip_installed = module_available("aip")
        camera_installed = module_available("picamera2")
        pillow_installed = module_available("PIL")
        pan_tilt_installed = module_available("agent_yuntai.panTilt")

        face_ready = face_configured and aip_installed
        camera_ready = camera_installed and pillow_installed
        return {
            "available": face_ready and camera_ready,
            "face": {
                "available": face_ready,
                "configured": face_configured,
                "sdk_installed": aip_installed,
                "message": self._face_error
                or ("就绪" if face_ready else "需要百度 AIP SDK 和三项 BAIDU_* 配置"),
            },
            "camera": {
                "available": camera_ready,
                "message": "就绪" if camera_ready else "需要 picamera2 与 Pillow",
            },
            "pan_tilt": {
                "available": pan_tilt_installed,
                "message": self._pan_tilt_error
                or ("就绪" if pan_tilt_installed else "未安装树莓派云台库"),
            },
        }

    def search_face_raw(self, image_base64, config=None, group_id="equipment_users"):
        """Same Baidu call as the legacy web/start.py (no quality/liveness options)."""

        if not image_base64:
            raise ValueError("缺少人脸图片")
        if "base64," in image_base64:
            image_base64 = image_base64.split("base64,", 1)[1]
        client = self._get_face_client(config)
        return client.search(
            image=image_base64,
            image_type="BASE64",
            group_id_list=group_id,
        )

    def search_face(self, image_base64, config=None, group_id="equipment_users"):
        if not image_base64:
            raise ValueError("缺少人脸图片")
        if "base64," in image_base64:
            image_base64 = image_base64.split("base64,", 1)[1]
        client = self._get_face_client(config)
        return client.search(
            image=image_base64,
            image_type="BASE64",
            group_id_list=group_id,
            options={
                "quality_control": "NORMAL",
                "liveness_control": "NORMAL",
            },
        )

    def register_face(
        self,
        user_id,
        image_base64,
        config=None,
        group_id="equipment_users",
        replace=False,
    ):
        if not image_base64:
            raise ValueError("缺少人脸图片")
        if "base64," in image_base64:
            image_base64 = image_base64.split("base64,", 1)[1]
        client = self._get_face_client(config)
        method = client.updateUser if replace else client.addUser
        options = {
            "quality_control": "NORMAL",
            "liveness_control": "NORMAL",
            "action_type": "REPLACE",
        }
        return method(
            image=image_base64,
            image_type="BASE64",
            group_id=group_id,
            user_id=str(user_id),
            options=options,
        )

    def delete_face(self, user_id, config=None, group_id="equipment_users"):
        """Remove a user from the Baidu face group; callers tolerate failures."""
        client = self._get_face_client(config)
        return client.deleteUser(
            user_id=str(user_id),
            group_id=group_id,
        )

    def capture_face(self):
        with self._operation_lock:
            try:
                from picamera2 import Picamera2
                from PIL import Image
            except (ImportError, OSError) as exc:
                raise HardwareUnavailable(f"树莓派相机不可用: {exc}") from exc

            camera = Picamera2()
            try:
                camera.configure(
                    camera.create_still_configuration(main={"size": (1280, 960)})
                )
                camera.start()
                time.sleep(0.5)
                frame = camera.capture_array()
                image = Image.fromarray(frame)
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=88)
                return base64.b64encode(output.getvalue()).decode("ascii")
            finally:
                self._release_camera(camera)

    def capture_face_at_position(
        self, pan, tilt, on_stage=None, *, timeout=30.0, deadline=None, cancel_event=None
    ):
        """Capture and return to the original position within a finite budget.

        An optional deadline uses time.monotonic(); otherwise timeout includes
        queueing, movement and capture. If capture blocks, the guard's monitor
        stops motors independently; capture's finally releases the camera later.
        Keep the legacy capture/move call signatures for existing adapters.
        """
        if deadline is None:
            timeout = float(timeout)
            if not math.isfinite(timeout):
                raise ValueError("timeout must be finite")
            deadline = time.monotonic() + max(0.0, timeout)
        else:
            deadline = float(deadline)
            if not math.isfinite(deadline):
                raise ValueError("deadline must be finite")
        try:
            with self._guarded_operation(deadline, cancel_event) as cancellation:
                moved = False
                try:
                    self._emit_stage(on_stage, "face_position")
                    self._check_operation(deadline, cancellation)
                    self.move_to_face(pan, tilt)
                    moved = True
                    self._check_operation(deadline, cancellation)
                    self._emit_stage(on_stage, "capture")
                    self._check_operation(deadline, cancellation)
                    image = self.capture_face()
                    self._check_operation(deadline, cancellation)
                finally:
                    if moved:
                        # Never turn back after cancellation or a late capture.
                        self._check_operation(deadline, cancellation)
                        self._emit_stage(on_stage, "return_position")
                        self._check_operation(deadline, cancellation)
                        try:
                            self.move_to_face(pan, tilt, reverse=True)
                        except _OperationInterrupted:
                            raise
                        except Exception:
                            logger.warning("云台无法返回原位", exc_info=True)
                        self._check_operation(deadline, cancellation)
            self._check_operation(deadline, cancellation)
            return image
        except _OperationInterrupted as exc:
            status = "timeout" if time.monotonic() >= deadline else exc.status
            message = "人脸拍照超时" if status == "timeout" else "人脸拍照已取消"
            raise HardwareUnavailable(message) from exc

    @staticmethod
    def _release_camera(camera):
        """Always stop and close a Picamera2 instance, never masking the result."""

        if camera is None:
            return
        stop = getattr(camera, "stop", None)
        if stop:
            try:
                stop()
            except Exception:
                logger.warning("相机停止失败", exc_info=True)
        close = getattr(camera, "close", None)
        if close:
            try:
                close()
            except Exception:
                logger.warning("相机释放失败", exc_info=True)

    def capture_face_until_matched(
        self,
        pan,
        tilt,
        evaluator,
        deadline,
        cancel_event=None,
        interval=2.0,
        warmup=0.5,
        on_stage=None,
    ):
        """Turn to the face position and keep sampling until a face matches.

        This is a line-for-line port of the legacy web/start.py loop:

            configure still 640x480 RGB888 -> start -> sleep 0.5
            -> capture jpeg -> stop -> Baidu search -> sleep 2 on failure

        with the same 30 second budget (the caller passes the deadline) and
        the same score threshold of 70.  The camera object is created once and
        closed when the loop ends; start/stop still happen per attempt.
        """

        attempts = 0
        last_message = ""
        camera = None
        moved = False
        try:
            with self._guarded_operation(deadline, cancel_event) as cancellation:
                try:
                    self._emit_stage(on_stage, "face_position")
                    self.move_to_face(pan, tilt, deadline=deadline, cancel_event=cancellation)
                    moved = True
                    from picamera2 import Picamera2

                    self._check_operation(deadline, cancellation)
                    camera = Picamera2()
                    while True:
                        self._check_operation(deadline, cancellation)
                        attempts += 1
                        self._emit_stage(on_stage, "face_verify", f"第 {attempts} 次")
                        try:
                            camera.configure(camera.create_still_configuration(
                                main={"size": (640, 480), "format": "RGB888"}
                            ))
                            self._check_operation(deadline, cancellation)
                            camera.start()
                            warm_until = time.monotonic() + max(0.0, float(warmup))
                            while time.monotonic() < warm_until:
                                self._check_operation(deadline, cancellation)
                                cancellation.stopped.wait(min(0.05, max(0, warm_until - time.monotonic())))
                            self._check_operation(deadline, cancellation)
                            stream = io.BytesIO()
                            camera.capture_file(stream, format="jpeg")
                            self._check_operation(deadline, cancellation)
                            payload = base64.b64encode(stream.getvalue()).decode("utf-8")
                        finally:
                            try:
                                camera.stop()
                            except Exception:
                                logger.warning("相机停止失败", exc_info=True)

                        self._check_operation(deadline, cancellation)
                        try:
                            matched, message = evaluator(payload)
                        except HardwareUnavailable:
                            raise
                        except Exception as exc:
                            matched, message = None, f"人脸识别调用失败：{exc}"
                        # A late successful response is not permission to move.
                        self._check_operation(deadline, cancellation)
                        if matched:
                            return {"status": "matched", "matched": matched,
                                    "attempts": attempts, "message": ""}
                        last_message = message or "未识别到已注册人脸"
                        sleep_until = min(deadline, time.monotonic() + max(0.0, float(interval)))
                        while time.monotonic() < sleep_until:
                            self._check_operation(deadline, cancellation)
                            cancellation.stopped.wait(min(0.05, max(0, sleep_until - time.monotonic())))
                finally:
                    self._release_camera(camera)
                    # Cancellation/timeout must not start a return gesture.
                    try:
                        self._check_operation(deadline, cancellation)
                        if moved:
                            self._emit_stage(on_stage, "return_position")
                            self.move_to_face(pan, tilt, reverse=True,
                                              deadline=deadline, cancel_event=cancellation)
                    finally:
                        self._stop_hardware()
        except _OperationInterrupted as exc:
            message = "用户取消，已停止人脸识别"
            if exc.status == "timeout":
                message = f"人脸识别超时：已尝试 {attempts} 次"
                if last_message:
                    message += f"，最后一次结果：{last_message}"
            return {"status": exc.status, "attempts": attempts, "message": message}
        except (ImportError, OSError) as exc:
            raise HardwareUnavailable(f"树莓派相机不可用: {exc}") from exc

    @staticmethod
    def _emit_stage(on_stage, name, message=None):
        if on_stage is None:
            return
        try:
            if message is None:
                on_stage(name)
            else:
                on_stage(name, message)
        except Exception:
            logger.debug("阶段回调执行失败: %s", name, exc_info=True)

    def point_to_cabinet(self, tag_id, delay=3.0, timeout=30.0, cancel_event=None):
        """Legacy entry point with a finite budget and explicit failure."""
        result = self.point_to_cabinet_status(
            tag_id, delay=delay, timeout=timeout, cancel_event=cancel_event
        )
        if result.get("status") != "aligned":
            raise HardwareUnavailable(result.get("message") or "云台定位失败")
        return result

    def point_to_cabinet_status(
        self,
        tag_id,
        delay=3.0,
        timeout=30.0,
        cancel_event=None,
        on_stage=None,
        lost_timeout=8.0,
    ):
        """Point within one budget covering queue, setup, alignment and hold.

        A blocked native camera call cannot be killed safely in Python. The
        monitor stops the physical axes independently, and the operation stays
        occupied until the worker returns and releases its camera.
        """
        timeout, delay = float(timeout), max(0.0, float(delay))
        if not math.isfinite(timeout) or not math.isfinite(delay):
            raise ValueError("timeout and delay must be finite")
        deadline = time.monotonic() + max(0.0, timeout)
        camera = None
        result = None
        stopped = None
        try:
            with self._guarded_operation(deadline, cancel_event) as cancellation:
                try:
                    pan_tilt = self._get_pan_tilt()
                    import apriltag
                    from picamera2 import Picamera2
                    from tag_align import align_to_tag

                    self._check_operation(deadline, cancellation)
                    camera = Picamera2()
                    detector = apriltag.apriltag("tag36h11", threads=4)
                    self._check_operation(deadline, cancellation)
                    camera.configure(
                        camera.create_preview_configuration(main={"size": (1920, 1080)})
                    )
                    self._check_operation(deadline, cancellation)
                    camera.start()
                    with self._command_lock:
                        self._check_operation(deadline, cancellation)
                        try:
                            pan_tilt["turn_led"](True)
                        except Exception:
                            logger.warning("云台指示灯不可用，继续执行定位", exc_info=True)

                    result = align_to_tag(
                        int(tag_id), camera, detector,
                        deadline=deadline,
                        cancel_event=cancellation,
                        lost_timeout=lost_timeout,
                        on_stage=lambda stage: self._emit_stage(on_stage, stage),
                        motors=(pan_tilt["x_motor"], pan_tilt["y_motor"]),
                        command_lock=self._command_lock,
                    )
                    self._check_operation(deadline, cancellation)
                    if result.get("status") == "aligned":
                        self._emit_stage(on_stage, "hold")
                        hold_until = time.monotonic() + delay
                        while time.monotonic() < hold_until:
                            self._check_operation(deadline, cancellation)
                            cancellation.stopped.wait(min(
                                0.05, max(0, hold_until - time.monotonic()),
                                max(0, deadline - time.monotonic()),
                            ))
                        self._check_operation(deadline, cancellation)
                    result["tag_id"] = int(tag_id)
                finally:
                    # Stop before potentially blocking camera cleanup. A failed
                    # stop must never be reported as successful alignment.
                    stopped = self._stop_hardware()
                    self._emit_stage(on_stage, "cleanup")
                    self._release_camera(camera)
                    if result is not None:
                        result["stop_confirmed"] = stopped
                        if time.monotonic() >= deadline:
                            result.update(status="timeout", message="定位超时")
                        elif cancellation.is_set():
                            result.update(status="cancelled", message="定位已取消")
                        if not stopped and result.get("status") == "aligned":
                            result.update(status="hardware_error", message="定位结束，但硬件停止未确认")
            return result
        except _OperationInterrupted as exc:
            return {"status": exc.status, "tag_id": int(tag_id), "stop_confirmed": stopped,
                    "message": "定位超时" if exc.status == "timeout" else "定位已取消"}
        except Exception as exc:
            return {"status": "hardware_error", "tag_id": int(tag_id), "stop_confirmed": stopped,
                    "message": f"定位过程异常：{exc}"}

    def emergency_stop(self):
        """Best-effort stop that does not depend on the worker thread.

        Called by the watchdog and the cancel endpoint so motors stop and the
        LED turns off even if a capture or a Baidu call is currently blocking.
        """

        with self._command_lock:
            if self._active_cancel is not None:
                self._active_cancel.stopped.set()
            return self._stop_hardware()

    def _stop_hardware(self):
        """Stop without cancelling a successfully completed operation."""
        with self._command_lock:
            try:
                pan_tilt = self._get_pan_tilt()
            except Exception:
                logger.warning("紧急停止时云台不可用", exc_info=True)
                return False
            stopped = True
            for name in ("x_motor", "y_motor"):
                motor = pan_tilt.get(name)
                if motor is None:
                    stopped = False
                    continue
                try:
                    motor.setSpeed(0)
                except Exception:
                    stopped = False
                    logger.warning("紧急停止电机失败: %s", name, exc_info=True)
            try:
                pan_tilt["turn_led"](False)
            except Exception:
                stopped = False
                logger.warning("紧急停止时关闭指示灯失败", exc_info=True)
            return stopped

    def move_to_face(self, pan, tilt, reverse=False, *, deadline=float("inf"), cancel_event=None):
        with self._operation_lock:
            pan_tilt = self._get_pan_tilt()
            pan, tilt = float(pan), float(tilt)
            if not all(math.isfinite(value) for value in (pan, tilt)):
                raise ValueError("云台角度必须为有限数值")
            cancellation = cancel_event if cancel_event is not None else self._active_cancel
            try:
                for name, angle in (("x_motor", pan), ("y_motor", tilt)):
                    direction = (0 if angle < 0 else 1) if reverse else (1 if angle < 0 else 0)
                    remaining = abs(angle)
                    # motorDrive.turnAngle() emits blocking GPIO pulses. Small
                    # bounded steps let emergency_stop fence every new command.
                    while remaining > 0:
                        with self._command_lock:
                            self._check_operation(deadline, cancellation)
                            step = min(9.0, remaining)
                            pan_tilt[name].turnAngle(step, direction)
                        remaining -= step
            finally:
                # Do not latch cancellation on normal moves: capture may follow.
                with self._command_lock:
                    for name in ("x_motor", "y_motor"):
                        try:
                            pan_tilt[name].setSpeed(0)
                        except Exception:
                            logger.warning("云台转动后停止失败: %s", name, exc_info=True)


hardware = HardwareService()

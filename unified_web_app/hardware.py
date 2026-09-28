"""Optional hardware and Baidu Face adapters.

Imports are intentionally lazy so a development machine can run the website
without Raspberry Pi packages, a camera, or Baidu credentials.
"""

import base64
import io
import logging
import os
import threading
import time


logger = logging.getLogger(__name__)


class HardwareUnavailable(RuntimeError):
    """Raised when an optional capability is not configured or installed."""


class HardwareService:
    def __init__(self):
        self._lock = threading.RLock()
        # Serialize all physical camera and pan/tilt operations.  The tuned
        # motorDrive module remains an implementation detail behind this
        # adapter and is never modified by the website.
        self._operation_lock = threading.RLock()
        self._face_client = None
        self._face_error = None
        self._pan_tilt = None
        self._pan_tilt_error = None

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
                try:
                    camera.stop()
                finally:
                    close = getattr(camera, "close", None)
                    if close:
                        close()

    def capture_face_at_position(self, pan, tilt):
        """Move to the configured face position, capture, and always return."""

        with self._operation_lock:
            moved = False
            try:
                self.move_to_face(pan, tilt)
                moved = True
                return self.capture_face()
            finally:
                if moved:
                    try:
                        self.move_to_face(pan, tilt, reverse=True)
                    except Exception:
                        logger.warning("云台无法返回原位", exc_info=True)

    def point_to_cabinet(self, tag_id, delay=3.0):
        with self._operation_lock:
            pan_tilt = self._get_pan_tilt()
            try:
                import apriltag
                from picamera2 import Picamera2
            except (ImportError, OSError) as exc:
                raise HardwareUnavailable(f"AprilTag 或相机库不可用: {exc}") from exc

            camera = Picamera2()
            detector = apriltag.apriltag("tag36h11", threads=4)
            led_enabled = False
            try:
                camera.configure(
                    camera.create_preview_configuration(main={"size": (1920, 1080)})
                )
                camera.start()
                try:
                    pan_tilt["turn_led"](True)
                    led_enabled = True
                except Exception:
                    # Some tuned deployments intentionally leave the indicator
                    # pin unconfigured.  Pointing must still be able to run.
                    logger.warning("云台指示灯不可用，继续执行定位", exc_info=True)
                pan_tilt["look_at"](int(tag_id), camera, detector)
                time.sleep(max(0.0, float(delay)))
            finally:
                if led_enabled:
                    try:
                        pan_tilt["turn_led"](False)
                    except Exception:
                        logger.warning("无法关闭云台指示灯", exc_info=True)
                try:
                    camera.stop()
                finally:
                    close = getattr(camera, "close", None)
                    if close:
                        close()

    def move_to_face(self, pan, tilt, reverse=False):
        with self._operation_lock:
            pan_tilt = self._get_pan_tilt()
            pan = float(pan)
            tilt = float(tilt)
            if reverse:
                pan_direction = 0 if pan < 0 else 1
                tilt_direction = 0 if tilt < 0 else 1
            else:
                pan_direction = 1 if pan < 0 else 0
                tilt_direction = 1 if tilt < 0 else 0
            pan_tilt["x_motor"].turnAngle(abs(pan), pan_direction)
            pan_tilt["y_motor"].turnAngle(abs(tilt), tilt_direction)


hardware = HardwareService()

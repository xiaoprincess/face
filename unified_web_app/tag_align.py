"""Status-aware AprilTag alignment used by the hardware test endpoint.

The tuned pan/tilt behaviour lives in the web/agent_yuntai submodule. That
module is shared with the old site and its lookAt() blocks forever and returns
nothing, so this wrapper reproduces the same control law (same PID gains, same
focal length, same tolerance) while adding the pieces the tag test needs:

* a hard deadline,
* a cancellation event,
* a machine readable result: aligned / lost / timeout / cancelled / error,
* guaranteed motor stop on every exit path.

Callers that only need pointing keep using hardware.point_to_cabinet();
nothing here changes motorDrive.py or the submodule.
"""

from __future__ import annotations

import math
import time
from contextlib import nullcontext

TAG_FAMILY = "tag36h11"
TAG_SIZE_METERS = 0.05
FOCAL_X = 947.0
FOCAL_Y = 947.0
FRAME_WIDTH = 1920
FRAME_HEIGHT = 1080
PRINCIPAL_X = FRAME_WIDTH / 2
PRINCIPAL_Y = FRAME_HEIGHT / 2

# Same gains and limits as the tuned web/agent_yuntai/panTilt.py on the Pi:
# simple_pid.PID(10, 0.8, 1) and SPEED_LIMIT = (-150, 150).  Keep these in sync
# whenever the tuned module changes.
PID_KP = 10
PID_KI = 0.8
PID_KD = 1
SPEED_LIMIT = (-150, 150)
ALIGN_TOLERANCE_DEGREES = 0.3
# The tuned lookAt() gave up after 30 frames (~2.3 s at 1080p).  Two seconds is
# shorter than auto-exposure settling and the gimbal's post-move vibration, so
# the default is now a time window.  max_lost_frames stays available for callers
# that want the old frame cap as well.
LOST_GRACE_SECONDS = 8.0
MAX_LOST_FRAMES = 30

STATUS_ALIGNED = "aligned"
STATUS_LOST = "lost"
STATUS_TIMEOUT = "timeout"
STATUS_CANCELLED = "cancelled"
STATUS_ERROR = "hardware_error"

STAGE_SEARCH = "tag_search"
STAGE_ALIGN = "align"


def _result(status, message, **extra):
    payload = {"status": status, "message": message}
    payload.update(extra)
    return payload


def _stop_motors(x_motor, y_motor):
    stopped = True
    for motor in (x_motor, y_motor):
        if motor is None:
            stopped = False
            continue
        try:
            motor.setSpeed(0)
        except Exception:
            stopped = False
    return stopped


def align_to_tag(
    tag_id,
    camera,
    detector,
    *,
    deadline,
    cancel_event=None,
    lost_timeout=LOST_GRACE_SECONDS,
    max_lost_frames=None,
    tolerance=ALIGN_TOLERANCE_DEGREES,
    on_stage=None,
    motors=None,
    command_lock=None,
):
    """Bring tag_id to the image centre.

    Parameters
    ----------
    tag_id : int
        AprilTag id in the configured family.
    camera : picamera2.Picamera2
        A started camera instance owned by the caller.
    detector : apriltag detector
        Detector created by the caller (kept alive for the whole gesture).
    deadline : float
        time.monotonic() based instant after which the loop must stop.
    cancel_event : threading.Event, optional
        Set when the caller wants the gesture to stop.
    lost_timeout : float
        Continuous seconds without seeing the tag before reporting lost.
    max_lost_frames : int, optional
        Optional legacy frame cap; None means time window only.
    tolerance : float
        Yaw/pitch tolerance in degrees.

    Returns
    -------
    dict with status in {aligned, lost, timeout, cancelled, hardware_error}
    plus diagnostics (yaw, pitch, distance, iterations, lost_frames, elapsed).
    """

    def emit(stage):
        if on_stage is not None:
            try:
                on_stage(stage)
            except Exception:
                pass

    started = time.monotonic()
    x_motor, y_motor = motors if motors is not None else (None, None)
    command_lock = command_lock if command_lock is not None else nullcontext()
    outcome = {}

    def finish(status, message, **extra):
        outcome.update(_result(status, message, **extra))
        return outcome

    def interrupted():
        if time.monotonic() >= deadline:
            return finish(STATUS_TIMEOUT, "定位超时，未在限定时间内对准标签")
        if cancel_event is not None and cancel_event.is_set():
            return finish(STATUS_CANCELLED, "定位已取消")
        return None

    iterations = 0
    lost_frames = 0
    lost_since = None
    last_yaw = 0.0
    last_pitch = 0.0
    distance = 0.0

    try:
        if motors is None:
            from agent_yuntai.panTilt import x_motor, y_motor
        import cv2
        from simple_pid import PID

        x_pid = PID(PID_KP, PID_KI, PID_KD, setpoint=0)
        y_pid = PID(PID_KP, PID_KI, PID_KD, setpoint=0)
        x_pid.output_limits = SPEED_LIMIT
        y_pid.output_limits = SPEED_LIMIT
        emit(STAGE_SEARCH)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return finish(
                    STATUS_CANCELLED,
                    "用户取消，已停止对准",
                    iterations=iterations,
                    elapsed=round(time.monotonic() - started, 3),
                )
            if time.monotonic() >= deadline:
                return finish(
                    STATUS_TIMEOUT,
                    "定位超时，未在限定时间内对准标签",
                    iterations=iterations,
                    elapsed=round(time.monotonic() - started, 3),
                    yaw=round(last_yaw, 2),
                    pitch=round(last_pitch, 2),
                )

            frame = camera.capture_array()
            if interrupted() is not None:
                return outcome
            gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            gray = cv2.flip(gray, -1)
            tags = detector.detect(gray)
            if interrupted() is not None:
                return outcome
            iterations += 1

            target = None
            for tag in tags:
                try:
                    if int(tag["id"]) == int(tag_id):
                        target = tag
                        break
                except (KeyError, TypeError, ValueError):
                    continue

            if target is None:
                # Do not keep driving from the last pose while the tag is lost.
                with command_lock:
                    if not _stop_motors(x_motor, y_motor):
                        return finish(STATUS_ERROR, "标签丢失且电机停止未确认")
                lost_frames += 1
                if lost_since is None:
                    lost_since = time.monotonic()
                missing_seconds = time.monotonic() - lost_since
                timed_out_loss = (
                    lost_timeout is not None and missing_seconds >= float(lost_timeout)
                )
                frame_cap = (
                    max_lost_frames is not None and lost_frames >= max_lost_frames
                )
                if timed_out_loss or frame_cap:
                    return finish(
                        STATUS_LOST,
                        f"连续 {missing_seconds:.1f} 秒（{lost_frames} 帧）"
                        f"未检测到标签 {tag_id}",
                        iterations=iterations,
                        lost_frames=lost_frames,
                        lost_seconds=round(missing_seconds, 1),
                        elapsed=round(time.monotonic() - started, 3),
                    )
                continue

            lost_frames = 0
            lost_since = None
            pose = detector.estimate_tag_pose(
                target, TAG_SIZE_METERS, FOCAL_X, FOCAL_Y, PRINCIPAL_X, PRINCIPAL_Y
            )
            if interrupted() is not None:
                return outcome
            tx, ty, tz = (float(value[0]) for value in pose["t"])
            if not all(math.isfinite(value) for value in (tx, ty, tz)):
                return finish(STATUS_ERROR, "标签位姿不是有限数值")
            if tz == 0:
                continue
            last_yaw = math.degrees(math.atan2(tx, tz))
            last_pitch = math.degrees(math.atan2(ty, tz))
            distance = tz

            if abs(last_yaw) <= tolerance and abs(last_pitch) <= tolerance:
                emit(STAGE_ALIGN)
                if interrupted() is not None:
                    return outcome
                return finish(
                    STATUS_ALIGNED,
                    "已对准目标标签",
                    yaw=round(last_yaw, 2),
                    pitch=round(last_pitch, 2),
                    distance_m=round(distance, 3),
                    iterations=iterations,
                    elapsed=round(time.monotonic() - started, 3),
                )

            emit(STAGE_ALIGN)
            with command_lock:
                if interrupted() is not None:
                    return outcome
                x_motor.setSpeed(-x_pid(last_yaw))
                if interrupted() is not None:
                    return outcome
                y_motor.setSpeed(-y_pid(last_pitch))
    except Exception as exc:  # hardware, estimation and camera errors
        return finish(
            STATUS_ERROR,
            f"定位过程异常：{exc}",
            iterations=iterations,
            elapsed=round(time.monotonic() - started, 3),
        )
    finally:
        with command_lock:
            stopped = _stop_motors(x_motor, y_motor)
        outcome["stop_confirmed"] = stopped
        if not stopped and outcome.get("status") == STATUS_ALIGNED:
            outcome.update(status=STATUS_ERROR, message="已对准，但电机停止未确认")

"""Status-aware variant of web/agent_yuntai/panTilt.py (NOT deployed by default).

The tuned pan/tilt driver lives in the gitee submodule web/agent_yuntai and is
shared with the old site; it must not be replaced blindly.  This file is the
reviewed drop-in version used when the team wants lookAt() itself to report a
result.  Copy it over web/agent_yuntai/panTilt.py on the Raspberry Pi only
after comparing with the current tuned file and re-running the mechanical
check.

Changes versus the tuned original
---------------------------------
1. lookAt(tag_id, picam2, detector, *, deadline=None, cancel_event=None,
   result=False) returns a dict when result=True:
      {"status": "aligned" | "lost" | "timeout" | "cancelled" | "error",
       "message": str, "yaw": float, "pitch": float, "distance_m": float,
       "iterations": int, "elapsed": float}
   Existing callers that pass three arguments keep working and still get None.
2. Motors are stopped on every exit path (return, exception, cancel).
3. A deadline and a cancellation event end the loop instead of blocking forever.

Nothing else changes: same PID gains, same focal length, same tag size, same
30-frame loss limit, same LED pin and motor pins.
"""

import math
import time

import cv2
import RPi.GPIO as GPIO
import simple_pid

from agent_yuntai import motorDrive

fx, fy = 947.0, 947.0
cx, cy = 1920 / 2, 1080 / 2
tag_size = 0.05
SPEED_LIMIT = (-100, 100)
LED_PIN = 26

motorDrive.init()
# GPIO.setup(LED_PIN, GPIO.OUT)

x_motor = motorDrive.Motor(17, 27, 22)
y_motor = motorDrive.Motor(5, 6, 13)


def turnLED(flag: bool):
    GPIO.output(LED_PIN, GPIO.HIGH if flag else GPIO.LOW)


def _stop_motors():
    for motor in (x_motor, y_motor):
        try:
            motor.setSpeed(0)
        except Exception:
            pass


def _result(status, message, **extra):
    payload = {
        "status": status,
        "message": message,
        "yaw": 0.0,
        "pitch": 0.0,
        "distance_m": 0.0,
        "iterations": 0,
        "elapsed": 0.0,
    }
    payload.update(extra)
    return payload


def lookAt(
    tag_id: int,
    picam2,
    detector,
    *,
    deadline=None,
    cancel_event=None,
    result: bool = False,
    max_lost_frames: int = 30,
    tolerance: float = 0.3,
):
    """Pan/tilt the mount to bring the AprilTag with *tag_id* to the centre.

    deadline     time.monotonic() value after which the loop gives up
    cancel_event threading.Event that stops the loop when set
    result       when True return a status dict; otherwise return None
                 (backward compatible with existing callers)
    """

    started = time.monotonic()
    x_pid = simple_pid.PID(10, 1, 0.8, setpoint=0)
    y_pid = simple_pid.PID(10, 1, 0.8, setpoint=0)
    x_pid.output_limits = SPEED_LIMIT
    y_pid.output_limits = SPEED_LIMIT

    iterations = 0
    lost_frames = 0
    last_yaw = 0.0
    last_pitch = 0.0
    distance = 0.0
    outcome = None

    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                outcome = _result("cancelled", "已取消", iterations=iterations)
                break
            if deadline is not None and time.monotonic() >= deadline:
                outcome = _result(
                    "timeout",
                    "定位超时",
                    yaw=last_yaw,
                    pitch=last_pitch,
                    iterations=iterations,
                )
                break

            frame = picam2.capture_array()
            frame_grey = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
            frame_grey = cv2.flip(frame_grey, -1)
            tags = detector.detect(frame_grey)
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
                lost_frames += 1
                if lost_frames >= max_lost_frames:
                    outcome = _result(
                        "lost",
                        "连续丢失目标 %d 帧" % lost_frames,
                        iterations=iterations,
                    )
                    break
                continue

            lost_frames = 0
            pose = detector.estimate_tag_pose(
                target, tag_size, fx, fy, cx, cy
            )
            tx, ty, tz = (float(value[0]) for value in pose["t"])
            if tz == 0:
                continue
            last_yaw = math.degrees(math.atan2(tx, tz))
            last_pitch = math.degrees(math.atan2(ty, tz))
            distance = tz

            if abs(last_yaw) <= tolerance and abs(last_pitch) <= tolerance:
                outcome = _result(
                    "aligned",
                    "已对准",
                    yaw=last_yaw,
                    pitch=last_pitch,
                    distance_m=distance,
                    iterations=iterations,
                )
                break

            x_motor.setSpeed(-x_pid(last_yaw))
            y_motor.setSpeed(-y_pid(last_pitch))
    except Exception as error:
        outcome = _result("error", "定位异常：%s" % error, iterations=iterations)
    finally:
        _stop_motors()

    if outcome is None:
        outcome = _result("error", "定位未完成", iterations=iterations)
    outcome["elapsed"] = round(time.monotonic() - started, 3)
    outcome["yaw"] = round(outcome.get("yaw", 0.0), 2)
    outcome["pitch"] = round(outcome.get("pitch", 0.0), 2)
    outcome["distance_m"] = round(outcome.get("distance_m", 0.0), 3)
    return outcome if result else None

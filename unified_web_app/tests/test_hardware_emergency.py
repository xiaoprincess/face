"""Hardware emergency stop tests (no Raspberry Pi required)."""

from __future__ import annotations

import unittest
import sys
import threading
import time
import types
from unittest.mock import Mock, call, patch

from hardware import HardwareService, HardwareUnavailable


class FakeMotor:
    def __init__(self):
        self.speeds = []
        self.angles = []
        self.stopped = threading.Event()

    def setSpeed(self, value):
        self.speeds.append(value)
        if value == 0:
            self.stopped.set()

    def turnAngle(self, angle, direction):
        self.angles.append((angle, direction))


class EmergencyStopTests(unittest.TestCase):
    def test_emergency_stop_stops_motors_and_led(self):
        x_motor = FakeMotor()
        y_motor = FakeMotor()
        led_calls = []
        pan_tilt = {
            "x_motor": x_motor,
            "y_motor": y_motor,
            "turn_led": lambda flag: led_calls.append(flag),
        }
        service = HardwareService()
        with patch.object(service, "_get_pan_tilt", return_value=pan_tilt):
            self.assertTrue(service.emergency_stop())
        self.assertEqual(x_motor.speeds[-1], 0)
        self.assertEqual(y_motor.speeds[-1], 0)
        self.assertEqual(led_calls[-1], False)

    def test_emergency_stop_is_safe_without_pan_tilt(self):
        service = HardwareService()
        with patch.object(service, "_get_pan_tilt", side_effect=RuntimeError("no motor")):
            self.assertFalse(service.emergency_stop())

    def test_led_success_does_not_hide_failed_axis(self):
        motor = FakeMotor()
        broken = FakeMotor()
        def fail(value):
            raise RuntimeError('axis unavailable')
        broken.setSpeed = fail
        service = HardwareService()
        with patch.object(service, '_get_pan_tilt', return_value={
            'x_motor': motor, 'y_motor': broken, 'turn_led': lambda value: None,
        }):
            self.assertFalse(service.emergency_stop())

    def test_missing_axis_is_not_confirmed_stopped(self):
        service = HardwareService()
        with patch.object(service, '_get_pan_tilt', return_value={
            'x_motor': FakeMotor(), 'turn_led': lambda value: None,
        }):
            self.assertFalse(service.emergency_stop())


class PointingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.service = HardwareService()
        self.x, self.y = FakeMotor(), FakeMotor()
        self.led = Mock()
        self.service._pan_tilt = {
            "x_motor": self.x, "y_motor": self.y, "turn_led": self.led,
            "look_at": Mock(side_effect=AssertionError("legacy lookAt must not run")),
        }
        self.camera = Mock()
        self.camera.capture_array.return_value = object()
        self.camera.capture_file.side_effect = lambda stream, **kw: stream.write(b"jpeg")
        self.detector = Mock()
        self.detector.detect.return_value = [{"id": 5}]
        self.detector.estimate_tag_pose.return_value = {"t": [[0], [0], [1]]}
        self.camera_factory = Mock(return_value=self.camera)
        self.detector_factory = Mock(return_value=self.detector)
        cv2 = types.ModuleType("cv2")
        cv2.COLOR_RGB2GRAY = 0
        cv2.cvtColor = lambda frame, code: frame
        cv2.flip = lambda frame, code: frame
        pid = types.ModuleType("simple_pid")

        class PID:
            def __init__(self, *args, **kwargs):
                pass

            def __call__(self, value):
                return value

        pid.PID = PID
        apriltag = types.ModuleType("apriltag")
        apriltag.apriltag = self.detector_factory
        picamera = types.ModuleType("picamera2")
        picamera.Picamera2 = self.camera_factory
        self.enterContext(patch.dict(sys.modules, {
            "cv2": cv2, "simple_pid": pid, "apriltag": apriltag,
            "picamera2": picamera,
        }))
        self.enterContext(patch("hardware.logger"))

    def assert_cleaned(self):
        self.assertEqual(self.x.speeds[-1], 0)
        self.assertEqual(self.y.speeds[-1], 0)
        self.assertEqual(self.led.call_args.args, (False,))
        self.camera.stop.assert_called()
        self.camera.close.assert_called_once()

    def test_legacy_entry_delegates_with_finite_timeout(self):
        with patch.object(self.service, "point_to_cabinet_status", return_value={"status": "aligned"}) as status:
            self.assertEqual(self.service.point_to_cabinet(5, 0), {"status": "aligned"})
            status.assert_called_once_with(5, delay=0, timeout=30.0, cancel_event=None)

    def test_legacy_entry_never_claims_success_for_failure(self):
        for status in ("lost", "timeout", "cancelled", "hardware_error"):
            with self.subTest(status=status):
                with patch.object(self.service, "point_to_cabinet_status", return_value={
                    "status": status, "message": status,
                }), self.assertRaisesRegex(HardwareUnavailable, status):
                    self.service.point_to_cabinet(5)

    def test_success_stops_axes_led_and_camera(self):
        result = self.service.point_to_cabinet(5, delay=0)
        self.assertEqual(result["status"], "aligned")
        self.assertTrue(result["stop_confirmed"])
        self.service._pan_tilt["look_at"].assert_not_called()
        self.assert_cleaned()

    def test_setup_failures_always_release_constructed_camera(self):
        for failing in (self.detector_factory, self.camera.configure, self.camera.start):
            with self.subTest(failing=failing):
                self.camera.reset_mock()
                failing.side_effect = RuntimeError("setup failure")
                try:
                    result = self.service.point_to_cabinet_status(5, delay=0)
                finally:
                    failing.side_effect = None
                self.assertEqual(result["status"], "hardware_error")
                self.assert_cleaned()

    def test_camera_close_runs_even_when_stop_raises(self):
        self.camera.stop.side_effect = RuntimeError("stop failed")
        self.assertEqual(self.service.point_to_cabinet_status(5, delay=0)["status"], "aligned")
        self.camera.close.assert_called_once()
        self.assertEqual(self.x.speeds[-1], 0)

    def test_failed_axis_stop_does_not_report_aligned(self):
        self.y.setSpeed = Mock(side_effect=RuntimeError("broken axis"))
        result = self.service.point_to_cabinet_status(5, delay=0)
        self.assertEqual(result["status"], "hardware_error")
        self.assertFalse(result["stop_confirmed"])
        self.camera.close.assert_called_once()

    def test_failed_led_off_does_not_report_aligned(self):
        def led(flag):
            if not flag:
                raise RuntimeError("LED stuck")
        self.led.side_effect = led
        result = self.service.point_to_cabinet_status(5, delay=0)
        self.assertEqual(result["status"], "hardware_error")
        self.assertFalse(result["stop_confirmed"])
        self.camera.close.assert_called_once()

    def test_cancel_during_hold_is_prompt_and_cleans_up(self):
        cancel = threading.Event()
        def stage(name):
            if name == "hold":
                cancel.set()
        started = time.monotonic()
        result = self.service.point_to_cabinet_status(5, delay=10, cancel_event=cancel, on_stage=stage)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(result["status"], "cancelled")
        self.assert_cleaned()

    def test_hold_expiring_budget_is_timeout(self):
        result = self.service.point_to_cabinet_status(5, delay=10, timeout=0.05)
        self.assertEqual(result["status"], "timeout")
        self.assert_cleaned()

    def test_cancel_during_cleanup_cannot_report_aligned(self):
        cancel = threading.Event()
        def stage(name):
            if name == "cleanup":
                cancel.set()
        result = self.service.point_to_cabinet_status(5, delay=0, cancel_event=cancel, on_stage=stage)
        self.assertEqual(result["status"], "cancelled")
        self.assert_cleaned()

    def test_pre_cancelled_or_expired_work_does_not_open_hardware(self):
        cancel = threading.Event()
        cancel.set()
        self.assertEqual(self.service.point_to_cabinet_status(5, cancel_event=cancel)["status"], "cancelled")
        self.assertEqual(self.service.point_to_cabinet_status(5, timeout=0)["status"], "timeout")
        self.camera_factory.assert_not_called()
        self.assertEqual(self.x.speeds, [])

    def test_queue_wait_counts_against_budget_without_stopping_owner(self):
        result = []
        with self.service._operation_lock:
            worker = threading.Thread(target=lambda: result.append(
                self.service.point_to_cabinet_status(5, timeout=0.05)
            ))
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["status"], "timeout")
        self.camera_factory.assert_not_called()
        self.assertEqual(self.x.speeds, [])

    def _blocked_capture(self, reason):
        entered, release = threading.Event(), threading.Event()
        cancel = threading.Event()
        results = []
        frames = 0
        self.detector.estimate_tag_pose.return_value = {"t": [[0.1], [0.1], [1]]}

        def capture():
            nonlocal frames
            frames += 1
            if frames == 2:
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test failed to release capture")
            return object()

        self.camera.capture_array.side_effect = capture
        worker = threading.Thread(target=lambda: results.append(
            self.service.point_to_cabinet_status(
                5, delay=0, timeout=0.15 if reason == "timeout" else 2,
                cancel_event=cancel,
            )
        ))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(any(speed != 0 for speed in self.x.speeds))
            if reason == "cancelled":
                cancel.set()
            elif reason == "emergency":
                self.assertTrue(self.service.emergency_stop())
            self.assertTrue(self.y.stopped.wait(1))
            self.assertTrue(worker.is_alive(), "camera remains blocked while physical motors stop")
            self.assertEqual(self.x.speeds[-1], 0)
            self.assertEqual(self.led.call_args.args, (False,))
            stop_index = len(self.x.speeds)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["status"], "timeout" if reason == "timeout" else "cancelled")
        self.assertTrue(all(speed == 0 for speed in self.x.speeds[stop_index:]))
        self.assert_cleaned()

    def test_watchdog_stops_live_motors_during_blocked_capture(self):
        self._blocked_capture("timeout")

    def test_cancel_monitor_stops_live_motors_during_blocked_capture(self):
        self._blocked_capture("cancelled")

    def test_emergency_stop_latches_against_late_capture(self):
        self._blocked_capture("emergency")

    def test_late_face_match_after_cancel_does_not_return_or_succeed(self):
        cancel = threading.Event()
        def evaluate(payload):
            cancel.set()
            return (object(), 90), None
        result = self.service.capture_face_until_matched(
            18, 18, evaluate, deadline=time.monotonic() + 2,
            cancel_event=cancel, warmup=0,
        )
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.x.angles, [(9, 0), (9, 0)])
        self.assertEqual(self.y.angles, [(9, 0), (9, 0)])
        self.assert_cleaned()

    def test_face_timeout_during_blocked_evaluator_stops_hardware(self):
        entered, release = threading.Event(), threading.Event()
        result = []
        def evaluate(payload):
            entered.set()
            release.wait(3)
            return (object(), 90), None
        worker = threading.Thread(target=lambda: result.append(
            self.service.capture_face_until_matched(
                9, 9, evaluate, deadline=time.monotonic() + 0.15, warmup=0,
            )
        ))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            # Initial move also stops the motors, so observe the watchdog LED.
            limit = time.monotonic() + 1
            while not self.led.called and time.monotonic() < limit:
                time.sleep(0.01)
            self.assertTrue(self.led.called)
            self.assertTrue(worker.is_alive())
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result[0]["status"], "timeout")
        self.assertEqual(len(self.x.angles), 1)
        self.assert_cleaned()

    def test_position_capture_preserves_legacy_calls_and_returns_to_origin(self):
        stages = []
        with patch.object(self.service, "capture_face", return_value="face-image") as capture:
            with patch.object(self.service, "move_to_face") as move:
                image = self.service.capture_face_at_position(9, 18, stages.append)
        self.assertEqual(image, "face-image")
        capture.assert_called_once_with()
        self.assertEqual(move.call_args_list, [call(9, 18), call(9, 18, reverse=True)])
        self.assertEqual(stages, ["face_position", "capture", "return_position"])
        self.assertIsNone(self.service._active_cancel)

    def test_position_capture_failure_keeps_legacy_return_gesture(self):
        with patch.object(self.service, "capture_face", side_effect=RuntimeError("capture failed")):
            with patch.object(self.service, "move_to_face") as move:
                with self.assertRaisesRegex(RuntimeError, "capture failed"):
                    self.service.capture_face_at_position(9, 18)
        self.assertEqual(move.call_args_list, [call(9, 18), call(9, 18, reverse=True)])
        self.assertEqual(self.led.call_args.args, (False,))

    def test_position_capture_queue_deadline_does_not_stop_current_owner(self):
        errors = []
        def capture():
            try:
                self.service.capture_face_at_position(9, 18, deadline=time.monotonic() + 0.05)
            except HardwareUnavailable as exc:
                errors.append(str(exc))
        with self.service._operation_lock:
            worker = threading.Thread(target=capture)
            worker.start()
            worker.join(1)
            self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn("超时", errors[0])
        self.camera_factory.assert_not_called()
        self.assertEqual(self.x.speeds, [])
        self.led.assert_not_called()

    def test_position_capture_pre_cancelled_or_expired_never_moves(self):
        cancel = threading.Event()
        cancel.set()
        for kwargs in ({"cancel_event": cancel}, {"timeout": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(HardwareUnavailable):
                self.service.capture_face_at_position(9, 18, **kwargs)
        self.assertEqual(self.x.angles, [])
        self.camera_factory.assert_not_called()
        self.led.assert_not_called()

    def test_position_capture_rejects_unbounded_budgets(self):
        for kwargs in ({"timeout": float("inf")}, {"deadline": float("nan")}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.service.capture_face_at_position(9, 18, **kwargs)
        self.assertEqual(self.x.angles, [])
        self.camera_factory.assert_not_called()

    def test_position_capture_cancel_in_stage_never_starts_capture_or_return(self):
        cancel = threading.Event()
        def stage(name):
            if name == "capture":
                cancel.set()
        with patch.object(self.service, "capture_face") as capture:
            with patch.object(self.service, "move_to_face") as move:
                with self.assertRaisesRegex(HardwareUnavailable, "取消"):
                    self.service.capture_face_at_position(9, 18, stage, cancel_event=cancel)
        capture.assert_not_called()
        move.assert_called_once_with(9, 18)

    def test_position_capture_cancel_in_return_stage_never_turns_back(self):
        cancel = threading.Event()
        def stage(name):
            if name == "return_position":
                cancel.set()
        with patch.object(self.service, "capture_face", return_value="late-image"):
            with patch.object(self.service, "move_to_face") as move:
                with self.assertRaisesRegex(HardwareUnavailable, "取消"):
                    self.service.capture_face_at_position(9, 18, stage, cancel_event=cancel)
        move.assert_called_once_with(9, 18)

    def test_position_capture_cancel_during_move_never_captures_or_returns(self):
        cancel = threading.Event()
        self.x.turnAngle = Mock(side_effect=lambda *args: cancel.set())
        with self.assertRaisesRegex(HardwareUnavailable, "取消"):
            self.service.capture_face_at_position(18, 18, cancel_event=cancel)
        self.x.turnAngle.assert_called_once_with(9, 0)
        self.assertEqual(self.y.angles, [])
        self.camera_factory.assert_not_called()
        self.assertEqual(self.x.speeds[-1], 0)

    def _blocked_position_capture(self, reason):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        cancel = threading.Event()
        errors, images = [], []
        def capture_array():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test failed to release camera")
            return object()
        def run():
            try:
                images.append(self.service.capture_face_at_position(
                    9, 18, timeout=0.15 if reason == "timeout" else 2, cancel_event=cancel,
                ))
            except HardwareUnavailable as exc:
                errors.append(str(exc))
        self.camera.capture_array.side_effect = capture_array
        self.led.side_effect = lambda enabled: stopped.set() if not enabled else None
        pil = types.ModuleType("PIL")
        pil.Image = Mock()
        pil.Image.fromarray.return_value.save.side_effect = lambda stream, **kw: stream.write(b"jpeg")
        with patch.dict(sys.modules, {"PIL": pil}), patch("hardware.time.sleep", return_value=None):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                if reason == "cancelled":
                    cancel.set()
                elif reason == "emergency":
                    self.assertTrue(self.service.emergency_stop())
                self.assertTrue(stopped.wait(1))
                self.assertTrue(worker.is_alive(), "monitor stops independently of camera")
                self.assertEqual(self.x.speeds[-1], 0)
                self.assertEqual(self.y.speeds[-1], 0)
                self.camera.close.assert_not_called()
            finally:
                release.set()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(images, [], "a late image must not be reported as success")
        self.assertEqual(len(errors), 1)
        self.assertIn("超时" if reason == "timeout" else "取消", errors[0])
        self.assertEqual(self.x.angles, [(9, 0)])
        self.assertEqual(self.y.angles, [(9, 0), (9, 0)])
        self.assert_cleaned()

    def test_position_capture_timeout_stops_independently_of_blocked_camera(self):
        self._blocked_position_capture("timeout")

    def test_position_capture_cancel_stops_independently_of_blocked_camera(self):
        self._blocked_position_capture("cancelled")

    def test_position_capture_emergency_stop_prevents_late_return_gesture(self):
        self._blocked_position_capture("emergency")


if __name__ == "__main__":
    unittest.main()

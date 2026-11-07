"""Timing tests for tag_align without camera, motors or OpenCV."""

from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import tag_align


class FakeMotor:
    def __init__(self):
        self.speeds = []

    def setSpeed(self, value):
        self.speeds.append(value)


class FakeCamera:
    def capture_array(self):
        return object()


class FakeDetector:
    def detect(self, frame):
        return []

    def estimate_tag_pose(self, *args, **kwargs):
        raise AssertionError("no tag should be estimated")


def fake_modules():
    cv2 = types.ModuleType("cv2")
    cv2.COLOR_RGB2GRAY = 0
    cv2.cvtColor = lambda frame, code: frame
    cv2.flip = lambda frame, code: frame

    simple_pid = types.ModuleType("simple_pid")

    class PID:
        def __init__(self, *args, **kwargs):
            self.output_limits = (0, 0)

        def __call__(self, value):
            return 0.0

    simple_pid.PID = PID

    agent = types.ModuleType("agent_yuntai")
    pan_tilt = types.ModuleType("agent_yuntai.panTilt")
    pan_tilt.x_motor = FakeMotor()
    pan_tilt.y_motor = FakeMotor()
    agent.panTilt = pan_tilt
    return {
        "cv2": cv2,
        "simple_pid": simple_pid,
        "agent_yuntai": agent,
        "agent_yuntai.panTilt": pan_tilt,
    }


class LostGraceTests(unittest.TestCase):
    def run_align(self, **kwargs):
        with patch.dict(sys.modules, fake_modules()):
            return tag_align.align_to_tag(
                14,
                FakeCamera(),
                FakeDetector(),
                deadline=time.monotonic() + 5,
                **kwargs,
            )

    def test_default_is_time_based_eight_seconds(self):
        started = time.monotonic()
        result = self.run_align(lost_timeout=0.4)
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "lost")
        self.assertGreaterEqual(elapsed, 0.4)
        self.assertLess(elapsed, 3)
        self.assertIn("秒", result["message"])
        self.assertGreaterEqual(result["lost_seconds"], 0.4)

    def test_legacy_frame_cap_still_supported(self):
        result = self.run_align(lost_timeout=None, max_lost_frames=3)
        self.assertEqual(result["status"], "lost")
        self.assertEqual(result["lost_frames"], 3)

    def test_timeout_still_wins_over_lost(self):
        with patch.dict(sys.modules, fake_modules()):
            result = tag_align.align_to_tag(
                14,
                FakeCamera(),
                FakeDetector(),
                deadline=time.monotonic() + 0.2,
                lost_timeout=5,
            )
        self.assertEqual(result["status"], "timeout")


class AlignmentSafetyTests(unittest.TestCase):
    def setUp(self):
        self.modules = fake_modules()
        # A nonzero output makes accidental post-cancel movement observable.
        self.modules["simple_pid"].PID = Mock(return_value=Mock(return_value=25))
        self.x = self.modules["agent_yuntai.panTilt"].x_motor
        self.y = self.modules["agent_yuntai.panTilt"].y_motor
        self.camera = Mock()
        self.detector = Mock()
        self.detector.detect.return_value = [{"id": 14}]
        self.detector.estimate_tag_pose.return_value = {"t": [[0.1], [0.1], [1]]}
        self.cancel = threading.Event()
        self.enterContext(patch.dict(sys.modules, self.modules))

    def align(self, **kwargs):
        return tag_align.align_to_tag(
            14, self.camera, self.detector, deadline=time.monotonic() + 2,
            cancel_event=self.cancel, **kwargs,
        )

    def assert_stopped_without_moving(self):
        self.assertEqual(self.x.speeds, [0])
        self.assertEqual(self.y.speeds, [0])

    def test_cancel_during_capture_prevents_detection_and_movement(self):
        self.camera.capture_array.side_effect = lambda: self.cancel.set()
        self.assertEqual(self.align()["status"], "cancelled")
        self.detector.detect.assert_not_called()
        self.assert_stopped_without_moving()

    def test_cancel_during_detection_prevents_pose_and_movement(self):
        def detect(frame):
            self.cancel.set()
            return [{"id": 14}]
        self.detector.detect.side_effect = detect
        self.assertEqual(self.align()["status"], "cancelled")
        self.detector.estimate_tag_pose.assert_not_called()
        self.assert_stopped_without_moving()

    def test_cancel_during_pose_prevents_movement(self):
        def pose(*args):
            self.cancel.set()
            return {"t": [[0.1], [0.1], [1]]}
        self.detector.estimate_tag_pose.side_effect = pose
        self.assertEqual(self.align()["status"], "cancelled")
        self.assert_stopped_without_moving()

    def test_stage_callback_cancelling_prevents_new_commands(self):
        def stage(name):
            if name == "align":
                self.cancel.set()
        self.assertEqual(self.align(on_stage=stage)["status"], "cancelled")
        self.assert_stopped_without_moving()

    def test_deadline_expiring_inside_capture_cannot_return_aligned(self):
        now = [10.0]
        def capture():
            now[0] = 12.0
            return object()
        self.camera.capture_array.side_effect = capture
        with patch("tag_align.time.monotonic", side_effect=lambda: now[0]):
            result = tag_align.align_to_tag(14, self.camera, self.detector, deadline=11)
        self.assertEqual(result["status"], "timeout")
        self.detector.detect.assert_not_called()
        self.assert_stopped_without_moving()

    def test_pid_initialization_failure_still_stops_both_axes(self):
        self.modules["simple_pid"].PID.side_effect = RuntimeError("PID failed")
        self.assertEqual(self.align()["status"], "hardware_error")
        self.assert_stopped_without_moving()

    def test_camera_exception_still_stops_both_axes(self):
        self.camera.capture_array.side_effect = RuntimeError("camera failed")
        self.assertEqual(self.align()["status"], "hardware_error")
        self.assert_stopped_without_moving()

    def test_motor_stop_failure_cannot_report_aligned(self):
        self.detector.estimate_tag_pose.return_value = {"t": [[0], [0], [1]]}
        self.x.setSpeed = Mock(side_effect=RuntimeError("stop failed"))
        result = self.align()
        self.assertEqual(result["status"], "hardware_error")
        self.assertFalse(result["stop_confirmed"])
        self.assertEqual(self.y.speeds[-1], 0)

    def test_loss_stops_previous_motion_before_next_capture(self):
        self.detector.detect.side_effect = [[{"id": 14}], [], []]
        frame = [0]
        def capture():
            frame[0] += 1
            if frame[0] == 3:
                self.assertEqual(self.x.speeds[-1], 0)
                self.assertEqual(self.y.speeds[-1], 0)
            return object()
        self.camera.capture_array.side_effect = capture
        result = self.align(lost_timeout=None, max_lost_frames=2)
        self.assertEqual(result["status"], "lost")
        self.assertTrue(any(speed != 0 for speed in self.x.speeds))
        self.assertEqual(self.x.speeds[-1], 0)

    def test_nonfinite_pose_never_drives_motors(self):
        self.detector.estimate_tag_pose.return_value = {"t": [[float("nan")], [0], [1]]}
        self.assertEqual(self.align()["status"], "hardware_error")
        self.assert_stopped_without_moving()


if __name__ == "__main__":
    unittest.main()

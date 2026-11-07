"""Tests for the authenticated AprilTag hardware test endpoints.

Hardware is always mocked: these tests never move a motor, open a camera or
call Baidu.  They assert that a tag test never creates borrow records.
"""

from __future__ import annotations

import shutil
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import parts  # noqa: E402
from app import create_app
from core_models import User
from extensions import db
from parts_models import Cabinet, CabinetComponent, PartApplication
from apriltag_test import TagTestJob

DEVICE_HEADERS = {"X-Device-Token": "device-test-token"}


class TagTestApiTests(unittest.TestCase):
    def test_finished_watchdog_cannot_stop_next_job(self):
        old = TagTestJob('old', 1)
        old.succeed({'success': True})
        new = TagTestJob('new', 2)
        with parts.device_borrow_lock, parts._tag_job_ownership(new):
            parts._tag_test_watchdog(old, 10)
            parts._tag_test_force_stop(old)
            self.hardware.emergency_stop.assert_not_called()

    def test_queued_job_cannot_stop_active_job(self):
        active = TagTestJob('active', 1)
        queued = TagTestJob('queued', 2)
        with parts.device_borrow_lock, parts._tag_job_ownership(active):
            parts._tag_test_force_stop(queued)
            self.hardware.emergency_stop.assert_not_called()
            self.assertIsNone(queued.snapshot()['stop_confirmed'])

    def test_failed_emergency_stop_is_reported(self):
        job = TagTestJob('failed-stop', 1)
        self.hardware.emergency_stop.return_value = False
        with parts.device_borrow_lock, parts._tag_job_ownership(job):
            parts._tag_test_force_stop(job)
            self.assertFalse(job.snapshot()['stop_confirmed'])
            self.assertIn('未确认', job.snapshot()['error'])

    def test_worker_exit_cancels_all_timers(self):
        job = TagTestJob('timers', 1)
        with patch('apriltag_test.threading.Timer') as timer:
            with parts.device_borrow_lock, parts._tag_job_ownership(job):
                job.schedule_timer(90, lambda: None, ())
            timer.return_value.cancel.assert_called_once()
            job.schedule_timer(90, lambda: None, ())
            self.assertEqual(timer.call_count, 1)

    def test_cancel_finished_job_does_not_stop_new_owner(self):
        old, _ = parts.tag_test_registry.create('finished-cancel', 1)
        old.succeed({'success': True})
        active = TagTestJob('new-owner', 2)
        with parts.device_borrow_lock, parts._tag_job_ownership(active):
            response = self.client.post(
                '/parts/api/device/tag-test/finished-cancel/cancel',
                headers=DEVICE_HEADERS,
            )
            self.assertEqual(response.status_code, 200)
            self.hardware.emergency_stop.assert_not_called()

    def setUp(self):
        base = Path(__file__).resolve().parent / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.temp_dir = base / ("case-" + str(time.time_ns()))
        self.temp_dir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, str(self.temp_dir), True)
        self.hardware = self.enterContext(patch("parts.hardware"))
        def fake_capture(pan, tilt, evaluator, deadline=None, cancel_event=None, on_stage=None, **kwargs):
            if on_stage is not None:
                on_stage("face_position")
                on_stage("face_verify", "第 2 次人脸识别")
                on_stage("return_position")
            return {
                "status": "matched",
                "matched": (SimpleNamespace(username="alice", id=1), 91.2),
                "attempts": 2,
                "message": "",
            }

        self.hardware.capture_face_until_matched.side_effect = fake_capture
        self.hardware.point_to_cabinet_status.return_value = {
            "status": "aligned",
            "message": "已对准目标标签",
            "tag_id": 11,
            "yaw": 0.1,
            "pitch": 0.2,
            "iterations": 5,
        }
        self.face_result = self.enterContext(patch("parts._face_result"))
        self.face_result.return_value = (
            (SimpleNamespace(username="alice", id=1), 91.2),
            None,
        )

        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "UPLOAD_FOLDER": str(self.temp_dir / "uploads"),
                "PARTS_DEVICE_TOKEN": "device-test-token",
                "PARTS_TAG_TEST_ENABLED": True,
            }
        )
        self.client = self.app.test_client()
        with self.app.app_context():
            db.session.add(
                Cabinet(
                    cabinet_number="11",
                    equipment_name="零件库存",
                    description="常用零件",
                    components=[
                        CabinetComponent(
                            component_name="AO3401A", quantity=20, unit="个", notes="箱号 A1"
                        )
                    ],
                )
            )
            db.session.add(Cabinet(cabinet_number="20", equipment_name="空柜"))
            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def wait_for(self, request_id, timeout=10.0):
        deadline = time.monotonic() + timeout
        snapshot = None
        while time.monotonic() < deadline:
            response = self.client.get(
                f"/parts/api/device/tag-test/{request_id}", headers=DEVICE_HEADERS
            )
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            snapshot = response.get_json()
            if snapshot.get("terminal"):
                return snapshot
            time.sleep(0.05)
        self.fail(f"job did not finish: {snapshot}")

    def start(self, tag_id=11, request_id="tagtest1", timeout_seconds=20):
        return self.client.post(
            "/parts/api/device/tag-test",
            json={"tag_id": tag_id, "request_id": request_id, "timeout_seconds": timeout_seconds},
            headers=DEVICE_HEADERS,
        )

    def test_cabinet_list_requires_token_and_includes_empty_cabinets(self):
        self.assertEqual(self.client.get("/parts/api/device/cabinets").status_code, 401)
        response = self.client.get(
            "/parts/api/device/cabinets", headers={"X-Device-Token": "wrong"}
        )
        self.assertEqual(response.status_code, 401)

        response = self.client.get("/parts/api/device/cabinets", headers=DEVICE_HEADERS)
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["cabinet_count"], 2)
        self.assertEqual(payload["component_count"], 1)
        numbers = [item["cabinet_number"] for item in payload["cabinets"]]
        self.assertEqual(numbers, ["11", "20"])
        empty = [item for item in payload["cabinets"] if item["cabinet_number"] == "20"][0]
        self.assertEqual(empty["components"], [])
        self.assertIn("updated_at", empty)

    def test_invalid_tag_ids_are_rejected(self):
        for value in (587, -1, "abc"):
            with self.subTest(value=value):
                response = self.start(tag_id=value)
                self.assertEqual(response.status_code, 400)
                self.assertIn("Tag", response.get_json()["message"])

    def test_successful_test_reports_stages_and_creates_no_borrow_record(self):
        response = self.start()
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertTrue(payload["created"])
        # The worker may finish before this response is serialised.
        self.assertIn(payload["state"], {"running", "done"})

        snapshot = self.wait_for("tagtest1")
        self.assertEqual(snapshot["state"], "done")
        result = snapshot["result"]
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "aligned")
        self.assertEqual(result["tag_id"], 11)
        self.assertEqual(result["username"], "alice")
        self.assertEqual(result['face_id'], '1')
        self.assertEqual(result['user_id'], 1)
        self.assertEqual(snapshot['face_id'], '1')
        self.assertEqual(result["face_attempts"], 2)
        self.assertFalse(result["borrow_record_created"])
        stage_names = [stage["name"] for stage in snapshot["stages"]]
        self.assertIn("face_verify", stage_names)
        self.assertIn("done", stage_names)
        self.hardware.point_to_cabinet_status.assert_called_once()
        self.hardware.emergency_stop.assert_not_called()
        _, kwargs = self.hardware.point_to_cabinet_status.call_args
        self.assertEqual(kwargs["timeout"], 20)
        with self.app.app_context():
            self.assertEqual(PartApplication.query.count(), 0)

    def test_face_failure_stops_before_pointing(self):
        def fake_timeout(pan, tilt, evaluator, deadline=None, cancel_event=None, on_stage=None, **kwargs):
            if on_stage is not None:
                on_stage("face_position")
                on_stage("face_verify", "第 5 次人脸识别")
                on_stage("return_position")
            return {
                "status": "timeout",
                "attempts": 5,
                "message": "人脸识别超时，未能在限定时间内识别到已注册人脸",
            }

        self.hardware.capture_face_until_matched.side_effect = fake_timeout
        response = self.start(request_id="facetest1")
        self.assertEqual(response.status_code, 202)
        snapshot = self.wait_for("facetest1")
        self.assertEqual(snapshot["state"], "failed")
        self.assertEqual(snapshot["error_code"], "face_timeout")
        self.assertIn("人脸", snapshot["error"])
        statuses = {stage["name"]: stage["status"] for stage in snapshot["stages"]}
        self.assertEqual(statuses.get("face_verify"), "failed")
        self.assertEqual(statuses.get("return_position"), "ok")
        self.assertEqual(snapshot["face_attempts"], 5)
        self.hardware.point_to_cabinet_status.assert_not_called()
        with self.app.app_context():
            self.assertEqual(PartApplication.query.count(), 0)

    def test_cancel_stops_the_waiting_job(self):
        def blocked_point(*args, **kwargs):
            cancel_event = kwargs["cancel_event"]
            for _ in range(100):
                if cancel_event.is_set():
                    return {"status": "cancelled", "message": "用户取消，已停止对准"}
                time.sleep(0.05)
            return {"status": "timeout", "message": "测试超时"}

        self.hardware.point_to_cabinet_status.side_effect = blocked_point
        response = self.start(request_id="canceltest1")
        self.assertEqual(response.status_code, 202)
        time.sleep(0.2)
        cancel = self.client.post(
            "/parts/api/device/tag-test/canceltest1/cancel", headers=DEVICE_HEADERS
        )
        self.assertEqual(cancel.status_code, 200)
        snapshot = self.wait_for("canceltest1")
        self.assertEqual(snapshot["state"], "cancelled")
        self.hardware.emergency_stop.assert_called()

    def test_duplicate_request_id_does_not_rerun_hardware(self):
        self.start(request_id="duptest1")
        self.wait_for("duptest1")
        response = self.start(request_id="duptest1")
        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.get_json()["created"])
        self.assertEqual(self.hardware.capture_face_until_matched.call_count, 1)

    def test_disabled_endpoint_returns_503(self):
        self.app.config["PARTS_TAG_TEST_ENABLED"] = False
        response = self.start(request_id="disabled1")
        self.assertEqual(response.status_code, 503)

    def test_legacy_face_evaluator_keeps_old_baidu_call_and_threshold(self):
        self.app.config["BAIDU_GROUP_ID"] = "equipment_users"
        self.hardware.search_face_raw.return_value = {
            "error_code": 0,
            "result": {"user_list": [{"score": "88.5", "user_id": "7"}]},
        }
        with self.app.app_context(), patch("parts.db.session.get") as get_user:
            get_user.return_value = SimpleNamespace(username="u")
            matched, message = parts._face_result_legacy("image")
            self.assertIsNone(message)
            self.assertEqual(matched[1], 88.5)
            self.assertEqual(matched.face_id, '7')
            self.hardware.search_face_raw.assert_called_once()

            self.hardware.search_face_raw.return_value = {
                "error_code": 0,
                "result": {"user_list": [{"score": "60", "user_id": "7"}]},
            }
            matched, message = parts._face_result_legacy("image")
            self.assertIsNone(matched)
            self.assertIn("匹配度过低", message)

    def test_unknown_job_returns_404(self):
        response = self.client.get("/parts/api/device/tag-test/nope", headers=DEVICE_HEADERS)
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

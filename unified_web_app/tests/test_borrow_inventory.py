"""Borrow/return transactions against temporary SQLite only; hardware is mocked."""
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import parts
from app import create_app
from apriltag_test import JobRegistry
from core_models import User
from extensions import db
from parts_models import Cabinet, CabinetComponent, InventoryMovement, PartApplication
from sqlalchemy.exc import IntegrityError


DEVICE_HEADERS = {"X-Device-Token": "inventory-test-token"}
CSRF_HEADERS = {"X-CSRF-Token": "inventory-csrf"}


class BorrowInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / "inventory.sqlite"
        self.config = {
            "TESTING": True, "SECRET_KEY": "inventory-test",
            "SQLALCHEMY_DATABASE_URI": "sqlite:///" + self.db_path.as_posix(),
            "PARTS_DEVICE_TOKEN": "inventory-test-token",
            "PARTS_SKIP_FACE_VERIFICATION": True,
            "PARTS_FALLBACK_APPLICANT_EMAIL": "owner@example.test",
            "UPLOAD_FOLDER": str(Path(self.temp.name) / "uploads"),
        }
        self.hardware = self.enterContext(patch("parts.hardware"))
        self.hardware.status.side_effect = AssertionError("No real hardware")
        self.point_patch = patch("parts._point_after_application")
        self.point_patch.start()
        self.addCleanup(self.point_patch.stop)
        self.app = create_app(self.config)
        self.addCleanup(self.dispose)
        with self.app.app_context():
            db.session.add_all([
                User(id=1, username="owner", email="owner@example.test", password_hash="unused"),
                User(id=2, username="other", email="other@example.test", password_hash="unused"),
                User(id=3, username="admin", email="admin@example.test", password_hash="unused", role="admin"),
                Cabinet(id=1, cabinet_number="11", equipment_name="零件", components=[
                    CabinetComponent(id=1, component_name="螺丝", quantity=5),
                    CabinetComponent(id=2, component_name="螺丝", quantity=8),
                ]),
            ])
            db.session.commit()
        self.client = self.login_client(1)

    def dispose(self):
        with self.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def login_client(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
            session["_csrf_token"] = "inventory-csrf"
        return client

    def borrow(self, request_id="borrow-1", quantity=2, component_id=1, source="xiaozhi-mcp"):
        return self.client.post("/parts/api/device/borrow", headers=DEVICE_HEADERS, json={
            "component_id": component_id, "quantity": quantity, "request_id": request_id, "source": source,
        })

    def return_loan(self, record_id, client=None):
        return (client or self.client).post(f"/parts/api/applications/{record_id}/return", headers=CSRF_HEADERS, json={})

    def snapshot(self):
        with self.app.app_context():
            return ([db.session.get(CabinetComponent, item).quantity for item in (1, 2)],
                    PartApplication.query.count(), InventoryMovement.query.count())

    def test_borrow_and_same_request_replay_debit_exact_id_once(self):
        first = self.borrow()
        self.assertEqual(first.status_code, 201)
        replay = self.borrow()
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json["duplicate"])
        self.assertEqual(replay.json["record_id"], first.json["record_id"])
        self.assertEqual(self.snapshot(), ([3, 8], 1, 1))
        with self.app.app_context():
            movement = InventoryMovement.query.one()
            self.assertEqual((movement.component_id, movement.delta, movement.quantity_after), (1, -2, 3))

    def test_changed_duplicate_parameters_conflict_without_any_debit(self):
        self.assertEqual(self.borrow().status_code, 201)
        for changes in ({"quantity": 3}, {"component_id": 2}, {"source": "different"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.borrow(**changes).status_code, 409)
        self.assertEqual(self.snapshot(), ([3, 8], 1, 1))

    def test_insufficient_stock_and_invalid_quantities_never_create_records(self):
        self.assertEqual(self.borrow(quantity=6).status_code, 409)
        for quantity in (True, 1.5, "1.0", 0, -1, 100001):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.borrow(quantity=quantity).status_code, 400)
        self.assertEqual(self.snapshot(), ([5, 8], 0, 0))

    def test_database_unique_request_id_and_nullable_legacy_ids(self):
        self.borrow()
        with self.app.app_context():
            db.session.add(PartApplication(user_id=1, equipment_name="x", purpose="duplicate", request_id="borrow-1"))
            with self.assertRaises(IntegrityError):
                db.session.commit()
            db.session.rollback()
            for _ in range(2):
                db.session.add(PartApplication(user_id=1, equipment_name="x", purpose="legacy"))
            db.session.commit()
        self.assertEqual(self.snapshot(), ([3, 8], 3, 1))

    def test_commit_failure_rolls_back_record_debit_and_ledger(self):
        with patch.object(db.session, "commit", side_effect=RuntimeError("injected")), self.assertLogs("parts", "ERROR"):
            self.assertEqual(self.borrow().status_code, 500)
        self.assertEqual(self.snapshot(), ([5, 8], 0, 0))

    def test_web_multiple_item_failure_rolls_back_every_debit(self):
        response = self.client.post("/parts/applications", headers=CSRF_HEADERS, data={
            "component_ids[]": ["1", "2"], "quantities[]": ["2", "9"], "purpose": "test",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), ([5, 8], 0, 0))

    def test_web_explicit_ids_debit_and_expose_return_action(self):
        self.client.post("/parts/applications", headers=CSRF_HEADERS, data={
            "component_ids[]": ["2"], "equipment_names[]": ["do not trust this name"],
            "quantities[]": ["3"], "purpose": "test",
        })
        self.assertEqual(self.snapshot(), ([5, 5], 1, 1))
        page = self.client.get("/parts/applications").get_data(as_text=True)
        self.assertIn("归还入库", page)
        with self.app.app_context():
            self.assertEqual(PartApplication.query.one().equipment_items[0]["name"], "螺丝")

    def test_free_text_paths_never_guess_component_from_name(self):
        # Even exact names/cabinet matches must not authorize inventory changes.
        self.client.post("/parts/applications", headers=CSRF_HEADERS, data={
            "equipment_names[]": ["螺丝"], "quantities[]": ["2"], "purpose": "legacy",
        })
        response = self.client.post("/parts/api/face/apply", headers=DEVICE_HEADERS,
                                    json={"user_id": 1, "equipment_name": "螺丝", "quantity": 2})
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json["inventory_managed"])
        with self.app.app_context():
            user = db.session.get(User, 1)
            with patch("parts._face_result", return_value=((user, 99), None)):
                self.assertEqual(self.client.post("/parts/api/face/verify", headers=DEVICE_HEADERS,
                                                 json={"equipment_name": "螺丝"}).status_code, 200)
            self.hardware.capture_face_at_position.return_value = "fake-image"
            with patch("parts._face_result", return_value=((user, 99), None)):
                parts._command_worker(self.app, "螺丝")
            ids = [item.id for item in PartApplication.query.all()]
        self.assertEqual(self.snapshot(), ([5, 8], 4, 0))
        for record_id in ids:
            self.assertEqual(self.return_loan(record_id).status_code, 409)
        self.assertIn("未关联库存", self.client.get("/parts/applications").get_data(as_text=True))

    def test_return_once_keeps_original_history_and_replay_does_not_reborrow(self):
        record_id = self.borrow().json["record_id"]
        self.assertEqual(self.return_loan(record_id).status_code, 200)
        self.assertEqual(self.return_loan(record_id).status_code, 409)
        self.assertEqual(self.snapshot(), ([5, 8], 1, 2))
        replay = self.borrow()
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json["status"], "returned")
        with self.app.app_context():
            application = db.session.get(PartApplication, record_id)
            self.assertEqual(application.status, "approved")
            self.assertIsNotNone(application.returned_at)
            self.assertEqual(application.returned_by_id, 1)

    def test_return_and_status_permissions_and_csrf(self):
        record_id = self.borrow().json["record_id"]
        other = self.login_client(2)
        self.assertEqual(self.return_loan(record_id, other).status_code, 403)
        self.assertEqual(other.get(f"/parts/api/applications/{record_id}").status_code, 403)
        self.assertEqual(self.client.post(f"/parts/api/applications/{record_id}/return", json={}).status_code, 400)
        device = self.app.test_client()
        self.assertEqual(device.get("/parts/api/device/borrow/borrow-1").status_code, 401)
        self.assertEqual(device.get("/parts/api/device/borrow/borrow-1", headers=DEVICE_HEADERS).status_code, 200)
        self.assertEqual(self.return_loan(record_id, self.login_client(3)).status_code, 200)
        with self.app.app_context():
            self.assertEqual(db.session.get(PartApplication, record_id).returned_by_id, 3)

    def test_failed_return_rolls_back_claim_and_all_stock(self):
        record_id = self.borrow().json["record_id"]
        with patch.object(db.session, "commit", side_effect=RuntimeError("injected")), self.assertLogs("parts", "ERROR"):
            self.assertEqual(self.return_loan(record_id).status_code, 500)
        self.assertEqual(self.snapshot(), ([3, 8], 1, 1))
        self.assertEqual(self.return_loan(record_id).status_code, 200)

    def test_edit_replenishes_in_place_and_preserves_borrow_ledger(self):
        record_id = self.borrow().json["record_id"]
        response = self.login_client(3).post("/parts/admin/cabinets/1/edit", headers=CSRF_HEADERS, data={
            "cabinet_number": "11", "equipment_name": "零件",
            "component_ids[]": ["1", "2"], "component_original_quantities[]": ["3", "8"],
            "component_names[]": ["螺丝", "螺丝"], "component_quantities[]": ["10", "8"],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), ([10, 8], 1, 1))
        self.assertEqual(self.return_loan(record_id).status_code, 200)
        self.assertEqual(self.snapshot(), ([12, 8], 1, 2))

    def test_stale_admin_edit_cannot_overwrite_new_borrow(self):
        self.borrow()
        response = self.login_client(3).post("/parts/admin/cabinets/1/edit", headers=CSRF_HEADERS, data={
            "cabinet_number": "12", "equipment_name": "changed",
            "component_ids[]": ["1", "2"], "component_original_quantities[]": ["5", "8"],
            "component_names[]": ["螺丝", "螺丝"], "component_quantities[]": ["10", "8"],
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.snapshot(), ([3, 8], 1, 1))
        with self.app.app_context():
            self.assertEqual(db.session.get(Cabinet, 1).cabinet_number, "11")

    def test_cabinet_rebuild_single_and_batch_delete_preserve_ledger(self):
        record_id = self.borrow().json["record_id"]
        admin = self.login_client(3)
        for path, data in (
            ("/parts/admin/cabinets/1/edit", {"cabinet_number": "12", "equipment_name": "changed", "component_names[]": ["new"]}),
            ("/parts/admin/cabinets/1/delete", {}),
            ("/parts/admin/cabinets/batch-delete", {"ids[]": ["1"]}),
        ):
            with self.subTest(path=path):
                self.assertEqual(admin.post(path, headers=CSRF_HEADERS, data=data).status_code, 302)
                self.assertEqual(self.snapshot(), ([3, 8], 1, 1))
                with self.app.app_context():
                    self.assertEqual(db.session.get(Cabinet, 1).cabinet_number, "11")
        self.assertEqual(self.return_loan(record_id).status_code, 200)
        admin.post("/parts/admin/cabinets/1/delete", headers=CSRF_HEADERS)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(Cabinet, 1))

    def concurrent_borrows(self, request_ids):
        barrier = threading.Barrier(2)
        original = parts._create_application

        def create_together(*args, **kwargs):
            barrier.wait(timeout=5)
            return original(*args, **kwargs)

        def run(request_id):
            # Intentionally bypass the per-process device lock. This exercises
            # database arbitration used by separate workers/processes.
            with self.app.test_request_context():
                return parts._run_device_borrow(1, 4, request_id, "test")

        with patch("parts._create_application", side_effect=create_together), ThreadPoolExecutor(max_workers=2) as pool:
            return list(pool.map(run, request_ids))

    def test_concurrent_borrows_cannot_oversell(self):
        outcomes = self.concurrent_borrows(["parallel-a", "parallel-b"])
        self.assertEqual(sorted(code for _, code in outcomes), [201, 409])
        self.assertEqual(self.snapshot(), ([1, 8], 1, 1))

    def test_concurrent_same_request_is_one_transaction(self):
        outcomes = self.concurrent_borrows(["parallel-same", "parallel-same"])
        self.assertEqual(sorted(code for _, code in outcomes), [200, 201])
        self.assertEqual(self.snapshot(), ([1, 8], 1, 1))

    def test_concurrent_returns_only_restore_stock_once(self):
        record_id = self.borrow().json["record_id"]
        barrier = threading.Barrier(2)

        def run(_):
            client = self.login_client(1)
            barrier.wait(timeout=5)
            return self.return_loan(record_id, client).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(run, range(2))), [200, 409])
        self.assertEqual(self.snapshot(), ([5, 8], 1, 2))

    def test_guidance_worker_context_persists_timeout_without_stock_rollback(self):
        self.app.config["PARTS_SKIP_FACE_VERIFICATION"] = False
        # Build the explicit inventory loan without performing face operations.
        with self.app.app_context():
            application = parts._create_application(1, [], "test", component_quantities=[(1, 2)])
            db.session.commit()
            record_id = application.id
        self.hardware.point_to_cabinet_status.return_value = {"status": "timeout", "message": "测试超时"}
        parts._point_to_cabinet_safely(self.app, record_id, "11", 0, 2)
        self.hardware.point_to_cabinet_status.assert_called_once_with(11, delay=0, timeout=2)
        status = self.client.get(f"/parts/api/applications/{record_id}")
        self.assertEqual(status.json["guidance_status"], "timeout")
        self.assertEqual(status.json["guidance_status_label"], "已超时")
        self.assertEqual(self.snapshot(), ([3, 8], 1, 1))
        self.assertIn("已超时", self.client.get("/parts/applications").get_data(as_text=True))

    def test_return_cancels_queued_guidance_before_any_hardware_call(self):
        with self.app.app_context():
            self.app.config["PARTS_SKIP_FACE_VERIFICATION"] = False
            application = parts._create_application(1, [], "test", component_quantities=[(1, 2)])
            db.session.commit()
            record_id = application.id
        self.assertEqual(self.return_loan(record_id).status_code, 200)
        parts._point_to_cabinet_safely(self.app, record_id, "11", 0, 2)
        self.hardware.point_to_cabinet_status.assert_not_called()
        status = self.client.get(f"/parts/api/applications/{record_id}").json
        self.assertEqual(status["guidance_status"], "cancelled")
        self.assertEqual(self.snapshot(), ([5, 8], 1, 2))

    def test_restart_marks_queued_running_interrupted_and_is_idempotent(self):
        with self.app.app_context():
            for state in ("queued", "running", "aligned"):
                db.session.add(PartApplication(user_id=1, equipment_name="legacy", purpose="old", guidance_status=state))
            db.session.commit()
        for _ in range(2):
            restarted = create_app(self.config)
            with restarted.app_context():
                self.assertEqual([row.guidance_status for row in PartApplication.query.order_by(PartApplication.id)],
                                 ["interrupted", "interrupted", "aligned"])
                db.session.remove()
                db.engine.dispose()
        self.assertEqual(self.snapshot(), ([5, 8], 3, 0))

    def test_old_sqlite_schema_migrates_twice_without_touching_records_or_stock(self):
        self.dispose()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("DROP TABLE part_applications")
            connection.execute("""CREATE TABLE part_applications (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, equipment_name TEXT NOT NULL,
                quantity TEXT NOT NULL DEFAULT '1', photos TEXT NOT NULL DEFAULT '[]', purpose TEXT NOT NULL,
                expected_date DATE, status VARCHAR(20) NOT NULL DEFAULT 'approved', reject_reason TEXT,
                cabinet_id INTEGER, reviewed_by_id INTEGER, reviewed_at DATETIME,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
                CHECK(status IN ('pending', 'approved', 'rejected', 'cancelled')))""")
            for row_id, state in enumerate(("approved", "pending", "rejected", "cancelled"), 1):
                connection.execute("INSERT INTO part_applications (id,user_id,equipment_name,purpose,status,cabinet_id,created_at,updated_at) VALUES (?,1,'螺丝',?,?,1,'2020-01-01','2020-01-01')",
                                   (row_id, "[MCP 借用请求:old-request]", state))
        for _ in range(2):
            restarted = create_app(self.config)
            with restarted.app_context():
                rows = PartApplication.query.order_by(PartApplication.id).all()
                self.assertEqual([row.status for row in rows], ["approved", "pending", "rejected", "cancelled"])
                self.assertEqual([row.request_id for row in rows], ["old-request", None, None, None])
                self.assertTrue(all(not row.inventory_managed and row.returned_at is None for row in rows))
                self.assertEqual([row.purpose for row in rows], ["[MCP 借用请求:old-request]"] * 4)
                db.session.remove()
                db.engine.dispose()
        self.assertEqual(self.borrow(request_id="old-request").status_code, 409)
        for record_id in range(1, 5):
            self.assertEqual(self.return_loan(record_id).status_code, 409)
        self.assertEqual(self.snapshot(), ([5, 8], 4, 0))

    def test_tag_capacity_conflict_and_failed_start_release_reservation(self):
        registry = JobRegistry(max_jobs=1)
        registry.create("occupied", 1)
        with patch("parts.tag_test_registry", registry):
            conflict = self.client.post("/parts/api/device/tag-test", headers=DEVICE_HEADERS,
                                        json={"request_id": "occupied", "tag_id": 2})
            self.assertEqual(conflict.status_code, 409)
            full = self.client.post("/parts/api/device/tag-test", headers=DEVICE_HEADERS,
                                    json={"request_id": "new", "tag_id": 1})
            self.assertEqual(full.status_code, 429)
            self.assertEqual(full.headers["Retry-After"], "5")
        registry = JobRegistry(max_jobs=1)
        with patch("parts.tag_test_registry", registry), patch("parts.threading.Thread.start", side_effect=RuntimeError("injected")), self.assertLogs("parts", "ERROR"):
            response = self.client.post("/parts/api/device/tag-test", headers=DEVICE_HEADERS,
                                        json={"request_id": "failed", "tag_id": 1})
            self.assertEqual(response.status_code, 503)
            self.assertTrue(registry.get("failed").snapshot()["worker_finished"])
            self.assertTrue(registry.create("next", 2)[1])


if __name__ == "__main__":
    unittest.main()

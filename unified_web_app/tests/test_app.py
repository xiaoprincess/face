import tempfile
import unittest
import base64
from io import BytesIO
from pathlib import Path
from unittest.mock import ANY, patch

from flask import render_template_string

from app import create_app
from community_models import BlogPost, Notification
from core_models import User
from extensions import db, socketio
from parts_models import Cabinet, PartApplication


class IsolatedAppTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.hardware = self.enterContext(patch("parts.hardware"))
        for method in ("search_face", "register_face", "capture_face_at_position", "point_to_cabinet"):
            getattr(self.hardware, method).side_effect = AssertionError("Unexpected hardware call")
        self.hardware.status.return_value = {
            "available": False,
            "face": {"available": False, "message": "test face unavailable"},
            "camera": {"available": False, "message": "test camera unavailable"},
        }
        self.enterContext(patch("parts._point_after_application"))
        self.enterContext(patch("parts.threading.Thread.start", side_effect=AssertionError("Unexpected worker")))
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "UPLOAD_FOLDER": str(Path(self.temp_dir.name) / "uploads"),
                "PARTS_DEVICE_TOKEN": "device-test-token",
                "PARTS_SKIP_FACE_VERIFICATION": False,
            }
        )

        @self.app.get("/_test/csrf")
        def token_page():
            return render_template_string("{{ csrf_token() }}")

        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def csrf_token(self, client=None):
        return (client or self.client).get("/_test/csrf").get_data(as_text=True)

    def csrf_post(self, path, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers["X-CSRF-Token"] = self.csrf_token()
        return self.client.post(path, headers=headers, **kwargs)

    def register_admin(self):
        response = self.csrf_post(
            "/auth/register",
            data={
                "username": "admin",
                "email": "admin@example.test",
                "password": "password123",
                "confirm_password": "password123",
            },
        )
        self.assertEqual(response.status_code, 302)


class UnifiedAppTestCase(IsolatedAppTestCase):
    def test_public_health_and_protected_dashboard(self):
        self.assertEqual(self.client.get("/health").get_json(), {"status": "ok"})
        response = self.client.get("/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/login", response.headers["Location"])

    def test_unified_admin_parts_and_community_flow(self):
        self.register_admin()

        for path in ("/dashboard", "/parts/admin", "/community/", "/community/cloud"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

        response = self.csrf_post(
            "/parts/admin/cabinets",
            data={
                "cabinet_number": "A01",
                "equipment_name": "螺丝",
                "component_names[]": ["M2 螺丝"],
                "component_quantities[]": ["50"],
                "component_units[]": ["个"],
            },
        )
        self.assertEqual(response.status_code, 302)

        response = self.csrf_post(
            "/parts/applications",
            data={
                "equipment_names[]": ["M2 螺丝"],
                "quantities[]": ["4"],
                "purpose": "装配测试",
            },
        )
        self.assertEqual(response.status_code, 302)

        response = self.csrf_post(
            "/community/posts",
            data={"title": "零件使用交流", "content": "这是一条合并站点测试文章。"},
        )
        self.assertEqual(response.status_code, 302)

        with self.app.app_context():
            self.assertTrue(User.query.one().is_admin)
            self.assertEqual(Cabinet.query.count(), 1)
            application = PartApplication.query.one()
            self.assertEqual(application.cabinet.cabinet_number, "A01")
            self.assertEqual(BlogPost.query.count(), 1)

    def test_legacy_device_routes_and_open_redirect_protection(self):
        self.register_admin()

        self.assertEqual(self.client.get("/command?cmd=M2").status_code, 401)
        self.assertEqual(
            self.client.get("/command?cmd=M2", headers={"X-Device-Token": "device-test-token"}).status_code,
            503,
        )
        self.assertEqual(
            self.client.post(
                "/api/face_apply",
                json={"user_id": 1, "equipment_name": "M2"},
            ).status_code,
            401,
        )

        self.csrf_post("/auth/logout")
        response = self.csrf_post(
            "/auth/login?next=https://example.com",
            data={"identity": "admin", "password": "password123"},
        )
        self.assertEqual(response.headers["Location"], "/dashboard")

    def test_realtime_connection_and_private_upload_storage(self):
        self.register_admin()
        realtime_client = socketio.test_client(
            self.app, flask_test_client=self.client
        )
        self.assertTrue(realtime_client.is_connected())
        realtime_client.disconnect()

        response = self.csrf_post(
            "/parts/applications",
            data={
                "equipment_names[]": "测试零件",
                "quantities[]": "1",
                "purpose": "验证私有上传目录",
                "equipment_photos[]": (BytesIO(b"test-image"), "part.jpg"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            filename = PartApplication.query.one().photo_list[0]
        self.assertTrue((Path(self.app.config["UPLOAD_FOLDER"]) / filename).is_file())

        self.csrf_post("/auth/logout")
        self.assertEqual(self.client.get(f"/static/uploads/{filename}").status_code, 404)
        self.assertEqual(self.client.get(f"/parts/uploads/{filename}").status_code, 302)

    def test_opening_notifications_marks_them_read_and_clears_badge_count(self):
        self.register_admin()
        with self.app.app_context():
            db.session.add(
                Notification(
                    user_id=1,
                    sender_id=None,
                    type="mention",
                    content="测试通知",
                    link="/community/",
                )
            )
            db.session.commit()

        before = self.client.get("/community/api/notifications/unread-count")
        self.assertEqual(before.get_json(), {"count": 1})
        response = self.client.get("/community/notifications")
        self.assertEqual(response.status_code, 200)
        after = self.client.get("/community/api/notifications/unread-count")
        self.assertEqual(after.get_json(), {"count": 0})
        with self.app.app_context():
            self.assertTrue(Notification.query.one().is_read)

    def test_signup_can_capture_and_register_face(self):
        self.hardware.register_face.side_effect = None
        self.hardware.register_face.return_value = {"error_code": 0}
        image_bytes = b"\xff\xd8\xff" + b"x" * 2048 + b"\xff\xd9"
        browser_image = base64.b64encode(image_bytes).decode("ascii")

        with patch("auth.hardware", self.hardware):
            response = self.csrf_post(
                "/auth/register",
                data={
                    "username": "face-user",
                    "email": "face@example.test",
                    "password": "password123",
                    "confirm_password": "password123",
                    "register_face": "on",
                    "face_image": (BytesIO(image_bytes), "face.jpg"),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 302)
        self.hardware.capture_face_at_position.assert_not_called()
        self.hardware.register_face.assert_called_once_with(
            1, browser_image, ANY, "equipment_users", replace=False
        )
        with self.app.app_context():
            self.assertTrue(db.session.get(User, 1).face_registered)

    def test_signup_face_failure_rolls_back_account(self):
        self.hardware.register_face.side_effect = None
        self.hardware.register_face.return_value = {
            "error_code": 222202,
            "error_msg": "pic not has face",
        }
        with patch("auth.hardware", self.hardware):
            response = self.csrf_post(
                "/auth/register",
                data={
                    "username": "failed-face",
                    "email": "failed@example.test",
                    "password": "password123",
                    "confirm_password": "password123",
                    "register_face": "on",
                    "face_image": (
                        BytesIO(b"\xff\xd8\xff" + b"x" * 2048 + b"\xff\xd9"),
                        "face.jpg",
                    ),
                },
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 503)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)

    def test_signup_face_requires_valid_browser_photo_without_pi_camera(self):
        with patch("auth.hardware", self.hardware):
            response = self.csrf_post(
                "/auth/register",
                data={
                    "username": "missing-photo",
                    "email": "missing@example.test",
                    "password": "password123",
                    "confirm_password": "password123",
                    "register_face": "on",
                },
            )
        self.assertEqual(response.status_code, 400)
        self.hardware.capture_face_at_position.assert_not_called()
        self.hardware.register_face.assert_not_called()
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)

    def test_existing_user_can_enroll_and_update_face_from_browser_camera(self):
        self.register_admin()
        page = self.client.get("/parts/face")
        self.assertEqual(page.status_code, 200)
        self.assertIn("打开电脑摄像头", page.get_data(as_text=True))

        image_bytes = b"\xff\xd8\xff" + b"x" * 2048 + b"\xff\xd9"
        browser_image = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
        self.hardware.register_face.side_effect = None
        self.hardware.register_face.return_value = {"error_code": 0}

        response = self.csrf_post(
            "/parts/api/face/register",
            json={"image": browser_image, "password": "password123"},
        )
        self.assertEqual(response.status_code, 200)
        self.hardware.capture_face_at_position.assert_not_called()
        self.hardware.register_face.assert_called_once_with(
            1, browser_image, ANY, "equipment_users", replace=False
        )
        with self.app.app_context():
            self.assertTrue(db.session.get(User, 1).face_registered)

        self.hardware.register_face.reset_mock()
        response = self.csrf_post(
            "/parts/api/face/register",
            json={"image": browser_image, "password": "password123"},
        )
        self.assertEqual(response.status_code, 200)
        self.hardware.register_face.assert_called_once_with(
            1, browser_image, ANY, "equipment_users", replace=True
        )

        rejected = self.csrf_post(
            "/parts/api/face/register",
            json={"capture": True, "password": "password123"},
        )
        self.assertEqual(rejected.status_code, 400)
        self.hardware.capture_face_at_position.assert_not_called()

    def test_face_login_uses_same_registered_user_and_group(self):
        self.register_admin()
        with self.app.app_context():
            user = db.session.get(User, 1)
            user.face_registered = True
            db.session.commit()
        self.csrf_post("/auth/logout")
        login_page = self.client.get("/auth/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn("使用人脸登录", login_page.get_data(as_text=True))

        image_bytes = b"\xff\xd8\xff" + b"x" * 2048 + b"\xff\xd9"
        browser_image = "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
        self.hardware.search_face.side_effect = None
        self.hardware.search_face.return_value = {
            "error_code": 0,
            "result": {"user_list": [{"user_id": "1", "score": 96.5}]},
        }

        response = self.csrf_post(
            "/parts/api/face/login",
            json={"image": browser_image},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["redirect"], "/dashboard")
        self.hardware.capture_face_at_position.assert_not_called()
        self.hardware.search_face.assert_called_once_with(
            browser_image, ANY, "equipment_users"
        )
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_navigation_names_and_order_are_consistent(self):
        self.register_admin()
        response = self.client.get("/community/")
        page = response.get_data(as_text=True)
        nav_start = page.index('<nav class="community-nav"')
        nav_end = page.index("</nav>", nav_start)
        navigation = page[nav_start:nav_end]
        labels = [">社区<", ">私聊<", ">通知<", ">云盘<", ">公告管理<"]
        positions = [navigation.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()

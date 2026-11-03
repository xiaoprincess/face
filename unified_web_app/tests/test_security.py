import importlib
import base64
import unittest
from unittest.mock import patch

import app as app_module
from core_models import User
from extensions import db
from parts_models import PartApplication
from security import DEVICE_ENDPOINTS
from tests.test_app import IsolatedAppTestCase


class SecurityTestCase(IsolatedAppTestCase):
    def setUp(self):
        super().setUp()

        @self.app.route("/parts/api/device/browser-write", methods=["POST"])
        @self.app.route("/_test/write", methods=["POST", "PUT", "PATCH", "DELETE"])
        def write_probe():
            return {"success": True}

    def test_all_write_methods_require_session_token(self):
        token = self.csrf_token()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for supplied in (None, "wrong", "无效令牌"):
                with self.subTest(method=method, supplied=supplied):
                    headers = {} if supplied is None else {"X-CSRF-Token": supplied}
                    response = self.client.open("/_test/write", method=method, json={}, headers=headers)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json["error"], "csrf_failed")
            for kwargs in ({"data": {"csrf_token": token}}, {"json": {}, "headers": {"X-CSRF-Token": token}}):
                with self.subTest(method=method, transport=kwargs):
                    self.assertEqual(self.client.open("/_test/write", method=method, **kwargs).status_code, 200)

    def test_token_is_bound_to_session_and_jinja_global_is_stable(self):
        token = self.csrf_token()
        self.assertEqual(token, self.csrf_token())
        other = self.app.test_client()
        self.assertEqual(other.post("/_test/write", headers={"X-CSRF-Token": token}).status_code, 400)
        other_token = self.csrf_token(other)
        self.assertNotEqual(token, other_token)
        self.assertEqual(other.post("/_test/write", headers={"X-CSRF-Token": token}).status_code, 400)
        self.assertEqual(other.post("/_test/write", headers={"X-CSRF-Token": other_token}).status_code, 200)

    def test_browser_errors_are_readable_html_or_json(self):
        response = self.client.post("/auth/login")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.mimetype, "text/html")
        self.assertIn("请刷新页面后重试", response.get_data(as_text=True))
        response = self.client.post("/auth/login", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("请刷新页面后重试", response.json["message"])

    def test_every_existing_browser_write_route_rejects_missing_csrf(self):
        # Device-looking paths, JSON and device credentials must not broaden exemptions.
        for rule in self.app.url_map.iter_rules():
            if "POST" not in rule.methods or rule.endpoint in DEVICE_ENDPOINTS:
                continue
            values = {name: 1 for name in rule.arguments}
            path = self.app.url_map.bind("localhost").build(rule.endpoint, values, method="POST")
            with self.subTest(path=path):
                response = self.client.post(path, json={}, headers={"X-Device-Token": "device-test-token"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json["error"], "csrf_failed")

    def test_registration_form_token_and_login_logout(self):
        data = {"username": "admin", "email": "admin@example.test", "password": "password123", "confirm_password": "password123"}
        self.assertEqual(self.client.post("/auth/register", data=data).status_code, 400)
        self.assertEqual(self.client.post("/auth/register", data={**data, "csrf_token": "wrong"}).status_code, 400)
        with self.app.app_context():
            self.assertEqual(User.query.count(), 0)
        data["csrf_token"] = self.csrf_token()
        self.assertEqual(self.client.post("/auth/register", data=data).status_code, 302)
        self.assertEqual(self.client.post("/auth/logout").status_code, 400)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        self.assertEqual(self.csrf_post("/auth/logout").status_code, 302)
        self.assertEqual(self.csrf_post("/auth/login", data={"identity": "admin", "password": "password123"}).status_code, 302)

    def test_face_login_register_and_aliases_require_csrf(self):
        self.register_admin()
        with self.app.app_context():
            user = db.session.get(User, 1)
            user.face_registered = True
            db.session.commit()
            db.session.refresh(user)
            db.session.expunge(user)
        self.hardware.register_face.side_effect = None
        self.hardware.register_face.return_value = {"error_code": 0}
        for path in ("/parts/api/face/login", "/api/face_login", "/parts/api/face/register", "/api/face_register"):
            with self.subTest(path=path), patch("parts._face_result", return_value=((user, 99), None)) as face:
                for token in (None, "wrong"):
                    headers = {"X-Device-Token": "device-test-token"}
                    if token is not None:
                        headers["X-CSRF-Token"] = token
                    response = self.client.post(path, json={"image": "test"}, headers=headers)
                    self.assertEqual(response.status_code, 400)
                    self.assertEqual(response.json["error"], "csrf_failed")
                face.assert_not_called()
                image_bytes = b"\xff\xd8\xff" + b"x" * 2048 + b"\xff\xd9"
                payload = {
                    "image": "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
                }
                if "register" in path:
                    payload["password"] = "password123"
                self.assertEqual(self.csrf_post(path, json=payload).status_code, 200)

    def test_device_routes_reject_missing_wrong_and_unconfigured_tokens(self):
        paths = ("/parts/api/device/borrow", "/parts/api/face/verify", "/api/face_verify", "/parts/api/face/apply", "/api/face_apply", "/command?cmd=M2")
        for configured in (None, "", "   ", "device-test-token"):
            self.app.config["PARTS_DEVICE_TOKEN"] = configured
            # An explicit empty config must not fall back to an environment token.
            with patch.dict("os.environ", {"PARTS_DEVICE_TOKEN": "ambient-test-token"}):
                for path in paths:
                    for supplied in (None, "wrong", "无效", "ambient-test-token", "device-test-token"):
                        if configured == supplied == "device-test-token":
                            continue
                        with self.subTest(configured=configured, path=path, supplied=supplied):
                            headers = {} if supplied is None else {"X-Device-Token": supplied}
                            method = "GET" if path.startswith("/command") else "POST"
                            response = self.client.open(path, method=method, headers=headers)
                            expected = 503 if path == "/parts/api/device/borrow" and configured != "device-test-token" else 401
                            self.assertEqual(response.status_code, expected)
        self.hardware.status.assert_not_called()

    def test_device_routes_accept_device_credentials_without_csrf(self):
        self.register_admin()
        device = self.app.test_client()  # No browser session cookie.
        with self.app.app_context():
            user = db.session.get(User, 1)
        self.hardware.status.return_value = {"available": True}
        for headers in ({"X-Device-Token": "device-test-token"}, {"Authorization": "Bearer device-test-token"}):
            for path in ("/parts/api/face/apply", "/api/face_apply"):
                response = device.post(path, json={"user_id": 1, "equipment_name": "M2"}, headers=headers)
                self.assertEqual(response.status_code, 201)
            with patch("parts._face_result", return_value=((user, 99), None)):
                for path in ("/parts/api/face/verify", "/api/face_verify"):
                    self.assertEqual(device.post(path, json={"equipment_name": "M2"}, headers=headers).status_code, 200)
            with patch("parts._run_device_borrow", return_value=({"success": True}, 201)) as borrow:
                response = device.post("/parts/api/device/borrow", json={"component_id": 1, "quantity": 1, "request_id": "test-request"}, headers=headers)
                self.assertEqual(response.status_code, 201)
                borrow.assert_called_once()
            with patch("parts.threading.Thread.start") as start:
                self.assertEqual(device.get("/command?cmd=M2", headers=headers).status_code, 202)
                start.assert_called_once()
        with self.app.app_context():
            self.assertEqual(PartApplication.query.count(), 8)

    def test_browser_login_and_csrf_do_not_replace_device_authentication(self):
        self.register_admin()
        headers = {"X-CSRF-Token": self.csrf_token()}
        for path in ("/parts/api/device/borrow", "/parts/api/face/verify", "/api/face_verify", "/parts/api/face/apply", "/api/face_apply"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, json={}, headers=headers).status_code, 401)
        self.assertEqual(self.client.get("/command?cmd=M2", headers=headers).status_code, 401)
        self.hardware.status.assert_not_called()


class StartupIsolationTestCase(unittest.TestCase):
    def test_wsgi_app_attribute_is_created_once_on_demand(self):
        sentinel = object()
        try:
            with patch("app.create_app", return_value=sentinel) as factory:
                self.assertIs(app_module.app, sentinel)
                self.assertIs(app_module.app, sentinel)
                factory.assert_called_once_with()
        finally:
            app_module.__dict__.pop("app", None)

    def test_factory_import_has_no_configuration_or_storage_side_effects(self):
        with patch("dotenv.load_dotenv") as dotenv, patch("extensions.db.create_all") as create_all, patch("pathlib.Path.mkdir") as mkdir, patch("pathlib.Path.read_text") as read:
            importlib.reload(app_module)
            dotenv.assert_not_called()
            create_all.assert_not_called()
            mkdir.assert_not_called()
            read.assert_not_called()

    def test_testing_factory_uses_memory_and_does_not_load_real_configuration(self):
        with patch("app.load_dotenv", side_effect=AssertionError("Real configuration")), patch("app._load_or_create_secret", side_effect=AssertionError("Real secret")), patch("pathlib.Path.mkdir", side_effect=AssertionError("Real directory")):
            app = app_module.create_app({"TESTING": True})
        with app.app_context():
            self.assertEqual(str(db.engine.url), "sqlite://")
            db.session.remove()
            db.drop_all()


if __name__ == "__main__":
    unittest.main()

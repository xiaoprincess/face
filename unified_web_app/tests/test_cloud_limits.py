"""Isolated cloud tests: memory SQLite, temporary uploads, real default CSRF.

Use the isolated test application; never load production configuration or storage.
Run: .venv/Scripts/python.exe -B -m unittest discover -s tests -p test_cloud_limits.py
"""

import os
import re
import sys
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from flask import Flask, request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import QueuePool

import community
from cloud_policy import ALLOWED_EXTENSIONS, display_filename, limits
from community_models import FileRecord
from core_models import User
from extensions import db, login_manager
from security import init_csrf


class CloudLimitsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.upload_dir = Path(self.temp.name) / "uploads"
        self.app = Flask("cloud-tests", template_folder=str(Path(community.__file__).parent / "templates"))
        self.app.config.update(
            TESTING=True, SECRET_KEY="cloud-test-only",
            SQLALCHEMY_DATABASE_URI=f"sqlite:///file:cloud-{uuid.uuid4().hex}?mode=memory&cache=shared&uri=true",
            SQLALCHEMY_ENGINE_OPTIONS={"poolclass": QueuePool, "connect_args": {"check_same_thread": False}},
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            UPLOAD_FOLDER=str(self.upload_dir), MAX_CONTENT_LENGTH=16 * 1024**2,
            CLOUD_USER_QUOTA_BYTES=256 * 1024**2,
            CLOUD_SITE_QUOTA_BYTES=2 * 1024**3, CLOUD_MAX_FILE_BYTES=16 * 1024**2,
        )
        db.init_app(self.app)
        login_manager.init_app(self.app)
        login_manager.login_view = "auth.login"
        login_manager.user_loader(lambda user_id: db.session.get(User, int(user_id)))
        init_csrf(self.app)
        self.app.register_blueprint(community.community_bp)
        # Template navigation targets; cloud routes/models/templates are real.
        for endpoint in ("index", "dashboard", "auth.login", "auth.register", "auth.logout", "parts.index"):
            self.app.add_url_rule("/test/" + endpoint, endpoint, lambda: "test")
        with self.app.app_context():
            db.create_all()
            db.session.add_all([
                User(id=1, username="所有者", email="one@test.invalid", password_hash="unused"),
                User(id=2, username="访客", email="two@test.invalid", password_hash="unused"),
                User(id=3, username="管理员", email="admin@test.invalid", password_hash="unused", role="admin"),
            ])
            db.session.commit()
        self.addCleanup(self.cleanup_db)
        self.client = self.client_for(1)

    def cleanup_db(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()

    def client_for(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        return client

    def token(self, client):
        response = client.get("/community/cloud")
        self.assertEqual(response.status_code, 200)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)

    def upload(self, content=b"hello", name="资料.txt", client=None):
        client = client or self.client
        return client.post("/community/cloud/upload", data={
            "csrf_token": self.token(client), "file": (BytesIO(content), name),
        })

    def files(self):
        return list(self.upload_dir.iterdir()) if self.upload_dir.exists() else []

    def records(self):
        with self.app.app_context():
            return [(r.id, r.filename, r.original_name, r.file_size, r.user_id) for r in FileRecord.query.order_by(FileRecord.id)]

    def messages(self, client=None):
        with (client or self.client).session_transaction() as session:
            return " ".join(message for _, message in session.get("_flashes", []))

    def test_chinese_name_round_trip_and_uuid_storage(self):
        response = self.upload(b"project", r"C:\fakepath\中文 项目.PDF")
        self.assertEqual(response.status_code, 302)
        record = self.records()[0]
        self.assertEqual(record[2:], ("中文 项目.PDF", 7, 1))
        self.assertRegex(record[1], r"^[0-9a-f]{32}\.pdf$")
        response = self.client.get(f"/community/cloud/{record[0]}/download")
        self.assertEqual(response.data, b"project")
        self.assertIn("中文 项目.PDF", unquote(response.headers["Content-Disposition"]))
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(response.mimetype, "application/octet-stream")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        response.close()

    def test_path_and_control_characters_removed(self):
        self.assertEqual(display_filename("../../目录/项\x00目\r\n\u202e.txt"), ("项目.txt", "txt"))
        self.upload(name="../../项目.txt")
        self.assertEqual(self.records()[0][2], "项目.txt")
        self.assertEqual(len(self.files()), 1)

    def test_common_project_formats_accepted(self):
        for extension in ALLOWED_EXTENSIONS:
            with self.subTest(extension=extension):
                self.assertEqual(self.upload(b"x", f"项目.{extension}").status_code, 302)
        self.assertEqual(len(self.records()), len(ALLOWED_EXTENSIONS))

    def test_active_missing_and_disguised_extensions_rejected(self):
        names = ["x.html", "x.HTM", "x.svg", "x.exe", "x.bat", "x.cmd", "x.ps1", "x.js",
                 "x.mjs", "x.sh", "x.php", "x.com", "x.scr", "x.docm", "x.xlsx.exe",
                 "x.txt.js", "x", ".pdf", "x.txt.", "x.txt:evil", "中" * 100 + ".pdf"]
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(self.upload(name=name).status_code, 302)
                self.assertEqual(self.records(), [])
                self.assertEqual(self.files(), [])
                self.assertTrue("格式" in self.messages() or "文件名" in self.messages())

    def test_missing_file_feedback(self):
        self.client.post("/community/cloud/upload", data={"csrf_token": self.token(self.client)})
        self.assertIn("请选择文件", self.messages())
        self.assertEqual(self.records(), [])

    def test_personal_quota_exact_boundary_and_old_file_preserved(self):
        self.app.config["CLOUD_USER_QUOTA_BYTES"] = 5
        self.upload(b"12345")
        original = self.files()[0]
        self.upload(b"x", "second.txt")
        self.assertIn("个人云盘空间不足", self.messages())
        self.assertEqual(original.read_bytes(), b"12345")
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.files(), [original])

    def test_site_quota_includes_other_users_and_admin(self):
        self.app.config["CLOUD_SITE_QUOTA_BYTES"] = 5
        self.upload(b"1234")
        other = self.client_for(2)
        self.upload(b"x", client=other)
        admin = self.client_for(3)
        self.upload(b"x", client=admin)
        self.assertIn("全站云盘空间不足", self.messages(admin))
        self.assertEqual(sum(r[3] for r in self.records()), 5)
        self.assertEqual(len(self.files()), 2)

    def test_admin_has_personal_quota_too(self):
        self.app.config["CLOUD_USER_QUOTA_BYTES"] = 1
        admin = self.client_for(3)
        self.upload(b"xx", client=admin)
        self.assertIn("个人云盘空间不足", self.messages(admin))
        self.assertEqual(self.records(), [])

    def test_single_file_limit_and_request_limit_feedback(self):
        self.app.config["CLOUD_MAX_FILE_BYTES"] = 3
        self.upload(b"1234")
        self.assertIn("单文件上限", self.messages())
        self.assertEqual(self.files(), [])
        self.app.config["MAX_CONTENT_LENGTH"] = 1024
        self.upload(b"x" * 2048)
        self.assertIn("上传请求过大", self.messages())
        self.assertEqual(self.records(), [])

    def test_usage_page_and_csrf_fields(self):
        self.upload(b"12345")
        page = self.client.get("/community/cloud").get_data(as_text=True)
        for expected in ("已用 5 B", "256.00 MiB", "16.00 MiB", ".kicad_sch", "资料.txt"):
            self.assertIn(expected, page)
        self.assertNotIn("全站空间", page)
        admin_page = self.client_for(3).get("/community/cloud").get_data(as_text=True)
        self.assertIn("2.00 GiB", admin_page)
        self.assertGreaterEqual(page.count('name="csrf_token"'), 2)

    def test_csrf_required_for_upload_and_delete(self):
        token = self.token(self.client)
        for supplied in (None, "wrong"):
            data = {"file": (BytesIO(b"x"), "项目.txt")}
            if supplied is not None:
                data["csrf_token"] = supplied
            self.assertEqual(self.client.post("/community/cloud/upload", data=data).status_code, 400)
        self.assertEqual(self.records(), [])
        self.upload()
        record_id = self.records()[0][0]
        self.assertEqual(self.client.post(f"/community/cloud/{record_id}/delete").status_code, 400)
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.client.post(f"/community/cloud/{record_id}/delete", data={"csrf_token": token}).status_code, 302)
        self.assertEqual(self.records(), [])
        self.assertEqual(self.files(), [])

    def test_permissions_and_private_listing(self):
        self.upload()
        record_id = self.records()[0][0]
        other = self.client_for(2)
        self.assertNotIn("资料.txt", other.get("/community/cloud").get_data(as_text=True))
        self.assertEqual(other.get(f"/community/cloud/{record_id}/download").status_code, 403)
        self.assertEqual(other.post(f"/community/cloud/{record_id}/delete", data={"csrf_token": self.token(other)}).status_code, 403)
        self.assertEqual(len(self.files()), 1)
        admin = self.client_for(3)
        self.assertIn("资料.txt", admin.get("/community/cloud").get_data(as_text=True))
        download = admin.get(f"/community/cloud/{record_id}/download")
        self.assertEqual(download.status_code, 200)
        download.close()
        self.assertEqual(admin.post(f"/community/cloud/{record_id}/delete", data={"csrf_token": self.token(admin)}).status_code, 302)
        self.assertEqual(self.records(), [])

    def test_anonymous_access_denied(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/community/cloud").status_code, 302)
        self.assertEqual(anonymous.get("/community/cloud/1/download").status_code, 302)
        self.assertIn(anonymous.post("/community/cloud/upload", data={"file": (BytesIO(b"x"), "x.txt")}).status_code, (302, 400))
        self.assertEqual(self.records(), [])

    def test_post_write_quota_check_cleans_only_new_file(self):
        self.upload(b"old")
        original = self.files()[0]
        self.app.config["CLOUD_USER_QUOTA_BYTES"] = 10
        token = self.token(self.client)
        with patch.object(community, "_cloud_usage", side_effect=[(3, 3), (10, 10)]):
            self.client.post("/community/cloud/upload", data={"csrf_token": token, "file": (BytesIO(b"new"), "new.txt")})
        self.assertIn("个人云盘空间不足", self.messages())
        self.assertEqual(self.files(), [original])
        self.assertEqual(original.read_bytes(), b"old")
        self.assertEqual(len(self.records()), 1)

    def test_stream_growth_cannot_bypass_precheck(self):
        self.app.config["CLOUD_USER_QUOTA_BYTES"] = 4
        class UnderreportedStream(BytesIO):
            def tell(self):
                return 1
        real_clean = community.display_filename
        def replace_stream(name):
            request.files["file"].stream.close()
            request.files["file"].stream = UnderreportedStream(b"123456789")
            return real_clean(name)
        with patch.object(community, "display_filename", side_effect=replace_stream):
            self.upload(b"x")
        self.assertIn("个人云盘空间不足", self.messages())
        self.assertEqual(self.files(), [])
        self.assertEqual(self.records(), [])

    def test_database_failure_removes_new_file_and_rolls_back(self):
        self.upload(b"old")
        original = self.files()[0]
        with self.assertLogs(self.app.logger, level="ERROR"), patch.object(db.session, "commit", side_effect=SQLAlchemyError("test failure")):
            self.upload(b"new")
        self.assertIn("上传失败", self.messages())
        self.assertEqual(self.files(), [original])
        self.assertEqual(len(self.records()), 1)

    def test_uuid_collision_never_overwrites_or_deletes_existing_file(self):
        fixed = uuid.uuid4()
        with patch.object(community.uuid, "uuid4", return_value=fixed):
            self.upload(b"old")
            with self.assertLogs(self.app.logger, level="ERROR"):
                self.upload(b"new")
        self.assertIn("上传失败", self.messages())
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.files()[0].read_bytes(), b"old")
        self.assertEqual(len(self.records()), 1)

    def test_two_concurrent_uploads_cannot_exceed_site_quota(self):
        self.app.config["CLOUD_SITE_QUOTA_BYTES"] = 10
        self.assert_concurrent_quota(self.client_for(2), "全站云盘空间不足")

    def test_two_concurrent_uploads_cannot_exceed_personal_quota(self):
        self.app.config["CLOUD_USER_QUOTA_BYTES"] = 10
        self.assert_concurrent_quota(self.client_for(1), "个人云盘空间不足")

    def assert_concurrent_quota(self, second_client, expected_error):
        clients = [self.client, second_client]
        tokens = [self.token(c) for c in clients]
        # Both requests finish authentication before either enters the lock.
        barrier = threading.Barrier(2)
        real_clean = community.display_filename
        def synchronize(name):
            result = real_clean(name)
            barrier.wait(timeout=5)
            return result
        def send(index):
            return clients[index].post("/community/cloud/upload", data={
                "csrf_token": tokens[index], "file": (BytesIO(b"123456"), f"{index}.txt"),
            }).status_code
        with patch.object(community, "display_filename", side_effect=synchronize):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(send, range(2))), [302, 302])
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(self.files()), 1)
        self.assertEqual(self.records()[0][3], 6)
        self.assertIn(expected_error, self.messages(clients[0]) + self.messages(clients[1]))

    def test_partial_write_failure_cleans_new_file(self):
        self.upload(b"old")
        original = self.files()[0]
        class BrokenStream(BytesIO):
            def read(self, size=-1):
                if self.tell() > 0:
                    raise OSError("simulated storage/read failure")
                return super().read(size)
        real_clean = community.display_filename
        def replace_stream(name):
            request.files["file"].stream.close()
            request.files["file"].stream = BrokenStream(b"x" * 70000)
            return real_clean(name)
        with self.assertLogs(self.app.logger, level="ERROR"), patch.object(community, "display_filename", side_effect=replace_stream):
            self.upload(b"new")
        self.assertIn("上传失败", self.messages())
        self.assertEqual(self.files(), [original])
        self.assertEqual(original.read_bytes(), b"old")
        self.assertEqual(len(self.records()), 1)

    def test_lowering_quota_keeps_existing_files(self):
        self.upload(b"existing")
        original = self.files()[0]
        self.app.config["CLOUD_USER_QUOTA_BYTES"] = 1
        self.upload(b"x")
        self.assertIn("个人云盘空间不足", self.messages())
        self.assertEqual(self.files(), [original])
        self.assertEqual(original.read_bytes(), b"existing")
        self.assertEqual(len(self.records()), 1)

    def test_quota_defaults_environment_and_config(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(limits({}), (256 * 1024**2, 2 * 1024**3, 16 * 1024**2))
        with patch.dict(os.environ, {"CLOUD_USER_QUOTA_BYTES": "12", "CLOUD_SITE_QUOTA_BYTES": "20"}):
            self.assertEqual(limits({"CLOUD_MAX_FILE_BYTES": 8}), (12, 20, 8))
            self.assertEqual(limits({"CLOUD_USER_QUOTA_BYTES": 9})[0], 9)
        for invalid in (-1, "unlimited", True, 1.5, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                limits({"CLOUD_USER_QUOTA_BYTES": invalid})
        self.assertEqual(limits({"CLOUD_USER_QUOTA_BYTES": 0})[0], 0)


if __name__ == "__main__":
    unittest.main()

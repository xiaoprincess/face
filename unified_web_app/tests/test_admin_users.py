import sqlite3
import tempfile
import unittest
from pathlib import Path

from app import create_app
from community_models import Announcement, BlogPost, Comment, FileRecord, Message, Notification
from core_models import User, migrate_user_schema
from extensions import db, socketio
from parts_models import PartApplication
from tests.test_app import IsolatedAppTestCase


class AdminUserManagementTestCase(IsolatedAppTestCase):
    def csrf_post_as(self, client, path, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers["X-CSRF-Token"] = self.csrf_token(client)
        return client.post(path, headers=headers, **kwargs)

    def create_user(self, username, email=None, role="user"):
        with self.app.app_context():
            user = User(username=username, email=email or f"{username}@example.test", role=role)
            user.set_password("password123")
            db.session.add(user)
            db.session.commit()
            return user.id

    def login_client(self, identity, password="password123"):
        client = self.app.test_client()
        response = self.csrf_post_as(
            client, "/auth/login", data={"identity": identity, "password": password}
        )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return client


class AdminUsersPageTestCase(AdminUserManagementTestCase):
    def test_users_tab_requires_login_and_admin(self):
        response = self.client.get("/parts/admin?tab=users")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/auth/login", response.headers["Location"])

        self.register_admin()
        self.create_user("alice")
        member = self.login_client("alice")
        self.assertEqual(member.get("/parts/admin?tab=users").status_code, 403)

        response = self.client.get("/parts/admin?tab=users")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("用户管理", page)
        self.assertIn("alice@example.test", page)

    def test_users_search_filters_by_name_and_email(self):
        self.register_admin()
        self.create_user("alice")
        self.create_user("bob")

        page = self.client.get("/parts/admin?tab=users&q=ali").get_data(as_text=True)
        self.assertIn("alice@example.test", page)
        self.assertNotIn("bob@example.test", page)

        page = self.client.get("/parts/admin?tab=users&q=bob@example.test").get_data(as_text=True)
        self.assertIn("bob@example.test", page)
        self.assertNotIn("alice@example.test", page)

        # LIKE 通配符按字面匹配，不会放大搜索范围。
        page = self.client.get("/parts/admin?tab=users&q=%").get_data(as_text=True)
        self.assertIn("没有匹配的用户", page)


class AdminUserToggleTestCase(AdminUserManagementTestCase):
    def test_toggle_active_blocks_login_kicks_session_and_recovers(self):
        self.register_admin()
        alice_id = self.create_user("alice")
        member = self.login_client("alice")
        self.assertEqual(member.get("/dashboard").status_code, 200)
        realtime = socketio.test_client(self.app, flask_test_client=member)
        self.assertTrue(realtime.is_connected())

        response = self.csrf_post(f"/parts/admin/users/{alice_id}/toggle-active")
        self.assertEqual(response.status_code, 302)

        # 已有会话立即失效，Socket 被强断。
        self.assertEqual(member.get("/dashboard").status_code, 302)
        self.assertFalse(realtime.is_connected())

        # 停用账号无法重新登录，并得到明确提示。
        retry = self.app.test_client()
        response = self.csrf_post_as(
            retry, "/auth/login", data={"identity": "alice", "password": "password123"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("该账号已被停用", response.get_data(as_text=True))
        self.assertEqual(retry.get("/dashboard").status_code, 302)

        # 重新启用后可正常登录，数据一直保留。
        response = self.csrf_post(f"/parts/admin/users/{alice_id}/toggle-active")
        self.assertEqual(response.status_code, 302)
        member = self.login_client("alice")
        self.assertEqual(member.get("/dashboard").status_code, 200)

    def test_admin_cannot_toggle_self(self):
        self.register_admin()
        with self.app.app_context():
            admin_id = User.query.filter_by(username="admin").one().id

        response = self.csrf_post(f"/parts/admin/users/{admin_id}/toggle-active")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertTrue(User.query.get(admin_id).is_active)


class AdminUserPasswordRoleTestCase(AdminUserManagementTestCase):
    def test_reset_password_rejects_short_and_updates_hash(self):
        self.register_admin()
        alice_id = self.create_user("alice")
        member = self.login_client("alice")

        response = self.csrf_post(
            f"/parts/admin/users/{alice_id}/reset-password",
            data={"new_password": "short"},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertTrue(User.query.get(alice_id).check_password("password123"))

        response = self.csrf_post(
            f"/parts/admin/users/{alice_id}/reset-password",
            data={"new_password": "newpass456"},
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            user = User.query.get(alice_id)
            self.assertTrue(user.check_password("newpass456"))
            self.assertFalse(user.check_password("password123"))
        self.assertEqual(member.get("/dashboard").status_code, 302)

    def test_role_promote_demote_and_self_guard(self):
        self.register_admin()
        alice_id = self.create_user("alice")
        with self.app.app_context():
            admin_id = User.query.filter_by(username="admin").one().id

        response = self.csrf_post(
            f"/parts/admin/users/{alice_id}/role", data={"role": "admin"}
        )
        self.assertEqual(response.status_code, 302)
        member = self.login_client("alice")
        self.assertEqual(member.get("/parts/admin?tab=users").status_code, 200)

        response = self.csrf_post(
            f"/parts/admin/users/{alice_id}/role", data={"role": "user"}
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertFalse(User.query.get(alice_id).is_admin)
        self.assertEqual(member.get("/parts/admin?tab=users").status_code, 403)

        for payload in ({"role": "superuser"}, {}):
            response = self.csrf_post(
                f"/parts/admin/users/{alice_id}/role", data=payload
            )
            self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertEqual(User.query.get(alice_id).role, "user")

        response = self.csrf_post(f"/parts/admin/users/{admin_id}/role", data={"role": "user"})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertTrue(User.query.get(admin_id).is_admin)


class AdminUserDeleteTestCase(AdminUserManagementTestCase):
    def test_delete_user_without_records_removes_data_and_files(self):
        self.register_admin()
        alice_id = self.create_user("alice")
        upload_folder = Path(self.app.config["UPLOAD_FOLDER"])
        upload_folder.mkdir(parents=True, exist_ok=True)
        stored_name = "alice-cloud-file.bin"
        (upload_folder / stored_name).write_bytes(b"abc")

        with self.app.app_context():
            admin_id = User.query.filter_by(username="admin").one().id
            post = BlogPost(title="标题", content="正文", user_id=alice_id)
            db.session.add(post)
            db.session.flush()
            db.session.add(Comment(content="评论", user_id=alice_id, post_id=post.id))
            db.session.add(Comment(content="管理员回复", user_id=admin_id, post_id=post.id))
            db.session.add(
                Message(content="你好", sender_id=alice_id, receiver_id=admin_id)
            )
            db.session.add(
                Notification(
                    user_id=admin_id,
                    sender_id=alice_id,
                    type="mention",
                    content="提到了你",
                )
            )
            db.session.add(
                Announcement(title="公告", content="内容", user_id=alice_id)
            )
            db.session.add(
                FileRecord(
                    filename=stored_name,
                    original_name="资料.bin",
                    file_size=3,
                    user_id=alice_id,
                )
            )
            db.session.commit()

        response = self.csrf_post(f"/parts/admin/users/{alice_id}/delete")
        self.assertEqual(response.status_code, 302)

        with self.app.app_context():
            self.assertIsNone(db.session.get(User, alice_id))
            self.assertEqual(BlogPost.query.count(), 0)
            self.assertEqual(Comment.query.count(), 0)
            self.assertEqual(Message.query.count(), 0)
            self.assertEqual(Notification.query.count(), 0)
            self.assertEqual(Announcement.query.count(), 0)
            self.assertEqual(FileRecord.query.count(), 0)
            self.assertEqual(User.query.filter_by(username="admin").count(), 1)
        self.assertFalse((upload_folder / stored_name).exists())

    def test_delete_user_with_borrow_records_is_refused(self):
        self.register_admin()
        alice_id = self.create_user("alice")
        with self.app.app_context():
            db.session.add(
                PartApplication(
                    user_id=alice_id,
                    equipment_name="电阻",
                    quantity="10",
                    purpose="课设",
                    status="approved",
                )
            )
            db.session.commit()

        response = self.csrf_post(
            f"/parts/admin/users/{alice_id}/delete", follow_redirects=True
        )
        self.assertIn("改用「停用」", response.get_data(as_text=True))
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(User, alice_id))
            self.assertEqual(PartApplication.query.count(), 1)

    def test_admin_cannot_delete_self(self):
        self.register_admin()
        with self.app.app_context():
            admin_id = User.query.filter_by(username="admin").one().id
        response = self.csrf_post(f"/parts/admin/users/{admin_id}/delete")
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(User, admin_id))

    def test_user_actions_require_csrf(self):
        self.register_admin()
        alice_id = self.create_user("alice")
        for path in (
            f"/parts/admin/users/{alice_id}/toggle-active",
            f"/parts/admin/users/{alice_id}/reset-password",
            f"/parts/admin/users/{alice_id}/role",
            f"/parts/admin/users/{alice_id}/delete",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    self.client.post(path, data={"new_password": "newpass456"}).status_code,
                    400,
                )


class MigrateUserSchemaTestCase(unittest.TestCase):
    def test_migrate_user_schema_adds_is_active_to_legacy_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            connection = sqlite3.connect(db_path)
            connection.execute(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY,
                    username VARCHAR(80) NOT NULL UNIQUE,
                    email VARCHAR(120) NOT NULL UNIQUE,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    face_registered BOOLEAN NOT NULL,
                    created_at DATETIME NOT NULL,
                    last_seen DATETIME NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO users (username, email, password_hash, role, face_registered,"
                " created_at, last_seen) VALUES ('legacy', 'legacy@example.test',"
                " 'legacy-hash', 'user', 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
            connection.commit()
            connection.close()

            legacy_app = create_app(
                {
                    "TESTING": True,
                    "SECRET_KEY": "test-secret",
                    "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
                    "UPLOAD_FOLDER": str(Path(tmp) / "uploads"),
                }
            )
            try:
                with legacy_app.app_context():
                    columns = {
                        row[1]
                        for row in db.session.execute(db.text("PRAGMA table_info(users)"))
                    }
                    self.assertIn("is_active", columns)
                    user = User.query.filter_by(username="legacy").one()
                    self.assertTrue(user.is_active)
                    migrate_user_schema()  # 幂等：重复执行不再改动。
            finally:
                with legacy_app.app_context():
                    db.session.remove()
                    db.engine.dispose()


if __name__ == "__main__":
    unittest.main()

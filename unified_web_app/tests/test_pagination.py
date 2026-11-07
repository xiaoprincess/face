"""Pagination uses isolated SQLite and never touches hardware or production data."""
from tests.test_app import IsolatedAppTestCase
from community_models import Announcement, Notification, FileRecord
from core_models import User
from extensions import db


class PaginationTests(IsolatedAppTestCase):
    def test_lists_have_accessible_second_page(self):
        self.register_admin()
        with self.app.app_context():
            user = User.query.first()
            for number in range(23):
                db.session.add(Announcement(title=f"notice-{number}", content="body", user_id=user.id))
                db.session.add(Notification(user_id=user.id, type="mention", content=f"notification-{number}"))
                db.session.add(FileRecord(user_id=user.id, original_name=f"file-{number}.txt",
                                          filename=f"file-{number}.txt", file_size=1))
            db.session.commit()
        for path in ("/community/cloud", "/community/announcements", "/community/notifications"):
            first = self.client.get(path)
            self.assertEqual(first.status_code, 200)
            self.assertIn("下一页", first.get_data(as_text=True))
            second = self.client.get(path + "?page=2")
            self.assertEqual(second.status_code, 200)
            self.assertIn("上一页", second.get_data(as_text=True))
            self.assertNotIn("下一页", second.get_data(as_text=True))

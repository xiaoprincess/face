"""Validate upgrades and authenticated pages on a disposable database copy."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from contextlib import closing
from unittest.mock import patch


def fingerprint(path):
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        values = [connection.execute(sql).fetchall() for sql in (
            "SELECT id, quantity FROM cabinet_components ORDER BY id",
            "SELECT id, user_id, equipment_name, quantity, purpose, status FROM part_applications ORDER BY id",
        )]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    args = parser.parse_args()
    source = Path(args.database).resolve()
    original = fingerprint(source)
    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "unified_web_app"))
    from app import create_app
    from core_models import User
    from extensions import db
    with tempfile.TemporaryDirectory(prefix="parts-upgrade-") as directory:
        target = Path(directory) / "copy.db"
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
        config = {"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///" + target.as_posix(),
                  "UPLOAD_FOLDER": str(Path(directory) / "uploads")}
        app = create_app(config)
        assert fingerprint(target) == original, "Migration changed stock or historical records"
        # A second startup proves the migration can be rerun safely.
        second_app = create_app(config)
        with second_app.app_context():
            db.session.remove()
            db.engine.dispose()
        assert fingerprint(target) == original
        with app.app_context():
            user = User.query.filter_by(role="admin").first() or User.query.first()
            user_id = user.id if user else None
            admin = user.is_admin if user else False
        if user_id:
            client = app.test_client()
            with client.session_transaction() as session:
                session["_user_id"] = str(user_id)
                session["_fresh"] = True
            with patch("parts.hardware.status", return_value={"available": False}):
                for path in ["/dashboard", "/parts/applications", "/community/cloud", "/community/notifications"] + (
                    ["/parts/admin", "/community/announcements"] if admin else []
                ):
                    response = client.get(path)
                    assert response.status_code == 200, (path, response.status_code)
                    print("PAGE_OK", path)
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
    assert fingerprint(source) == original, "Source changed during validation"
    print("MIGRATION_OK: repeatable; historical loans and stock preserved; source untouched")


if __name__ == "__main__":
    main()

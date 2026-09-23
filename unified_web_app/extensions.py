from datetime import datetime, timezone

from flask_login import LoginManager
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()
login_manager = LoginManager()
socketio = SocketIO(async_mode="threading")


def utc_now():
    """Return naive UTC for SQLite without deprecated datetime.utcnow()."""

    return datetime.now(timezone.utc).replace(tzinfo=None)

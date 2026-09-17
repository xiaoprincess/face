import hashlib
import hmac
import re

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db, utc_now


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")
    face_registered = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utc_now)
    last_seen = db.Column(db.DateTime, nullable=False, default=utc_now)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        if re.fullmatch(r"[0-9a-fA-F]{64}", self.password_hash or ""):
            legacy_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
            return hmac.compare_digest(self.password_hash.lower(), legacy_hash)
        return check_password_hash(self.password_hash, password)

    @property
    def uses_legacy_password_hash(self):
        return bool(re.fullmatch(r"[0-9a-fA-F]{64}", self.password_hash or ""))

    @property
    def is_admin(self):
        return self.role == "admin"

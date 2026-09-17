import os
import secrets
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, url_for
from flask_login import current_user, login_required
from dotenv import load_dotenv

from auth import auth_bp
from core_models import User
from extensions import db, login_manager, socketio


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _load_or_create_secret(instance_path):
    configured = os.environ.get("UNIFIED_SECRET_KEY", "").strip()
    if configured:
        return configured
    secret_file = Path(instance_path) / ".secret_key"
    if secret_file.exists():
        return secret_file.read_text(encoding="utf-8").strip()
    secret = secrets.token_hex(32)
    secret_file.write_text(secret, encoding="utf-8")
    return secret


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    upload_folder = Path(app.instance_path) / "uploads"
    upload_folder.mkdir(parents=True, exist_ok=True)

    app.config.from_mapping(
        SECRET_KEY=_load_or_create_secret(app.instance_path),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{Path(app.instance_path) / 'unified.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        UPLOAD_FOLDER=str(upload_folder),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PARTS_DEVICE_TOKEN=os.environ.get("PARTS_DEVICE_TOKEN", "").strip(),
        BAIDU_GROUP_ID=os.environ.get("BAIDU_GROUP_ID", "").strip(),
    )
    if test_config:
        app.config.update(test_config)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "请先登录"
    socketio.init_app(app, cors_allowed_origins=None)

    from community import community_bp
    from parts import parts_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(parts_bp)
    app.register_blueprint(community_bp)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard"))
        return render_template("index.html")

    @app.route("/dashboard")
    @login_required
    def dashboard():
        return render_template("dashboard.html")

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    with app.app_context():
        from community_models import (  # noqa: F401
            Announcement,
            BlogPost,
            Comment,
            FileRecord,
            Message,
            Notification,
        )
        from parts_models import (  # noqa: F401
            Cabinet,
            CabinetComponent,
            PartApplication,
            SystemSetting,
        )

        db.create_all()

    return app


app = create_app()


if __name__ == "__main__":
    socketio.run(
        app,
        # 允许同一局域网内的手机访问网站；登录会话仍由 Flask 的 Cookie 管理。
        host="0.0.0.0",
        port=5050,
        debug=False,
        allow_unsafe_werkzeug=True,
    )

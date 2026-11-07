import os
import secrets
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, session, url_for
from flask_login import current_user, login_required, logout_user
from dotenv import load_dotenv

from auth import _password_fingerprint, auth_bp
from core_models import User, migrate_user_schema
from extensions import db, login_manager, socketio
from security import init_csrf


BASE_DIR = Path(__file__).resolve().parent


def _env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
    testing = bool(test_config and test_config.get("TESTING"))
    if not testing:
        load_dotenv(BASE_DIR / ".env")
    app = Flask(__name__, instance_relative_config=True)
    upload_folder = Path(app.instance_path) / "uploads"
    if not testing:
        Path(app.instance_path).mkdir(parents=True, exist_ok=True)
        upload_folder.mkdir(parents=True, exist_ok=True)

    app.config.from_mapping(
        SECRET_KEY="test-only-secret" if testing else None,
        SQLALCHEMY_DATABASE_URI=(
            "sqlite://" if testing else f"sqlite:///{Path(app.instance_path) / 'unified.db'}"
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
        UPLOAD_FOLDER=str(upload_folder),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PARTS_DEVICE_TOKEN=os.environ.get("PARTS_DEVICE_TOKEN", "").strip(),
        PARTS_SKIP_FACE_VERIFICATION=_env_flag("PARTS_SKIP_FACE_VERIFICATION"),
        PARTS_TAG_TEST_ENABLED=_env_flag("PARTS_TAG_TEST_ENABLED", True),
        PARTS_TAG_FACE_TIMEOUT=os.environ.get("PARTS_TAG_FACE_TIMEOUT", "30"),
        PARTS_TAG_LOST_TIMEOUT=os.environ.get("PARTS_TAG_LOST_TIMEOUT", "8"),
        PARTS_GUIDANCE_TIMEOUT=30.0,
        PARTS_RECOVER_GUIDANCE_ON_STARTUP=True,
        PARTS_FALLBACK_APPLICANT_EMAIL=os.environ.get(
            "PARTS_FALLBACK_APPLICANT_EMAIL", "xiaozhi-bot@local.test"
        ).strip(),
        BAIDU_GROUP_ID=os.environ.get("BAIDU_GROUP_ID", "").strip(),
    )
    if test_config:
        app.config.update(test_config)
    if not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = (
            "test-only-secret" if testing else _load_or_create_secret(app.instance_path)
        )

    init_csrf(app)

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

    @app.before_request
    def _sign_out_disabled_users():
        # 已登录但被停用的账号在下一次请求时立即失效；借用与社区数据保留。
        if not current_user.is_authenticated:
            return
        if not current_user.is_active:
            logout_user()
            flash("账号已被停用，如有疑问请联系管理员", "error")
            return
        # 管理员重置密码后，旧登录会话失效。缺失指纹的旧会话不受影响，
        # 下次登录起开始记录。
        fingerprint = session.get("_auth_pw_fingerprint")
        if fingerprint is not None and fingerprint != _password_fingerprint(
            current_user.password_hash
        ):
            logout_user()
            flash("密码已被管理员重置，请使用新密码重新登录", "error")

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
            migrate_parts_schema,
        )

        db.create_all()
        migrate_user_schema()
        migrate_parts_schema(recover_guidance=app.config["PARTS_RECOVER_GUIDANCE_ON_STARTUP"])

    return app


def __getattr__(name):
    # Preserve WSGI's ``from app import app`` without initializing production
    # storage or loading configuration when tests import only create_app.
    if name == "app":
        instance = create_app()
        globals()[name] = instance
        return instance
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    app = create_app()
    socketio.run(
        app,
        # 允许同一局域网内的手机访问网站；登录会话仍由 Flask 的 Cookie 管理。
        host="0.0.0.0",
        port=5050,
        debug=False,
        allow_unsafe_werkzeug=True,
    )

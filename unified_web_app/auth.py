import base64
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user

from core_models import User
from extensions import db, socketio, utc_now
from hardware import HardwareUnavailable, hardware
from parts_models import SystemSetting


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _disconnect_user_sockets(user_id):
    room = f"user_{user_id}"
    participants = list(socketio.server.manager.get_participants("/", room))
    for participant in participants:
        sid = participant[0] if isinstance(participant, tuple) else participant
        socketio.server.disconnect(sid, namespace="/")


def _registration_face_group():
    return str(
        current_app.config.get("BAIDU_GROUP_ID")
        or SystemSetting.get_value("face_group_id", "equipment_users")
    ).strip() or "equipment_users"


def _validated_browser_face_image(upload):
    if upload is None or not upload.filename:
        raise ValueError("请先使用当前电脑的摄像头拍摄人脸照片")
    image_bytes = upload.stream.read(5 * 1024 * 1024 + 1)
    if len(image_bytes) < 1024:
        raise ValueError("摄像头照片内容不完整，请重新拍摄")
    if len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("摄像头照片不能超过 5 MB")
    if not image_bytes.startswith(b"\xff\xd8\xff"):
        raise ValueError("摄像头照片不是有效的 JPEG 图片，请重新拍摄")
    return base64.b64encode(image_bytes).decode("ascii")


def _register_face_during_signup(user, image):
    result = hardware.register_face(
        user.id,
        image,
        current_app.config,
        _registration_face_group(),
        replace=False,
    )
    if result.get("error_code") != 0:
        raise ValueError(result.get("error_msg") or "百度人脸注册失败")
    user.face_registered = True


def _safe_next_url():
    target = (request.args.get("next") or "").strip()
    parsed = urlsplit(target)
    if (
        target.startswith("/")
        and not target.startswith("//")
        and "\\" not in target
        and not parsed.scheme
        and not parsed.netloc
    ):
        return target
    return url_for("dashboard")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        register_face = request.form.get("register_face") == "on"
        face_image = request.files.get("face_image")

        if not username or not email or not password:
            flash("请完整填写注册信息", "error")
        elif password != confirm:
            flash("两次输入的密码不一致", "error")
        elif len(password) < 8:
            flash("密码至少需要 8 位", "error")
        elif User.query.filter(
            (User.username == username) | (User.email == email)
        ).first():
            flash("用户名或邮箱已存在", "error")
        else:
            if register_face:
                try:
                    face_image = _validated_browser_face_image(face_image)
                except ValueError as exc:
                    flash(f"人脸登记失败，账号尚未创建：{exc}", "error")
                    return render_template(
                        "auth/register.html",
                        username=username,
                        email=email,
                        register_face=True,
                    ), 400
            is_first_user = User.query.count() == 0
            role = "admin" if is_first_user else "user"
            user = User(username=username, email=email, role=role)
            user.set_password(password)
            try:
                db.session.add(user)
                db.session.flush()
                if register_face:
                    _register_face_during_signup(user, face_image)
                db.session.commit()
            except (HardwareUnavailable, ValueError) as exc:
                db.session.rollback()
                flash(f"人脸登记失败，账号尚未创建：{exc}", "error")
                return render_template(
                    "auth/register.html",
                    username=username,
                    email=email,
                    register_face=True,
                ), 503
            except Exception:
                db.session.rollback()
                current_app.logger.exception("注册账号或登记人脸失败")
                flash("注册失败，账号尚未创建，请稍后重试", "error")
                return render_template(
                    "auth/register.html",
                    username=username,
                    email=email,
                    register_face=register_face,
                ), 500
            login_user(user)
            message = "注册成功。你是首个账号，已成为管理员。" if is_first_user else "注册成功"
            if register_face:
                message += " 人脸信息已同步登记。"
            flash(message, "success")
            return redirect(url_for("dashboard"))

    return render_template("auth/register.html", register_face=True)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        identity = request.form.get("identity", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter(
            (User.username == identity) | (User.email == identity.lower())
        ).first()
        if user and user.check_password(password):
            if user.uses_legacy_password_hash:
                user.set_password(password)
            user.last_seen = utc_now()
            db.session.commit()
            login_user(user, remember=request.form.get("remember") == "on")
            return redirect(_safe_next_url())
        flash("用户名、邮箱或密码错误", "error")

    return render_template("auth/login.html")


@auth_bp.route("/logout", methods=["POST"])
def logout():
    if current_user.is_authenticated:
        _disconnect_user_sockets(current_user.id)
        logout_user()
    return redirect(url_for("index"))

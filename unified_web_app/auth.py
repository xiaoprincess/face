from urllib.parse import urlsplit

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_user, logout_user

from core_models import User
from extensions import db, utc_now


auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


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
            is_first_user = User.query.count() == 0
            role = "admin" if is_first_user else "user"
            user = User(username=username, email=email, role=role)
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            message = "注册成功。你是首个账号，已成为管理员。" if is_first_user else "注册成功"
            flash(message, "success")
            return redirect(url_for("dashboard"))

    return render_template("auth/register.html")


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
        logout_user()
    return redirect(url_for("index"))

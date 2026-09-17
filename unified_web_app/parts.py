import json
import logging
import secrets
import threading
import uuid
from datetime import date
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from flask_login import current_user, login_required, login_user
from sqlalchemy import case
from werkzeug.utils import secure_filename

from core_models import User
from extensions import db, utc_now
from hardware import HardwareUnavailable, hardware
from parts_models import Cabinet, CabinetComponent, PartApplication, SystemSetting


parts_bp = Blueprint("parts", __name__, url_prefix="/parts")
logger = logging.getLogger(__name__)
device_borrow_lock = threading.RLock()

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}
SETTING_RULES = {
    "laser_delay": (0.0, 60.0, 3.0),
    "face_pan_angle": (-180.0, 180.0, 0.0),
    "face_tilt_angle": (-180.0, 180.0, 0.0),
    "face_threshold": (0.0, 100.0, 70.0),
}


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def _setting_float(key):
    minimum, maximum, default = SETTING_RULES[key]
    try:
        value = float(SystemSetting.get_value(key, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def _group_id():
    return str(
        current_app.config.get("BAIDU_GROUP_ID")
        or SystemSetting.get_value("face_group_id", "equipment_users")
    ).strip() or "equipment_users"


def _parse_expected_date(raw_value):
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return None
    try:
        return date.fromisoformat(raw_value)
    except ValueError as exc:
        raise ValueError("期望日期格式应为 YYYY-MM-DD") from exc


def _form_items():
    names = request.form.getlist("equipment_names[]")
    quantities = request.form.getlist("quantities[]")
    if not names:
        names = [request.form.get("equipment_name", "")]
        quantities = [request.form.get("quantity", "1")]

    items = []
    for index, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        if not name:
            continue
        if len(name) > 200:
            raise ValueError("器材名称不能超过 200 个字符")
        quantity = quantities[index].strip() if index < len(quantities) else "1"
        quantity = quantity or "1"
        if len(quantity) > 30:
            raise ValueError("数量说明不能超过 30 个字符")
        items.append((name, quantity))
    if not items:
        raise ValueError("请至少填写一种器材")
    if len(items) > 20:
        raise ValueError("单次申请最多包含 20 种器材")
    return items


def _upload_directory():
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


def _save_uploaded_images(files):
    saved_names = []
    upload_dir = _upload_directory()
    try:
        for storage in files[:8]:
            if not storage or not storage.filename:
                continue
            safe_original = secure_filename(storage.filename)
            if not safe_original or "." not in safe_original:
                raise ValueError("上传文件名无效")
            extension = safe_original.rsplit(".", 1)[1].lower()
            if extension not in ALLOWED_IMAGE_EXTENSIONS:
                raise ValueError("仅支持 PNG、JPG、GIF、BMP 或 WEBP 图片")
            if storage.mimetype and not (
                storage.mimetype.startswith("image/")
                or storage.mimetype == "application/octet-stream"
            ):
                raise ValueError("上传内容必须是图片")

            stored_name = f"{uuid.uuid4().hex}.{extension}"
            target = (upload_dir / stored_name).resolve()
            if target.parent != upload_dir:
                raise ValueError("上传文件名不安全")
            storage.save(target)
            saved_names.append(stored_name)
    except Exception:
        for saved_name in saved_names:
            try:
                (upload_dir / saved_name).unlink(missing_ok=True)
            except OSError:
                logger.warning("无法清理上传文件 %s", saved_name, exc_info=True)
        raise
    return saved_names


def _match_cabinet(equipment_names):
    normalized_names = [name.strip().casefold() for name in equipment_names if name.strip()]
    best_cabinet = None
    best_score = 0
    for cabinet in Cabinet.query.order_by(Cabinet.cabinet_number).all():
        candidates = [cabinet.equipment_name] + [item.component_name for item in cabinet.components]
        for requested in normalized_names:
            for candidate in candidates:
                candidate = (candidate or "").strip().casefold()
                if not candidate:
                    continue
                if requested == candidate:
                    score = 10000 + len(candidate)
                elif requested in candidate or candidate in requested:
                    score = min(len(requested), len(candidate))
                else:
                    continue
                if score > best_score:
                    best_cabinet, best_score = cabinet, score
    return best_cabinet


def _create_application(user_id, items, purpose, expected_date=None, cabinet=None, photos=None):
    if cabinet is None:
        cabinet = _match_cabinet([name for name, _quantity in items])
    application = PartApplication(
        user_id=user_id,
        equipment_name=PartApplication.pack([name for name, _quantity in items]),
        quantity=PartApplication.pack([quantity for _name, quantity in items]),
        photos=json.dumps(photos or [], ensure_ascii=False),
        purpose=purpose,
        expected_date=expected_date,
        cabinet=cabinet,
        status="pending",
    )
    db.session.add(application)
    db.session.flush()
    return application


def _application_counts(query):
    applications = query.all()
    counts = {status: 0 for status in ("pending", "approved", "rejected", "cancelled")}
    for application in applications:
        counts[application.status] = counts.get(application.status, 0) + 1
    counts["total"] = len(applications)
    return applications, counts


@parts_bp.route("/")
@login_required
def index():
    if current_user.is_admin:
        return redirect(url_for("parts.admin_dashboard"))
    return redirect(url_for("parts.applications"))


@parts_bp.route("/applications", methods=["GET"])
@login_required
def applications():
    query = PartApplication.query.filter_by(user_id=current_user.id).order_by(
        PartApplication.created_at.desc()
    )
    user_applications, counts = _application_counts(query)
    cabinets = Cabinet.query.order_by(Cabinet.cabinet_number).all()
    return render_template(
        "parts/applications.html",
        applications=user_applications,
        counts=counts,
        cabinets=cabinets,
    )


@parts_bp.route("/applications", methods=["POST"])
@parts_bp.route("/apply", methods=["POST"])
@login_required
def apply_equipment():
    saved_photos = []
    try:
        items = _form_items()
        purpose = request.form.get("purpose", "").strip()
        if not purpose:
            raise ValueError("请填写申请用途")
        if len(purpose) > 2000:
            raise ValueError("申请用途不能超过 2000 个字符")

        cabinet = None
        cabinet_id = request.form.get("cabinet_id", "").strip()
        if cabinet_id:
            try:
                cabinet = db.session.get(Cabinet, int(cabinet_id))
            except ValueError as exc:
                raise ValueError("柜子编号无效") from exc
            if cabinet is None:
                raise ValueError("所选柜子不存在")

        saved_photos = _save_uploaded_images(request.files.getlist("equipment_photos[]"))
        _create_application(
            current_user.id,
            items,
            purpose,
            expected_date=_parse_expected_date(request.form.get("expected_date")),
            cabinet=cabinet,
            photos=saved_photos,
        )
        db.session.commit()
        flash("器材申请已提交", "success")
    except ValueError as exc:
        db.session.rollback()
        for filename in saved_photos:
            (_upload_directory() / filename).unlink(missing_ok=True)
        flash(str(exc), "error")
    except Exception:
        db.session.rollback()
        for filename in saved_photos:
            (_upload_directory() / filename).unlink(missing_ok=True)
        logger.exception("提交器材申请失败")
        flash("提交失败，请稍后重试", "error")
    return redirect(url_for("parts.applications"))


@parts_bp.route("/applications/<int:application_id>/cancel", methods=["POST"])
@login_required
def cancel_application(application_id):
    application = PartApplication.query.filter_by(
        id=application_id, user_id=current_user.id
    ).first_or_404()
    if application.status != "pending":
        abort(409, description="只能取消待审批申请")
    application.status = "cancelled"
    application.updated_at = utc_now()
    db.session.commit()
    flash("申请已取消", "success")
    return redirect(url_for("parts.applications"))


@parts_bp.route("/admin")
@admin_required
def admin_dashboard():
    query = PartApplication.query.order_by(
        case(
            (PartApplication.status == "pending", 1),
            (PartApplication.status == "approved", 2),
            (PartApplication.status == "rejected", 3),
            else_=4,
        ),
        PartApplication.created_at.desc(),
    )
    all_applications, counts = _application_counts(query)
    cabinets = Cabinet.query.order_by(Cabinet.cabinet_number).all()
    settings = {key: _setting_float(key) for key in SETTING_RULES}
    return render_template(
        "parts/admin.html",
        applications=all_applications,
        counts=counts,
        cabinets=cabinets,
        settings=settings,
        user_count=User.query.filter_by(role="user").count(),
        hardware_status=hardware.status(current_app.config),
    )


def _review_application(application_id, status, reason=None):
    application = db.session.get(PartApplication, application_id)
    if application is None:
        abort(404)
    if application.status != "pending":
        abort(409, description="该申请已处理")
    application.status = status
    application.reject_reason = reason if status == "rejected" else None
    application.reviewed_by_id = current_user.id
    application.reviewed_at = utc_now()
    db.session.commit()
    return application


@parts_bp.route("/admin/applications/<int:application_id>/approve", methods=["POST"])
@parts_bp.route("/approve/<int:application_id>", methods=["POST"])
@admin_required
def approve_application(application_id):
    application = _review_application(application_id, "approved")
    if application.cabinet and not current_app.config.get("PARTS_SKIP_FACE_VERIFICATION"):
        delay = _setting_float("laser_delay")
        threading.Thread(
            target=_point_to_cabinet_safely,
            args=(application.cabinet.id, application.cabinet.cabinet_number, delay),
            daemon=True,
        ).start()
    flash("申请已批准", "success")
    return redirect(url_for("parts.admin_dashboard"))


@parts_bp.route("/admin/applications/<int:application_id>/reject", methods=["POST"])
@parts_bp.route("/reject/<int:application_id>", methods=["POST"])
@admin_required
def reject_application(application_id):
    reason = request.form.get("reject_reason", "").strip()
    if not reason:
        flash("请填写拒绝原因", "error")
        return redirect(url_for("parts.admin_dashboard"))
    _review_application(application_id, "rejected", reason[:2000])
    flash("申请已拒绝", "success")
    return redirect(url_for("parts.admin_dashboard"))


def _component_rows():
    names = request.form.getlist("component_names[]") or request.form.getlist("components[]")
    quantities = request.form.getlist("component_quantities[]")
    units = request.form.getlist("component_units[]")
    notes = request.form.getlist("component_notes[]")
    rows = []
    for index, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        if not name:
            continue
        try:
            quantity = int(quantities[index]) if index < len(quantities) else 1
        except (TypeError, ValueError) as exc:
            raise ValueError(f"元件“{name}”的数量无效") from exc
        if quantity < 0:
            raise ValueError("元件数量不能为负数")
        rows.append(
            {
                "component_name": name[:200],
                "quantity": quantity,
                "unit": ((units[index] if index < len(units) else "件").strip() or "件")[:30],
                "notes": ((notes[index] if index < len(notes) else "").strip() or None),
            }
        )
    return rows


@parts_bp.route("/admin/cabinets", methods=["POST"])
@parts_bp.route("/cabinet/create", methods=["POST"])
@admin_required
def cabinet_create():
    cabinet_number = request.form.get("cabinet_number", "").strip()
    equipment_name = request.form.get("equipment_name", "").strip()
    if not cabinet_number or not equipment_name:
        flash("柜号和器材分类不能为空", "error")
        return redirect(url_for("parts.admin_dashboard"))
    if Cabinet.query.filter_by(cabinet_number=cabinet_number).first():
        flash("柜号已存在", "error")
        return redirect(url_for("parts.admin_dashboard"))
    try:
        cabinet = Cabinet(
            cabinet_number=cabinet_number[:50],
            equipment_name=equipment_name[:200],
            description=request.form.get("description", "").strip()[:2000] or None,
        )
        cabinet.components = [CabinetComponent(**row) for row in _component_rows()]
        db.session.add(cabinet)
        db.session.commit()
        flash("柜子已创建", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("parts.admin_dashboard"))


@parts_bp.route("/admin/cabinets/<int:cabinet_id>/edit", methods=["POST"])
@parts_bp.route("/cabinet/edit/<int:cabinet_id>", methods=["POST"])
@admin_required
def cabinet_edit(cabinet_id):
    cabinet = db.session.get(Cabinet, cabinet_id)
    if cabinet is None:
        abort(404)
    cabinet_number = request.form.get("cabinet_number", "").strip()
    equipment_name = request.form.get("equipment_name", "").strip()
    if not cabinet_number or not equipment_name:
        flash("柜号和器材分类不能为空", "error")
        return redirect(url_for("parts.admin_dashboard"))
    duplicate = Cabinet.query.filter(
        Cabinet.cabinet_number == cabinet_number, Cabinet.id != cabinet.id
    ).first()
    if duplicate:
        flash("柜号已存在", "error")
        return redirect(url_for("parts.admin_dashboard"))
    try:
        cabinet.cabinet_number = cabinet_number[:50]
        cabinet.equipment_name = equipment_name[:200]
        cabinet.description = request.form.get("description", "").strip()[:2000] or None
        cabinet.components = [CabinetComponent(**row) for row in _component_rows()]
        db.session.commit()
        flash("柜子已更新", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("parts.admin_dashboard"))


@parts_bp.route("/admin/cabinets/<int:cabinet_id>/delete", methods=["POST"])
@parts_bp.route("/cabinet/delete/<int:cabinet_id>", methods=["POST"])
@admin_required
def cabinet_delete(cabinet_id):
    cabinet = db.session.get(Cabinet, cabinet_id)
    if cabinet is None:
        abort(404)
    PartApplication.query.filter_by(cabinet_id=cabinet.id).update({"cabinet_id": None})
    db.session.delete(cabinet)
    db.session.commit()
    flash("柜子已删除，历史申请已保留", "success")
    return redirect(url_for("parts.admin_dashboard"))


@parts_bp.route("/admin/cabinets/batch-delete", methods=["POST"])
@parts_bp.route("/cabinet/batch_delete", methods=["POST"])
@admin_required
def cabinet_batch_delete():
    raw_ids = request.form.getlist("ids[]")
    ids = []
    for raw_id in raw_ids:
        try:
            ids.append(int(raw_id))
        except (TypeError, ValueError):
            continue
    if ids:
        PartApplication.query.filter(PartApplication.cabinet_id.in_(ids)).update(
            {"cabinet_id": None}, synchronize_session=False
        )
        for cabinet in Cabinet.query.filter(Cabinet.id.in_(ids)).all():
            db.session.delete(cabinet)
        db.session.commit()
        flash(f"已删除 {len(ids)} 个柜子，历史申请已保留", "success")
    return redirect(url_for("parts.admin_dashboard"))


@parts_bp.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        try:
            for key, (minimum, maximum, _default) in SETTING_RULES.items():
                if key not in request.form:
                    continue
                value = float(request.form[key])
                if not minimum <= value <= maximum:
                    raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
                SystemSetting.set_value(key, value)
            group_id = request.form.get("face_group_id", "").strip()
            if group_id:
                SystemSetting.set_value("face_group_id", group_id[:100])
            db.session.commit()
            flash("设置已保存", "success")
        except (TypeError, ValueError) as exc:
            db.session.rollback()
            flash(str(exc), "error")
        return redirect(url_for("parts.settings"))
    return render_template(
        "parts/settings.html",
        settings={key: _setting_float(key) for key in SETTING_RULES},
        face_group_id=_group_id(),
        hardware_status=hardware.status(current_app.config),
    )


@parts_bp.route("/api/cabinets")
@login_required
def api_cabinets():
    cabinets = Cabinet.query.order_by(Cabinet.cabinet_number).all()
    return jsonify([cabinet.to_dict() for cabinet in cabinets])


@parts_bp.route("/api/cabinets/<int:cabinet_id>")
@login_required
def api_cabinet_detail(cabinet_id):
    cabinet = db.session.get(Cabinet, cabinet_id)
    if cabinet is None:
        return jsonify({"success": False, "message": "柜子不存在"}), 404
    data = {"success": True, "cabinet": cabinet.to_dict()}
    if current_user.is_admin:
        data["applications"] = [
            application.to_dict()
            for application in PartApplication.query.filter_by(cabinet_id=cabinet.id)
            .order_by(PartApplication.created_at.desc())
            .all()
        ]
    return jsonify(data)


@parts_bp.route("/uploads/<filename>")
@login_required
def uploaded_file(filename):
    safe_name = secure_filename(filename)
    if not safe_name or safe_name != filename:
        abort(404)
    if not current_user.is_admin:
        owns_file = any(
            safe_name in application.photo_list
            for application in PartApplication.query.filter_by(user_id=current_user.id).all()
        )
        if not owns_file:
            abort(404)
    return send_from_directory(_upload_directory(), safe_name)


@parts_bp.route("/api/hardware/status")
def hardware_status():
    status = hardware.status(current_app.config)
    status["device_auth_configured"] = bool(
        current_app.config.get("PARTS_DEVICE_TOKEN")
        or __import__("os").environ.get("PARTS_DEVICE_TOKEN")
    )
    return jsonify(status), 200 if status["available"] else 503


def _face_result(image):
    result = hardware.search_face(image, current_app.config, _group_id())
    if result.get("error_code") != 0:
        return None, f"人脸服务返回错误: {result.get('error_msg', 'unknown')}"
    matches = result.get("result", {}).get("user_list", [])
    if not matches:
        return None, "未找到匹配人脸"
    match = matches[0]
    score = float(match.get("score", 0))
    if score < _setting_float("face_threshold"):
        return None, f"匹配度过低（{score:.1f}）"
    try:
        user_id = int(match.get("user_id", ""))
    except (TypeError, ValueError):
        return None, "人脸服务返回的用户编号无效"
    user = db.session.get(User, user_id)
    if user is None:
        return None, "匹配用户不存在"
    return (user, score), None


@parts_bp.route("/api/face/login", methods=["POST"])
def face_login_api():
    data = request.get_json(silent=True) or {}
    try:
        matched, message = _face_result(data.get("image", ""))
    except (HardwareUnavailable, ValueError) as exc:
        return jsonify({"success": False, "available": False, "message": str(exc)}), 503
    except Exception:
        logger.exception("人脸登录失败")
        return jsonify({"success": False, "message": "人脸服务调用失败"}), 502
    if not matched:
        return jsonify({"success": False, "message": message}), 401
    user, score = matched
    login_user(user)
    return jsonify(
        {
            "success": True,
            "message": f"欢迎 {user.username}",
            "score": score,
            "redirect": url_for("parts.index"),
        }
    )


@parts_bp.route("/api/face/register", methods=["POST"])
@login_required
def face_register_api():
    data = request.get_json(silent=True) or {}
    try:
        result = hardware.register_face(
            current_user.id, data.get("image", ""), current_app.config, _group_id()
        )
    except (HardwareUnavailable, ValueError) as exc:
        return jsonify({"success": False, "available": False, "message": str(exc)}), 503
    except Exception:
        logger.exception("人脸注册失败")
        return jsonify({"success": False, "message": "人脸服务调用失败"}), 502
    if result.get("error_code") != 0:
        return jsonify({"success": False, "message": result.get("error_msg", "注册失败")}), 502
    current_user.face_registered = True
    db.session.commit()
    return jsonify({"success": True, "message": "人脸注册成功"})


def _device_authorized():
    import os

    expected = str(
        current_app.config.get("PARTS_DEVICE_TOKEN")
        or os.environ.get("PARTS_DEVICE_TOKEN", "")
    )
    if not expected:
        return True
    supplied = request.headers.get("X-Device-Token", "")
    if not supplied and request.headers.get("Authorization", "").startswith("Bearer "):
        supplied = request.headers["Authorization"][7:]
    return secrets.compare_digest(expected, supplied)


def _device_token_configured():
    import os

    return bool(
        str(
            current_app.config.get("PARTS_DEVICE_TOKEN")
            or os.environ.get("PARTS_DEVICE_TOKEN", "")
        ).strip()
    )


def _parse_borrow_quantity(raw_value):
    try:
        quantity = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("借用数量必须是整数") from exc
    if quantity < 1 or quantity > 100000:
        raise ValueError("借用数量必须在 1 到 100000 之间")
    return quantity


def _parse_borrow_request_id(raw_value):
    request_id = str(raw_value or "").strip()
    if not request_id:
        raise ValueError("缺少借用请求编号")
    if len(request_id) > 80 or not all(char.isalnum() or char in "-_" for char in request_id):
        raise ValueError("借用请求编号格式无效")
    return request_id


def _borrow_result(application, request_id, score=None, duplicate=False):
    result = {
        "success": True,
        "message": "借用申请已提交，等待管理员审批。",
        "application_id": application.id,
        "request_id": request_id,
        "username": application.applicant.username if application.applicant else "",
        "status": application.status,
        "items": application.equipment_items,
        "cabinet_number": application.cabinet.cabinet_number if application.cabinet else "",
        "duplicate": duplicate,
    }
    if score is not None:
        result["face_score"] = round(float(score), 1)
    return result


def _existing_borrow_request(request_id):
    marker = f"[MCP 借用请求:{request_id}]"
    return (
        PartApplication.query.filter(PartApplication.purpose.contains(marker))
        .order_by(PartApplication.id.desc())
        .first()
    )


def _hardware_ready_for_device_borrow():
    status = hardware.status(current_app.config)
    if status.get("available") and status.get("pan_tilt", {}).get("available"):
        return None
    missing = []
    for name, label in (("face", "人脸服务"), ("camera", "相机"), ("pan_tilt", "云台")):
        detail = status.get(name, {})
        if not detail.get("available"):
            missing.append(f"{label}：{detail.get('message', '不可用')}")
    return "；".join(missing) or "树莓派硬件未就绪"


def _run_borrow_without_face(component, quantity, request_id, source):
    """Create a clearly marked pending request while camera verification is disabled."""

    applicant_email = str(
        current_app.config.get("PARTS_FALLBACK_APPLICANT_EMAIL")
        or "xiaozhi-bot@local.test"
    ).strip().lower()
    user = User.query.filter_by(email=applicant_email).first()
    if user is None:
        return {
            "success": False,
            "message": "跳过人脸识别时使用的系统申请账号不存在。",
        }, 503

    try:
        application = _create_application(
            user.id,
            [(component.component_name, str(quantity))],
            f"[MCP 借用请求:{request_id}] [未进行人脸识别] 来源: {source}，摄像头暂不可用",
            cabinet=component.cabinet,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception("创建免人脸识别借用申请失败")
        return {"success": False, "message": "借用申请创建失败，请稍后重试。"}, 500

    result = _borrow_result(application, request_id)
    result.update(
        {
            "message": "借用申请已提交，等待管理员审批；本次未进行人脸识别。",
            "face_verification": "skipped",
        }
    )
    return result, 201


def _run_device_borrow(component_id, quantity, request_id, source):
    """Use one camera/servo operation to authenticate a borrower and create one pending application."""

    existing = _existing_borrow_request(request_id)
    if existing is not None:
        return _borrow_result(existing, request_id, duplicate=True), 200

    component = db.session.get(CabinetComponent, component_id)
    if component is None:
        return {"success": False, "message": "未找到指定零件，请先重新查询库存。"}, 404
    if quantity > component.quantity:
        return {
            "success": False,
            "message": f"库存不足：{component.component_name} 当前仅剩 {component.quantity}{component.unit}。",
            "available_quantity": component.quantity,
            "component_id": component.id,
        }, 409

    if current_app.config.get("PARTS_SKIP_FACE_VERIFICATION"):
        return _run_borrow_without_face(component, quantity, request_id, source)

    unavailable = _hardware_ready_for_device_borrow()
    if unavailable:
        return {"success": False, "available": False, "message": unavailable}, 503

    pan = _setting_float("face_pan_angle")
    tilt = _setting_float("face_tilt_angle")
    try:
        image = hardware.capture_face_at_position(pan, tilt)
        matched, message = _face_result(image)
        if not matched:
            return {"success": False, "message": message or "人脸识别失败"}, 401

        user, score = matched
        application = _create_application(
            user.id,
            [(component.component_name, str(quantity))],
            f"[MCP 借用请求:{request_id}] [人脸识别自动借用申请] 来源: {source}，匹配分数: {score:.1f}",
            cabinet=component.cabinet,
        )
        db.session.commit()
        result = _borrow_result(application, request_id, score=score)
    except (HardwareUnavailable, ValueError) as exc:
        db.session.rollback()
        return {"success": False, "message": str(exc)}, 503
    except Exception:
        db.session.rollback()
        logger.exception("处理 MCP 借用申请失败")
        return {"success": False, "message": "借用申请处理失败，请稍后重试。"}, 500

    _point_after_application(application)
    return result, 201


@parts_bp.route("/api/device/borrow", methods=["POST"])
def device_borrow_api():
    """MCP-only borrow endpoint: exact component, quantity, face verification and pending application."""

    if not _device_token_configured():
        return jsonify({"success": False, "message": "借用接口未配置 PARTS_DEVICE_TOKEN"}), 503
    if not _device_authorized():
        return jsonify({"success": False, "message": "设备认证失败"}), 401
    data = request.get_json(silent=True) or {}
    try:
        component_id = int(data.get("component_id", ""))
        if component_id <= 0:
            raise ValueError("零件编号无效")
        quantity = _parse_borrow_quantity(data.get("quantity", 1))
        request_id = _parse_borrow_request_id(data.get("request_id"))
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    source = str(data.get("source", "xiaozhi-mcp")).strip()[:80] or "xiaozhi-mcp"
    with device_borrow_lock:
        result, status_code = _run_device_borrow(component_id, quantity, request_id, source)
    return jsonify(result), status_code


@parts_bp.route("/api/face/verify", methods=["POST"])
def face_verify_api():
    if not _device_authorized():
        return jsonify({"success": False, "message": "设备认证失败"}), 401
    data = request.get_json(silent=True) or {}
    equipment_name = str(data.get("equipment_name", "")).strip()
    if not equipment_name:
        return jsonify({"success": False, "message": "缺少器材名称"}), 400
    try:
        matched, message = _face_result(data.get("image", ""))
    except (HardwareUnavailable, ValueError) as exc:
        return jsonify({"success": False, "available": False, "message": str(exc)}), 503
    except Exception:
        logger.exception("人脸验证失败")
        return jsonify({"success": False, "message": "人脸服务调用失败"}), 502
    if not matched:
        return jsonify({"success": False, "message": message}), 401
    user, score = matched
    application = _create_application(
        user.id,
        [(equipment_name[:200], str(data.get("quantity", "1"))[:30] or "1")],
        f"[人脸验证自动申请] 匹配分数: {score:.1f}",
    )
    db.session.commit()
    _point_after_application(application)
    return jsonify(
        {
            "success": True,
            "username": user.username,
            "score": score,
            "application_id": application.id,
            "equipment": equipment_name,
        }
    )


@parts_bp.route("/api/face/apply", methods=["POST"])
def face_apply_api():
    if not _device_authorized():
        return jsonify({"success": False, "message": "设备认证失败"}), 401
    data = request.get_json(silent=True) or {}
    equipment_name = str(data.get("equipment_name", "")).strip()
    if not equipment_name:
        return jsonify({"success": False, "message": "缺少器材名称"}), 400
    try:
        user_id = int(data.get("user_id", ""))
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "用户编号无效"}), 400
    user = db.session.get(User, user_id)
    if user is None:
        return jsonify({"success": False, "message": "用户不存在"}), 404
    quantity = str(data.get("quantity", "1")).strip()[:30] or "1"
    device = str(data.get("device", "esp32")).strip()[:80] or "esp32"
    score = data.get("score", 0)
    application = _create_application(
        user.id,
        [(equipment_name[:200], quantity)],
        f"[人脸识别自动申请] 来自 {device}，匹配分数: {score}",
    )
    db.session.commit()
    _point_after_application(application)
    return jsonify(
        {
            "success": True,
            "message": f"已为 {user.username} 创建申请",
            "application_id": application.id,
            "equipment": equipment_name,
            "quantity": quantity,
        }
    ), 201


def _point_to_cabinet_safely(cabinet_id, cabinet_number, delay):
    try:
        tag_id = int(str(cabinet_number).strip())
        if tag_id < 0:
            raise ValueError
    except (TypeError, ValueError):
        logger.warning(
            "柜子 %s 的编号 %r 不是有效的 AprilTag ID，已跳过云台定位",
            cabinet_id,
            cabinet_number,
        )
        return
    try:
        with device_borrow_lock:
            hardware.point_to_cabinet(tag_id, delay)
    except Exception:
        logger.warning("云台无法指向柜子 %s", cabinet_id, exc_info=True)


def _point_after_application(application):
    if current_app.config.get("PARTS_SKIP_FACE_VERIFICATION") or not application.cabinet:
        return
    threading.Thread(
        target=_point_to_cabinet_safely,
        args=(
            application.cabinet.id,
            application.cabinet.cabinet_number,
            _setting_float("laser_delay"),
        ),
        daemon=True,
    ).start()


def _command_worker(app, equipment_name):
    with app.app_context():
        with device_borrow_lock:
            try:
                pan = _setting_float("face_pan_angle")
                tilt = _setting_float("face_tilt_angle")
                image = hardware.capture_face_at_position(pan, tilt)
                matched, message = _face_result(image)
                if not matched:
                    logger.warning("ESP32 命令人脸验证未通过: %s", message)
                    return
                user, score = matched
                application = _create_application(
                    user.id,
                    [(equipment_name[:200], "1")],
                    f"[ESP32 自动申请] 人脸匹配分数: {score:.1f}",
                )
                db.session.commit()
                _point_after_application(application)
            except Exception:
                db.session.rollback()
                logger.exception("处理 ESP32 命令失败")


def command_api():
    equipment_name = request.args.get("cmd", "").strip()
    if not equipment_name:
        return "ERROR: Empty command", 400
    status = hardware.status(current_app.config)
    if not status["available"]:
        missing = "; ".join(
            value["message"]
            for key, value in status.items()
            if key in {"face", "camera"} and not value["available"]
        )
        return f"ERROR: Hardware unavailable: {missing}", 503
    app = current_app._get_current_object()
    threading.Thread(target=_command_worker, args=(app, equipment_name), daemon=True).start()
    return f"OK: {equipment_name}", 202


@admin_required
def laser_delay_compat_api():
    if request.method == "GET":
        return jsonify({"delay": _setting_float("laser_delay")})
    data = request.get_json(silent=True) or request.form
    try:
        value = float(data.get("delay", 3.0))
        minimum, maximum, _default = SETTING_RULES["laser_delay"]
        if not minimum <= value <= maximum:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "激光延时必须在 0 到 60 秒之间"}), 400
    SystemSetting.set_value("laser_delay", value)
    db.session.commit()
    if request.is_json:
        return jsonify({"success": True, "delay": value})
    return redirect(url_for("parts.settings"))


@admin_required
def face_angles_compat_api():
    if request.method == "GET":
        return jsonify(
            {"pan": _setting_float("face_pan_angle"), "tilt": _setting_float("face_tilt_angle")}
        )
    data = request.get_json(silent=True) or request.form
    try:
        pan = float(data.get("pan", 0))
        tilt = float(data.get("tilt", 0))
        if not -180 <= pan <= 180 or not -180 <= tilt <= 180:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "云台角度必须在 -180 到 180 之间"}), 400
    SystemSetting.set_value("face_pan_angle", pan)
    SystemSetting.set_value("face_tilt_angle", tilt)
    db.session.commit()
    if request.is_json:
        return jsonify({"success": True, "pan": pan, "tilt": tilt})
    return redirect(url_for("parts.settings"))


@parts_bp.record_once
def register_compatibility_routes(state):
    """Register legacy app-level URLs despite the blueprint's /parts prefix."""

    app = state.app
    routes = (
        ("/command", "parts.command_compat", command_api, ["GET"]),
        ("/api/face_login", "parts.face_login_compat", face_login_api, ["POST"]),
        ("/api/face_register", "parts.face_register_compat", face_register_api, ["POST"]),
        ("/api/face_verify", "parts.face_verify_compat", face_verify_api, ["POST"]),
        ("/api/face_apply", "parts.face_apply_compat", face_apply_api, ["POST"]),
        ("/api/hardware/status", "parts.hardware_status_compat", hardware_status, ["GET"]),
        ("/api/face_status", "parts.face_status_compat", hardware_status, ["GET"]),
        ("/api/laser_delay", "parts.laser_delay_compat", laser_delay_compat_api, ["GET", "POST"]),
        ("/api/face_angles", "parts.face_angles_compat", face_angles_compat_api, ["GET", "POST"]),
    )
    for rule, endpoint, view, methods in routes:
        if endpoint not in app.view_functions:
            app.add_url_rule(rule, endpoint, view, methods=methods)

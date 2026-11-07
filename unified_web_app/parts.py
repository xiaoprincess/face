import base64
import binascii
import json
import logging
import re
import secrets
import threading
import time
import uuid
from datetime import date
from contextlib import contextmanager
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
from werkzeug.utils import secure_filename
from sqlalchemy.exc import IntegrityError

from apriltag_test import JobCapacityError, JobConflictError, registry as tag_test_registry
from core_models import User
from extensions import db, utc_now
from hardware import HardwareUnavailable, hardware
from parts_models import Cabinet, CabinetComponent, InventoryMovement, PartApplication, SystemSetting


parts_bp = Blueprint("parts", __name__, url_prefix="/parts")
logger = logging.getLogger(__name__)
device_borrow_lock = threading.RLock()
tag_owner_lock = threading.RLock()
active_tag_job = None


@contextmanager
def _tag_job_ownership(job):
    """Use inside device_borrow_lock; serialize owner handoff and emergency stop."""
    global active_tag_job
    with tag_owner_lock:
        active_tag_job = job
    try:
        yield
    finally:
        with tag_owner_lock:
            if active_tag_job is job:
                active_tag_job = None
            job.cancel_timers()


def _stop_owned_tag_job(job):
    with tag_owner_lock:
        if active_tag_job is not job:
            return None
        try:
            return hardware.emergency_stop() is True
        except Exception:
            logger.exception("任务紧急停止失败")
            return False

# Tag family used by the pan/tilt camera. tag36h11 defines ids 0..586.
APRILTAG_FAMILY = "tag36h11"
APRILTAG_MAX_ID = 586
TAG_TEST_DEFAULT_TIMEOUT = 45.0
TAG_TEST_MIN_TIMEOUT = 10.0
TAG_TEST_MAX_TIMEOUT = 180.0
TAG_TEST_FORCE_STOP_GRACE = 15.0
TAG_FACE_TIMEOUT_DEFAULT = 30.0
TAG_FACE_TIMEOUT_MIN = 5.0
TAG_FACE_TIMEOUT_MAX = 60.0

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


def _validated_face_data_url(value):
    value = str(value or "").strip()
    match = re.fullmatch(r"data:image/jpeg;base64,([A-Za-z0-9+/=]+)", value)
    if not match:
        raise ValueError("请先使用当前电脑的摄像头拍摄 JPEG 照片")
    try:
        image_bytes = base64.b64decode(match.group(1), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("摄像头照片格式无效，请重新拍摄") from exc
    if len(image_bytes) < 1024:
        raise ValueError("摄像头照片内容不完整，请重新拍摄")
    if len(image_bytes) > 5 * 1024 * 1024:
        raise ValueError("摄像头照片不能超过 5 MB")
    if not image_bytes.startswith(b"\xff\xd8\xff"):
        raise ValueError("摄像头照片不是有效的 JPEG 图片，请重新拍摄")
    return value


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
        raise ValueError("单次借用登记最多包含 20 种器材")
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


def _create_application(user_id, items, purpose, expected_date=None, cabinet=None, photos=None,
                        component_quantities=None, request_id=None, request_payload=None):
    # Only explicit ids authorize stock changes. Free text is a legacy record.
    quantities = {}
    for component_id, quantity in component_quantities or []:
        quantity = _parse_borrow_quantity(quantity)
        quantities[component_id] = quantities.get(component_id, 0) + quantity
    if quantities:
        components = [db.session.get(CabinetComponent, component_id) for component_id in quantities]
        if any(component is None for component in components):
            raise ValueError("所选库存零件不存在")
        if len({component.cabinet_id for component in components}) != 1:
            raise ValueError("一次库存借用只能选择同一柜子的零件")
        cabinet = components[0].cabinet
        items = [(component.component_name, str(quantities[component.id])) for component in components]
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
        status="approved",  # Legacy DB value for a completed, directly recorded loan.
        request_id=request_id,
        request_payload=request_payload,
        inventory_managed=bool(quantities),
        guidance_status=("queued" if cabinet and not current_app.config.get("PARTS_SKIP_FACE_VERIFICATION") else "skipped"),
        guidance_message=("等待指引" if cabinet and not current_app.config.get("PARTS_SKIP_FACE_VERIFICATION") else "未关联柜子或已跳过硬件指引"),
        guidance_updated_at=utc_now(),
    )
    db.session.add(application)
    db.session.flush()
    for component_id, quantity in sorted(quantities.items()):
        changed = CabinetComponent.query.filter(
            CabinetComponent.id == component_id, CabinetComponent.quantity >= quantity,
        ).update({CabinetComponent.quantity: CabinetComponent.quantity - quantity}, synchronize_session=False)
        if changed != 1:
            raise ValueError("库存不足，请重新查询可用库存")
        balance = db.session.query(CabinetComponent.quantity).filter_by(id=component_id).scalar()
        db.session.add(InventoryMovement(application_id=application.id, component_id=component_id,
                                         kind="borrow", delta=-quantity, quantity_after=balance, actor_id=user_id))
    db.session.flush()
    # Bulk conditional updates must not leave cached component quantities stale.
    db.session.expire_all()
    return application


def _application_counts(query):
    applications = query.all()
    counts = {"recorded": 0, "historical": 0, "returned": 0}
    for application in applications:
        key = application.record_status if application.record_status in {"recorded", "returned"} else "historical"
        counts[key] += 1
    counts["total"] = len(applications)
    return applications, counts


@parts_bp.route("/")
@login_required
def index():
    if current_user.is_admin:
        return redirect(url_for("parts.admin_dashboard"))
    return redirect(url_for("parts.applications"))


@parts_bp.route("/applications", methods=["GET"])
@parts_bp.route("/records", methods=["GET"])
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
@parts_bp.route("/records", methods=["POST"])
@login_required
def apply_equipment():
    saved_photos = []
    try:
        selected = request.form.getlist("component_ids[]")
        quantities = request.form.getlist("quantities[]")
        component_quantities = None
        if any(selected):
            if not all(selected) or len(selected) != len(quantities):
                raise ValueError("库存借用请为每项选择零件；自由文本请单独登记")
            component_quantities = [(int(component_id), _parse_borrow_quantity(quantity))
                                    for component_id, quantity in zip(selected, quantities)]
            items = []  # Names are taken from the selected ids by the transaction.
        else:
            items = _form_items()
        purpose = request.form.get("purpose", "").strip()
        if not purpose:
            raise ValueError("请填写借用用途")
        if len(purpose) > 2000:
            raise ValueError("借用用途不能超过 2000 个字符")

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
        application = _create_application(
            current_user.id,
            items,
            purpose,
            expected_date=_parse_expected_date(request.form.get("expected_date")),
            cabinet=cabinet,
            photos=saved_photos,
            component_quantities=component_quantities,
        )
        db.session.commit()
        _point_after_application(application)
        flash("借用已保存并扣减可用库存" if application.inventory_managed else "自由文本记录已保存，未关联库存，不支持归还入库", "success")
    except ValueError as exc:
        db.session.rollback()
        for filename in saved_photos:
            (_upload_directory() / filename).unlink(missing_ok=True)
        flash(str(exc), "error")
    except Exception:
        db.session.rollback()
        for filename in saved_photos:
            (_upload_directory() / filename).unlink(missing_ok=True)
        logger.exception("保存借用记录失败")
        flash("提交失败，请稍后重试", "error")
    return redirect(url_for("parts.applications"))


@parts_bp.route("/admin")
@admin_required
def admin_dashboard():
    active_tab = request.args.get("tab", "inventory").strip().lower()
    if active_tab == "applications":  # Existing bookmarks remain valid.
        active_tab = "records"
    if active_tab not in {"records", "inventory", "create"}:
        active_tab = "inventory"
    query = PartApplication.query.order_by(
        PartApplication.created_at.desc(),
        PartApplication.id.desc(),
    )
    all_applications, counts = _application_counts(query)
    cabinets = Cabinet.query.order_by(Cabinet.cabinet_number).all()
    component_count = sum(len(cabinet.components) for cabinet in cabinets)
    low_stock_count = sum(
        1
        for cabinet in cabinets
        for component in cabinet.components
        if component.quantity <= 5
    )
    settings = {key: _setting_float(key) for key in SETTING_RULES}
    return render_template(
        "parts/admin.html",
        active_tab=active_tab,
        applications=all_applications,
        counts=counts,
        cabinets=cabinets,
        component_count=component_count,
        low_stock_count=low_stock_count,
        settings=settings,
        user_count=User.query.filter_by(role="user").count(),
        hardware_status=hardware.status(current_app.config),
    )


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
        return redirect(url_for("parts.admin_dashboard", tab="create"))
    if Cabinet.query.filter_by(cabinet_number=cabinet_number).first():
        flash("柜号已存在", "error")
        return redirect(url_for("parts.admin_dashboard", tab="create"))
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
        return redirect(
            url_for("parts.admin_dashboard", tab="inventory", _anchor=f"cabinet-{cabinet.id}")
        )
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(url_for("parts.admin_dashboard", tab="create"))


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
        return redirect(
            url_for("parts.admin_dashboard", tab="inventory", _anchor=f"cabinet-{cabinet.id}")
        )
    duplicate = Cabinet.query.filter(
        Cabinet.cabinet_number == cabinet_number, Cabinet.id != cabinet.id
    ).first()
    if duplicate:
        flash("柜号已存在", "error")
        return redirect(
            url_for("parts.admin_dashboard", tab="inventory", _anchor=f"cabinet-{cabinet.id}")
        )
    try:
        ids = request.form.getlist("component_ids[]")
        names = request.form.getlist("component_names[]")
        rows = _component_rows()
        if ids:
            originals = request.form.getlist("component_original_quantities[]")
            if len(ids) != len(names) or len(originals) != len(names):
                raise ValueError("零件列表已过期，请刷新页面后编辑")
            existing = {item.id: item for item in cabinet.components}
            seen = set()
            row_iter = iter(rows)
            for index, name in enumerate(names):
                if not str(name or "").strip():
                    continue
                row = next(row_iter)
                if not ids[index]:
                    db.session.add(CabinetComponent(cabinet=cabinet, **row))
                    continue
                try:
                    component_id = int(ids[index])
                    old_quantity = int(originals[index])
                except (TypeError, ValueError) as exc:
                    raise ValueError("零件编号或原始库存无效，请刷新页面") from exc
                if component_id not in existing or component_id in seen:
                    raise ValueError("零件编号不属于该柜或重复")
                seen.add(component_id)
                changed = CabinetComponent.query.filter_by(id=component_id, cabinet_id=cabinet.id, quantity=old_quantity).update(row, synchronize_session=False)
                if changed != 1:
                    raise ValueError("库存已被借还操作更新，请刷新后重新编辑")
            for component_id, component in existing.items():
                if component_id not in seen:
                    if InventoryMovement.query.filter_by(component_id=component_id).first():
                        raise ValueError("有借还流水的零件不能删除，可保留编号修改名称和数量")
                    db.session.delete(component)
        else:
            if _cabinet_has_inventory_history(cabinet.id):
                raise ValueError("该柜有库存借还流水，不能重建零件；请刷新后保留原零件编号编辑")
            cabinet.components = [CabinetComponent(**row) for row in rows]
        cabinet.cabinet_number = cabinet_number[:50]
        cabinet.equipment_name = equipment_name[:200]
        cabinet.description = request.form.get("description", "").strip()[:2000] or None
        db.session.commit()
        flash("柜子已更新", "success")
    except ValueError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    return redirect(
        url_for("parts.admin_dashboard", tab="inventory", _anchor=f"cabinet-{cabinet.id}")
    )


@parts_bp.route("/admin/cabinets/<int:cabinet_id>/delete", methods=["POST"])
@parts_bp.route("/cabinet/delete/<int:cabinet_id>", methods=["POST"])
@admin_required
def cabinet_delete(cabinet_id):
    cabinet = db.session.get(Cabinet, cabinet_id)
    if cabinet is None:
        abort(404)
    if _cabinet_has_inventory_history(cabinet.id):
        flash("该柜有库存借还流水，不能删除", "error")
        return redirect(url_for("parts.admin_dashboard", tab="inventory"))
    PartApplication.query.filter_by(cabinet_id=cabinet.id).update({"cabinet_id": None})
    db.session.delete(cabinet)
    db.session.commit()
    flash("柜子已删除，历史借用记录已保留", "success")
    return redirect(url_for("parts.admin_dashboard", tab="inventory"))


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
        if any(_cabinet_has_inventory_history(cabinet_id) for cabinet_id in ids):
            flash("所选柜子有库存借还流水，未执行批量删除", "error")
            return redirect(url_for("parts.admin_dashboard", tab="inventory"))
        PartApplication.query.filter(PartApplication.cabinet_id.in_(ids)).update(
            {"cabinet_id": None}, synchronize_session=False
        )
        for cabinet in Cabinet.query.filter(Cabinet.id.in_(ids)).all():
            db.session.delete(cabinet)
        db.session.commit()
        flash(f"已删除 {len(ids)} 个柜子，历史借用记录已保留", "success")
    return redirect(url_for("parts.admin_dashboard", tab="inventory"))


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
    status["device_auth_configured"] = _device_token_configured()
    return jsonify(status), 200 if status["available"] else 503


class FaceMatch(tuple):
    """Preserve the face-library ID without changing two-value unpacking."""

    def __new__(cls, user, score, face_id):
        value = super().__new__(cls, (user, score))
        value.face_id = str(face_id)
        return value


def _face_result_legacy(image):
    """Baidu search exactly like web/start.py: no quality/liveness options, score >= 70."""

    try:
        result = hardware.search_face_raw(image, current_app.config, _group_id())
    except HardwareUnavailable:
        raise
    if result.get("error_code") != 0:
        return None, f"人脸服务返回错误: {result.get('error_msg', 'unknown')}"
    matches = result.get("result", {}).get("user_list", [])
    if not matches:
        return None, "未找到匹配人脸"
    match = matches[0]
    try:
        score = float(match.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    if score < 70:
        return None, f"匹配度过低（{score:.1f}）"
    try:
        user_id = int(match.get("user_id", ""))
    except (TypeError, ValueError):
        return None, "人脸服务返回的用户编号无效"
    user = db.session.get(User, user_id)
    if user is None:
        return None, "匹配用户不存在"
    return FaceMatch(user, score, match['user_id']), None


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
    if {"user_id", "capture", "replace"}.intersection(data):
        return jsonify({"success": False, "message": "人脸登录不允许指定账号或服务器摄像头"}), 400
    try:
        image = _validated_face_data_url(data.get("image"))
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    try:
        matched, message = _face_result(image)
    except HardwareUnavailable as exc:
        return jsonify({"success": False, "available": False, "message": str(exc)}), 503
    except Exception:
        logger.exception("人脸登录失败")
        return jsonify({"success": False, "message": "人脸服务调用失败"}), 502
    if not matched:
        return jsonify({"success": False, "message": message}), 401
    user, score = matched
    if not user.face_registered:
        return jsonify({"success": False, "message": "该账号尚未完成人脸录入"}), 401
    user.last_seen = utc_now()
    db.session.commit()
    login_user(user)
    return jsonify(
        {
            "success": True,
            "message": f"欢迎 {user.username}",
            "score": score,
            "redirect": url_for("dashboard"),
        }
    )


@parts_bp.get("/face")
@login_required
def face_enrollment():
    return render_template("parts/face.html")


@parts_bp.route("/api/face/register", methods=["POST"])
@login_required
def face_register_api():
    data = request.get_json(silent=True) or {}
    forbidden_fields = {"user_id", "replace", "capture"}.intersection(data)
    if forbidden_fields:
        return jsonify({"success": False, "message": "不允许指定账号、更新方式或服务器摄像头"}), 400
    password = str(data.get("password") or "")
    if not password or not current_user.check_password(password):
        return jsonify({"success": False, "message": "当前密码不正确"}), 403
    try:
        image = _validated_face_data_url(data.get("image"))
        replacing = bool(current_user.face_registered)
        result = hardware.register_face(
            current_user.id,
            image,
            current_app.config,
            _group_id(),
            replace=replacing,
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
    return jsonify(
        {
            "success": True,
            "registered": True,
            "message": "人脸信息已更新" if replacing else "人脸注册成功",
        }
    )


def _device_authorized():
    expected = str(current_app.config.get("PARTS_DEVICE_TOKEN") or "").strip()
    if not expected:
        return False
    supplied = request.headers.get("X-Device-Token", "")
    if not supplied and request.headers.get("Authorization", "").startswith("Bearer "):
        supplied = request.headers["Authorization"][7:]
    return secrets.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def _device_token_configured():
    return bool(str(current_app.config.get("PARTS_DEVICE_TOKEN") or "").strip())


def _cabinet_has_inventory_history(cabinet_id):
    # Acquire a write lock before checking, closing check/delete races on SQLite.
    Cabinet.query.filter_by(id=cabinet_id).update({Cabinet.id: Cabinet.id}, synchronize_session=False)
    return db.session.query(InventoryMovement.id).join(
        CabinetComponent, InventoryMovement.component_id == CabinetComponent.id,
    ).filter(CabinetComponent.cabinet_id == cabinet_id).first() is not None


def _parse_borrow_quantity(raw_value):
    if isinstance(raw_value, bool) or not re.fullmatch(r"[0-9]+", str(raw_value).strip()):
        raise ValueError("借用数量必须是整数")
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


def _tag_test_enabled():
    return bool(current_app.config.get("PARTS_TAG_TEST_ENABLED", True))


def _tag_lost_timeout():
    """Seconds the camera keeps waiting for the target tag before failing."""

    try:
        value = float(current_app.config.get("PARTS_TAG_LOST_TIMEOUT", 8))
    except (TypeError, ValueError):
        value = 8.0
    return min(max(value, 2.0), 30.0)


def _tag_face_timeout():
    """How long the camera keeps retrying face recognition, like the old Pi flow."""

    try:
        value = float(current_app.config.get("PARTS_TAG_FACE_TIMEOUT", TAG_FACE_TIMEOUT_DEFAULT))
    except (TypeError, ValueError):
        value = TAG_FACE_TIMEOUT_DEFAULT
    return min(max(value, TAG_FACE_TIMEOUT_MIN), TAG_FACE_TIMEOUT_MAX)


def _parse_tag_id(raw_value):
    try:
        tag_id = int(str(raw_value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("AprilTag ID 必须是整数") from exc
    if tag_id < 0 or tag_id > APRILTAG_MAX_ID:
        raise ValueError(f"AprilTag ID 必须在 0 到 {APRILTAG_MAX_ID} 之间（{APRILTAG_FAMILY}）")
    return tag_id


def _parse_tag_test_request_id(raw_value):
    request_id = str(raw_value or "").strip()
    if not request_id:
        raise ValueError("缺少测试请求编号")
    if len(request_id) > 80 or not all(char.isalnum() or char in "-_" for char in request_id):
        raise ValueError("测试请求编号只能包含字母、数字、连字符或下划线")
    return request_id


def _parse_tag_test_timeout(raw_value):
    if raw_value in (None, ""):
        return TAG_TEST_DEFAULT_TIMEOUT
    try:
        timeout = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("定位时限必须是数字") from exc
    if not TAG_TEST_MIN_TIMEOUT <= timeout <= TAG_TEST_MAX_TIMEOUT:
        raise ValueError(
            f"定位时限必须在 {TAG_TEST_MIN_TIMEOUT:.0f} 到 {TAG_TEST_MAX_TIMEOUT:.0f} 秒之间"
        )
    return timeout


def _borrow_result(application, request_id, score=None, duplicate=False):
    result = {
        "success": True,
        "message": "借用已登记，无需审批。",
        "record_id": application.id,
        "application_id": application.id,
        "request_id": request_id,
        "username": application.applicant.username if application.applicant else "",
        "status": application.record_status,
        "items": application.equipment_items,
        "cabinet_number": application.cabinet.cabinet_number if application.cabinet else "",
        "duplicate": duplicate,
        "inventory_managed": application.inventory_managed,
        "inventory_status": application.inventory_status,
        "returned_at": application.returned_at.isoformat() if application.returned_at else None,
        "guidance_status": application.guidance_status,
        "guidance_status_label": application.guidance_status_label,
        "guidance_message": application.guidance_message or "",
        "status_url": url_for("parts.device_borrow_status_api", request_id=request_id) if request_id else url_for("parts.application_status_api", application_id=application.id),
    }
    if score is not None:
        result["face_score"] = round(float(score), 1)
    return result


def _existing_borrow_request(request_id):
    return PartApplication.query.filter_by(request_id=request_id).first()


def _borrow_payload(component_id, quantity, source):
    return json.dumps({"component_id": component_id, "quantity": quantity, "source": source}, sort_keys=True)


def _duplicate_borrow_result(existing, payload):
    if existing.request_payload != payload or not existing.inventory_managed:
        return {"success": False, "message": "请求编号已使用且参数不同，或属于无法核对库存的历史记录", "duplicate": True, "record_id": existing.id}, 409
    return _borrow_result(existing, existing.request_id, duplicate=True), 200


def _commit_device_borrow(user_id, component, quantity, request_id, source, purpose, score=None):
    payload = _borrow_payload(component.id, quantity, source)
    try:
        application = _create_application(
            user_id, [], purpose, component_quantities=[(component.id, quantity)],
            request_id=request_id, request_payload=payload,
        )
        db.session.commit()
    except IntegrityError:
        # Other workers/processes are arbitrated by the DB unique index.
        db.session.rollback()
        existing = _existing_borrow_request(request_id)
        if existing is not None:
            return _duplicate_borrow_result(existing, payload)
        logger.exception("库存流水约束冲突")
        return {"success": False, "message": "库存事务冲突，请重试"}, 409
    except ValueError as exc:
        db.session.rollback()
        return {"success": False, "message": str(exc)}, 409
    except Exception:
        db.session.rollback()
        logger.exception("保存借用库存事务失败")
        return {"success": False, "message": "借用登记失败，请稍后重试"}, 500
    _point_after_application(application)
    return _borrow_result(application, request_id, score=score), 201


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
    """Record a loan with an explicit unverified marker while the camera is disabled."""

    applicant_email = str(
        current_app.config.get("PARTS_FALLBACK_APPLICANT_EMAIL")
        or "xiaozhi-bot@local.test"
    ).strip().lower()
    user = User.query.filter_by(email=applicant_email).first()
    if user is None:
        return {
            "success": False,
            "message": "跳过人脸识别时使用的系统借用账号不存在。",
        }, 503

    result, status_code = _commit_device_borrow(
        user.id, component, quantity, request_id, source,
        f"[MCP 借用请求:{request_id}] [未进行人脸识别] 来源: {source}",
    )
    if result.get("success"):
        result["face_verification"] = "skipped"
    return result, status_code


def _run_device_borrow(component_id, quantity, request_id, source):
    """Authenticate a borrower and directly record one loan, without an approval step."""

    existing = _existing_borrow_request(request_id)
    if existing is not None:
        return _duplicate_borrow_result(existing, _borrow_payload(component_id, quantity, source))

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
        return _commit_device_borrow(
            user.id, component, quantity, request_id, source,
            f"[MCP 借用请求:{request_id}] [人脸识别借用登记] 来源: {source}，匹配分数: {score:.1f}",
            score=score,
        )
    except (HardwareUnavailable, ValueError) as exc:
        db.session.rollback()
        return {"success": False, "message": str(exc)}, 503
    except Exception:
        db.session.rollback()
        logger.exception("处理 MCP 借用登记失败")
        return {"success": False, "message": "借用登记失败，请稍后重试。"}, 500

@parts_bp.route("/api/device/borrow", methods=["POST"])
def device_borrow_api():
    """MCP borrow endpoint: exact component, quantity, face verification and direct recording."""

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


@parts_bp.route("/api/device/borrow/<request_id>")
def device_borrow_status_api(request_id):
    if not _device_token_configured():
        return jsonify({"success": False, "message": "设备接口未配置"}), 503
    if not _device_authorized():
        return jsonify({"success": False, "message": "设备认证失败"}), 401
    application = _existing_borrow_request(request_id)
    if application is None:
        abort(404)
    return jsonify(_borrow_result(application, request_id))


@parts_bp.route("/api/applications/<int:application_id>")
@login_required
def application_status_api(application_id):
    application = db.session.get(PartApplication, application_id)
    if application is None:
        abort(404)
    if application.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    return jsonify({"success": True, **application.to_dict()})


@parts_bp.route("/api/applications/<int:application_id>/return", methods=["POST"])
@parts_bp.route("/applications/<int:application_id>/return", methods=["POST"])
@login_required
def return_application(application_id):
    def respond(success, message, code):
        if request.path.startswith("/parts/api/"):
            return jsonify({"success": success, "message": message, "record_id": application_id}), code
        flash(message, "success" if success else "error")
        return redirect(url_for("parts.applications"))

    application = db.session.get(PartApplication, application_id)
    if application is None:
        abort(404)
    if application.user_id != current_user.id and not current_user.is_admin:
        abort(403)
    if not application.inventory_managed:
        return respond(False, "历史或自由文本记录未扣减库存，不能归还入库", 409)
    try:
        claimed = PartApplication.query.filter_by(
            id=application_id, inventory_managed=True, returned_at=None, status="approved",
        ).update({"returned_at": utc_now(), "returned_by_id": current_user.id}, synchronize_session=False)
        if claimed != 1:
            db.session.rollback()
            return respond(False, "该借用已归还或状态不允许归还", 409)
        PartApplication.query.filter_by(id=application_id, guidance_status="queued").update({
            "guidance_status": "cancelled", "guidance_message": "借用已归还，取消排队指引",
            "guidance_updated_at": utc_now(),
        }, synchronize_session=False)
        movements = InventoryMovement.query.filter_by(application_id=application_id, kind="borrow").order_by(InventoryMovement.component_id).all()
        if not movements:
            raise ValueError("缺少原始库存扣减流水，不能自动入库")
        for movement in movements:
            changed = CabinetComponent.query.filter_by(id=movement.component_id).update(
                {CabinetComponent.quantity: CabinetComponent.quantity - movement.delta}, synchronize_session=False,
            )
            if changed != 1:
                raise ValueError("原库存零件已不存在，需要人工核对")
            balance = db.session.query(CabinetComponent.quantity).filter_by(id=movement.component_id).scalar()
            db.session.add(InventoryMovement(application_id=application_id, component_id=movement.component_id,
                                             kind="return", delta=-movement.delta, quantity_after=balance, actor_id=current_user.id))
        db.session.commit()
        return respond(True, "已归还并恢复可用库存", 200)
    except (ValueError, IntegrityError) as exc:
        db.session.rollback()
        return respond(False, str(exc) if isinstance(exc, ValueError) else "归还事务冲突，请刷新核对", 409)
    except Exception:
        db.session.rollback()
        logger.exception("归还库存事务失败")
        return respond(False, "归还失败，请稍后重试", 500)


def _run_tag_test_job(app, job, pan, tilt, laser_delay, align_timeout):
    """Run one AprilTag test.  Never writes borrow records or changes inventory."""

    with app.app_context():
        try:
            with device_borrow_lock, _tag_job_ownership(job):
                if job.state != "running":
                    return
                if job.cancel_event.is_set():
                    job.cancelled("排队期间已取消，未执行硬件操作")
                    return
                face_result = hardware.capture_face_until_matched(
                    pan,
                    tilt,
                    _face_result_legacy,
                    deadline=time.monotonic() + _tag_face_timeout(),
                    cancel_event=job.cancel_event,
                    on_stage=job.stage,
                ) or {}
                face_status = str(face_result.get("status") or "hardware_error")
                attempts = int(face_result.get("attempts") or 0)
                job.set_face_attempts(attempts)
                if face_status == "cancelled":
                    job.cancelled(
                        face_result.get("message") or "用户取消，已停止测试",
                        stage="face_verify",
                    )
                    return
                if face_status != "matched":
                    code = "face_timeout" if face_status == "timeout" else "face_failed"
                    job.fail(
                        code,
                        face_result.get("message") or "人脸识别失败",
                        stage="face_verify",
                    )
                    return
                user, score = face_result["matched"]
                face_id = getattr(face_result['matched'], 'face_id', str(user.id))
                job.set_user(user.username, round(float(score), 1), face_id=face_id, user_id=user.id)
                job.stage('face_matched', f'人脸识别通过：{user.username}，人脸 ID：{face_id}')
                if job.cancel_event.is_set():
                    job.cancelled("用户取消，已停止测试")
                    return

                point = hardware.point_to_cabinet_status(
                    job.tag_id,
                    delay=laser_delay,
                    timeout=align_timeout,
                    cancel_event=job.cancel_event,
                    on_stage=job.stage,
                    lost_timeout=_tag_lost_timeout(),
                ) or {}
                status = str(point.get("status") or "hardware_error")
                if status == "aligned":
                    job.stage("done")
                    stages = job.snapshot()["stages"]
                    job.succeed(
                        {
                            "success": True,
                            "status": "aligned",
                            "tag_id": job.tag_id,
                            "message": f"已通过人脸识别并对准标签 {job.tag_id}",
                            "username": user.username,
                            "face_id": face_id,
                            "user_id": user.id,
                            "face_score": round(float(score), 1),
                            "face_attempts": attempts,
                            "point": point,
                            "stages": stages,
                            "borrow_record_created": False,
                        }
                    )
                elif status == "cancelled":
                    job.cancelled(
                        point.get("message") or "用户取消，已停止测试", stage="align"
                    )
                elif status == "timeout":
                    job.timed_out(point.get("message") or "定位超时", stage="align")
                elif status == "lost":
                    job.fail(
                        "tag_not_found",
                        point.get("message") or "未在视野中找到目标标签",
                        stage="align",
                    )
                else:
                    job.fail(
                        "hardware_error",
                        point.get("message") or "云台定位异常",
                        stage="align",
                    )
        except HardwareUnavailable as exc:
            job.fail("hardware_unavailable", str(exc))
        except Exception:
            logger.exception("AprilTag 测试执行失败")
            job.fail("error", "测试执行异常，请查看网站日志")
        finally:
            job.finish_worker()


def _tag_test_force_stop(job):
    # Force the motors to stop even if the worker thread is stuck in a blocking
    # capture or Baidu call; the job state alone does not stop hardware.
    with tag_owner_lock:
        if job.snapshot()["terminal"]:
            return
        stopped = _stop_owned_tag_job(job)
        job.stop_confirmed = stopped
        message = "测试超过最终时限；已向两轴和指示灯成功发送停止命令" if stopped else "测试超过最终时限；硬件停止未确认"
        job.timed_out(message, stage="align")


def _tag_test_watchdog(job, align_timeout):
    with tag_owner_lock:
        if job.snapshot()["terminal"]:
            return
        job.request_cancel()
        job.schedule_timer(TAG_TEST_FORCE_STOP_GRACE, _tag_test_force_stop, (job,))


@parts_bp.route("/api/device/cabinets")
def device_cabinets_api():
    """Complete cabinet list for the desktop AprilTag test client."""

    if not _device_token_configured():
        return jsonify({"success": False, "message": "设备接口未配置 PARTS_DEVICE_TOKEN"}), 503
    if not _device_authorized():
        return jsonify({"success": False, "message": "设备认证失败"}), 401

    cabinets = Cabinet.query.order_by(Cabinet.cabinet_number).all()
    payload = []
    for cabinet in cabinets:
        item = cabinet.to_dict()
        item["updated_at"] = cabinet.updated_at.isoformat() if cabinet.updated_at else None
        payload.append(item)
    return jsonify(
        {
            "success": True,
            "source": "raspberry-pi",
            "generated_at": utc_now().isoformat(),
            "cabinet_count": len(payload),
            "component_count": sum(len(item["components"]) for item in payload),
            "cabinets": payload,
        }
    )


@parts_bp.route("/api/device/tag-test", methods=["POST"])
def device_tag_test_start_api():
    if not _tag_test_enabled():
        return jsonify({"success": False, "message": "标签测试接口未启用"}), 503
    if not _device_token_configured():
        return jsonify({"success": False, "message": "设备接口未配置 PARTS_DEVICE_TOKEN"}), 503
    if not _device_authorized():
        return jsonify({"success": False, "message": "设备认证失败"}), 401

    data = request.get_json(silent=True) or {}
    try:
        tag_id = _parse_tag_id(data.get("tag_id"))
        request_id = _parse_tag_test_request_id(data.get("request_id"))
        timeout = _parse_tag_test_timeout(data.get("timeout_seconds"))
    except ValueError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    try:
        job, created = tag_test_registry.create(request_id, tag_id)
    except JobCapacityError as exc:
        return jsonify({"success": False, "message": str(exc)}), 429, {"Retry-After": "5"}
    except JobConflictError as exc:
        return jsonify({"success": False, "message": str(exc)}), 409
    if not created:
        return jsonify({"success": True, "created": False, **job.snapshot()}), 202

    try:
        app = current_app._get_current_object()
        worker = threading.Thread(
            target=_run_tag_test_job,
            args=(
                app,
                job,
                _setting_float("face_pan_angle"),
                _setting_float("face_tilt_angle"),
                _setting_float("laser_delay"),
                timeout,
            ),
            name=f"tag-test-{request_id}",
            daemon=True,
        )
        job.attach_worker(worker)
        worker.start()
    except Exception:
        job.fail("worker_start_failed", "无法启动测试线程")
        job.finish_worker()
        logger.exception("无法启动 AprilTag 测试线程")
        return jsonify({"success": False, **job.snapshot()}), 503
    job.schedule_timer(timeout + 60.0, _tag_test_watchdog, (job, timeout))
    return jsonify({"success": True, "created": True, **job.snapshot()}), 202


@parts_bp.route("/api/device/tag-test/<request_id>")
def device_tag_test_status_api(request_id):
    if not _device_token_configured():
        return jsonify({"success": False, "message": "设备接口未配置 PARTS_DEVICE_TOKEN"}), 503
    if not _device_authorized():
        return jsonify({"success": False, "message": "设备认证失败"}), 401
    snapshot = tag_test_registry.snapshot(request_id)
    if snapshot is None:
        return jsonify({"success": False, "message": "测试任务不存在或已过期"}), 404
    return jsonify({"success": True, **snapshot})


@parts_bp.route("/api/device/tag-test/<request_id>/cancel", methods=["POST"])
def device_tag_test_cancel_api(request_id):
    if not _device_token_configured():
        return jsonify({"success": False, "message": "设备接口未配置 PARTS_DEVICE_TOKEN"}), 503
    if not _device_authorized():
        return jsonify({"success": False, "message": "设备认证失败"}), 401
    job = tag_test_registry.get(request_id)
    if job is None:
        return jsonify({"success": False, "message": "测试任务不存在或已过期"}), 404
    with tag_owner_lock:
        if not job.snapshot()["terminal"]:
            job.request_cancel()
            job.stop_confirmed = _stop_owned_tag_job(job)
    return jsonify({"success": True, "message": "已请求停止测试", **job.snapshot()})


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
        f"[人脸验证借用登记] 匹配分数: {score:.1f}",
    )
    db.session.commit()
    _point_after_application(application)
    return jsonify(
        {
            "success": True,
            "username": user.username,
            "score": score,
            "application_id": application.id,
            "record_id": application.id,
            "status": application.record_status,
            "inventory_managed": application.inventory_managed,
            "guidance_status": application.guidance_status,
            "guidance_message": application.guidance_message,
            "inventory_note": "自由文本登记，未关联库存，不支持归还入库",
            "inventory_status": application.inventory_status,
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
        f"[人脸识别借用登记] 来自 {device}，匹配分数: {score}",
    )
    db.session.commit()
    _point_after_application(application)
    return jsonify(
        {
            "success": True,
            "message": f"已为 {user.username} 保存自由文本登记；未关联库存，不支持归还入库",
            "application_id": application.id,
            "record_id": application.id,
            "status": application.record_status,
            "equipment": equipment_name,
            "quantity": quantity,
            "inventory_managed": application.inventory_managed,
            "guidance_status": application.guidance_status,
            "guidance_message": application.guidance_message,
            "inventory_note": "自由文本登记，未关联库存，不支持归还入库",
            "inventory_status": application.inventory_status,
        }
    ), 201


def _set_guidance_status(application_id, status, message):
    PartApplication.query.filter(PartApplication.id == application_id,
                                 PartApplication.guidance_status.in_(("queued", "running"))).update({
        "guidance_status": status, "guidance_message": str(message)[:2000],
        "guidance_updated_at": utc_now(),
    }, synchronize_session=False)
    db.session.commit()


def _point_to_cabinet_safely(app, application_id, cabinet_number, delay, timeout):
    # A worker must own its Flask context and ORM session, never a request object.
    with app.app_context():
        try:
            with device_borrow_lock:
                claimed = PartApplication.query.filter_by(id=application_id, guidance_status="queued", returned_at=None).update({
                    "guidance_status": "running", "guidance_message": "正在定位柜子",
                    "guidance_updated_at": utc_now(),
                }, synchronize_session=False)
                db.session.commit()
                if claimed != 1:
                    return
                try:
                    tag_id = _parse_tag_id(cabinet_number)
                except ValueError as exc:
                    _set_guidance_status(application_id, "invalid_tag", str(exc))
                    return
                point = hardware.point_to_cabinet_status(tag_id, delay=delay, timeout=timeout) or {}
                status = str(point.get("status") or "hardware_error")
                if status not in {"aligned", "lost", "timeout", "cancelled", "hardware_error"}:
                    status = "hardware_error"
                _set_guidance_status(application_id, status, point.get("message") or status)
        except Exception as exc:
            db.session.rollback()
            logger.warning("借用 %s 的硬件指引失败", application_id, exc_info=True)
            try:
                _set_guidance_status(application_id, "hardware_error", str(exc))
            except Exception:
                db.session.rollback()
                logger.exception("无法保存指引失败状态")
        finally:
            db.session.remove()


def _point_after_application(application):
    if application.guidance_status != "queued":
        return
    try:
        app = current_app._get_current_object()
        timeout = float(app.config.get("PARTS_GUIDANCE_TIMEOUT", 30))
        if not 1 <= timeout <= 180:
            timeout = 30.0
        threading.Thread(
            target=_point_to_cabinet_safely,
            args=(app, application.id, application.cabinet.cabinet_number,
                  _setting_float("laser_delay"), timeout),
            name=f"borrow-guide-{application.id}", daemon=True,
        ).start()
    except Exception as exc:
        logger.exception("无法启动借用指引")
        db.session.rollback()
        _set_guidance_status(application.id, "hardware_error", f"无法启动指引: {exc}")


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
                    f"[ESP32 借用登记] 人脸匹配分数: {score:.1f}",
                )
                db.session.commit()
                _point_after_application(application)
            except Exception:
                db.session.rollback()
                logger.exception("处理 ESP32 命令失败")


def command_api():
    if not _device_authorized():
        return jsonify({"success": False, "message": "设备认证失败"}), 401
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

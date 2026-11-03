import re
import uuid
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
from flask_login import current_user, login_required
from flask_socketio import join_room, leave_room
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import RequestEntityTooLarge

from cloud_policy import (
    ALLOWED_EXTENSIONS,
    UPLOAD_LOCK,
    CloudUploadError,
    check_capacity,
    display_filename,
    limits,
    readable_size,
)

from community_models import (
    Announcement,
    BlogPost,
    Comment,
    FileRecord,
    Message,
    Notification,
)
from core_models import User
from extensions import db, socketio


community_bp = Blueprint(
    "community", __name__, url_prefix="/community", template_folder="templates"
)

MAX_TEXT_LENGTH = 5000
MENTION_RE = re.compile(r"(?<!\w)@([\w.-]{1,80})", re.UNICODE)


def _clean_text(value, maximum=MAX_TEXT_LENGTH):
    text = (value or "").strip()
    if not text or len(text) > maximum:
        return None
    return text


def _is_owner_or_admin(owner_id):
    return current_user.is_authenticated and (
        current_user.id == owner_id or current_user.is_admin
    )


def _message_dict(message, viewer_id=None):
    if viewer_id is None:
        viewer_id = current_user.id
    return {
        "id": message.id,
        "content": message.content,
        "sender_id": message.sender_id,
        "receiver_id": message.receiver_id,
        "sender": message.sender.username,
        "timestamp": message.timestamp.isoformat(timespec="seconds"),
        "is_read": message.is_read,
        "is_mine": message.sender_id == viewer_id,
    }


def _notify_mentions(text, link, description):
    names = set(MENTION_RE.findall(text))
    if not names:
        return
    users = User.query.filter(User.username.in_(names), User.id != current_user.id).all()
    for user in users:
        notification = Notification(
            user_id=user.id,
            sender_id=current_user.id,
            type="mention",
            content=f"{current_user.username} 在{description}中提到了你",
            link=link,
        )
        db.session.add(notification)
        socketio.emit(
            "new_notification",
            {"type": notification.type, "content": notification.content, "link": link},
            room=f"user_{user.id}",
        )


def _store_private_message(receiver_id, content):
    receiver = db.session.get(User, receiver_id)
    clean_content = _clean_text(content)
    if receiver is None or receiver.id == current_user.id:
        return None, "收件人无效"
    if clean_content is None:
        return None, f"消息不能为空且不能超过 {MAX_TEXT_LENGTH} 字"
    message = Message(
        content=clean_content,
        sender_id=current_user.id,
        receiver_id=receiver.id,
    )
    db.session.add(message)
    db.session.flush()
    notification = Notification(
        user_id=receiver.id,
        sender_id=current_user.id,
        type="message",
        content=f"{current_user.username} 给你发来一条消息",
        link=url_for("community.chat", user=current_user.id),
    )
    db.session.add(notification)
    db.session.commit()
    sender_payload = _message_dict(message, current_user.id)
    receiver_payload = _message_dict(message, receiver.id)
    socketio.emit("new_message", receiver_payload, room=f"user_{receiver.id}")
    socketio.emit("new_message", sender_payload, room=f"user_{current_user.id}")
    socketio.emit(
        "new_notification",
        {"type": "message", "content": notification.content, "link": notification.link},
        room=f"user_{receiver.id}",
    )
    return sender_payload, None


@community_bp.route("/")
@login_required
def index():
    page = request.args.get("page", 1, type=int)
    posts = (
        BlogPost.query.filter_by(is_deleted=False)
        .order_by(BlogPost.created_at.desc())
        .paginate(page=max(page, 1), per_page=10, error_out=False)
    )
    announcements = (
        Announcement.query.filter_by(is_active=True)
        .order_by(Announcement.created_at.desc())
        .limit(5)
        .all()
    )
    return render_template(
        "community/index.html", posts=posts, announcements=announcements
    )


@community_bp.route("/posts", methods=["POST"])
@login_required
def create_post():
    title = _clean_text(request.form.get("title"), 200)
    content = _clean_text(request.form.get("content"))
    if title is None or content is None:
        flash("标题和正文不能为空，且内容不能过长", "error")
        return redirect(url_for("community.index"))
    post = BlogPost(title=title, content=content, user_id=current_user.id)
    db.session.add(post)
    db.session.flush()
    _notify_mentions(
        content,
        url_for("community.post_detail", post_id=post.id),
        f"文章《{title}》",
    )
    db.session.commit()
    flash("文章发布成功", "success")
    return redirect(url_for("community.post_detail", post_id=post.id))


@community_bp.route("/posts/<int:post_id>")
@login_required
def post_detail(post_id):
    post = BlogPost.query.filter_by(id=post_id, is_deleted=False).first_or_404()
    post.views += 1
    db.session.commit()
    comments = (
        Comment.query.filter_by(post_id=post.id, parent_id=None)
        .order_by(Comment.created_at.asc())
        .all()
    )
    return render_template("community/post.html", post=post, comments=comments)


@community_bp.route("/posts/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_post(post_id):
    post = db.get_or_404(BlogPost, post_id)
    if not _is_owner_or_admin(post.user_id):
        abort(403)
    post.is_deleted = True
    db.session.commit()
    flash("文章已删除", "success")
    return redirect(url_for("community.index"))


@community_bp.route("/posts/<int:post_id>/comments", methods=["POST"])
@login_required
def add_comment(post_id):
    post = BlogPost.query.filter_by(id=post_id, is_deleted=False).first_or_404()
    content = _clean_text(request.form.get("content"))
    parent_id = request.form.get("parent_id", type=int)
    if content is None:
        flash("评论不能为空且不能过长", "error")
        return redirect(url_for("community.post_detail", post_id=post.id))
    if parent_id is not None:
        parent = db.session.get(Comment, parent_id)
        if parent is None or parent.post_id != post.id:
            abort(400)
    comment = Comment(
        content=content,
        user_id=current_user.id,
        post_id=post.id,
        parent_id=parent_id,
    )
    db.session.add(comment)
    db.session.flush()
    link = url_for("community.post_detail", post_id=post.id) + f"#comment-{comment.id}"
    _notify_mentions(content, link, "评论")
    recipient_id = parent.user_id if parent_id is not None else post.user_id
    if recipient_id != current_user.id:
        db.session.add(
            Notification(
                user_id=recipient_id,
                sender_id=current_user.id,
                type="reply",
                content=f"{current_user.username} 回复了《{post.title}》",
                link=link,
            )
        )
        socketio.emit(
            "new_notification",
            {"type": "reply", "content": f"{current_user.username} 回复了你", "link": link},
            room=f"user_{recipient_id}",
        )
    db.session.commit()
    flash("评论已发表", "success")
    return redirect(link)


@community_bp.route("/comments/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(comment_id):
    comment = db.get_or_404(Comment, comment_id)
    if not _is_owner_or_admin(comment.user_id):
        abort(403)
    post_id = comment.post_id
    if comment.replies and not current_user.is_admin:
        # Preserve other members' replies when an author removes a parent comment.
        comment.content = "[该评论已由作者删除]"
    else:
        db.session.delete(comment)
    db.session.commit()
    flash("评论已删除", "success")
    return redirect(url_for("community.post_detail", post_id=post_id))


@community_bp.route("/announcements")
@login_required
def announcements():
    if not current_user.is_admin:
        abort(403)
    items = Announcement.query.order_by(Announcement.created_at.desc()).all()
    return render_template("community/announcements.html", announcements=items)


@community_bp.route("/announcements", methods=["POST"])
@login_required
def create_announcement():
    if not current_user.is_admin:
        abort(403)
    title = _clean_text(request.form.get("title"), 200)
    content = _clean_text(request.form.get("content"))
    if title is None or content is None:
        flash("公告标题和内容不能为空", "error")
    else:
        db.session.add(
            Announcement(title=title, content=content, user_id=current_user.id)
        )
        db.session.commit()
        flash("公告已发布", "success")
    return redirect(url_for("community.announcements"))


@community_bp.route("/announcements/<int:announcement_id>/toggle", methods=["POST"])
@login_required
def toggle_announcement(announcement_id):
    if not current_user.is_admin:
        abort(403)
    announcement = db.get_or_404(Announcement, announcement_id)
    announcement.is_active = not announcement.is_active
    db.session.commit()
    return redirect(url_for("community.announcements"))


@community_bp.route("/announcements/<int:announcement_id>/delete", methods=["POST"])
@login_required
def delete_announcement(announcement_id):
    if not current_user.is_admin:
        abort(403)
    announcement = db.get_or_404(Announcement, announcement_id)
    db.session.delete(announcement)
    db.session.commit()
    flash("公告已删除", "success")
    return redirect(url_for("community.announcements"))


@community_bp.route("/cloud")
@login_required
def cloud():
    query = FileRecord.query
    if not current_user.is_admin:
        query = query.filter_by(user_id=current_user.id)
    files = query.order_by(FileRecord.uploaded_at.desc()).all()
    user_used, site_used = _cloud_usage(current_user.id)
    user_limit, site_limit, file_limit = limits(current_app.config)
    return render_template(
        "community/cloud.html", files=files,
        user_used=user_used, site_used=site_used,
        user_limit=user_limit, site_limit=site_limit, file_limit=file_limit,
        request_limit=current_app.config.get("MAX_CONTENT_LENGTH"),
        allowed_extensions=sorted(ALLOWED_EXTENSIONS), readable_size=readable_size,
    )


def _cloud_usage(user_id):
    user_used = db.session.query(func.coalesce(func.sum(FileRecord.file_size), 0)).filter(
        FileRecord.user_id == user_id
    ).scalar()
    site_used = db.session.query(func.coalesce(func.sum(FileRecord.file_size), 0)).scalar()
    return int(user_used), int(site_used)


@community_bp.errorhandler(RequestEntityTooLarge)
def cloud_request_too_large(error):
    if request.endpoint != "community.upload_file":
        return error
    maximum = current_app.config.get("MAX_CONTENT_LENGTH")
    description = readable_size(maximum) if maximum is not None else "站点配置值"
    flash(f"上传请求过大：请求上限 {description}（包含文件和表单），请选择更小的文件", "error")
    return redirect(url_for("community.cloud"), code=303)


@community_bp.route("/cloud/upload", methods=["POST"])
@login_required
def upload_file():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        flash("请选择文件", "error")
        return redirect(url_for("community.cloud"))
    try:
        original_name, suffix = display_filename(uploaded.filename)
    except CloudUploadError as error:
        flash(str(error), "error")
        return redirect(url_for("community.cloud"))
    user_id = current_user.id
    # Start quota reads only after acquiring the lock, including a fresh DB
    # transaction (authentication may already have opened a read transaction).
    with UPLOAD_LOCK:
        destination = None
        created = False
        try:
            db.session.rollback()
            quota = limits(current_app.config)
            user_used, site_used = _cloud_usage(user_id)
            # Werkzeug's multipart upload streams are seekable temporary files.
            # Check actual length, never the client-supplied Content-Length.
            uploaded.stream.seek(0, 2)
            size = uploaded.stream.tell()
            uploaded.stream.seek(0)
            check_capacity(size, user_used, site_used, quota)
            stored_name = f"{uuid.uuid4().hex}.{suffix}"
            upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
            upload_dir.mkdir(parents=True, exist_ok=True)
            destination = upload_dir / stored_name
            with destination.open("xb") as output:
                created = True
                written = 0
                budget = min(quota[2], quota[0] - user_used, quota[1] - site_used)
                while True:
                    chunk = uploaded.stream.read(min(64 * 1024, budget - written + 1))
                    if not chunk:
                        break
                    written += len(chunk)
                    check_capacity(written, user_used, site_used, quota)
                    output.write(chunk)
            # Recheck persisted size and database totals before committing.
            size = destination.stat().st_size
            check_capacity(size, *_cloud_usage(user_id), quota)
            db.session.add(FileRecord(
                filename=stored_name, original_name=original_name,
                file_size=size, user_id=user_id,
            ))
            db.session.commit()
        except (CloudUploadError, OSError, SQLAlchemyError) as error:
            db.session.rollback()
            if created:
                try:
                    destination.unlink(missing_ok=True)
                except OSError:
                    current_app.logger.exception("Could not remove failed cloud upload %s", destination)
            if isinstance(error, CloudUploadError):
                flash(str(error), "error")
            else:
                current_app.logger.exception("Cloud upload failed")
                flash("上传失败，文件未保存，请稍后重试或联系管理员", "error")
            return redirect(url_for("community.cloud"))
    flash(f"已上传 {original_name}", "success")
    return redirect(url_for("community.cloud"))


@community_bp.route("/cloud/<int:file_id>/download")
@login_required
def download_file(file_id):
    record = db.get_or_404(FileRecord, file_id)
    if not _is_owner_or_admin(record.user_id):
        abort(403)
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"])
    if not (upload_dir / record.filename).is_file():
        abort(404)
    response = send_from_directory(
        upload_dir, record.filename, as_attachment=True, download_name=record.original_name,
        mimetype="application/octet-stream",
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@community_bp.route("/cloud/<int:file_id>/delete", methods=["POST"])
@login_required
def delete_file(file_id):
    record = db.get_or_404(FileRecord, file_id)
    if not _is_owner_or_admin(record.user_id):
        abort(403)
    upload_dir = Path(current_app.config["UPLOAD_FOLDER"]).resolve()
    stored_path = (upload_dir / record.filename).resolve()
    if stored_path.parent != upload_dir:
        abort(400)
    if stored_path.is_file():
        stored_path.unlink()
    db.session.delete(record)
    db.session.commit()
    flash("文件已删除", "success")
    return redirect(url_for("community.cloud"))


@community_bp.route("/chat")
@login_required
def chat():
    users = User.query.filter(User.id != current_user.id).order_by(User.username).all()
    selected_id = request.args.get("user", type=int)
    selected = db.session.get(User, selected_id) if selected_id else None
    if selected is not None and selected.id == current_user.id:
        selected = None
    return render_template("community/chat.html", users=users, selected=selected)


@community_bp.route("/api/messages/<int:user_id>")
@login_required
def private_messages(user_id):
    if db.session.get(User, user_id) is None or user_id == current_user.id:
        abort(404)
    messages = (
        Message.query.filter(
            or_(
                and_(Message.sender_id == current_user.id, Message.receiver_id == user_id),
                and_(Message.sender_id == user_id, Message.receiver_id == current_user.id),
            )
        )
        .order_by(Message.timestamp.desc())
        .limit(100)
        .all()
    )
    Message.query.filter_by(
        sender_id=user_id, receiver_id=current_user.id, is_read=False
    ).update({"is_read": True})
    db.session.commit()
    return jsonify([_message_dict(message) for message in reversed(messages)])


@community_bp.route("/api/messages/<int:user_id>/read", methods=["POST"])
@login_required
def mark_private_messages_read(user_id):
    """Mark the open private conversation as read.

    Loading a conversation marks its existing messages as read. This endpoint
    also handles messages that arrive while the conversation stays open.
    """

    if db.session.get(User, user_id) is None or user_id == current_user.id:
        abort(404)
    Message.query.filter_by(
        sender_id=user_id, receiver_id=current_user.id, is_read=False
    ).update({"is_read": True})
    db.session.commit()
    return jsonify({"ok": True})


@community_bp.route("/api/messages/<int:user_id>", methods=["POST"])
@login_required
def send_private_message(user_id):
    data = request.get_json(silent=True) or request.form
    payload, error = _store_private_message(user_id, data.get("content"))
    if error:
        return jsonify({"error": error}), 400
    return jsonify(payload), 201


@community_bp.route("/api/messages/unread")
@login_required
def unread_counts():
    rows = (
        db.session.query(Message.sender_id, func.count(Message.id))
        .filter_by(receiver_id=current_user.id, is_read=False)
        .group_by(Message.sender_id)
        .all()
    )
    return jsonify({str(sender_id): count for sender_id, count in rows})


@community_bp.route("/api/notifications/unread-count")
@login_required
def unread_notification_count():
    count = Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).count()
    return jsonify({"count": count})


@community_bp.route("/notifications")
@login_required
def notifications():
    unread_count = Notification.query.filter_by(
        user_id=current_user.id, is_read=False
    ).update({"is_read": True})
    if unread_count:
        db.session.commit()
    items = (
        Notification.query.filter_by(user_id=current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(100)
        .all()
    )
    return render_template("community/notifications.html", notifications=items)


@community_bp.route("/notifications/read-all", methods=["POST"])
@login_required
def read_all_notifications():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update(
        {"is_read": True}
    )
    db.session.commit()
    return redirect(url_for("community.notifications"))


@socketio.on("connect")
def socket_connect(auth=None):
    if not current_user.is_authenticated:
        return False
    join_room(f"user_{current_user.id}")
    return True


@socketio.on("disconnect")
def socket_disconnect(reason=None):
    if current_user.is_authenticated:
        leave_room(f"user_{current_user.id}")


@socketio.on("send_private_message")
def socket_private_message(data):
    if not current_user.is_authenticated:
        return {"ok": False, "error": "请先登录"}
    data = data or {}
    receiver_id = data.get("receiver_id")
    if not isinstance(receiver_id, int):
        return {"ok": False, "error": "收件人无效"}
    payload, error = _store_private_message(receiver_id, data.get("content"))
    return {"ok": error is None, "message": payload, "error": error}

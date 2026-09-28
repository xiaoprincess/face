"""Session synchronizer tokens for browser writes, without extra dependencies."""

import secrets

from flask import jsonify, make_response, request, session


CSRF_SESSION_KEY = "_csrf_token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Only these existing device views authenticate independently of browser cookies.
# Keep explicit aliases here: face login/register must always require CSRF.
DEVICE_ENDPOINTS = frozenset({
    "parts.device_borrow_api",
    "parts.face_verify_api",
    "parts.face_apply_api",
    "parts.face_verify_compat",
    "parts.face_apply_compat",
    "parts.command_compat",
})


def csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _csrf_error():
    message = "安全验证失败：CSRF 令牌缺失或无效，请刷新页面后重试。"
    wants_json = (
        request.is_json
        or request.path.startswith(("/api/", "/parts/api/"))
        or request.headers.get("X-CSRF-Token") is not None
        or request.accept_mimetypes.best_match(["text/html", "application/json"])
        == "application/json"
    )
    if wants_json:
        return jsonify(success=False, error="csrf_failed", message=message), 400
    return make_response(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<title>安全验证失败</title><h1>安全验证失败</h1>'
        f"<p>{message}</p></html>",
        400,
    )


def init_csrf(app):
    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def protect_browser_writes():
        if request.method not in UNSAFE_METHODS or request.endpoint in DEVICE_ENDPOINTS:
            return None
        expected = session.get(CSRF_SESSION_KEY)
        supplied = request.headers.get("X-CSRF-Token")
        if supplied is None:
            supplied = request.form.get("csrf_token")
        if (
            not isinstance(expected, str)
            or not expected
            or not isinstance(supplied, str)
            or not supplied
            or not secrets.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))
        ):
            return _csrf_error()
        return None

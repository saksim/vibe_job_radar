"""Single-owner remote workbench; desktop transport and team device API stay separate."""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import hashlib
import hmac
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from importlib.resources import files
import json
from pathlib import Path
import secrets
import signal
import stat
import threading
import time
from urllib.parse import quote, unquote, urlsplit

from .application import WorkbenchApplication
from .application_http import WorkbenchHTTP, Response, decode_request_body
from .workspace import Workspace

MAX_BODY = 2_000_000
SESSION_SECONDS = 8 * 60 * 60
SERVER_CAPABILITIES = {
    "mode": "server", "single_owner": True,
    "public_collection": True, "reports": True, "evidence": True, "quality": True,
    "desktop_browser": False, "desktop_updates": False, "windows_startup": False,
    "network_settings": False, "team_credential_migration": False,
}


def json_response(status, value):
    return Response(status, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8")


def validate_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path or parsed.query or parsed.fragment
            or not value.isascii() or any(c.isspace() for c in value)):
        raise ValueError("origin 必须是精确的 ASCII 来源地址，不含路径、账号或参数。")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("origin 端口无效。") from None
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("origin 端口无效。")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("远程来源必须使用 HTTPS；HTTP 只允许本机开发。")
    return value


def read_password(path: Path) -> str:
    # Do not accept a directory/device, a symlink, or an unbounded secret file.
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 1024:
        raise ValueError("密码必须来自不超过 1024 字节的普通秘密文件。")
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not 16 <= len(value) <= 512 or any(c in value for c in "\r\n\x00"):
        raise ValueError("密码需为 16～512 字符的单行内容。")
    return value


@dataclass(frozen=True)
class Session:
    csrf: str
    expires: float


class OwnerSessions:
    """Bounded process-local sessions. Restart revokes all; no password is logged."""
    def __init__(self, password: str, *, clock=time.monotonic):
        self.clock = clock
        self.salt = secrets.token_bytes(32)
        self.password_hash = self._digest(password)
        self.sessions: dict[str, Session] = {}
        self.attempts = deque()
        self.lock = threading.Lock()

    def _digest(self, password):
        return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), self.salt, 600_000)

    def login(self, password):
        now = self.clock()
        # Global bound also works behind a proxy without trusting forwarded IPs.
        with self.lock:
            while self.attempts and now - self.attempts[0] >= 60:
                self.attempts.popleft()
            if len(self.attempts) >= 5:
                return None, 429
            self.attempts.append(now)
        if (not isinstance(password, str) or len(password) > 512
                or not hmac.compare_digest(self._digest(password), self.password_hash)):
            return None, 401
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v.expires > now}
            if len(self.sessions) >= 16:
                del self.sessions[next(iter(self.sessions))]
            key = secrets.token_urlsafe(32)
            self.sessions[key] = Session(secrets.token_urlsafe(32), now + SESSION_SECONDS)
        return key, 200

    def get(self, key):
        with self.lock:
            session = self.sessions.get(key)
            if session and session.expires <= self.clock():
                self.sessions.pop(key, None)
                return None
            return session

    def revoke(self, key):
        with self.lock:
            self.sessions.pop(key, None)


class ServerWorkbench:
    def __init__(self, application, *, origin: str, password: str, sessions=None):
        self.application = application
        self.http = WorkbenchHTTP(application)
        self.origin = validate_origin(origin)
        self.authority = urlsplit(origin).netloc
        self.secure = origin.startswith("https://")
        self.cookie_name = "__Host-radar-session" if self.secure else "radar-session"
        self.sessions = sessions or OwnerSessions(password)

    def _cookie_key(self, environ):
        raw = environ.get("HTTP_COOKIE", "")
        if len(raw) > 8192:
            return ""
        parts = [part.strip().split("=", 1)[0] for part in raw.split(";")]
        if parts.count(self.cookie_name) != 1:
            return ""
        try:
            cookie = SimpleCookie()
            cookie.load(raw)
            return cookie[self.cookie_name].value
        except (CookieError, KeyError, ValueError):
            return ""

    def _cookie(self, key, *, clear=False):
        return (f"{self.cookie_name}={key}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={0 if clear else SESSION_SECONDS}" + ("; Secure" if self.secure else ""))

    def _body(self, environ, path, limit=MAX_BODY):
        if environ.get("HTTP_TRANSFER_ENCODING"):
            return None, json_response(415, {"error": "不接受 Transfer-Encoding。"})
        if environ.get("CONTENT_TYPE", "").split(";")[0].strip().lower() != "application/json":
            return None, json_response(415, {"error": "只接受 application/json。"})
        try:
            length = int(environ.get("CONTENT_LENGTH", ""))
        except (TypeError, ValueError):
            return None, json_response(411, {"error": "缺少有效的 Content-Length。"})
        if not 0 < length <= limit:
            return None, json_response(413, {"error": "请求为空或超过大小限制。"})
        raw = environ["wsgi.input"].read(length)
        try:
            if len(raw) != length:
                raise ValueError
            return decode_request_body(path, raw), None
        except (ValueError, UnicodeError):
            return None, json_response(400, {"error": "请求必须为完整的 UTF-8 JSON 对象。"})

    def _blocked(self, path, method):
        if path in {"/guided", "/guided.js"} or path.startswith(("/api/guided/", "/api/windows/", "/api/releases/")):
            return True
        if path in {"/api/collection/handoff_preview", "/api/collection/handoff_start", "/api/team/protect"}:
            return True
        return method == "POST" and path.startswith("/api/network/")

    def __call__(self, environ, start_response):
        extra = []
        try:
            response, extra = self._dispatch(environ)
        except Exception:
            # Never include user input, secrets, paths or tracebacks in remote errors.
            response = json_response(500, {"error": "服务器操作失败；请检查服务状态。"})
        headers = [
            ("Content-Type", response.content_type), ("Content-Length", str(len(response.body))),
            ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"), ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
             "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
        ]
        if self.secure:
            headers.append(("Strict-Transport-Security", "max-age=31536000"))
        if response.filename:
            headers.append(("Content-Disposition", "attachment; filename*=UTF-8''" + quote(response.filename)))
        headers.extend(extra)
        start_response(f"{response.status} {HTTPStatus(response.status).phrase}", headers)
        return [response.body]

    def _dispatch(self, environ):
        method = environ.get("REQUEST_METHOD", "GET")
        path = unquote(environ.get("PATH_INFO", "/"))
        if method == "GET" and path == "/healthz":
            return json_response(200, {"ok": True}), []
        # External Host and Origin are explicit configuration, never inferred from
        # spoofable forwarded headers. Reverse proxy must preserve the original Host.
        if (environ.get("HTTP_HOST") != self.authority
                or environ.get("HTTP_ORIGIN", self.origin) != self.origin
                or environ.get("HTTP_SEC_FETCH_SITE") == "cross-site"):
            return json_response(403, {"error": "来源与服务器配置不匹配。"}), []
        if method not in {"GET", "POST"}:
            return json_response(405, {"error": "不支持此请求方法。"}), []
        if method == "POST" and environ.get("HTTP_ORIGIN") != self.origin:
            return json_response(403, {"error": "操作需要同源 Origin。"}), []
        if method == "GET" and path in {"/login", "/server-login.js"}:
            name = "server_login.html" if path == "/login" else "server_login.js"
            mime = "text/html" if name.endswith(".html") else "text/javascript"
            return Response(200, files("vibe_job_radar").joinpath(name).read_bytes(), mime + "; charset=utf-8"), []
        if method == "POST" and path == "/api/session/login":
            data, error = self._body(environ, path, 4096)
            if error:
                return error, []
            if set(data) != {"password"}:
                return json_response(400, {"error": "请输入工作台密码。"}), []
            key, status = self.sessions.login(data["password"])
            if key is None:
                return json_response(status, {"error": "登录未通过；尝试频繁时请在一分钟后重试。"}), []
            # Reauthentication rotates and revokes the old session, if present.
            self.sessions.revoke(self._cookie_key(environ))
            return json_response(200, {"ok": True}), [("Set-Cookie", self._cookie(key))]
        key = self._cookie_key(environ)
        session = self.sessions.get(key)
        if session is None:
            if path in {"/", "/advanced", "/quality"} and method == "GET":
                return Response(303, b"", "text/plain"), [("Location", "/login")]
            return json_response(401, {"error": "会话已过期，请重新登录服务器工作台。"}), []
        if method == "GET" and path == "/server-session.js":
            script = ("sessionStorage.setItem('radar-session'," + json.dumps(session.csrf) + ");\n"
                      "window.radarServer=" + json.dumps(SERVER_CAPABILITIES) + ";\n"
                      "const serverFragment=new URLSearchParams(location.hash.slice(1));"
                      "if(serverFragment.has('token')){serverFragment.delete('token');"
                      "history.replaceState(null,'',location.pathname+(serverFragment.size?'#'+serverFragment:''));}\n")
            script += files("vibe_job_radar").joinpath("server_ui.js").read_text(encoding="utf-8")
            return Response(200, script.encode("utf-8"), "text/javascript; charset=utf-8"), []
        if path.startswith("/api/"):
            token = environ.get("HTTP_X_RADAR_TOKEN", "")
            if not hmac.compare_digest(token.encode("utf-8"), session.csrf.encode("ascii")):
                return json_response(403, {"error": "会话校验失败，请刷新页面。"}), []
        if method == "GET" and path == "/api/runtime":
            return json_response(200, SERVER_CAPABILITIES), []
        if method == "POST" and path == "/api/session/logout":
            self.sessions.revoke(key)
            return json_response(200, {"ok": True}), [("Set-Cookie", self._cookie("", clear=True))]
        if self._blocked(path, method):
            return json_response(409, {"error": "此功能属于桌面入口；服务器模式不执行此操作。",
                                       "code": "desktop_capability_required"}), []
        if method == "POST":
            data, error = self._body(environ, path)
            if error:
                return error, []
            return self.http.post(path, data), []
        response = self.http.get(path)
        if path == "/api/status" and response.status == 200:
            value = json.loads(response.body)
            value["runtime"] = SERVER_CAPABILITIES
            value["acquisition_sites"] = []
            return json_response(200, value), []
        if response.status == 200 and response.content_type.startswith("text/html"):
            body = response.body
            for script in (b"network-settings.js", b"release-updates.js", b"windows-startup.js"):
                body = body.replace(b'<script src="/' + script + b'" defer></script>', b"")
            body = body.replace(b"<head>", b'<head><script src="/server-session.js" defer></script>', 1)
            return Response(response.status, body, response.content_type, response.filename), []
        return response, []


def main(argv=None):
    parser = argparse.ArgumentParser(description="单拥有者服务器工作台；浏览器访问，同一私人工作区。")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        origin = validate_origin(args.origin)
        password = read_password(args.password_file)
        if not 1 <= args.port <= 65535:
            raise ValueError("端口必须为 1～65535。")
        from waitress import create_server
    except (OSError, ValueError, ImportError):
        parser.error("服务器配置无效或缺少 service 依赖；核对 origin、密码文件、端口及安装说明。")
    application = WorkbenchApplication(Workspace(args.workspace))
    server = None
    previous = None
    try:
        app = ServerWorkbench(application, origin=origin, password=password)
        del password
        server = create_server(app, host=args.host, port=args.port, threads=4,
                               connection_limit=64, backlog=64, channel_timeout=20,
                               max_request_header_size=8192, max_request_body_size=MAX_BODY,
                               clear_untrusted_proxy_headers=True, expose_tracebacks=False,
                               ident="vibe-radar")
        def stop(signum, frame):
            raise KeyboardInterrupt
        previous = signal.signal(signal.SIGTERM, stop)
        application.start()
        print("服务器工作台已启动：" + origin + "/login", flush=True)
        server.run()
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.close()
            server.task_dispatcher.shutdown()
        application.close()
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

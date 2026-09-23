"""Loopback-only browser UI. Not a public/multi-user web deployment."""
from __future__ import annotations

import argparse
import hmac
import json
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .workspace import InputError, Workspace
from .collection import Collector
from .collection_handoff import CollectionHandoff
from .guided.service import GuidedService, MESSAGES
from .guided.contracts import CrawlError
from .collection_guidance import CollectionGuidance
from .evidence_ui import Conflict, EvidenceService
from .public_tasks import PublicTasks

MAX_BODY = 2_000_000
_DEFAULT_PUBLIC_CLIENT = object()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, workspace: Workspace, port: int = 0, *, public_client=_DEFAULT_PUBLIC_CLIENT):
        self.workspace = workspace
        self.collector = Collector(workspace)
        self.guidance = CollectionGuidance(workspace)
        self.evidence = EvidenceService(workspace)
        self.guided = GuidedService(workspace)
        self.handoff = CollectionHandoff(self.collector, self.guided)
        if public_client is _DEFAULT_PUBLIC_CLIENT:
            from .local_public import LocalPublicDataClient
            public_client = LocalPublicDataClient(workspace)
        self.public_tasks = PublicTasks(workspace, hybrid_client=public_client)
        self.token = secrets.token_urlsafe(32)
        self.mutation_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)
        self.authority = f"127.0.0.1:{self.server_address[1]}"
        self.origin = f"http://{self.authority}"

    def server_close(self):
        self.guided.close()
        self.public_tasks.close()
        super().server_close()

    @property
    def entry_url(self) -> str:
        # The fragment is not transmitted in HTTP requests or Referer headers.
        return f"{self.origin}/#token={self.token}"


class Handler(BaseHTTPRequestHandler):
    server_version = "VibeRadarLocal/1"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        # Do not log request bodies, URLs, JD text, credentials or local tokens.
        pass

    def _respond(self, code: int, body: bytes, content_type: str, *, filename: str = ""):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                         "style-src 'unsafe-inline'; connect-src 'self'; img-src data:; "
                         "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename, safe=""))
        self.end_headers()
        try:
            self.wfile.write(body)
            self.wfile.flush()
            if code >= 400 and self.command == 'POST':
                self._discard_rejected_body()
        except OSError:
            pass

    def _discard_rejected_body(self):
        """After sending a refusal, drain only an unambiguous bounded frame.

        Closing a Windows socket with unread inbound data can reset the peer
        before it sees the 403. Never parse unauthorized JSON, dispatch it,
        reuse the connection, or wait indefinitely for a slow/truncated body.
        """
        if getattr(self, '_body_consumed', False) or self.headers.get('Transfer-Encoding'):
            return
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            return
        try:
            remaining = int(lengths[0])
        except ValueError:
            return
        if not 0 < remaining <= MAX_BODY:
            return
        previous_timeout = self.connection.gettimeout()
        deadline = time.monotonic() + .2
        try:
            while remaining:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                self.connection.settimeout(wait)
                chunk = self.rfile.read1(min(remaining, 8192))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            self.connection.settimeout(previous_timeout)

    def _json(self, code: int, data: dict):
        self._respond(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _authorized(self, *, token_required: bool = True) -> bool:
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if hosts != [self.server.authority] or (origins and origins != [self.server.origin]):
            self._json(403, {"error": "仅允许本机工作台来源；请使用启动器显示的 127.0.0.1 地址。"})
            return False
        tokens = self.headers.get_all("X-Radar-Token", [])
        if token_required and (len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode("utf-8"), self.server.token.encode("utf-8"))):
            self._json(403, {"error": "本地会话令牌无效；请重新打开终端中带 #token 的完整地址。"})
            return False
        return True

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        public = path in {"/", "/app.js", "/advanced", "/advanced.js", "/collection-help.js", "/guided", "/guided.js", "/network-settings.js"}
        if not self._authorized(token_required=not public):
            return
        try:
            if public:
                name = {"/": "workbench.html", "/app.js": "workbench.js", "/advanced": "advanced.html", "/advanced.js": "advanced.js", "/collection-help.js": "collection_help.js", "/guided": "guided.html", "/guided.js": "guided.js", "/network-settings.js": "network_settings.js"}[path]
                mime = "text/html" if name.endswith(".html") else "text/javascript"
                self._respond(200, files("vibe_job_radar").joinpath(name).read_bytes(), mime + "; charset=utf-8")
            elif path == "/api/network/state":
                self._json(200, self.server.workspace.network_state())
            elif path == "/api/public/state":
                self._json(200, self.server.public_tasks.state())
            elif path == "/api/guided/state":
                self._json(200, self.server.guided.state())
            elif path == "/api/evidence/export":
                self._respond(200, self.server.evidence.export(), "application/zip", filename="personal-evidence.zip")
            elif path.startswith("/api/evidence/attachment/"):
                artifact = self.server.evidence._artifact_path(path.removeprefix("/api/evidence/attachment/"))
                self._respond(200, artifact.read_bytes(), "application/octet-stream", filename=artifact.name)
            elif path == "/api/status":
                self._json(200, self.server.workspace.status())
            elif path == "/api/doctor":
                self._json(200, self.server.workspace.doctor())
            elif path.startswith("/api/report/"):
                run_id = path.removeprefix("/api/report/")
                self._json(200, self.server.workspace.report(run_id))
            elif path.startswith("/api/download/"):
                parts = path.removeprefix("/api/download/").split("/")
                if len(parts) != 2:
                    raise InputError("无效下载路径。")
                artifact = self.server.workspace.report_file(*parts)
                self._respond(200, artifact.read_bytes(), "application/octet-stream", filename=artifact.name)
            else:
                self._json(404, {"error": "入口不存在。"})
        except InputError as exc:
            self._json(400, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": f"读取失败 [{type(exc).__name__}]；请检查本地目录和报告文件。"})

    def do_POST(self):
        # Close rejected POST connections: unconsumed bodies must not become another request.
        self.close_connection = True
        if not self._authorized():
            return
        if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
            self._json(415, {"error": "只接受带 Content-Length 的 application/json。"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        try:
            if len(lengths) != 1:
                raise ValueError
            length = int(lengths[0])
        except ValueError:
            self._json(411, {"error": "缺少有效的 Content-Length。"})
            return
        if not 0 < length <= MAX_BODY:
            self._json(413, {"error": "请求为空或超过 2 MB。"})
            return
        try:
            raw = self.rfile.read(length)
            self._body_consumed = True
            if len(raw) != length:
                raise ValueError
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeError, TimeoutError):
            self._json(400, {"error": "请求必须为完整的 UTF-8 JSON 对象。"})
            return
        route = urlsplit(self.path).path
        methods = {"/api/job": "add_job", "/api/import": "import_file", "/api/plan": "plan",
                   "/api/discover": "discover", "/api/analyze": "analyze", "/api/network/preferences": "network_preferences"}
        target = self.server.workspace
        if route.startswith("/api/public/"):
            target = self.server.public_tasks
            methods = {"/api/public/" + name: name for name in ("start", "search")}
        elif route.startswith("/api/guided/"):
            target = self.server.guided
            methods = {"/api/guided/" + name: name for name in ("create", "action", "install", "check_browser", "diagnose", "export", "diagnostics")}
        elif route.startswith("/api/evidence/"):
            target = self.server.evidence
            methods = {"/api/evidence/" + name: name for name in ("state", "catalogue", "review", "upload", "metric", "save", "remove", "generate")}
        elif route in {"/api/collection/handoff_preview", "/api/collection/handoff_start"}:
            target = self.server.handoff
            methods = {"/api/collection/" + name: name for name in ("handoff_preview", "handoff_start")}
        elif route.startswith("/api/collection/"):
            target = self.server.guidance if route == "/api/collection/preview" else self.server.collector
            methods = {"/api/collection/" + name: name for name in ("start", "step", "status", "list", "register", "preview")}
        if route not in methods:
            self._json(404, {"error": "入口不存在。"})
            return
        if not self.server.mutation_lock.acquire(blocking=False):
            self._json(409, {"error": "已有操作执行中，请勿重复提交。"})
            return
        try:
            result = getattr(target, methods[route])(data)
            status, response = 200, result
        except CrawlError as exc:
            status, response = 400, {"error": MESSAGES.get(exc.code, "请检查平台、输入和当前任务状态。"), "code": exc.code}
        except Conflict as exc:
            status, response = 409, {"error": str(exc)}
        except InputError as exc:
            status, response = 400, {"error": str(exc)}
        except (ValueError, TypeError) as exc:
            # Core exceptions may contain user data: show the type, not raw contents.
            status, response = 400, {"error": f"输入校验失败 [{type(exc).__name__}]；请核对字段、来源链接及正文。"}
        except Exception as exc:
            status, response = 500, {"error": f"操作失败 [{type(exc).__name__}]；请检查网络/API额度或本地目录。"}
        finally:
            self.server.mutation_lock.release()
        # A successful response must mean the write lock is already released.
        self._json(status, response)

    def do_OPTIONS(self):
        self._json(403, {"error": "不开放跨站调用。"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vibe Job Radar 本地浏览器工作台（可选受控浏览器与人工登录协助）")
    parser.add_argument("--workspace", type=Path, default=Path.home() / ".vibe-job-radar")
    parser.add_argument("--port", type=int, default=0, help="默认由系统选择空闲端口，仅绑定 127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--doctor", action="store_true", help="离线检查 Python、SQLite 和目录写入能力")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port 必须为 0～65535")
    try:
        workspace = Workspace(args.workspace)
        diagnostic = workspace.doctor()
        if args.doctor:
            print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
            return 0
        with LocalServer(workspace, args.port) as server:
            print(f"本地工作台：{server.entry_url}", flush=True)
            print(f"数据目录：{workspace.root}\n仅本机访问；不要分享带令牌的地址。关闭终端或按 Ctrl+C 停止。", flush=True)
            if not args.no_browser:
                try:
                    if not webbrowser.open(server.entry_url):
                        print("未能自动打开浏览器，请复制上述完整地址。", flush=True)
                except webbrowser.Error:
                    print("请手工在本机浏览器打开上述完整地址。", flush=True)
            server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\n工作台已停止，数据保留在本机。")
    except (OSError, ValueError) as exc:
        print(f"启动失败 [{type(exc).__name__}]：{exc}\n请检查 Python 版本、目录权限或改用 --port 0。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

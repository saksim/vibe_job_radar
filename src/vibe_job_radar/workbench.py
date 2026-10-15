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

from .runtime_identity import bootstrap as _bootstrap_runtime
_bootstrap_runtime()

from .workspace import InputError, Workspace
from .collection import Collector
from .collection_runtime import CollectionRunner, CollectionBusy
from .collection_handoff import CollectionHandoff
from .guided.service import GuidedService, MESSAGES
from .guided.contracts import CrawlError
from .collection_guidance import CollectionGuidance
from .evidence_ui import Conflict, EvidenceService
from .quality_ui import QualityService
from .quality_packet import decode_json
from .public_tasks import PublicTasks, PublicTaskBusy
from .guided.ownership import GuidedTaskBusy
from .public_schedule import PublicSchedule
from .public_queue import PublicQueue
from .windows_startup import WindowsStartup, StartupChanged

MAX_BODY = 2_000_000
_DEFAULT_PUBLIC_CLIENT = object()


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    # A cold page opens several connections before the accept thread can run.
    # Keep a bounded pending queue so a brief scheduling pause does not drop JS.
    request_queue_size = 64

    def __init__(self, workspace: Workspace, port: int = 0, *, public_client=_DEFAULT_PUBLIC_CLIENT):
        self.workspace = workspace
        self.collector = Collector(workspace)
        self.collection_runner = CollectionRunner(self.collector)
        self.guidance = CollectionGuidance(workspace)
        self.evidence = EvidenceService(workspace)
        self.quality = QualityService(workspace)
        from .team_client import TeamUI
        self.team = TeamUI(workspace)
        from .release_updates import ReleaseUpdates
        self.releases = ReleaseUpdates(workspace)
        self.guided = GuidedService(workspace)
        self.handoff = CollectionHandoff(self.collector, self.guided)
        if public_client is _DEFAULT_PUBLIC_CLIENT:
            from .team_client import public_client as configured_public_client
            public_client = configured_public_client(workspace)
        self.public_tasks = PublicTasks(workspace, hybrid_client=public_client)
        self.public_schedule = PublicSchedule(workspace, self.public_tasks)
        self.public_queue = PublicQueue(workspace, self.public_tasks)
        self.windows_startup = WindowsStartup(workspace)
        self.token = secrets.token_urlsafe(32)
        self.mutation_lock = threading.Lock()
        super().__init__(("127.0.0.1", port), Handler)
        self.authority = f"127.0.0.1:{self.server_address[1]}"
        self.origin = f"http://{self.authority}"

    def serve_forever(self, poll_interval=0.5):
        try:
            self.public_schedule.start()
            self.public_queue.start()
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self.public_queue.close()
            self.collection_runner.close()
            self.public_schedule.close()

    def server_close(self):
        self.team.close()
        self.releases.close()
        self.workspace.close_network()
        self.public_queue.close()
        self.collection_runner.close()
        self.public_schedule.close()
        self.guided.close()
        self.public_tasks.close()
        super().server_close()

    def upgrade_preflight(self):
        with self.guided._lock:
            if (self.guided._busy or self.guided._backends or self.guided._closing_backends
                    or self.guided._closure_uncertain or self.guided._ownership.foreign()):
                raise InputError('请先停止采集并关闭采集浏览器；自动升级不会中断该会话。')
        if self.public_tasks.busy() or (self.collection_runner._thread and self.collection_runner._thread.is_alive()):
            raise InputError('请先等待或停止当前公开/后台采集任务，再自动升级。')
        if self.team._thread and self.team._thread.is_alive():
            raise InputError('团队同步正在执行，请完成后再自动升级。')

    def upgrade_quiesce(self):
        if not self.mutation_lock.acquire(blocking=False):
            raise InputError('另一个操作正在执行，请完成后再自动升级。')
        try:
            self.upgrade_preflight()
            self.public_queue.close();self.public_schedule.close()
            self.upgrade_preflight()
            if self.public_queue.is_running() or self.public_schedule.is_running():
                raise InputError('后台调度尚未退出，未切换版本。请等待任务完成后重启当前工作台；原计划和待办保留。')
        except BaseException:
            self.mutation_lock.release()
            self.public_schedule.start();self.public_queue.start()
            raise
        # Keep mutations closed until this process exits and releases OS owners.

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
        """After a refusal, discard a bounded prefix of an unambiguous frame.

        Closing a Windows socket with unread inbound data can reset the peer
        before it sees the 403/413. Never parse unauthorized JSON, dispatch it,
        reuse the connection, or wait indefinitely for a slow/truncated body.
        An oversized declaration can still have a short buffered body; ignore
        its excess length without leaving that prefix unread. The original
        byte/time limits also apply to oversized or incomplete transmissions.
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
        if remaining <= 0:
            return
        remaining = min(remaining, MAX_BODY)
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
        public = path in {"/quality", "/quality.js", "/", "/app.js", "/report-sources.js", "/advanced", "/advanced.js", "/collection-help.js", "/guided", "/guided.js", "/network-settings.js", "/public-schedule.js", "/public-history.js", "/team.js", "/release-updates.js", "/public-queue.js", "/windows-startup.js"}
        if not self._authorized(token_required=not public):
            return
        try:
            if public:
                name = {"/quality": "quality.html", "/quality.js": "quality.js", "/": "workbench.html", "/app.js": "workbench.js", "/report-sources.js": "report_sources.js", "/advanced": "advanced.html", "/advanced.js": "advanced.js", "/collection-help.js": "collection_help.js", "/guided": "guided.html", "/guided.js": "guided.js", "/network-settings.js": "network_settings.js", "/public-schedule.js": "public_schedule.js", "/public-history.js": "public_history.js", "/team.js": "team.js", "/release-updates.js": "release_updates.js", "/public-queue.js": "public_queue.js", "/windows-startup.js": "windows_startup.js"}[path]
                mime = "text/html" if name.endswith(".html") else "text/javascript"
                self._respond(200, files("vibe_job_radar").joinpath(name).read_bytes(), mime + "; charset=utf-8")
            elif path == "/api/network/state":
                self._json(200, self.server.workspace.network_state())
            elif path == "/api/releases/state":
                self._json(200, self.server.releases.state())
            elif path == "/api/team/state":
                self._json(200, self.server.team.state())
            elif path == "/api/public/state":
                self._json(200, self.server.public_tasks.state())
            elif path == "/api/public/history":
                self._json(200, self.server.public_tasks.history())
            elif path == "/api/public/history/export":
                self._respond(200, json.dumps(self.server.public_tasks.history(), ensure_ascii=False, indent=2).encode("utf-8"),
                              "application/json; charset=utf-8", filename="source-history.json")
            elif path == "/api/public/schedule/state":
                self._json(200, self.server.public_schedule.state())
            elif path == "/api/public/queue/state":
                self._json(200, self.server.public_queue.state())
            elif path == "/api/windows/startup/state":
                self._json(200, self.server.windows_startup.state())
            elif path == "/api/guided/state":
                self._json(200, self.server.guided.state())
            elif path.startswith("/api/quality/export/"):
                parts = path.removeprefix("/api/quality/export/").split("/")
                if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdecimal() or len(parts[1]) > 10:
                    raise InputError("缺少有效的导出版本。")
                run_id, revision = parts[0], int(parts[1])
                self._respond(200, self.server.quality.export({"report_id": run_id, "expected_revision": revision}),
                              "application/zip", filename=f"jd-quality-{run_id}-v{revision}.zip")
            elif path == "/api/evidence/export":
                self._respond(200, self.server.evidence.export(), "application/zip", filename="personal-evidence.zip")
            elif path.startswith("/api/evidence/attachment/"):
                artifact = self.server.evidence._artifact_path(path.removeprefix("/api/evidence/attachment/"))
                self._respond(200, artifact.read_bytes(), "application/octet-stream", filename=artifact.name)
            elif path == "/api/status":
                self._json(200, {**self.server.workspace.status(),
                                'acquisition_sites':self.server.guided.registry.describe()})
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
        except Conflict as exc:
            self._json(409, {"error": str(exc)})
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
            data = (decode_json(raw) if urlsplit(self.path).path.startswith(("/api/quality/", "/api/team/", "/api/releases/", "/api/network/proxy-auth"))
                    else json.loads(raw.decode("utf-8")))
            if not isinstance(data, dict):
                raise ValueError
        except (ValueError, UnicodeError, TimeoutError):
            self._json(400, {"error": "请求必须为完整的 UTF-8 JSON 对象。"})
            return
        route = urlsplit(self.path).path
        methods = {"/api/job": "add_job", "/api/import": "import_file", "/api/plan": "plan",
                   "/api/discover": "discover", "/api/analyze": "analyze", "/api/network/preferences": "network_preferences",
                   "/api/network/proxy": "network_proxy_preferences",
                   "/api/network/proxy-auth": "network_proxy_auth_preferences",
                   "/api/network/proxy-auth/clear": "network_proxy_auth_clear",
                   "/api/network/pac": "network_pac_preferences",
                   "/api/network/pac-domain": "network_domain_pac_preferences",
                   "/api/network/pac-domain-dns": "network_domain_pac_dns_preferences",
                   "/api/network/pac-url": "network_url_pac_preferences",
                   "/api/network/pac-url-dns": "network_url_pac_dns_preferences",
                   "/api/network/system-pac": "network_system_pac_preferences",
                   "/api/network/wpad/discover": "network_wpad_discover",
                   "/api/network/wpad/cancel": "network_wpad_cancel",
                   "/api/network/wpad": "network_wpad_preferences",
                   "/api/network/pac/check": "network_pac_check"}
        target = self.server.workspace
        if route.startswith("/api/windows/startup/"):
            target = self.server.windows_startup
            methods = {"/api/windows/startup/" + name: name for name in ("enable", "disable")}
        elif route.startswith("/api/releases/"):
            target = self.server.releases
            methods = {"/api/releases/" + name: name for name in ("check", "download", "install")}
        elif route.startswith("/api/team/"):
            target = self.server.team
            methods = {"/api/team/" + name: name for name in ("check", "sync")}
        elif route.startswith("/api/public/schedule/"):
            target = self.server.public_schedule
            methods = {"/api/public/schedule/" + name: name for name in ("configure", "disable")}
        elif route.startswith("/api/public/queue/"):
            target = self.server.public_queue
            methods = {"/api/public/queue/" + name: name for name in ("enqueue", "remove", "pause", "resume")}
        elif route.startswith("/api/public/"):
            target = self.server.public_tasks
            methods = {"/api/public/" + name: name for name in ("start", "search", "cancel", "resume")}
        elif route.startswith("/api/guided/"):
            target = self.server.guided
            methods = {"/api/guided/" + name: name for name in ("create", "action", "install", "check_browser", "diagnose", "export", "diagnostics")}
        elif route.startswith("/api/quality/"):
            target = self.server.quality
            methods = {"/api/quality/" + name: name for name in ("open", "save", "save_pair", "import_annotations")}
        elif route.startswith("/api/evidence/"):
            target = self.server.evidence
            methods = {"/api/evidence/" + name: name for name in ("state", "catalogue", "sources", "source", "review", "upload", "metric", "save", "remove", "generate")}
        elif route in {"/api/collection/handoff_preview", "/api/collection/handoff_start"}:
            target = self.server.handoff
            methods = {"/api/collection/" + name: name for name in ("handoff_preview", "handoff_start")}
        elif route.startswith("/api/collection/background/"):
            target = self.server.collection_runner
            methods = {"/api/collection/background/" + name: name for name in ("start", "state", "pause")}
        elif route.startswith("/api/collection/"):
            target = self.server.guidance if route == "/api/collection/preview" else self.server.collector
            methods = {"/api/collection/" + name: name for name in ("start", "step", "status", "list", "register", "preview", "category_next_preview", "category_next_start", "category_page_preview", "category_page_start", "category_recovery_preview", "category_recovery_start", "category_page_retry_preview", "category_page_retry_start")}
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
        except PublicTaskBusy as exc:
            status, response = 409, {"error": str(exc), "code": "public_task_busy"}
        except CollectionBusy as exc:
            status, response = 409, {"error": str(exc), "code": "collection_busy"}
        except GuidedTaskBusy as exc:
            status, response = 409, {"error": str(exc), "code": "guided_task_busy"}
        except StartupChanged as exc:
            status, response = 409, {"error": str(exc), "code": "startup_changed"}
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
    original_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Vibe Job Radar 本地浏览器工作台（可选受控浏览器与人工登录协助）")
    parser.add_argument("--workspace", type=Path, default=Path.home() / ".vibe-job-radar")
    parser.add_argument("--port", type=int, default=0, help="默认由系统选择空闲端口，仅绑定 127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    modes=parser.add_mutually_exclusive_group()
    modes.add_argument("--doctor", action="store_true", help="离线检查 Python、SQLite 和目录写入能力")
    modes.add_argument("--team-install", type=Path, help="从管理员配置文件安装本机团队连接，然后退出")
    parser.add_argument("--team-credential", type=Path, help="仅配合 --team-install 的设备凭据文件；不要把令牌写入命令行")
    modes.add_argument('--public-worker',action='store_true',help='仅运行已确认的公开查询计划和待办，不启动网页服务器或浏览器')
    modes.add_argument('--native-browser-check',action='store_true',help='检查新临时原生浏览器的空白页、请求拒绝与退出；不连接招聘网站')
    modes.add_argument('--ntlm-component-check',action='store_true',help='离线检查NTLMv2组件及隔离进程；只用人工凭据，不连接代理或站点')
    parser.add_argument('--native-browser-channel',choices=['bundled','msedge','chrome'],help='仅用于原生组件检查，默认配套Chromium')
    modes.add_argument('--apply-update',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--upgrade-handoff',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--ignore-installed-update',action='store_true',help='故障恢复时启动此便携版本，不转到已安装的新版本')
    args = parser.parse_args(original_argv)
    if args.apply_update:
        from .release_install import apply_update
        try:return apply_update(args.apply_update)
        except (OSError,ValueError,KeyError,TypeError):return 2
    if args.upgrade_handoff and (args.doctor or args.team_install or args.public_worker or args.native_browser_check or args.ntlm_component_check):
        parser.error('升级交接只用于工作台启动')
    if not (args.ignore_installed_update or args.upgrade_handoff or args.doctor or args.team_install or args.native_browser_check or args.ntlm_component_check):
        from .release_install import installed_command, spawn
        try:
            command=installed_command(args.workspace,original_argv)
            if command:
                spawn(command)
                return 0
        except (OSError,ValueError,KeyError,TypeError):
            print('已安装版本入口校验失败；保留数据。可使用 --ignore-installed-update 启动此程序进行恢复。',file=sys.stderr)
            return 2
    if not 0 <= args.port <= 65535:
        parser.error("port 必须为 0～65535")
    if args.team_install:
        if not args.team_credential or args.port:parser.error("团队安装需要 --team-credential，且不启动服务端口")
        from .team_client import install_connection
        try:
            print(json.dumps(install_connection(args.workspace,args.team_install,args.team_credential),ensure_ascii=False))
            return 0
        except (OSError,ValueError,CrawlError):
            print("团队连接安装失败；核对管理员文件及工作区，原数据保留。",file=sys.stderr)
            return 2
    if args.team_credential:parser.error("--team-credential 仅配合 --team-install")
    if args.ntlm_component_check:
        if args.port or args.native_browser_channel:parser.error('NTLM组件检查不启动网页或浏览器')
        from .ntlm_check import run_cli
        return run_cli()
    if args.native_browser_check:
        if args.port:parser.error('原生组件检查不启动服务，请移除--port')
        from .guided.native_check import run_cli
        return run_cli(channel=args.native_browser_channel)
    if args.native_browser_channel:parser.error('--native-browser-channel 仅配合 --native-browser-check')
    if args.public_worker:
        if args.port:parser.error('独立worker不监听端口，请移除--port')
        from .public_worker import run_cli
        return run_cli(args.workspace)
    try:
        workspace = Workspace(args.workspace)
        diagnostic = workspace.doctor()
        if args.doctor:
            print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
            return 0
        with LocalServer(workspace, args.port) as server:
            from .runtime import is_portable
            if is_portable() and sys.platform=='win32':
                from .release_install import AutomaticInstaller
                server.releases.installer=AutomaticInstaller(workspace,shutdown=server.shutdown,
                    quiesce=server.upgrade_quiesce,preflight=server.upgrade_preflight,no_browser=args.no_browser)
            if args.upgrade_handoff:
                from .release_install import acknowledge_ready
                acknowledge_ready(args.upgrade_handoff,workspace,server.server_address[1])
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

"""Loopback-only browser UI. Not a public/multi-user web deployment."""
from __future__ import annotations

import argparse
import hmac
import json
import secrets
from socketserver import TCPServer
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from .runtime_identity import bootstrap as _bootstrap_runtime
_bootstrap_runtime()

from .workspace import InputError, Workspace
from .guided.contracts import CrawlError
from .application import WorkbenchApplication, SERVICE_NAMES, _DEFAULT_PUBLIC_CLIENT
from .application_http import MAX_BODY, STATIC_ROUTES, Response, WorkbenchHTTP, decode_request_body


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    # A cold page opens several connections before the accept thread can run.
    # Keep a bounded pending queue so a brief scheduling pause does not drop JS.
    request_queue_size = 64

    def __init__(self, workspace: Workspace, port: int = 0, *, public_client=_DEFAULT_PUBLIC_CLIENT):
        self.application = WorkbenchApplication(workspace, public_client=public_client)
        self.http = WorkbenchHTTP(self.application)
        self.token = secrets.token_urlsafe(32)
        try:
            super().__init__(("127.0.0.1", port), Handler)
        except BaseException:
            self.application.close()
            raise
        self.authority = f"127.0.0.1:{self.server_address[1]}"
        self.origin = f"http://{self.authority}"

    def __getattr__(self, name):
        if name in SERVICE_NAMES:
            return getattr(self.application, name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in SERVICE_NAMES and 'application' in self.__dict__:
            setattr(self.application, name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in SERVICE_NAMES:
            delattr(self.application, name)
        else:
            super().__delattr__(name)

    def server_bind(self):
        # This listener has a fixed numeric loopback address. Reverse DNS is
        # unnecessary and can stall offline/VPN startup inside HTTPServer.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def serve_forever(self, poll_interval=0.5):
        try:
            self.application.start()
            super().serve_forever(poll_interval=poll_interval)
        finally:
            self.application.close()

    def server_close(self):
        try:
            self.application.close()
        finally:
            super().server_close()

    def upgrade_preflight(self):
        return self.application.upgrade_preflight()

    def upgrade_quiesce(self):
        return self.application.upgrade_quiesce()

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

    def _json(self, code: int, data: dict | bytes):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._respond(code, body, "application/json; charset=utf-8")

    def _deliver(self, response: Response):
        if response.content_type == "application/json; charset=utf-8" and not response.filename:
            self._json(response.status, response.body)
        else:
            self._respond(response.status, response.body, response.content_type, filename=response.filename)

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
        if self._authorized(token_required=path not in STATIC_ROUTES):
            self._deliver(self.server.http.get(path))

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
            data = decode_request_body(urlsplit(self.path).path, raw)
        except (ValueError, UnicodeError, TimeoutError):
            self._json(400, {"error": "请求必须为完整的 UTF-8 JSON 对象。"})
            return
        self._deliver(self.server.http.post(urlsplit(self.path).path, data))

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
    modes.add_argument("--team-protect-credential", action="store_true", help="明确将原团队设备凭据迁移到系统加密，然后退出；不联网")
    parser.add_argument("--team-encrypted", action="store_true", help="仅配合 --team-install，明确要求系统加密")
    parser.add_argument("--team-credential", type=Path, help="仅配合 --team-install 的设备凭据文件；不要把令牌写入命令行")
    modes.add_argument('--public-worker',action='store_true',help='仅运行已确认的公开查询计划和待办，不启动网页服务器或浏览器')
    modes.add_argument('--native-browser-check',action='store_true',help='检查新临时原生浏览器的空白页、请求拒绝与退出；不连接招聘网站')
    modes.add_argument('--ntlm-component-check',action='store_true',help='离线检查NTLMv2组件及隔离进程；只用人工凭据，不连接代理或站点')
    parser.add_argument('--native-browser-channel',choices=['bundled','msedge','chrome'],help='仅用于原生组件检查，默认配套Chromium')
    modes.add_argument('--apply-update',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--upgrade-handoff',type=Path,help=argparse.SUPPRESS)
    parser.add_argument('--update-parent-fd',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--ignore-installed-update',action='store_true',help='故障恢复时启动此便携版本，不转到已安装的新版本')
    args = parser.parse_args(original_argv)
    if args.apply_update:
        from .release_install import apply_update
        try:return apply_update(args.apply_update,**({'parent_fd':args.update_parent_fd} if args.update_parent_fd is not None else {}))
        except (OSError,ValueError,KeyError,TypeError):return 2
    if args.update_parent_fd is not None:parser.error('父进程描述符仅用于升级助手')
    if args.upgrade_handoff and (args.doctor or args.team_install or args.team_protect_credential or args.public_worker or args.native_browser_check or args.ntlm_component_check):
        parser.error('升级交接只用于工作台启动')
    if not (args.ignore_installed_update or args.upgrade_handoff or args.doctor or args.team_install or args.team_protect_credential or args.native_browser_check or args.ntlm_component_check):
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
    if args.team_encrypted and not args.team_install:parser.error("--team-encrypted 仅配合 --team-install")
    if args.team_protect_credential:
        if args.port or args.team_credential:parser.error("凭据迁移不启动服务，不接受新令牌文件")
        from .team_client import protect_connection
        from .system_credentials import MESSAGES
        try:
            print(json.dumps(protect_connection(args.workspace),ensure_ascii=False));return 0
        except (OSError,ValueError,CrawlError) as error:
            print(json.dumps({"ok":False,"error":getattr(error,"code","team_credential_invalid"),
                "message":MESSAGES.get(getattr(error,"code",""),"迁移未完成，原配置保留。")},ensure_ascii=False));return 2
    if args.team_install:
        if not args.team_credential or args.port:parser.error("团队安装需要 --team-credential，且不启动服务端口")
        from .team_client import install_connection
        try:
            print(json.dumps(install_connection(args.workspace,args.team_install,args.team_credential,encrypted=args.team_encrypted),ensure_ascii=False))
            return 0
        except (OSError,ValueError,CrawlError) as error:
            from .system_credentials import MESSAGES
            print(MESSAGES.get(getattr(error,"code",""),"团队连接安装失败；核对管理员文件及工作区，原数据保留。"),file=sys.stderr)
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
            from .release_install import supported
            if supported():
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
                    from .runtime import open_workbench_browser
                    if not open_workbench_browser(server.entry_url):
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

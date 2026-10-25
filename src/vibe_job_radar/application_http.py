"""Socket-independent workbench routes and response serialization.

Transports must authorize requests and validate their framing before dispatch.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import json

from .workspace import InputError
from .application_commands import ApplicationCommands, UnknownCommand, OperationBusy
from .report_tasks import ReportTaskBusy
from .collection_runtime import CollectionBusy
from .evidence_ui import Conflict
from .guided.contracts import CrawlError
from .guided.ownership import GuidedTaskBusy
from .guided.service import MESSAGES
from .public_tasks import PublicTaskBusy
from .quality_packet import decode_json
from .windows_startup import StartupChanged

MAX_BODY = 2_000_000
STATIC_ROUTES = {'/report-tasks.js': 'report_tasks.js', '/quality': 'quality.html', '/quality.js': 'quality.js', '/': 'workbench.html', '/app.js': 'workbench.js', '/report-sources.js': 'report_sources.js', '/advanced': 'advanced.html', '/advanced.js': 'advanced.js', '/collection-help.js': 'collection_help.js', '/guided': 'guided.html', '/guided.js': 'guided.js', '/network-settings.js': 'network_settings.js', '/public-schedule.js': 'public_schedule.js', '/public-history.js': 'public_history.js', '/team.js': 'team.js', '/release-updates.js': 'release_updates.js', '/public-queue.js': 'public_queue.js', '/windows-startup.js': 'windows_startup.js'}


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    content_type: str
    filename: str | None = None


def decode_request_body(path: str, raw: bytes) -> dict:
    value = (decode_json(raw) if path.startswith(('/api/quality/', '/api/team/',
             '/api/releases/', '/api/network/proxy-auth')) else json.loads(raw.decode('utf-8')))
    if not isinstance(value, dict):
        raise ValueError('request must be a JSON object')
    return value


class WorkbenchHTTP:
    def __init__(self, application):
        self.application = application
        self.commands = ApplicationCommands(application)

    @staticmethod
    def _json(code: int, data: dict) -> Response:
        return Response(code, json.dumps(data, ensure_ascii=False).encode('utf-8'),
                        'application/json; charset=utf-8')

    def get(self, path: str) -> Response:
        try:
            if path in STATIC_ROUTES:
                name = STATIC_ROUTES[path]
                mime = "text/html" if name.endswith(".html") else "text/javascript"
                return Response(200, files("vibe_job_radar").joinpath(name).read_bytes(), mime + "; charset=utf-8")
            elif path == "/api/reports/state":
                return self._json(200, self.commands.execute("report_tasks.state", {}))
            elif path == "/api/network/state":
                return self._json(200, self.application.workspace.network_state())
            elif path == "/api/releases/state":
                return self._json(200, self.application.releases.state())
            elif path == "/api/team/state":
                return self._json(200, self.application.team.state())
            elif path == "/api/public/state":
                return self._json(200, self.application.public_tasks.state())
            elif path == "/api/public/history":
                return self._json(200, self.application.public_tasks.history())
            elif path == "/api/public/history/export":
                return Response(200, json.dumps(self.application.public_tasks.history(), ensure_ascii=False, indent=2).encode("utf-8"),
                              "application/json; charset=utf-8", filename="source-history.json")
            elif path == "/api/public/schedule/state":
                return self._json(200, self.application.public_schedule.state())
            elif path == "/api/public/queue/state":
                return self._json(200, self.application.public_queue.state())
            elif path == "/api/windows/startup/state":
                return self._json(200, self.application.windows_startup.state())
            elif path == "/api/guided/state":
                return self._json(200, self.application.guided.state())
            elif path.startswith("/api/quality/export/"):
                parts = path.removeprefix("/api/quality/export/").split("/")
                if len(parts) != 2 or not parts[1].isascii() or not parts[1].isdecimal() or len(parts[1]) > 10:
                    raise InputError("缺少有效的导出版本。")
                run_id, revision = parts[0], int(parts[1])
                return Response(200, self.application.quality.export({"report_id": run_id, "expected_revision": revision}),
                              "application/zip", filename=f"jd-quality-{run_id}-v{revision}.zip")
            elif path == "/api/evidence/export":
                return Response(200, self.application.evidence.export(), "application/zip", filename="personal-evidence.zip")
            elif path.startswith("/api/evidence/attachment/"):
                artifact = self.application.evidence._artifact_path(path.removeprefix("/api/evidence/attachment/"))
                return Response(200, artifact.read_bytes(), "application/octet-stream", filename=artifact.name)
            elif path == "/api/status":
                return self._json(200, {**self.application.workspace.status(),
                                'acquisition_sites':self.application.guided.registry.describe()})
            elif path == "/api/doctor":
                return self._json(200, self.application.workspace.doctor())
            elif path.startswith("/api/report/"):
                run_id = path.removeprefix("/api/report/")
                return self._json(200, self.application.workspace.report(run_id))
            elif path.startswith("/api/download/"):
                parts = path.removeprefix("/api/download/").split("/")
                if len(parts) != 2:
                    raise InputError("无效下载路径。")
                artifact = self.application.workspace.report_file(*parts)
                return Response(200, artifact.read_bytes(), "application/octet-stream", filename=artifact.name)
            else:
                return self._json(404, {"error": "入口不存在。"})
        except Conflict as exc:
            return self._json(409, {"error": str(exc)})
        except InputError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            return self._json(500, {"error": f"读取失败 [{type(exc).__name__}]；请检查本地目录和报告文件。"})

    def post(self, route: str, data: dict) -> Response:
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
        service = "workspace"
        if route.startswith("/api/reports/"):
            service = "report_tasks"
            methods = {"/api/reports/start": "start"}
        elif route.startswith("/api/windows/startup/"):
            service = "windows_startup"
            methods = {"/api/windows/startup/" + name: name for name in ("enable", "disable")}
        elif route.startswith("/api/releases/"):
            service = "releases"
            methods = {"/api/releases/" + name: name for name in ("check", "download", "install")}
        elif route.startswith("/api/team/"):
            service = "team"
            methods = {"/api/team/" + name: name for name in ("check", "sync", "protect")}
        elif route.startswith("/api/public/schedule/"):
            service = "public_schedule"
            methods = {"/api/public/schedule/" + name: name for name in ("configure", "disable")}
        elif route.startswith("/api/public/queue/"):
            service = "public_queue"
            methods = {"/api/public/queue/" + name: name for name in ("enqueue", "remove", "pause", "resume")}
        elif route.startswith("/api/public/"):
            service = "public_tasks"
            methods = {"/api/public/" + name: name for name in ("start", "search", "cancel", "resume")}
        elif route.startswith("/api/guided/"):
            service = "guided"
            methods = {"/api/guided/" + name: name for name in ("create", "action", "install", "check_browser", "diagnose", "export", "diagnostics")}
        elif route.startswith("/api/quality/"):
            service = "quality"
            methods = {"/api/quality/" + name: name for name in ("open", "save", "save_pair", "import_annotations")}
        elif route.startswith("/api/evidence/"):
            service = "evidence"
            methods = {"/api/evidence/" + name: name for name in ("state", "catalogue", "sources", "source", "review", "upload", "metric", "save", "remove", "generate")}
        elif route in {"/api/collection/handoff_preview", "/api/collection/handoff_start"}:
            service = "handoff"
            methods = {"/api/collection/" + name: name for name in ("handoff_preview", "handoff_start")}
        elif route.startswith("/api/collection/background/"):
            service = "collection_runner"
            methods = {"/api/collection/background/" + name: name for name in ("start", "state", "pause")}
        elif route.startswith("/api/collection/"):
            service = "guidance" if route == "/api/collection/preview" else "collector"
            methods = {"/api/collection/" + name: name for name in ("start", "step", "status", "list", "register", "preview", "category_next_preview", "category_next_start", "category_page_preview", "category_page_start", "category_recovery_preview", "category_recovery_start", "category_page_retry_preview", "category_page_retry_start")}
        if route not in methods:
            return self._json(404, {"error": "入口不存在。"})
        try:
            result = self.commands.execute(service + "." + methods[route], data)
            status, response = 200, result
        except UnknownCommand:
            status, response = 404, {"error": "入口不存在。"}
        except OperationBusy as exc:
            status, response = 409, {"error": str(exc)}
        except ReportTaskBusy as exc:
            status, response = 409, {"error": str(exc), "code": "report_task_busy"}
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
        # Exclusive commands release their gate before returning. Background
        # report admission explicitly transfers it to the accepted worker.
        return self._json(status, response)

"""One isolated, authored HTTP/report/persistence smoke against a supplied image."""
from __future__ import annotations
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile
import time

from container_release import CHECKS, IDENTITY, command, require, save


def smoke(image, identity, output, *, docker_env=None):
    result = {"schema_version": 1, **{key: identity[key] for key in IDENTITY},
              "success": False, "external_collection": False, "checks": []}
    suffix = secrets.token_hex(8)
    container, volume = "radar-smoke-" + suffix, "radar-smoke-data-" + suffix
    cookie = csrf = ""
    running = created_volume = False

    def docker(*args, **kwargs):
        return command(["docker", *args], env=docker_env, **kwargs)

    def check(name, passed):
        result["checks"].append({"name": name, "passed": bool(passed)})
        require(passed, "Container smoke failed: " + name)

    with tempfile.TemporaryDirectory(prefix="radar-container-secret-") as folder:
        secret = Path(folder) / "password"
        password = secrets.token_urlsafe(36)
        secret.write_text(password, encoding="utf-8")
        secret.chmod(0o444)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        origin = "http://localhost:" + str(port)

        def request(path, method="GET", data=None, *, auth=True, token=True):
            headers = {"Host": "localhost:" + str(port)}
            if auth and cookie:
                headers["Cookie"] = cookie
            if auth and token and csrf:
                headers["X-Radar-Token"] = csrf
            body = None
            if method == "POST":
                headers.update(Origin=origin, **{"Content-Type": "application/json"})
                body = json.dumps(data, ensure_ascii=False).encode()
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request(method, path, body, headers)
                response = connection.getresponse()
                content = response.read(2_000_001)
                require(len(content) <= 2_000_000, "Oversized smoke response")
                return response.status, dict(response.getheaders()), content
            finally:
                connection.close()

        def start():
            nonlocal running
            docker("run", "-d", "--name", container, "--user", "10001:10001", "--init",
                   "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                   "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m,mode=1777",
                   "--mount", "type=volume,src=" + volume + ",dst=/data",
                   "--mount", "type=bind,src=" + str(secret) + ",dst=/run/secrets/password,readonly",
                   "-p", "127.0.0.1:" + str(port) + ":8080", image,
                   "--workspace", "/data", "--host", "0.0.0.0", "--port", "8080",
                   "--origin", origin, "--password-file", "/run/secrets/password", timeout=90)
            running = True
            deadline = time.monotonic() + 30
            while True:
                try:
                    status, _, body = request("/healthz", auth=False)
                    if status == 200 and json.loads(body) == {"ok": True}:
                        return
                except (OSError, http.client.HTTPException):
                    pass
                require(time.monotonic() < deadline, "Container did not become healthy")
                time.sleep(0.2)

        def stop(check_name):
            nonlocal running
            docker("stop", "--time", "60", container, timeout=75)
            info = json.loads(docker("inspect", container))[0]
            check(check_name, info["State"]["Running"] is False and info["State"]["ExitCode"] == 0)
            docker("rm", container)
            running = False

        def login(check_name):
            nonlocal cookie, csrf
            status, headers, _ = request("/api/session/login", "POST", {"password": password}, auth=False)
            check(check_name, status == 200)
            header = headers.get("Set-Cookie", "")
            cookie = header.split(";", 1)[0]
            status, _, body = request("/server-session.js", token=False)
            match = re.search(rb"sessionStorage\.setItem\('radar-session',(\"[^\"]+\")\);", body)
            require(status == 200 and match is not None, "Missing authenticated bootstrap")
            csrf = json.loads(match.group(1))
            return header, status

        try:
            docker("volume", "create", volume)
            created_volume = True
            start()
            info = json.loads(docker("inspect", container))[0]
            config = info["HostConfig"]
            check("non_root", info["Config"]["User"] == "10001:10001")
            check("read_only_root", config["ReadonlyRootfs"] is True)
            check("no_new_privileges", any(x.startswith("no-new-privileges") for x in config["SecurityOpt"]))
            check("capabilities_dropped", "ALL" in config["CapDrop"])
            runtime = json.loads(docker("exec", container, "python", "-c",
                "import json,os,platform; from vibe_job_radar._version import __version__; "
                "print(json.dumps(dict(version=__version__,machine=platform.machine(),uid=os.getuid())))"))
            check("runtime_version", runtime["version"] == identity["version"] and runtime["uid"] == 10001)
            check("runtime_architecture", runtime["machine"] in (
                {"amd64": ("x86_64",), "arm64": ("aarch64", "arm64")}[identity["architecture"]]))
            status, _, body = request("/healthz", auth=False)
            check("anonymous_health", status == 200 and json.loads(body) == {"ok": True})
            check("anonymous_private_denied", request("/api/status", auth=False)[0] == 401)
            header, status = login("password_login")
            check("cookie_flags", all(flag in header for flag in ("HttpOnly", "SameSite=Strict", "Path=/")))
            check("authenticated_bootstrap", status == 200)
            status, _, body = request("/api/runtime")
            check("server_mode", status == 200 and json.loads(body)["mode"] == "server")
            row = {"title": "时间序列算法工程师", "company": "AUTHORED CONTAINER FIXTURE",
                   "text": "负责时间序列预测。要求熟练使用 Cursor 进行 AI 辅助编程，必须编写单元测试并进行代码审查。",
                   "platform": "boss", "url": "https://www.zhipin.com/job_detail/offline-container-fixture.html",
                   "evidence_level": "full_text"}
            status, _, body = request("/api/import", "POST", {
                "filename": "authored-container.jsonl", "content": json.dumps(row, ensure_ascii=False),
                "rights_note": "Authored offline container fixture; not market data",
                "full_text_confirmed": True})
            check("authored_import", status == 200 and json.loads(body)["new_snapshots"] == 1)
            status, _, body = request("/api/analyze", "POST", {"dataset": "real", "roles": ["time_series"]})
            report = json.loads(body)
            check("original_report", status == 200 and report["manifest"]["stats"]["full_text_job_groups"] > 0
                  and report["manifest"]["stats"]["requirement_rows"] > 0)
            result["report_id"] = report["id"]
            path = "/api/download/" + report["id"] + "/requirements_zh.csv"
            status, headers, body = request(path)
            check("report_download", status == 200 and len(body) > 0
                  and "attachment;" in headers.get("Content-Disposition", ""))
            result["report_sha256"] = "sha256:" + hashlib.sha256(body).hexdigest()
            stop("first_stop_clean")
            start()
            check("restart_old_session_denied", request("/api/status")[0] == 401)
            login("restart_login")
            status, _, body = request("/api/report/" + report["id"])
            check("persistent_original_report", status == 200 and json.loads(body)["manifest"] == report["manifest"])
            status, _, body = request(path)
            check("persistent_report_download", status == 200 and
                  "sha256:" + hashlib.sha256(body).hexdigest() == result["report_sha256"])
            status, _, _ = request("/api/session/logout", "POST", {})
            check("logout_revoked", status == 200 and request("/api/status")[0] == 401)
            stop("second_stop_clean")
            require({row["name"] for row in result["checks"]} == set(CHECKS), "Missing smoke checks")
            result["success"] = True
        except Exception as error:
            result["error_type"] = type(error).__name__
            raise
        finally:
            try:
                if running:
                    docker("rm", "-f", container, timeout=75)
                if created_volume:
                    docker("volume", "rm", volume)
            finally:
                save(output, result)
    return result

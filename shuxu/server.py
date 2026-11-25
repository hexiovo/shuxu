from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shuxu.book import BookService, LOCAL_ACTIONS, TOOL_SPECS
from shuxu.materials import MAX_IMAGE_BYTES

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
STORAGE_ROOT = Path("I:/data/shuxu") if os.name == "nt" else PROJECT_ROOT
DATA_DIR = Path(os.environ.get("SHUXU_DATA_DIR", STORAGE_ROOT / "data")).resolve()
RUNTIME_DIR = Path(os.environ.get("SHUXU_RUNTIME_DIR", STORAGE_ROOT / "runtime")).resolve()
RUNTIME_FILE = RUNTIME_DIR / "service.json"
STARTUP_LOG = RUNTIME_DIR / "service-stderr.log"
PORT = int(os.environ.get("SHUXU_PORT", "17831"))
if not 1 <= PORT <= 65535:
    raise ValueError("SHUXU_PORT must be between 1 and 65535")

ACTION_NAMES = {item["name"] for item in TOOL_SPECS} | LOCAL_ACTIONS
LOGS: deque[dict[str, str]] = deque(maxlen=200)
LOG_LOCK = threading.Lock()
BOOK_SERVICE: BookService | None = None
SERVER: ThreadingHTTPServer | None = None
INSTANCE_ID = ""
BRIDGE_TOKEN = ""
SESSION_TOKEN = ""
STARTED_AT = ""
MCP_LAST_REQUEST_AT: str | None = None
_RESTART_DRIVER_LOCK = threading.Lock()
_RESTART_DRIVERS: set[tuple[str, int]] = set()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def record(level: str, message: str) -> None:
    item = {"at": utc_now(), "level": level, "message": message}
    with LOG_LOCK:
        LOGS.append(item)


def read_runtime() -> dict | None:
    if not RUNTIME_FILE.is_file():
        return None
    try:
        value = json.loads(RUNTIME_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Service identity file is unreadable; refusing to start or reuse a service.") from exc
    if not isinstance(value, dict) or not all(key in value for key in ("pid", "port", "instance_id", "bridge_token")):
        raise RuntimeError("Service identity file is incomplete; refusing to start or reuse a service.")
    return value


def process_is_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return True
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def verify_runtime(info: dict) -> bool:
    url = f"http://127.0.0.1:{info['port']}/api/health"
    request = urllib.request.Request(url, headers={"X-Book-Bridge": info["bridge_token"]})
    try:
        with urllib.request.urlopen(request, timeout=0.6) as response:
            payload = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return False
    data = payload.get("data", {})
    return payload.get("status") == "ok" and data.get("instance_id") == info["instance_id"]


def verified_runtime() -> dict | None:
    info = read_runtime()
    if info is None:
        return None
    if verify_runtime(info):
        return info
    if process_is_alive(int(info["pid"])):
        raise RuntimeError("A local process is running but its service identity could not be verified; refusing to reuse it.")
    RUNTIME_FILE.unlink()
    return None


def _pending_restart() -> tuple[str, str, dict] | None:
    path = DATA_DIR / "mcp24-operations.sqlite3"
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path, timeout=2)) as db:
            rows = db.execute(
                "SELECT actor_domain, operation_id, state_json FROM mcp24_service_restarts WHERE status IN ('stopping', 'starting', 'verifying') ORDER BY updated_at DESC"
            ).fetchall()
    except sqlite3.Error:
        return None
    for actor_domain, operation_id, state_json in rows:
        state = json.loads(state_json)
        if state.get("old_instance", {}).get("pid") != os.getpid():
            return actor_domain, operation_id, state
    return None


def _wait_for_restart_start() -> dict | None:
    pending = _pending_restart()
    if pending is None:
        return None
    actor_domain, operation_id, state = pending
    store = _maintenance_store()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            info = verified_runtime()
        except RuntimeError:
            info = None
        if info is not None and info.get("restart_operation_id") == operation_id:
            return info
        state = store.get_internal(actor_domain, operation_id)
        if state is None or state["status"] in {"failed", "completed"}:
            return None
        helper_pid = state.get("helper_pid")
        if helper_pid is not None and not process_is_alive(int(helper_pid)):
            _maintenance_failure(
                store, actor_domain, operation_id, "helper_exited",
                "独立重启进程提前退出；操作已标记为可重试，服务将恢复启动。",
            )
            return None
        time.sleep(0.1)
    _maintenance_failure(
        store, actor_domain, operation_id, "helper_start_timeout",
        "等待独立重启进程启动新实例时超时；操作已标记为可重试，服务将恢复启动。",
    )
    return None


def ensure_server(open_page: bool = False) -> dict:
    info = verified_runtime()
    if info is None:
        if not os.environ.get("SHUXU_SERVICE_RESTART_OPERATION_ID"):
            info = _wait_for_restart_start()
    if info is None:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        startup = STARTUP_LOG.open("ab")
        startup.seek(0, os.SEEK_END)
        startup_offset = startup.tell()
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        restart_operation_id = os.environ.get("SHUXU_SERVICE_RESTART_OPERATION_ID")
        process = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()), "serve"],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=startup,
            creationflags=flags,
            close_fds=True,
            env={**os.environ, **({"SHUXU_SERVICE_RESTART_OPERATION_ID": restart_operation_id} if restart_operation_id else {})},
        )
        startup.close()
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            info = verified_runtime()
            if info is not None:
                break
            if process.poll() is not None:
                with STARTUP_LOG.open("rb") as log_file:
                    log_file.seek(startup_offset)
                    startup_output = log_file.read().decode("utf-8", "replace")
                known_conflict = f"Port {PORT} is occupied by a service whose identity cannot be verified."
                if known_conflict in startup_output:
                    raise RuntimeError(f"书序检测到端口 {PORT} 已被其他本机服务占用，请更换 SHUXU_PORT 或先核验占用服务。")
                raise RuntimeError(f"The local service did not start; see {STARTUP_LOG}.")
            time.sleep(0.1)
        if info is None:
            raise RuntimeError(f"The local service did not become ready; see {STARTUP_LOG}.")
    if open_page:
        webbrowser.open(f"http://127.0.0.1:{info['port']}/")
    return info


def write_runtime(info: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, RUNTIME_FILE)


def remove_our_runtime(instance_id: str) -> None:
    try:
        info = read_runtime()
    except RuntimeError:
        return
    if info and info.get("instance_id") == instance_id:
        RUNTIME_FILE.unlink(missing_ok=True)


def wait_for_verified_service(seconds: float = 2.0) -> dict | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            info = verified_runtime()
        except RuntimeError:
            return None
        if info is not None:
            return info
        time.sleep(0.1)
    return None


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True
    block_on_close = False


def _maintenance_store():
    from shuxu.mcp24_ops import ServiceMaintenanceStore

    return ServiceMaintenanceStore(DATA_DIR / "mcp24-operations.sqlite3")


def _maintenance_failure(store, actor_domain: str, operation_id: str, code: str, message: str,
                         *, active_work: dict | None = None) -> None:
    state = store.get_internal(actor_domain, operation_id)
    if state is None:
        return
    attempts = state.get("attempts", [])
    if attempts:
        attempts[-1]["status"] = "failed"
        attempts[-1]["error_code"] = code
        attempts[-1]["retryable"] = True
    store.update(
        actor_domain, operation_id, status="failed",
        retryable=True,
        error={"code": code, "message": message},
        active_work=active_work or state.get("active_work"),
        attempts=attempts,
    )


def _active_restart_work(book, store) -> dict:
    scheduler = book._dispatch_scheduler.maintenance_snapshot()
    jobs = store.active_jobs()
    return {
        "active_dispatches": scheduler["active_dispatches"],
        "active_writes": scheduler["active_writes"],
        "active_actions": scheduler["active_actions"],
        "jobs": jobs,
    }


def _restart_driver(book, operation_id: str, actor_domain: str, attempt: int) -> None:
    try:
        _run_restart_driver(book, operation_id, actor_domain, attempt)
    finally:
        with _RESTART_DRIVER_LOCK:
            _RESTART_DRIVERS.discard((operation_id, attempt))


def _run_restart_driver(book, operation_id: str, actor_domain: str, attempt: int) -> None:
    from shuxu import mcp24_ops

    store = _maintenance_store()
    state = store.get_internal(actor_domain, operation_id)
    if state is None or state["attempt"] != attempt:
        return
    timeout_seconds = state["timeout_seconds"]
    deadline = time.monotonic() + timeout_seconds
    scheduler = book._dispatch_scheduler
    store.update(actor_domain, operation_id, status="draining")
    while True:
        active_work = _active_restart_work(book, store)
        if active_work["active_dispatches"] == 0 and not active_work["jobs"]:
            break
        if time.monotonic() >= deadline:
            _maintenance_failure(
                store, actor_domain, operation_id, "drain_timeout",
                "等待在途请求或已入列任务完成时超时；服务已恢复接收写入，可用相同 operation_id 重试。",
                active_work=active_work,
            )
            scheduler.finish_maintenance(operation_id)
            return
        with scheduler._admission:
            scheduler._admission.wait(min(0.1, max(0.0, deadline - time.monotonic())))
    scheduler.set_maintenance_state(operation_id, "stopping")
    store.update(actor_domain, operation_id, status="stopping", active_work=active_work)
    helper = [
        sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
        "maintenance-helper", "--operation-id", operation_id, "--actor-domain", actor_domain,
    ]
    if SERVER is None:
        _maintenance_failure(store, actor_domain, operation_id, "service_unavailable", "当前 HTTP 服务实例已不可用，可用相同 operation_id 重试。")
        scheduler.finish_maintenance(operation_id)
        return
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.Popen(
            helper, cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True,
        )
    except OSError as error:
        _maintenance_failure(
            store, actor_domain, operation_id, "helper_start_failed",
            f"无法启动独立重启进程（{type(error).__name__}）；服务仍在运行，可用相同 operation_id 重试。",
            active_work=active_work,
        )
        scheduler.finish_maintenance(operation_id)
        return
    SERVER.shutdown()


def start_service_restart(book, operation_id: str, actor_domain: str) -> None:
    state = _maintenance_store().get_internal(actor_domain, operation_id)
    if state is None or state["status"] != "accepted":
        return
    if state["old_instance"]["instance_id"] != INSTANCE_ID:
        return
    snapshot = book._dispatch_scheduler.maintenance_snapshot()
    if snapshot["operation_id"] != operation_id or snapshot["state"] != "draining":
        return
    attempt = state["attempt"]
    driver_key = (operation_id, attempt)
    with _RESTART_DRIVER_LOCK:
        if driver_key in _RESTART_DRIVERS:
            return
        _RESTART_DRIVERS.add(driver_key)
    threading.Thread(
        target=_restart_driver, args=(book, operation_id, actor_domain, attempt),
        daemon=True, name="shuxu-service-restart",
    ).start()


def _receipt_persisted(state: dict) -> bool:
    path = DATA_DIR / "shuxu.sqlite3"
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path, timeout=2)) as db:
            row = db.execute(
                "SELECT action, response_json FROM request_receipts WHERE request_id = ?",
                (state["request_receipt_id"],),
            ).fetchone()
    except sqlite3.Error:
        return False
    if row is None or row[0] != state["request_action"]:
        return False
    try:
        response = json.loads(row[1])
    except (TypeError, json.JSONDecodeError):
        return False
    return response.get("status") == "ok" and response.get("data", {}).get("operation_id") == state["operation_id"]


def _handshake(info: dict) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/api/mcp24/handshake",
        headers={"X-Book-Bridge": info["bridge_token"]},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.load(response)


def run_maintenance_helper(operation_id: str, actor_domain: str) -> int:
    from shuxu import mcp24_contract

    store = _maintenance_store()
    state = store.get_internal(actor_domain, operation_id)
    if state is None or state["status"] != "stopping":
        return 2
    store.update(actor_domain, operation_id, status="starting", helper_pid=os.getpid())
    old = state["old_instance"]
    deadline = time.monotonic() + max(30, state["timeout_seconds"] + 15)
    while process_is_alive(int(old["pid"])) and time.monotonic() < deadline:
        time.sleep(0.1)
    if process_is_alive(int(old["pid"])):
        _maintenance_failure(store, actor_domain, operation_id, "old_instance_exit_timeout", "旧服务进程未能在等待期限内退出；维护操作可重试。")
        return 2
    try:
        os.environ["SHUXU_SERVICE_RESTART_OPERATION_ID"] = operation_id
        info = ensure_server()
        state = store.update(
            actor_domain, operation_id, status="verifying",
            new_instance={"instance_id": info["instance_id"], "pid": info["pid"],
                          "service_version": info.get("service_version")},
        )
        handshake = _handshake(info)
        data = handshake.get("data", {})
        from shuxu import mcp24_contract as contract
        healthy = verify_runtime(info)
        matches = (
            healthy
            and info["instance_id"] != old["instance_id"]
            and info.get("restart_operation_id") == operation_id
            and data.get("instance_id") == info["instance_id"]
            and data.get("service_version") == contract.SERVICE_VERSION
            and info.get("service_version") == data.get("service_version")
            and info.get("service_version") == contract.SERVICE_VERSION
        )
        if not matches:
            raise RuntimeError("new service health, handshake, instance ID, or service version did not match")
        if not _receipt_persisted(state):
            _maintenance_failure(store, actor_domain, operation_id, "request_receipt_missing", "新实例已健康启动，但维护请求的业务回执未能从主数据库读回；可用相同 operation_id 重试。")
            return 2
        completion = {
            "operation_id": operation_id, "status": "completed",
            "request_id": state["request_id"], "request_action": state["request_action"],
            "business_receipt_persisted": True, "health_verified": True, "handshake_verified": True,
            "old_instance": {"instance_id": old["instance_id"], "service_version": old["service_version"]},
            "new_instance": {"instance_id": info["instance_id"], "service_version": data["service_version"]},
            "completed_at": utc_now(),
        }
        attempts = state.get("attempts", [])
        if attempts:
            attempts[-1]["status"] = "completed"
        store.update(
            actor_domain, operation_id, status="completed", retryable=False, error=None,
            new_instance=completion["new_instance"], completion_receipt=completion, attempts=attempts,
        )
        return 0
    except (OSError, RuntimeError, ValueError, urllib.error.URLError, json.JSONDecodeError) as error:
        _maintenance_failure(
            store, actor_domain, operation_id, "restart_verification_failed",
            f"重启或实例核验失败（{type(error).__name__}）；操作可用相同 operation_id 重试。",
        )
        return 2


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def route_tag(self) -> str:
        path = urllib.parse.urlsplit(self.path).path
        fixed_routes = {"/api/session", "/api/status", "/api/logs", "/api/health", "/api/shutdown", "/api/mcp24/handshake"}
        if path in fixed_routes:
            return "api." + path.rsplit("/", 1)[1]
        parts = path.split("/")
        if len(parts) == 3 and parts[1] == "api" and parts[2] in ACTION_NAMES:
            return "api.action"
        if path.startswith("/api/"):
            return "api.unknown"
        return "web"

    def log_message(self, format: str, *args) -> None:
        method = self.command if self.command in {"GET", "POST", "HEAD", "OPTIONS"} else "OTHER"
        status = str(getattr(self, "_response_status", "unknown"))
        record("INFO", f"{method} {self.route_tag()} {status}")

    def send_response(self, code: int, message: str | None = None) -> None:
        self._response_status = int(code)
        super().send_response(code, message)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._response_status = int(code)
        super().send_error(code, message, explain)

    def send_json(self, status: HTTPStatus, payload: dict, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status: HTTPStatus, code: str, message: str) -> None:
        self.send_json(status, {"status": code, "data": None, "error": {"code": code, "message": message}})

    def local_host(self) -> bool:
        expected = f"127.0.0.1:{self.server.server_address[1]}"
        return self.client_address[0] == "127.0.0.1" and self.headers.get("Host") == expected

    def same_origin(self) -> bool:
        if not self.local_host():
            return False
        expected = f"http://127.0.0.1:{self.server.server_address[1]}"
        origin = self.headers.get("Origin")
        if origin is not None:
            return origin == expected
        return self.headers.get("Sec-Fetch-Site") == "same-origin"

    def is_bridge(self) -> bool:
        supplied = self.headers.get("X-Book-Bridge", "")
        return self.local_host() and bool(BRIDGE_TOKEN) and secrets.compare_digest(supplied, BRIDGE_TOKEN)

    def authorize_action(self) -> str | None:
        context = self.authorization_context()
        return context[0] if context else None

    def authorization_context(self) -> tuple[str, dict] | None:
        from shuxu import mcp24_access

        host_id = self.headers.get("X-Shuxu-Host-ID")
        authorization = self.headers.get("Authorization", "")
        if host_id is not None or authorization:
            scheme, separator, bearer = authorization.partition(" ")
            if scheme != "Bearer" or not separator or not bearer:
                return None
            try:
                actor = mcp24_access.authenticate(BOOK_SERVICE, host_id, bearer)
            except mcp24_access.AccessError:
                return None
            return "mcp", actor
        if self.is_bridge():
            return "mcp", mcp24_access.legacy_actor()
        if self.same_origin() and secrets.compare_digest(self.headers.get("X-Book-Session", ""), SESSION_TOKEN):
            return "local", mcp24_access.local_actor()
        return None

    def do_GET(self) -> None:
        global MCP_LAST_REQUEST_AT
        parsed = urllib.parse.urlsplit(self.path)
        if not self.local_host():
            self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden", "Local access required.")
            return
        if parsed.path == "/api/health":
            if not self.is_bridge():
                self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden", "Service identity required.")
                return
            self.send_json(HTTPStatus.OK, {"status": "ok", "data": {"service": "running", "instance_id": INSTANCE_ID}})
            return
        if parsed.path == "/api/mcp24/handshake":
            context = self.authorization_context()
            if context is None:
                self.send_error_json(HTTPStatus.FORBIDDEN, "unauthorized", "Valid host credentials are required.")
                return
            expected_instance = self.headers.get("X-Shuxu-Expected-Instance-ID")
            if expected_instance and not secrets.compare_digest(expected_instance, INSTANCE_ID):
                self.send_error_json(HTTPStatus.CONFLICT, "instance_changed", "The service instance changed; reconnect and inspect its directory.")
                return
            from shuxu import mcp24_contract
            from shuxu import mcp24_ops

            _, actor = context
            catalog = mcp24_contract.filter_tools(actor, TOOL_SPECS)
            directory_id = mcp24_contract.directory_id(actor, TOOL_SPECS)
            directory_etag = mcp24_ops.scope_etag(actor, "mcp24-directory", directory_id, directory_id)
            if mcp24_ops.is_not_modified(self.headers.get("If-None-Match"), directory_etag):
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", directory_etag)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            envelope = {
                "protocol_version": mcp24_contract.PROTOCOL_VERSION,
                "service_version": mcp24_contract.SERVICE_VERSION,
                "instance_id": INSTANCE_ID,
                "directory_id": directory_id,
                "authorization_revision": actor["authorization_revision"],
                "directory_etag": directory_etag,
            }
            self.send_json(HTTPStatus.OK, {
                "status": "ok", "data": {**envelope, "tool_directory": catalog}, "error": None, "envelope": envelope,
            }, {"ETag": directory_etag})
            return
        if parsed.path == "/api/session":
            if not self.same_origin():
                self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden", "Same-origin access required.")
                return
            self.send_json(HTTPStatus.OK, {"status": "ok", "data": {"token": SESSION_TOKEN}})
            return
        if parsed.path == "/api/status":
            if not self.same_origin():
                self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden", "Same-origin access required.")
                return
            self.send_json(HTTPStatus.OK, {"status": "ok", "data": {"service": "running", "instance_id": INSTANCE_ID, "started_at": STARTED_AT, "database": "ready", "mcp_last_request_at": MCP_LAST_REQUEST_AT}})
            return
        if parsed.path == "/api/logs":
            if not self.same_origin():
                self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden", "Same-origin access required.")
                return
            try:
                limit = int(urllib.parse.parse_qs(parsed.query).get("limit", ["50"])[0])
            except ValueError:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid_params", "limit must be an integer.")
                return
            with LOG_LOCK:
                items = list(LOGS)[-max(1, min(limit, 100)):][::-1]
            self.send_json(HTTPStatus.OK, {"status": "ok", "data": {"items": items}})
            return
        if parsed.path.startswith("/api/"):
            self.send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Route not found.")
            return
        if not WEB_DIR.is_dir():
            self.send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "web_unavailable", "The local web files are not ready.")
            return
        relative = urllib.parse.unquote(parsed.path.lstrip("/")) or "index.html"
        target = (WEB_DIR / relative).resolve()
        if not target.is_relative_to(WEB_DIR.resolve()) or not target.is_file():
            self.send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Page not found.")
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"} else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        global MCP_LAST_REQUEST_AT
        parsed = urllib.parse.urlsplit(self.path)
        if not self.local_host():
            self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden", "Local access required.")
            return
        if parsed.path == "/api/shutdown":
            if not self.is_bridge():
                self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden", "Service identity required.")
                return
            self.send_json(HTTPStatus.OK, {"status": "ok", "data": {"instance_id": INSTANCE_ID}, "error": None})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        parts = parsed.path.split("/")
        if len(parts) != 3 or parts[1] != "api" or parts[2] not in ACTION_NAMES:
            self.send_error_json(HTTPStatus.NOT_FOUND, "not_found", "Action not found.")
            return
        authentication_started = time.perf_counter()
        context = self.authorization_context()
        authentication_ms = (time.perf_counter() - authentication_started) * 1000
        if context is None:
            self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden", "A valid local session is required.")
            return
        channel, actor = context
        expected_instance = self.headers.get("X-Shuxu-Expected-Instance-ID")
        if expected_instance and not secrets.compare_digest(expected_instance, INSTANCE_ID):
            self.send_error_json(HTTPStatus.CONFLICT, "instance_changed", "The service instance changed; reconnect and inspect its directory.")
            return
        action = parts[2]
        if action in LOCAL_ACTIONS and channel != "local":
            self.send_error_json(HTTPStatus.FORBIDDEN, "forbidden", "此操作仅供本机网页使用。")
            return
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self.send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "invalid_params", "Content-Type must be application/json.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid_params", "Content-Length must be an integer.")
            return
        limit = 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 32_768 if action in {"save_inspiration_image", "save_chat_screenshot"} else 1_000_000
        if length < 0 or length > limit:
            self.close_connection = True
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "invalid_params", "Request body is too large.")
            return
        try:
            params = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid_params", "Request body must be valid JSON.")
            return
        if not isinstance(params, dict):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid_params", "Request body must be a JSON object.")
            return
        upstream_request_id = self.headers.get("X-Shuxu-Request-ID")
        if upstream_request_id is not None:
            if "request_id" not in params or params["request_id"] != upstream_request_id:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid_params", "Header and body request IDs must match.")
                return
        upstream_message_id = self.headers.get("X-Shuxu-Upstream-Message-ID")
        operation_slot = self.headers.get("X-Shuxu-Operation-Slot")
        if upstream_message_id is not None and (not upstream_message_id or len(upstream_message_id) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in upstream_message_id)):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid_params", "Upstream message ID is invalid.")
            return
        if operation_slot is not None and (not upstream_message_id or not operation_slot or len(operation_slot) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in operation_slot)):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "invalid_params", "Operation slot is invalid.")
            return
        trace_request_id = params.get("request_id") or upstream_message_id
        if action in {"get_request_status", "get_request_diagnostics"}:
            trace_request_id = None
        runtime = getattr(BOOK_SERVICE, "_mcp24_ops_runtime", None)
        if trace_request_id and runtime is not None:
            from shuxu import mcp24_access, mcp24_ops

            trace_id = trace_request_id if actor["host_id"] in {mcp24_access.LEGACY_HOST_ID, mcp24_access.LOCAL_HOST_ID} else mcp24_ops.namespaced_request_id(actor, trace_request_id)
            runtime.traces.record(trace_id, "authentication", action, actor, authentication_ms)
        if channel == "mcp":
            MCP_LAST_REQUEST_AT = utc_now()
        try:
            result = BOOK_SERVICE.dispatch(
                action, params, channel=channel, actor=actor,
                upstream_message_id=upstream_message_id, operation_slot=operation_slot,
            )
        except Exception as exc:
            record("ERROR", f"{action} failed: {type(exc).__name__}")
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "The request could not be completed.")
            return
        record("INFO", f"{channel} action {action}: {result.get('status', 'unknown')}")
        from shuxu import mcp24_contract
        from shuxu import mcp24_ops

        envelope = dict(result.get("envelope") or {})
        envelope.update({
            "protocol_version": mcp24_contract.PROTOCOL_VERSION,
            "service_version": mcp24_contract.SERVICE_VERSION,
            "instance_id": INSTANCE_ID,
            "directory_id": mcp24_contract.directory_id(actor, TOOL_SPECS),
            "authorization_revision": actor["authorization_revision"],
        })
        etag = None
        if result.get("status") == "ok" and action in {"get_preferences", "get_preference_reason", "get_thought_context", "search_indexed_library", "list_prompts"}:
            with closing(BOOK_SERVICE._connect()) as version_db:
                preference_version = version_db.execute("SELECT version FROM preference_meta WHERE singleton = 1").fetchone()[0]
                index_revision = version_db.execute("SELECT revision FROM mcp_read_state WHERE singleton = 1").fetchone()[0]
            version = {"preferences": preference_version, "library": index_revision}
            variant = hashlib.sha256(json.dumps(
                {"action": action, "params": params, "result_sha256": hashlib.sha256(json.dumps(result.get("data"), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()},
                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            etag = mcp24_ops.scope_etag(actor, "mcp24:" + action, version, variant)
            envelope["etag"] = etag
        result["envelope"] = envelope
        if etag and mcp24_ops.is_not_modified(self.headers.get("If-None-Match"), etag):
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        restart_data = result.get("data") if action in {"restart_service", "retry_service_restart"} else None
        try:
            self.send_json(HTTPStatus.OK, result, {"ETag": etag} if etag else None)
        finally:
            if isinstance(restart_data, dict) and restart_data.get("status") == "accepted":
                start_service_restart(BOOK_SERVICE, restart_data["operation_id"], mcp24_ops._actor_domain(actor))


def run_server() -> int:
    global BOOK_SERVICE, BRIDGE_TOKEN, INSTANCE_ID, MCP_LAST_REQUEST_AT, SERVER, SESSION_TOKEN, STARTED_AT
    info = verified_runtime()
    if info is not None:
        return 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server = LocalServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        if wait_for_verified_service() is not None:
            return 0
        raise RuntimeError(f"Port {PORT} is occupied by a service whose identity cannot be verified.") from exc
    SERVER = server
    BOOK_SERVICE = BookService(DATA_DIR / "shuxu.sqlite3")
    INSTANCE_ID = secrets.token_hex(16)
    BRIDGE_TOKEN = secrets.token_urlsafe(32)
    SESSION_TOKEN = secrets.token_urlsafe(32)
    STARTED_AT = utc_now()
    MCP_LAST_REQUEST_AT = None
    from shuxu import mcp24_contract

    restart_operation_id = os.environ.get("SHUXU_SERVICE_RESTART_OPERATION_ID")
    BOOK_SERVICE._service_restart_context = {
        "pid": os.getpid(), "port": server.server_address[1], "instance_id": INSTANCE_ID,
        "service_version": mcp24_contract.SERVICE_VERSION,
    }
    info = {
        "pid": os.getpid(), "port": server.server_address[1], "instance_id": INSTANCE_ID,
        "bridge_token": BRIDGE_TOKEN, "started_at": STARTED_AT,
        "service_version": mcp24_contract.SERVICE_VERSION,
        "restart_operation_id": restart_operation_id,
    }
    write_runtime(info)
    record("INFO", "Local service ready.")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        remove_our_runtime(INSTANCE_ID)
        record("INFO", "Local service stopped.")
    return 0


def stop_server() -> int:
    info = verified_runtime()
    if info is None:
        print("Book Sequence service is not running.")
        return 0
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/api/shutdown",
        data=b"{}",
        headers={"Content-Type": "application/json", "X-Book-Bridge": info["bridge_token"]},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        payload = json.load(response)
    if payload.get("data", {}).get("instance_id") != info["instance_id"]:
        raise RuntimeError("The stop request reached an unexpected service instance.")
    print("Book Sequence stop requested.")
    return 0


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Local Book Sequence service.")
    parser.add_argument("command", choices=("start", "serve", "stop", "maintenance-helper"))
    parser.add_argument("--operation-id")
    parser.add_argument("--actor-domain")
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "start":
            info = ensure_server(open_page=args.open_browser)
            print(f"Book Sequence is available at http://127.0.0.1:{info['port']}/")
            return 0
        if args.command == "stop":
            return stop_server()
        if args.command == "maintenance-helper":
            if not args.operation_id or not args.actor_domain:
                raise RuntimeError("The maintenance helper requires an operation ID and actor domain.")
            return run_maintenance_helper(args.operation_id, args.actor_domain)
        if args.open_browser:
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        return run_server()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Book Sequence: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Shared scheduling, durable MCP operations, scoped validators and cleanup."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import mcp_workflows, privacy_guard


class MCP24OpsError(mcp_workflows.WorkflowError):
    pass


_JOB_ACTIONS = frozenset({"create_backup", "export_thought_bundle", "apply_actions"})
_SERVICE_MAINTENANCE_ACTIONS = frozenset({"restart_service", "get_service_restart", "retry_service_restart"})
_SERVICE_MAINTENANCE_ACTIVE = frozenset({"accepted", "draining", "stopping", "starting", "verifying"})
_CLEANUP_CATEGORIES = (
    "temporary_references", "selection_history", "undo_records", "write_receipts", "diagnostics",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | str | None = None) -> str:
    if value is None:
        value = _utc_now()
    elif isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _actor_domain(actor: Mapping[str, Any]) -> str:
    identity = {
        "host_id": actor["host_id"],
        "profile": actor["profile"],
        "scopes": sorted(actor["scopes"]),
        "authorization_revision": actor["authorization_revision"],
    }
    return _digest(identity)


def host_namespace(actor: Mapping[str, Any]) -> str:
    """Return access's public host/profile prefix for scoped cleanup filtering."""
    from .mcp24_access import host_namespace as access_host_namespace

    return access_host_namespace(actor)


def namespaced_request_id(actor: Mapping[str, Any], external_id: str) -> str:
    from .mcp24_access import namespace_id

    return namespace_id(actor, "request", external_id)


def _actor_scoped_id(actor: Mapping[str, Any], kind: str, external_id: str) -> str:
    if _uses_legacy_ids(actor):
        return external_id
    from .mcp24_access import namespace_id

    return namespace_id(actor, kind, external_id)


def scope_etag(actor: Mapping[str, Any], resource: str, version: Any, variant: Any = None) -> str:
    return '"' + _digest({
        "actor_domain": _actor_domain(actor), "resource": resource,
        "version": version, "variant": variant,
    }) + '"'


def is_not_modified(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    wanted = etag[2:] if etag.startswith("W/") else etag
    for tag in if_none_match.split(","):
        candidate = tag.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == wanted:
            return True
    return False


class _Gate:
    """A fair shared gate for ordinary SQLite work and exclusive restores."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def shared(self):
        with self._condition:
            while self._writer or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if not self._readers:
                    self._condition.notify_all()

    @contextmanager
    def exclusive(self):
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._condition.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1
        try:
            yield
        finally:
            with self._condition:
                self._writer = False
                self._condition.notify_all()


class SQLiteServiceScheduler:
    """Run regular requests concurrently; drain writes before service maintenance."""

    def __init__(self, book) -> None:
        self.book = book
        self._gate = _Gate()
        self._admission = threading.Condition()
        self._maintenance_state = "accepting"
        self._maintenance_operation_id: str | None = None
        self._active_dispatches = 0
        self._active_writes = 0
        self._active_actions: dict[str, int] = {}

    def dispatch(self, action: str, params: dict[str, Any], *, channel: str = "local", actor: Mapping[str, Any] | None = None, request_id: str | None = None, upstream_message_id: str | None = None, operation_slot: str | None = None) -> dict[str, Any]:
        from .book import _WRITE_ACTIONS, _error

        is_write = action in _WRITE_ACTIONS
        job = current_job_context()
        with self._admission:
            if action not in _SERVICE_MAINTENANCE_ACTIONS:
                rejected = self._maintenance_state == "stopping" or (
                    self._maintenance_state == "draining" and is_write and job is None
                )
                if rejected:
                    return _error(
                        "maintenance_in_progress", "书序正在排空并重启，暂不接受新的业务调用。",
                        {"operation_id": self._maintenance_operation_id, "dispatched": False,
                         "retry_with_same_request_id": True},
                    )
                self._active_dispatches += 1
                self._active_actions[action] = self._active_actions.get(action, 0) + 1
                if is_write:
                    self._active_writes += 1
        gate = self._gate.exclusive if action == "restore_backup" else self._gate.shared
        persistent = not getattr(self.book, "_uri", False) or getattr(self.book, "_mcp24_ops_path", None) is not None
        tracer = runtime(self.book).traces if persistent and actor is not None and request_id is not None else None
        try:
            with tracer.span(request_id, "scheduler", action, actor) if tracer else nullcontext() as scheduler_span:
                with gate():
                    with tracer.span(request_id, "storage", action, actor) if tracer else nullcontext() as storage_span:
                        try:
                            dispatch_kwargs = {"actor": actor}
                            if upstream_message_id is not None:
                                dispatch_kwargs["upstream_message_id"] = upstream_message_id
                            if operation_slot is not None:
                                dispatch_kwargs["operation_slot"] = operation_slot
                            result = self.book._dispatch(action, params, channel, **dispatch_kwargs)
                            if action == "apply_retention_cleanup":
                                if result.get("status") == "ok":
                                    cleaner = runtime(self.book).cleanup
                                    result["data"] = cleaner.finalize_apply(
                                        actor, params["plan_id"], params["request_id"], result["data"]
                                    )
                                elif getattr(self.book, "_mcp24_ops_runtime", None) is not None:
                                    apply_request_id = params.get("request_id")
                                    if isinstance(apply_request_id, str):
                                        self.book._mcp24_ops_runtime.cleanup.abort_apply(actor, apply_request_id)
                        except BaseException:
                            if action == "apply_retention_cleanup":
                                ops_runtime = getattr(self.book, "_mcp24_ops_runtime", None)
                                apply_request_id = params.get("request_id")
                                if ops_runtime is not None and isinstance(apply_request_id, str):
                                    ops_runtime.cleanup.abort_apply(actor, apply_request_id)
                            raise
                        if result.get("status") != "ok" and tracer:
                            storage_span.set_result(result["status"], _safe_error_code(result.get("error", {})))
                    if result.get("status") != "ok" and tracer:
                        scheduler_span.set_result(result["status"], _safe_error_code(result.get("error", {})))
                    return result
        finally:
            if action not in _SERVICE_MAINTENANCE_ACTIONS:
                with self._admission:
                    self._active_dispatches -= 1
                    self._active_actions[action] -= 1
                    if not self._active_actions[action]:
                        del self._active_actions[action]
                    if is_write:
                        self._active_writes -= 1
                    self._admission.notify_all()

    def begin_maintenance(self, operation_id: str, create_operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._admission:
            if self._maintenance_state != "accepting":
                raise MCP24OpsError(
                    "maintenance_in_progress", "已有书序维护操作正在运行。",
                    {"operation_id": self._maintenance_operation_id, "status": self._maintenance_state},
                )
            self._maintenance_state = "draining"
            self._maintenance_operation_id = operation_id
            try:
                return create_operation()
            except BaseException:
                self._maintenance_state = "accepting"
                self._maintenance_operation_id = None
                self._admission.notify_all()
                raise

    def set_maintenance_state(self, operation_id: str, state: str) -> None:
        with self._admission:
            if self._maintenance_operation_id == operation_id:
                self._maintenance_state = state
                self._admission.notify_all()

    def finish_maintenance(self, operation_id: str) -> None:
        with self._admission:
            if self._maintenance_operation_id == operation_id:
                self._maintenance_state = "accepting"
                self._maintenance_operation_id = None
                self._admission.notify_all()

    def maintenance_snapshot(self) -> dict[str, Any]:
        with self._admission:
            return {
                "state": self._maintenance_state,
                "operation_id": self._maintenance_operation_id,
                "active_writes": self._active_writes,
                "active_dispatches": self._active_dispatches,
                "active_actions": [
                    {"action": action, "count": count}
                    for action, count in sorted(self._active_actions.items())
                ],
            }

    def wait_for_dispatches(self, deadline: float) -> bool:
        with self._admission:
            while self._active_dispatches:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._admission.wait(min(remaining, 0.1))
            return True


class JobCancelled(Exception):
    pass


class JobContext:
    def __init__(self, ledger: "OpsLedger", actor_domain: str, job_id: str) -> None:
        self.ledger = ledger
        self.actor_domain = actor_domain
        self.job_id = job_id

    def check_cancelled(self) -> None:
        with closing(self.ledger._connect()) as db:
            row = db.execute(
                "SELECT status, cancel_requested FROM mcp24_async_jobs WHERE job_id = ? AND actor_domain = ?",
                (self.job_id, self.actor_domain),
            ).fetchone()
            if row["cancel_requested"] or row["status"] == "cancelled":
                with closing(self.ledger._connect()) as update:
                    update.execute("BEGIN IMMEDIATE")
                    update.execute(
                        "UPDATE mcp24_async_jobs SET status = 'cancelled', updated_at = ?, finished_at = ? "
                        "WHERE job_id = ? AND actor_domain = ? AND status = 'running'",
                        (_iso(), _iso(), self.job_id, self.actor_domain),
                    )
                    update.commit()
                raise JobCancelled

    def begin_commit(self) -> None:
        with closing(self.ledger._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, cancel_requested FROM mcp24_async_jobs WHERE job_id = ? AND actor_domain = ?",
                (self.job_id, self.actor_domain),
            ).fetchone()
            if row["status"] == "cancelled" or row["cancel_requested"]:
                db.execute(
                    "UPDATE mcp24_async_jobs SET status = 'cancelled', updated_at = ?, finished_at = ? "
                    "WHERE job_id = ? AND actor_domain = ? AND status = 'running'",
                    (_iso(), _iso(), self.job_id, self.actor_domain),
                )
                db.commit()
                raise JobCancelled
            if row["status"] != "running":
                db.rollback()
                raise MCP24OpsError("invalid_state", "任务不在可提交状态。")
            db.execute(
                "UPDATE mcp24_async_jobs SET status = 'committing', updated_at = ? "
                "WHERE job_id = ? AND actor_domain = ?",
                (_iso(), self.job_id, self.actor_domain),
            )
            db.commit()


_CURRENT_JOB_CONTEXT: ContextVar[JobContext | None] = ContextVar("shuxu_mcp24_job_context", default=None)


def current_job_context() -> JobContext | None:
    return _CURRENT_JOB_CONTEXT.get()


class OpsLedger:
    """A restore-independent sidecar for operation state and trace spans."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 5000")
        return db

    def _initialize(self) -> None:
        with closing(self._connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS mcp24_async_jobs (
                    job_id TEXT PRIMARY KEY,
                    actor_domain TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    operation_request_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    arguments_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    result_pruned INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    error_type TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    UNIQUE(actor_domain, request_id)
                );
                CREATE INDEX IF NOT EXISTS mcp24_async_jobs_status
                    ON mcp24_async_jobs(actor_domain, status, updated_at);
                CREATE TABLE IF NOT EXISTS mcp24_request_spans (
                    span_id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    actor_domain TEXT NOT NULL,
                    action TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    error_type TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS mcp24_request_spans_lookup
                    ON mcp24_request_spans(actor_domain, request_id, created_at);
                CREATE TABLE IF NOT EXISTS mcp24_cleanup_plans (
                    plan_id TEXT PRIMARY KEY,
                    actor_domain TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    applied_at TEXT,
                    apply_request_id TEXT,
                    result_json TEXT
                );
                CREATE INDEX IF NOT EXISTS mcp24_cleanup_plans_actor
                    ON mcp24_cleanup_plans(actor_domain, created_at);
            """)
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE mcp24_async_jobs SET status = 'interrupted', error_code = 'interrupted', "
                "error_type = NULL, updated_at = ? WHERE status IN ('running', 'committing')",
                (_iso(),),
            )
            db.commit()

    def submit(
        self,
        actor: Mapping[str, Any],
        action: str,
        arguments: Mapping[str, Any],
        request_id: str,
        runner: Callable[[str, dict[str, Any], str, JobContext], Any],
    ) -> dict[str, Any]:
        if action not in _JOB_ACTIONS:
            raise MCP24OpsError("invalid_action", "该操作不在异步任务白名单中。")
        actor_domain = _actor_domain(actor)
        normalized = dict(arguments)
        normalized.pop("request_id", None)
        body_hash = _digest({"action": action, "arguments": normalized})
        operation_request_id = "job-" + str(uuid.uuid4())
        now = _iso()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM mcp24_async_jobs WHERE actor_domain = ? AND request_id = ?",
                (actor_domain, request_id),
            ).fetchone()
            if prior is not None:
                if prior["action"] != action or prior["arguments_hash"] != body_hash:
                    db.rollback()
                    raise MCP24OpsError("idempotency_conflict", "request_id 已用于不同任务。")
                db.commit()
                return self._public(prior)
            job_id = str(uuid.uuid4())
            db.execute(
                """INSERT INTO mcp24_async_jobs
                   (job_id, actor_domain, request_id, operation_request_id, action, arguments_json,
                    arguments_hash, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)""",
                (job_id, actor_domain, request_id, operation_request_id, action, _json(normalized), body_hash, now, now),
            )
            db.commit()
        self._start_worker(actor_domain, job_id, runner)
        return self.get(actor, job_id)

    def get(self, actor: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM mcp24_async_jobs WHERE job_id = ? AND actor_domain = ?",
                (job_id, _actor_domain(actor)),
            ).fetchone()
        if row is None:
            raise MCP24OpsError("not_found", "未找到可访问的异步任务。")
        return self._public(row)

    def cancel(self, actor: Mapping[str, Any], job_id: str) -> dict[str, Any]:
        domain = _actor_domain(actor)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status, cancel_requested FROM mcp24_async_jobs WHERE job_id = ? AND actor_domain = ?",
                (job_id, domain),
            ).fetchone()
            if row is None:
                db.rollback()
                raise MCP24OpsError("not_found", "未找到可访问的异步任务。")
            if row["status"] == "queued":
                db.execute(
                    "UPDATE mcp24_async_jobs SET status = 'cancelled', cancel_requested = 1, updated_at = ?, finished_at = ? "
                    "WHERE job_id = ? AND actor_domain = ?",
                    (_iso(), _iso(), job_id, domain),
                )
                status = "cancelled"
            elif row["status"] == "running":
                db.execute(
                    "UPDATE mcp24_async_jobs SET cancel_requested = 1, updated_at = ? "
                    "WHERE job_id = ? AND actor_domain = ? AND status = 'running'",
                    (_iso(), job_id, domain),
                )
                status = "cancel_requested"
            elif row["status"] == "committing":
                status = "cannot_cancel_committing"
            else:
                status = "not_cancellable"
            db.commit()
        return {"job_id": job_id, "status": status}

    def resume_explicit(
        self,
        actor: Mapping[str, Any],
        job_id: str,
        runner: Callable[[str, dict[str, Any], str, JobContext], Any],
    ) -> dict[str, Any]:
        domain = _actor_domain(actor)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status FROM mcp24_async_jobs WHERE job_id = ? AND actor_domain = ?",
                (job_id, domain),
            ).fetchone()
            if row is None:
                db.rollback()
                raise MCP24OpsError("not_found", "未找到可访问的异步任务。")
            if row["status"] in {"queued", "interrupted"}:
                db.execute(
                    "UPDATE mcp24_async_jobs SET status = 'queued', cancel_requested = 0, error_code = NULL, "
                    "error_type = NULL, updated_at = ? WHERE job_id = ? AND actor_domain = ?",
                    (_iso(), job_id, domain),
                )
                db.commit()
                self._start_worker(domain, job_id, runner)
            else:
                db.commit()
        return self.get(actor, job_id)

    def diagnostics(self, actor: Mapping[str, Any], request_id: str) -> list[dict[str, Any]]:
        actor_domain = _actor_domain(actor)
        scoped_ids = {_actor_scoped_id(actor, "request", request_id)}
        with closing(self._connect()) as db:
            job = db.execute(
                "SELECT operation_request_id FROM mcp24_async_jobs WHERE actor_domain = ? AND request_id = ?",
                (actor_domain, request_id),
            ).fetchone()
            if job is not None:
                scoped_ids.add(_actor_scoped_id(actor, "request", job["operation_request_id"]))
            placeholders = ",".join("?" for _ in scoped_ids)
            rows = db.execute(
                f"""SELECT stage, duration_ms, status, error_code, error_type, created_at
                    FROM mcp24_request_spans WHERE actor_domain = ? AND request_id IN ({placeholders})
                    ORDER BY created_at, span_id""",
                (actor_domain, *sorted(scoped_ids)),
            ).fetchall()
        return [dict(row) for row in rows]

    def protected_jobs(self, actor: Mapping[str, Any]) -> tuple[set[str], set[str]]:
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT request_id, operation_request_id, arguments_json FROM mcp24_async_jobs "
                "WHERE actor_domain = ? AND status IN ('queued', 'running', 'committing', 'interrupted')",
                (_actor_domain(actor),),
            ).fetchall()
        requests, conversations = set(), set()
        scope_id = lambda kind, value: _actor_scoped_id(actor, kind, value)
        for row in rows:
            requests.update((scope_id("request", row["request_id"]), scope_id("request", row["operation_request_id"])))
            arguments = json.loads(row["arguments_json"])

            def protect_arguments(value: Mapping[str, Any]) -> None:
                conversation_id = value.get("conversation_id")
                if isinstance(conversation_id, str) and conversation_id:
                    conversations.add(scope_id("conversation", conversation_id))
                for key in ("request_id", "target_request_id"):
                    request_id = value.get(key)
                    if isinstance(request_id, str) and request_id:
                        requests.add(scope_id("request", request_id))

            protect_arguments(arguments)
            for step in arguments.get("actions", []):
                protect_arguments(step.get("arguments", {}))
        return requests, conversations

    def _start_worker(self, actor_domain: str, job_id: str, runner: Callable) -> None:
        thread = threading.Thread(target=self._run, args=(actor_domain, job_id, runner), daemon=True,
                                  name="shuxu-mcp24-" + job_id[:8])
        thread.start()

    def _run(self, actor_domain: str, job_id: str, runner: Callable) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM mcp24_async_jobs WHERE job_id = ? AND actor_domain = ?",
                (job_id, actor_domain),
            ).fetchone()
            if row is None or row["status"] != "queued":
                db.rollback()
                return
            if row["cancel_requested"]:
                db.execute(
                    "UPDATE mcp24_async_jobs SET status = 'cancelled', updated_at = ?, finished_at = ? "
                    "WHERE job_id = ? AND actor_domain = ?",
                    (_iso(), _iso(), job_id, actor_domain),
                )
                db.commit()
                return
            db.execute(
                "UPDATE mcp24_async_jobs SET status = 'running', started_at = ?, updated_at = ? "
                "WHERE job_id = ? AND actor_domain = ? AND status = 'queued'",
                (_iso(), _iso(), job_id, actor_domain),
            )
            db.commit()
            action = row["action"]
            arguments = json.loads(row["arguments_json"])
            operation_request_id = row["operation_request_id"]
        context = JobContext(self, actor_domain, job_id)
        try:
            result = runner(action, arguments, operation_request_id, context)
        except JobCancelled:
            self._finish(actor_domain, job_id, "cancelled", error_code="cancelled")
        except Exception as error:
            self._finish(actor_domain, job_id, "failed", error_code=_safe_error_code(error), error_type=type(error).__name__)
        else:
            status = "completed" if not isinstance(result, dict) or result.get("status", "ok") == "ok" else "failed"
            self._finish(actor_domain, job_id, status, result=result,
                         error_code=None if status == "completed" else _safe_error_code(result.get("error", {})))

    def _finish(self, actor_domain: str, job_id: str, status: str, *, result: Any = None,
                error_code: str | None = None, error_type: str | None = None) -> None:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE mcp24_async_jobs SET status = ?, result_json = ?, error_code = ?, error_type = ?,
                   updated_at = ?, finished_at = ? WHERE job_id = ? AND actor_domain = ?""",
                (status, None if result is None else _json(result), error_code, error_type, _iso(), _iso(), job_id, actor_domain),
            )
            db.commit()

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        result = None if row["result_json"] is None else json.loads(row["result_json"])
        return {
            "job_id": row["job_id"], "request_id": row["request_id"], "action": row["action"],
            "status": row["status"], "result": result, "result_pruned": bool(row["result_pruned"]),
            "error_code": row["error_code"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], "started_at": row["started_at"], "finished_at": row["finished_at"],
        }


def _safe_error_code(error: Any) -> str | None:
    code = error if isinstance(error, str) else error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
    return code if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else None


def _safe_token(value: str | None) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) else None


def _safe_error_type(value: str | None) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value) else None


class _TraceSpan:
    def __init__(self, outcome: dict[str, Any]) -> None:
        self.outcome = outcome

    def set_result(self, status: str, error_code: str | None = None, error_type: str | None = None) -> None:
        self.outcome.update(status=status, error_code=error_code, error_type=error_type)


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _attached_table_exists(db: sqlite3.Connection, alias: str, table: str) -> bool:
    return db.execute(f'SELECT 1 FROM "{alias}".sqlite_master WHERE type = \'table\' AND name = ?', (table,)).fetchone() is not None


def _older_than(value: str | None, cutoff: str) -> bool:
    if value is None:
        return False
    return datetime.fromisoformat(value.replace("Z", "+00:00")) < datetime.fromisoformat(cutoff.replace("Z", "+00:00"))


def _at_or_before(value: str | None, boundary: str) -> bool:
    if value is None:
        return False
    return datetime.fromisoformat(value.replace("Z", "+00:00")) <= datetime.fromisoformat(boundary.replace("Z", "+00:00"))


def _namespace_prefix(actor: Mapping[str, Any] | None) -> str | None:
    return None if actor is None else host_namespace(actor) + ":"


def _uses_legacy_ids(actor: Mapping[str, Any]) -> bool:
    return (actor["host_id"], actor["profile"]) in {
        ("legacy-stdio", "legacy_stdio"), ("local-web", "local_web"),
    }


def _in_scope(value: str | None, actor: Mapping[str, Any] | None, prefix: str | None, *, local_channel: str | None = None) -> bool:
    if value is None:
        return False
    if actor is not None:
        if _uses_legacy_ids(actor):
            return re.match(r"^[0-9a-f]{24}:(?:conversation|request):", value) is None
        return value.startswith(prefix or "")
    if local_channel is not None and local_channel != "local":
        return False
    return re.match(r"^[0-9a-f]{24}:(?:conversation|request):", value) is None


def _digest_row(row: Mapping[str, Any]) -> str:
    return _digest({key: row[key] for key in row.keys()})


def _retention_values(retention_days: int | None, overrides: Mapping[str, int] | None) -> dict[str, int]:
    if overrides is None:
        overrides = {}
    unknown = set(overrides) - set(_CLEANUP_CATEGORIES)
    if unknown:
        raise MCP24OpsError("invalid_params", "包含未定义的保留期限类别。", {"fields": sorted(unknown)})
    values: dict[str, int] = {}
    for key, value in (("retention_days", retention_days), *overrides.items()):
        if value is not None and (type(value) is not int or not 1 <= value <= 36500):
            raise MCP24OpsError("invalid_params", "保留期限必须是 1 到 36500 天的整数。", {"field": key})
    for category in _CLEANUP_CATEGORIES:
        value = overrides.get(category, retention_days)
        if value is not None:
            values[category] = value
    if not values:
        raise MCP24OpsError("invalid_params", "请提供公共保留期限或至少一个类别期限。")
    return values


class CleanupManager:
    """Preview exact, actor-scoped retention work and apply only that snapshot."""

    def __init__(self, book, ledger: OpsLedger) -> None:
        self.book = book
        self.ledger = ledger
        self._pending_applies: dict[tuple[str, str], sqlite3.Connection] = {}
        self._pending_lock = threading.Lock()

    def preview(
        self,
        db: sqlite3.Connection,
        *,
        actor: Mapping[str, Any] | None,
        retention_days: int | None,
        retention_overrides: Mapping[str, int] | None,
        conversation_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        effective = _retention_values(retention_days, retention_overrides)
        actor_domain = _actor_domain(actor) if actor is not None else "legacy-local"
        snapshot = self._snapshot(db, actor, actor_domain, effective, conversation_id, _iso(now))
        plan_id = str(uuid.uuid4())
        fingerprint = _digest(snapshot)
        created_at = _iso(now)
        with closing(self.ledger._connect()) as ledger_db:
            ledger_db.execute(
                """INSERT INTO mcp24_cleanup_plans
                   (plan_id, actor_domain, fingerprint, plan_json, state, created_at)
                   VALUES (?, ?, ?, ?, 'previewed', ?)""",
                (plan_id, actor_domain, fingerprint, _json(snapshot), created_at),
            )
            ledger_db.commit()
        return {"plan_id": plan_id, "fingerprint": fingerprint, "created_at": created_at,
                "retention_days": retention_days, "retention_overrides": dict(retention_overrides or {}),
                "categories": snapshot["categories"]}

    def apply(
        self,
        db: sqlite3.Connection,
        *,
        actor: Mapping[str, Any] | None,
        plan_id: str,
        fingerprint: str,
        confirmed: bool,
        request_id: str,
        now: datetime,
    ) -> dict[str, Any]:
        if confirmed is not True:
            raise MCP24OpsError("needs_clarification", "清理需要用户查看预览后的明确确认。")
        actor_domain = _actor_domain(actor) if actor is not None else "legacy-local"
        ledger_db = self.ledger._connect()
        ledger_db.execute("BEGIN IMMEDIATE")
        plan = ledger_db.execute(
            "SELECT * FROM mcp24_cleanup_plans WHERE plan_id = ? AND actor_domain = ?",
            (plan_id, actor_domain),
        ).fetchone()
        if plan is None or plan["fingerprint"] != fingerprint:
            ledger_db.rollback()
            ledger_db.close()
            raise MCP24OpsError("version_conflict", "清理计划或指纹已变化，请重新预览。")
        if plan["state"] == "applied":
            result = json.loads(plan["result_json"])
            ledger_db.commit()
            ledger_db.close()
            return result
        snapshot = json.loads(plan["plan_json"])
        current = self._snapshot(db, actor, actor_domain, snapshot["effective_days"],
                                 snapshot["conversation_id"], snapshot["evaluated_at"])
        if current["candidates"] != snapshot["candidates"]:
            ledger_db.rollback()
            ledger_db.close()
            raise MCP24OpsError("version_conflict", "预览后的待清理数据已变化，请重新预览。")
        counts = self._apply_business_rows(db, snapshot["candidates"], actor)
        self._merge_counts(counts, self._apply_ledger_rows(ledger_db, snapshot["candidates"]))
        pending_key = (actor_domain, request_id)
        ledger_db.execute(
            "UPDATE mcp24_cleanup_plans SET state = 'applying', apply_request_id = ? WHERE plan_id = ?",
            (request_id, plan_id),
        )
        with self._pending_lock:
            self._pending_applies[pending_key] = ledger_db
        return {"plan_id": plan_id, "fingerprint": fingerprint, "status": "applying",
                "counts": counts, "retained": snapshot["categories"]}

    def finalize_apply(
        self,
        actor: Mapping[str, Any] | None,
        plan_id: str,
        request_id: str,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        actor_domain = _actor_domain(actor) if actor is not None else "legacy-local"
        key = (actor_domain, request_id)
        with self._pending_lock:
            db = self._pending_applies.pop(key, None)
        if db is None:
            db = self.ledger._connect()
            db.execute("BEGIN IMMEDIATE")
            plan = db.execute("SELECT * FROM mcp24_cleanup_plans WHERE plan_id = ? AND actor_domain = ?",
                              (plan_id, actor_domain)).fetchone()
            if plan is None:
                db.rollback()
                db.close()
                raise MCP24OpsError("not_found", "未找到可访问的清理计划。")
            if plan["state"] == "applied":
                prior = json.loads(plan["result_json"])
                db.commit()
                db.close()
                return prior
            self._apply_ledger_rows(db, json.loads(plan["plan_json"])["candidates"])
        applied = {**dict(result), "status": "applied", "applied_at": _iso()}
        db.execute(
            "UPDATE mcp24_cleanup_plans SET state = 'applied', applied_at = ?, result_json = ? WHERE plan_id = ?",
            (applied["applied_at"], _json(applied), plan_id),
        )
        db.commit()
        db.close()
        return applied

    def abort_apply(self, actor: Mapping[str, Any] | None, request_id: str) -> None:
        actor_domain = _actor_domain(actor) if actor is not None else "legacy-local"
        with self._pending_lock:
            db = self._pending_applies.pop((actor_domain, request_id), None)
        if db is not None:
            db.rollback()
            db.close()

    @staticmethod
    def _merge_counts(target: dict[str, dict[str, int]], source: Mapping[str, Mapping[str, int]]) -> None:
        for category, operations in source.items():
            counts = target.setdefault(category, {})
            for operation, value in operations.items():
                counts[operation] = counts.get(operation, 0) + value

    def _snapshot(
        self,
        db: sqlite3.Connection,
        actor: Mapping[str, Any] | None,
        actor_domain: str,
        effective_days: Mapping[str, int],
        conversation_id: str,
        evaluated_at: str,
    ) -> dict[str, Any]:
        prefix = _namespace_prefix(actor)
        now_dt = datetime.fromisoformat(evaluated_at.replace("Z", "+00:00"))
        protected_requests, active_conversations = self.ledger.protected_jobs(actor) if actor is not None else (set(), set())
        active_conversations.add(conversation_id)
        unresolved_requests = self._unresolved_requests(db, actor)
        protected_requests.update(unresolved_requests)
        candidates: list[dict[str, Any]] = []
        categories: dict[str, Any] = {}

        def add(category: str, table: str, key: Any, row_hash: str, operation: str, **extra: Any) -> None:
            candidates.append({"category": category, "table": table, "key": key,
                               "row_sha256": row_hash, "operation": operation, **extra})

        def add_reason(report: dict[str, Any], reason: str) -> None:
            reasons = report.setdefault("protected_reasons", {})
            reasons[reason] = reasons.get(reason, 0) + 1

        for category, days in effective_days.items():
            cutoff = _iso(now_dt - timedelta(days=days))
            categories[category] = {"retention_days": days, "cutoff": cutoff, "evaluated_at": evaluated_at,
                                    "candidate_counts": {}, "protected_reasons": {}}

        # Conversation bindings and pick history remain inside the actor's host/profile namespace.
        if "temporary_references" in effective_days or "selection_history" in effective_days:
            rows: list[sqlite3.Row] = []
            if _table_exists(db, "mcp_conversation_entries"):
                rows = db.execute("SELECT rowid AS __rowid__, * FROM mcp_conversation_entries ORDER BY rowid DESC").fetchall()
            scoped_rows = [row for row in rows if _in_scope(row["conversation_id"], actor, prefix)]
            current_latest: set[str] = set()
            latest_bindings: set[str] = set()
            latest_selections: set[str] = set()
            current_prompt_refs: set[tuple[str, str]] = set()
            seen_conversations: set[str] = set()
            for row in scoped_rows:
                cid = row["conversation_id"]
                if cid not in seen_conversations:
                    current_latest.add(row["decision_id"])
                    seen_conversations.add(cid)
            seen_conversations.clear()
            for row in scoped_rows:
                cid = row["conversation_id"]
                if row["action"] == "bind" and cid not in seen_conversations:
                    latest_bindings.add(row["decision_id"])
                    seen_conversations.add(cid)
            seen_conversations.clear()
            for row in scoped_rows:
                cid = row["conversation_id"]
                if row["action"] in {"pick", "due"} and cid not in seen_conversations:
                    latest_selections.add(row["decision_id"])
                    seen_conversations.add(cid)
            if _table_exists(db, "mcp_thought_progress"):
                current_prompt_refs = {(row["conversation_id"], row["prompt_id"]) for row in db.execute(
                    "SELECT conversation_id, prompt_id FROM mcp_thought_progress")
                    if _in_scope(row["conversation_id"], actor, prefix)}
            for row in scoped_rows:
                cid, action, decision_id = row["conversation_id"], row["action"], row["decision_id"]
                if "temporary_references" in effective_days:
                    if action == "bind" and _older_than(row["created_at"], categories["temporary_references"]["cutoff"]):
                        report = categories["temporary_references"]
                        if cid in active_conversations:
                            add_reason(report, "current_or_active_conversation")
                        elif decision_id in latest_bindings:
                            add_reason(report, "latest_reference")
                        else:
                            add("temporary_references", "mcp_conversation_entries", decision_id,
                                _digest_row(row), "delete_row")
                            counts = report["candidate_counts"]
                            counts["bindings"] = counts.get("bindings", 0) + 1
                if "selection_history" in effective_days and action in {"pick", "due"} and _older_than(
                    row["created_at"], categories["selection_history"]["cutoff"]):
                    report = categories["selection_history"]
                    if cid in active_conversations:
                        add_reason(report, "current_or_active_conversation")
                    elif decision_id in current_latest or decision_id in latest_selections:
                        add_reason(report, "latest_selection_pointer")
                    elif (cid, row["prompt_id"]) in current_prompt_refs:
                        add_reason(report, "thought_progress_pointer")
                    else:
                        add("selection_history", "mcp_conversation_entries", decision_id,
                            _digest_row(row), "delete_row")
                        counts = report["candidate_counts"]
                        counts["picks_and_due"] = counts.get("picks_and_due", 0) + 1

        # Unresolved undo entries are never eligible; completed undo history is safely removable.
        if "undo_records" in effective_days and _table_exists(db, "mcp_undo_log"):
            report = categories["undo_records"]
            for row in db.execute("SELECT * FROM mcp_undo_log"):
                if not _in_scope(row["request_id"], actor, prefix, local_channel=row["channel"]):
                    continue
                if not _older_than(row["created_at"], report["cutoff"]):
                    continue
                if row["conversation_id"] == conversation_id or row["request_id"] in protected_requests:
                    add_reason(report, "current_or_active_request")
                elif row["undone_at"] is None:
                    add_reason(report, "undo_still_available")
                else:
                    add("undo_records", "mcp_undo_log", row["request_id"], _digest_row(row), "delete_row")
                    report["candidate_counts"]["completed_undo"] = report["candidate_counts"].get("completed_undo", 0) + 1

        if "write_receipts" in effective_days:
            report = categories["write_receipts"]
            observations = self._observation_map(db, actor)
            if _table_exists(db, "request_receipts"):
                for row in db.execute("SELECT * FROM request_receipts"):
                    if not _in_scope(row["request_id"], actor, prefix, local_channel=row["channel"]):
                        continue
                    if not _older_than(row["created_at"], report["cutoff"]):
                        continue
                    if row["request_id"] in protected_requests:
                        add_reason(report, "active_or_unresolved_request")
                        continue
                    try:
                        result = json.loads(row["response_json"])
                    except (TypeError, json.JSONDecodeError):
                        add_reason(report, "receipt_unreadable")
                        continue
                    if result.get("receipt_payload_pruned") is True:
                        add_reason(report, "already_pruned")
                        continue
                    if result.get("status") != "ok":
                        add_reason(report, "not_successfully_committed")
                        continue
                    observation = observations.get(row["request_id"])
                    proven = (observation is not None and observation["committed"] == 1)
                    if row["channel"] == "local":
                        proven = proven or result.get("data", {}).get("committed") is True
                    if not proven:
                        add_reason(report, "commit_proof_or_actor_scope_unavailable")
                        continue
                    data = result.get("data") if isinstance(result.get("data"), dict) else {}
                    if data.get("committed") is False or data.get("confirmed") is False:
                        add_reason(report, "not_confirmed")
                        continue
                    add("write_receipts", "request_receipts", row["request_id"], _digest_row(row),
                        "receipt_tombstone", action=row["action"], response_bytes=len(row["response_json"].encode("utf-8")))
                    report["candidate_counts"]["payloads_to_prune"] = report["candidate_counts"].get("payloads_to_prune", 0) + 1
            if actor is not None and _attached_table_exists(db, "mcp24_access", "idempotency"):
                from .mcp24_access import preview_idempotency_tombstones

                cutoff_dt = datetime.fromisoformat(report["cutoff"].replace("Z", "+00:00"))
                for item in preview_idempotency_tombstones(db, actor=actor, cutoff=cutoff_dt):
                    add("write_receipts", "mcp24_access.idempotency", item["row_key_hash"],
                        _digest(item), "access_idempotency_tombstone",
                        action=item["action"], updated_at=item["updated_at"], result_hash=item["result_hash"])
                    report["candidate_counts"]["idempotency_payloads_to_prune"] = report["candidate_counts"].get("idempotency_payloads_to_prune", 0) + 1
            with closing(self.ledger._connect()) as ledger_db:
                for row in ledger_db.execute(
                    """SELECT * FROM mcp24_async_jobs WHERE actor_domain = ? AND status IN ('completed', 'failed', 'cancelled')
                       AND result_json IS NOT NULL AND result_pruned = 0""", (actor_domain,)
                ):
                    if _older_than(row["created_at"], report["cutoff"]):
                        add("write_receipts", "mcp24_async_jobs", row["job_id"], _digest_row(row), "prune_job_result")
                        report["candidate_counts"]["completed_job_results"] = report["candidate_counts"].get("completed_job_results", 0) + 1

        if "temporary_references" in effective_days:
            self._preview_temporary_tables(db, actor, categories["temporary_references"], candidates,
                                           _older_than, _digest_row, add_reason, add)

        if "diagnostics" in effective_days:
            report = categories["diagnostics"]
            if actor is not None:
                with closing(self.ledger._connect()) as ledger_db:
                    for row in ledger_db.execute("SELECT * FROM mcp24_request_spans WHERE actor_domain = ?", (_actor_domain(actor),)):
                        if not _older_than(row["created_at"], report["cutoff"]):
                            continue
                        if row["request_id"] in protected_requests:
                            add_reason(report, "active_or_unresolved_request")
                            continue
                        add("diagnostics", "mcp24_request_spans", row["span_id"], _digest_row(row), "delete_ledger_row")
                        report["candidate_counts"]["spans"] = report["candidate_counts"].get("spans", 0) + 1
            else:
                add_reason(report, "actor_required_for_diagnostics")

        for report in categories.values():
            report["candidate_count"] = sum(report["candidate_counts"].values())
            report["protected_count"] = sum(report["protected_reasons"].values())
        return {
            "actor_domain": actor_domain,
            "effective_days": dict(effective_days),
            "conversation_id": conversation_id,
            "evaluated_at": evaluated_at,
            "categories": categories,
            "candidates": sorted(candidates, key=lambda item: (item["category"], item["table"], _json(item["key"]))),
        }

    @staticmethod
    def _unresolved_requests(db: sqlite3.Connection, actor: Mapping[str, Any] | None) -> set[str]:
        if actor is None or not _table_exists(db, "mcp24_request_observations"):
            return set()
        rows = db.execute(
            """SELECT request_id, state, committed FROM mcp24_request_observations
               WHERE host_id = ? AND profile = ? AND authorization_revision = ?""",
            (actor["host_id"], actor["profile"], actor["authorization_revision"]),
        )
        return {row["request_id"] for row in rows
                if row["state"] in {"running", "unknown"} or row["committed"] != 1}

    @staticmethod
    def _observation_map(db: sqlite3.Connection, actor: Mapping[str, Any] | None) -> dict[str, sqlite3.Row]:
        if actor is None or not _table_exists(db, "mcp24_request_observations"):
            return {}
        rows = db.execute(
            """SELECT request_id, state, committed, protected FROM mcp24_request_observations
               WHERE host_id = ? AND profile = ? AND authorization_revision = ?""",
            (actor["host_id"], actor["profile"], actor["authorization_revision"]),
        )
        return {row["request_id"]: row for row in rows}

    def _preview_temporary_tables(
        self,
        db: sqlite3.Connection,
        actor: Mapping[str, Any] | None,
        report: dict[str, Any],
        candidates: list[dict[str, Any]],
        older_than: Callable[[str | None, str], bool],
        digest_row: Callable[[Mapping[str, Any]], str],
        add_reason: Callable[[dict[str, Any], str], None],
        add: Callable[..., None],
    ) -> None:
        if _table_exists(db, "mcp24_pending_calls"):
            where = "host_id = ? AND profile = ? AND authorization_revision = ?" if actor else "1 = 0"
            values = (actor["host_id"], actor["profile"], actor["authorization_revision"]) if actor else ()
            for row in db.execute(f"SELECT * FROM mcp24_pending_calls WHERE {where}", values):
                expired = _at_or_before(row["expires_at"], report["evaluated_at"])
                if not expired and row["state"] not in {"expired", "consumed"}:
                    add_reason(report, "pending_call_not_expired")
                    continue
                if not (older_than(row["updated_at"], report["cutoff"]) or expired):
                    continue
                add("temporary_references", "mcp24_pending_calls", row["pending_id"], digest_row(row), "expire_pending_payload")
                report["candidate_counts"]["pending_call_payloads"] = report["candidate_counts"].get("pending_call_payloads", 0) + 1

        if _table_exists(db, "mcp24_export_artifact_refs"):
            for row in db.execute("SELECT * FROM mcp24_export_artifact_refs"):
                if actor is None or any(row[key] != actor[value] for key, value in (
                    ("host_id", "host_id"), ("profile", "profile"),
                    ("authorization_revision", "authorization_revision"),
                )):
                    continue
                expired = _at_or_before(row["expires_at"], report["evaluated_at"])
                if expired or older_than(row["created_at"], report["cutoff"]):
                    add("temporary_references", "mcp24_export_artifact_refs", row["artifact_id"], digest_row(row), "delete_row")
                    report["candidate_counts"]["export_references"] = report["candidate_counts"].get("export_references", 0) + 1

        if _table_exists(db, "mcp24_attachment_uploads"):
            uploads = db.execute("SELECT * FROM mcp24_attachment_uploads").fetchall()
            eligible_uploads: set[str] = set()
            for row in uploads:
                if actor is None or any(row[key] != actor[value] for key, value in (
                    ("host_id", "host_id"), ("profile", "profile"),
                    ("authorization_revision", "authorization_revision"),
                )):
                    continue
                expired = _at_or_before(row["expires_at"], report["evaluated_at"])
                old = older_than(row["updated_at"], report["cutoff"])
                complete = row["status"] == "complete"
                if not (expired or old):
                    continue
                if row["status"] == "uploading" and not expired:
                    add_reason(report, "upload_still_active")
                    continue
                eligible_uploads.add(row["upload_id"])
                if complete:
                    report["protected_reasons"]["completed_upload_receipt_kept"] = report["protected_reasons"].get("completed_upload_receipt_kept", 0) + 1
                else:
                    add("temporary_references", "mcp24_attachment_uploads", row["upload_id"], digest_row(row), "expire_upload_payload")
                    report["candidate_counts"]["expired_uploads"] = report["candidate_counts"].get("expired_uploads", 0) + 1
            if eligible_uploads and _table_exists(db, "mcp24_attachment_chunks"):
                placeholders = ",".join("?" for _ in eligible_uploads)
                query = (
                    "SELECT upload_id, part_number, part_sha256, size_bytes, content "
                    f"FROM mcp24_attachment_chunks WHERE upload_id IN ({placeholders})"
                )
                for row in db.execute(query, tuple(sorted(eligible_uploads))):
                    content = row["content"]
                    if content is None:
                        continue
                    chunk_snapshot = {key: row[key] for key in ("upload_id", "part_number", "part_sha256", "size_bytes")}
                    chunk_snapshot["content_sha256"] = hashlib.sha256(content).hexdigest()
                    add("temporary_references", "mcp24_attachment_chunks",
                        {"upload_id": row["upload_id"], "part_number": row["part_number"]},
                        _digest(chunk_snapshot), "clear_chunk_payload", upload_id=row["upload_id"],
                        payload_bytes=len(content))
                    report["candidate_counts"]["attachment_payloads"] = report["candidate_counts"].get("attachment_payloads", 0) + 1

    @staticmethod
    def _apply_business_rows(db: sqlite3.Connection, candidates: list[dict[str, Any]],
                             actor: Mapping[str, Any] | None) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for item in candidates:
            table, operation, key = item["table"], item["operation"], item["key"]
            if table == "mcp24_request_spans":
                continue
            if table == "mcp24_async_jobs":
                continue
            if operation == "access_idempotency_tombstone":
                from .mcp24_access import tombstone_idempotency_by_key

                outcome = tombstone_idempotency_by_key(
                    db, actor=actor, row_key_hash=key,
                    expected_updated_at=item["updated_at"], expected_result_hash=item["result_hash"],
                    now=_utc_now(),
                )
                affected = int(outcome["tombstoned"])
                cursor = None
            elif operation == "delete_row" and table in {"mcp_conversation_entries", "mcp_undo_log", "mcp24_export_artifact_refs"}:
                key_column = {"mcp_conversation_entries": "decision_id", "mcp_undo_log": "request_id",
                              "mcp24_export_artifact_refs": "artifact_id"}[table]
                cursor = db.execute(f'DELETE FROM "{table}" WHERE "{key_column}" = ?', (key,))
            elif operation == "receipt_tombstone":
                tombstone = _json({
                    "status": "receipt_payload_pruned", "receipt_payload_pruned": True,
                    "data": {"request_id": key, "action": item["action"], "committed": True,
                             "payload_pruned": True},
                    "error": {"code": "receipt_payload_pruned",
                              "message": "回执负载已按保留期限裁剪；原写入仍已提交。",
                              "details": {"request_id": key}},
                })
                cursor = db.execute("UPDATE request_receipts SET response_json = ? WHERE request_id = ?",
                                    (tombstone, key))
            elif operation == "expire_pending_payload":
                cursor = db.execute(
                    """UPDATE mcp24_pending_calls SET arguments_json = '{}', missing_fields_json = '[]',
                       state = 'expired', protected = 0, updated_at = ? WHERE pending_id = ?""",
                    (_iso(), key),
                )
            elif operation == "expire_upload_payload":
                cursor = db.execute(
                    "UPDATE mcp24_attachment_uploads SET status = 'cancelled', metadata_json = '{}', "
                    "result_json = ? WHERE upload_id = ? AND status != 'complete'",
                    (_json({"status": "expired", "payload_pruned": True}), key),
                )
            elif operation == "clear_chunk_payload":
                cursor = db.execute(
                    "UPDATE mcp24_attachment_chunks SET content = NULL WHERE upload_id = ? AND part_number = ? AND content IS NOT NULL",
                    (key["upload_id"], key["part_number"]),
                )
            else:
                raise MCP24OpsError("invalid_state", "清理计划包含不支持的变更。")
            category_counts = counts.setdefault(item["category"], {})
            category_counts[operation] = category_counts.get(operation, 0) + (affected if cursor is None else cursor.rowcount)
        return counts

    @staticmethod
    def _apply_ledger_rows(db: sqlite3.Connection, candidates: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for item in candidates:
            if item["table"] == "mcp24_request_spans" and item["operation"] == "delete_ledger_row":
                row = db.execute("SELECT * FROM mcp24_request_spans WHERE span_id = ?", (item["key"],)).fetchone()
                if row is None:
                    continue
                if _digest_row(row) != item["row_sha256"]:
                    raise MCP24OpsError("version_conflict", "诊断回执已变化，请重新预览。")
                cursor = db.execute("DELETE FROM mcp24_request_spans WHERE span_id = ?", (item["key"],))
            elif item["table"] == "mcp24_async_jobs" and item["operation"] == "prune_job_result":
                row = db.execute("SELECT * FROM mcp24_async_jobs WHERE job_id = ?", (item["key"],)).fetchone()
                if row is None or row["result_pruned"]:
                    continue
                if _digest_row(row) != item["row_sha256"]:
                    raise MCP24OpsError("version_conflict", "任务结果已变化，请重新预览。")
                cursor = db.execute("UPDATE mcp24_async_jobs SET result_json = NULL, result_pruned = 1 WHERE job_id = ?", (item["key"],))
            else:
                continue
            category_counts = counts.setdefault(item["category"], {})
            category_counts[item["operation"]] = category_counts.get(item["operation"], 0) + cursor.rowcount
        return counts


class TraceRecorder:
    """Persist request-correlated timings and sanitized error categories only."""

    def __init__(self, ledger: OpsLedger) -> None:
        self.ledger = ledger

    @contextmanager
    def span(self, request_id: str, stage: str, action: str, actor: Mapping[str, Any]):
        start = time.perf_counter()
        outcome = {"status": "ok", "error_code": None, "error_type": None}
        span = _TraceSpan(outcome)
        try:
            yield span
        except Exception as error:
            span.set_result("error", _safe_error_code(error), type(error).__name__)
            raise
        finally:
            with closing(self.ledger._connect()) as db:
                db.execute(
                    """INSERT INTO mcp24_request_spans
                       (span_id, request_id, actor_domain, action, stage, duration_ms, status,
                        error_code, error_type, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(uuid.uuid4()), request_id, _actor_domain(actor), _safe_token(action) or "unknown",
                     _safe_token(stage) or "unknown", round((time.perf_counter() - start) * 1000, 3),
                     _safe_token(outcome["status"]) or "error", _safe_error_code(outcome["error_code"]),
                     _safe_error_type(outcome["error_type"]), _iso()),
                )
                db.commit()

    def record(self, request_id: str, stage: str, action: str, actor: Mapping[str, Any],
               duration_ms: float, *, status: str = "ok", error_code: str | None = None,
               error_type: str | None = None) -> None:
        with closing(self.ledger._connect()) as db:
            db.execute(
                """INSERT INTO mcp24_request_spans
                   (span_id, request_id, actor_domain, action, stage, duration_ms, status,
                    error_code, error_type, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(uuid.uuid4()), request_id, _actor_domain(actor), _safe_token(action) or "unknown",
                 _safe_token(stage) or "unknown", round(duration_ms, 3), _safe_token(status) or "error",
                 _safe_error_code(error_code), _safe_error_type(error_type), _iso()),
            )
            db.commit()


class MCP24OpsRuntime:
    def __init__(self, book, ledger_path: str | Path) -> None:
        self.ledger = OpsLedger(ledger_path)
        self.traces = TraceRecorder(self.ledger)
        self.cleanup = CleanupManager(book, self.ledger)


_RUNTIME_LOCK = threading.Lock()


def runtime(book) -> MCP24OpsRuntime:
    current = getattr(book, "_mcp24_ops_runtime", None)
    if current is not None:
        return current
    with _RUNTIME_LOCK:
        current = getattr(book, "_mcp24_ops_runtime", None)
        if current is None:
            sidecar = getattr(book, "_mcp24_ops_path", None)
            if sidecar is None:
                if getattr(book, "_uri", False):
                    raise MCP24OpsError("not_available", "异步任务需要独立的持久化数据文件。")
                sidecar = Path(book.db_path).expanduser().resolve().parent / "mcp24-operations.sqlite3"
            current = MCP24OpsRuntime(book, sidecar)
            book._mcp24_ops_runtime = current
        return current


def _ledger_path(book) -> Path:
    sidecar = getattr(book, "_mcp24_ops_path", None)
    if sidecar is not None:
        return Path(sidecar).expanduser().resolve()
    if getattr(book, "_uri", False):
        raise MCP24OpsError("not_available", "维护操作需要独立的持久化数据文件。")
    return Path(book.db_path).expanduser().resolve().parent / "mcp24-operations.sqlite3"


class ServiceMaintenanceStore:
    """Persist restart state without initializing the async worker ledger."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS mcp24_service_restarts (
                actor_domain TEXT NOT NULL,
                operation_id TEXT NOT NULL,
                status TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(actor_domain, operation_id)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS mcp24_service_restarts_status ON mcp24_service_restarts(status, updated_at)")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 5000")
        return db

    def get(self, actor: Mapping[str, Any], operation_id: str) -> dict[str, Any]:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT state_json FROM mcp24_service_restarts WHERE actor_domain = ? AND operation_id = ?",
                (_actor_domain(actor), operation_id),
            ).fetchone()
        if row is None:
            raise MCP24OpsError("not_found", "未找到可访问的维护操作。", {"operation_id": operation_id})
        return json.loads(row["state_json"])

    def get_internal(self, actor_domain: str, operation_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT state_json FROM mcp24_service_restarts WHERE actor_domain = ? AND operation_id = ?",
                (actor_domain, operation_id),
            ).fetchone()
        return None if row is None else json.loads(row["state_json"])

    def create(self, actor: Mapping[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        now = _iso()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            exists = db.execute(
                "SELECT 1 FROM mcp24_service_restarts WHERE actor_domain = ? AND operation_id = ?",
                (_actor_domain(actor), state["operation_id"]),
            ).fetchone()
            if exists is not None:
                db.rollback()
                raise MCP24OpsError("operation_id_conflict", "operation_id 已用于其他维护操作。", {
                    "operation_id": state["operation_id"], "retry_with_same_operation_id": True,
                })
            db.execute(
                "INSERT INTO mcp24_service_restarts(actor_domain, operation_id, status, state_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (_actor_domain(actor), state["operation_id"], state["status"], _json(state), now, now),
            )
            db.commit()
        return state

    def prepare_retry(self, actor: Mapping[str, Any], operation_id: str, attempt: dict[str, Any]) -> dict[str, Any]:
        domain = _actor_domain(actor)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state_json FROM mcp24_service_restarts WHERE actor_domain = ? AND operation_id = ?",
                (domain, operation_id),
            ).fetchone()
            if row is None:
                db.rollback()
                raise MCP24OpsError("not_found", "未找到可访问的维护操作。", {"operation_id": operation_id})
            state = json.loads(row["state_json"])
            if state["status"] != "failed" or not state["retryable"]:
                db.rollback()
                raise MCP24OpsError("invalid_state", "当前维护操作不能重试。", {
                    "operation_id": operation_id, "status": state["status"], "retryable": state["retryable"],
                })
            attempt["attempt"] = state["attempt"] + 1
            attempt["status"] = "accepted"
            state["attempt"] = attempt["attempt"]
            state["status"] = "accepted"
            state["retryable"] = False
            state["error"] = None
            state["completion_receipt"] = None
            state["request_id"] = attempt["request_id"]
            state["request_receipt_id"] = attempt["request_receipt_id"]
            state["request_action"] = attempt["request_action"]
            state["timeout_seconds"] = attempt["timeout_seconds"]
            state["old_instance"] = attempt["old_instance"]
            state.setdefault("attempts", []).append(attempt)
            state["active_work"] = {"active_dispatches": 0, "active_writes": 0, "active_actions": [], "jobs": []}
            state["updated_at"] = _iso()
            db.execute(
                "UPDATE mcp24_service_restarts SET status = ?, state_json = ?, updated_at = ? WHERE actor_domain = ? AND operation_id = ?",
                (state["status"], _json(state), state["updated_at"], domain, operation_id),
            )
            db.commit()
        return state

    def update(self, actor_domain: str, operation_id: str, *, status: str | None = None, **changes: Any) -> dict[str, Any]:
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state_json FROM mcp24_service_restarts WHERE actor_domain = ? AND operation_id = ?",
                (actor_domain, operation_id),
            ).fetchone()
            if row is None:
                db.rollback()
                raise MCP24OpsError("not_found", "未找到可访问的维护操作。", {"operation_id": operation_id})
            state = json.loads(row["state_json"])
            state.update(changes)
            if status is not None:
                state["status"] = status
            state["updated_at"] = _iso()
            db.execute(
                "UPDATE mcp24_service_restarts SET status = ?, state_json = ?, updated_at = ? WHERE actor_domain = ? AND operation_id = ?",
                (state["status"], _json(state), state["updated_at"], actor_domain, operation_id),
            )
            db.commit()
        return state

    def active_jobs(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "mcp24_async_jobs" not in tables:
                return []
            return [dict(row) for row in db.execute(
                "SELECT job_id, action, status, updated_at FROM mcp24_async_jobs WHERE status IN ('queued', 'running', 'committing') ORDER BY created_at, job_id"
            )]


def _service_context(book) -> dict[str, Any]:
    context = getattr(book, "_service_restart_context", None)
    if not isinstance(context, dict) or not context.get("instance_id") or not context.get("pid"):
        raise MCP24OpsError("restart_unavailable", "当前 BookService 没有关联的本机服务实例。")
    return context


def _restart_attempt(actor: Mapping[str, Any], request_id: str, request_action: str,
                     operation_id: str, timeout_seconds: int, context: Mapping[str, Any]) -> dict[str, Any]:
    from . import mcp24_contract

    return {
        "attempt": 1,
        "request_id": request_id,
        "request_receipt_id": _actor_scoped_id(actor, "request", request_id),
        "request_action": request_action,
        "timeout_seconds": timeout_seconds,
        "old_instance": {
            "instance_id": context["instance_id"], "pid": context["pid"],
            "port": context["port"], "service_version": context.get("service_version", mcp24_contract.SERVICE_VERSION),
        },
        "accepted_at": _iso(),
    }


def _begin_restart(book, actor: Mapping[str, Any], params: Mapping[str, Any], *, retry: bool) -> dict[str, Any]:
    scheduler: SQLiteServiceScheduler = book._dispatch_scheduler
    context = _service_context(book)
    operation_id = params["operation_id"]
    request_action = "retry_service_restart" if retry else "restart_service"
    timeout_seconds = params.get("timeout_seconds", 120)
    store = ServiceMaintenanceStore(_ledger_path(book))
    if retry:
        prior = store.get(actor, operation_id)
        attempt = _restart_attempt(actor, params["request_id"], request_action, operation_id,
                                   timeout_seconds, context)
        attempt["attempt"] = prior["attempt"] + 1
        def create() -> dict[str, Any]:
            return store.prepare_retry(actor, operation_id, attempt)
    else:
        state = {
            "operation_id": operation_id, "status": "accepted", "attempt": 1,
            "retryable": False, "error": None, "completion_receipt": None,
            "active_work": {"active_dispatches": 0, "active_writes": 0, "active_actions": [], "jobs": []},
            "attempts": [],
        }
        state.update(_restart_attempt(actor, params["request_id"], request_action,
                                      operation_id, timeout_seconds, context))
        state["attempts"].append({key: value for key, value in state.items() if key not in {"attempts", "active_work"}})
        def create() -> dict[str, Any]:
            return store.create(actor, state)
    accepted = scheduler.begin_maintenance(operation_id, create)
    return {
        "operation_id": operation_id, "status": "accepted", "attempt": accepted["attempt"],
        "request_id": params["request_id"], "query_tool": "get_service_restart",
        "retry_with_same_operation_id": True,
    }


def schema(db: sqlite3.Connection) -> None:
    """Operations live in a restore-independent sidecar, not the book database."""


def specs(tool, field, ident, direction, version, include_private):
    retention_days = field("integer", minimum=1, maximum=36500)
    overrides = field("object", properties={name: retention_days for name in _CLEANUP_CATEGORIES},
                      additionalProperties=False)
    operation = field("string", enum=sorted(_JOB_ACTIONS))
    restart_timeout = field("integer", minimum=1, maximum=3600)
    return [
        tool("submit_async_job", "明确提交一个受限的备份、导出或批量操作，返回可查询的任务 ID。", {
            "request_id": ident, "action": operation, "arguments": field("object"),
        }, ("request_id", "action", "arguments")),
        tool("get_async_job", "读取当前宿主提交的异步任务状态和结果。", {"job_id": ident}, ("job_id",)),
        tool("cancel_async_job", "在提交阶段请求取消；已进入提交的任务会明确拒绝取消。", {
            "request_id": ident, "job_id": ident,
        }, ("request_id", "job_id")),
        tool("resume_async_job", "明确继续一个未自动重放的排队或中断任务。", {
            "request_id": ident, "job_id": ident,
        }, ("request_id", "job_id")),
        tool("get_request_diagnostics", "按 request_id 查询本宿主的阶段耗时和脱敏错误摘要。", {
            "request_id": ident,
        }, ("request_id",)),
        tool("restart_service", "关闭新写入并等待在途请求与已入列写入完成，接受一次由独立宿主进程执行的正常重启。", {
            "request_id": ident, "operation_id": ident, "timeout_seconds": restart_timeout,
        }, ("request_id", "operation_id")),
        tool("get_service_restart", "按 operation_id 读取宿主维护状态、排空阻塞、重启实例核验和持久回执。", {
            "operation_id": ident,
        }, ("operation_id",)),
        tool("retry_service_restart", "在可重试失败后以相同 operation_id 重新排空并启动宿主。", {
            "request_id": ident, "operation_id": ident, "timeout_seconds": restart_timeout,
        }, ("request_id", "operation_id")),
        tool("preview_retention_cleanup", "按指定的保留期限预览当前宿主可清理的数据与保护原因。", {
            "conversation_id": ident, "retention_days": retention_days,
            "retention_overrides": overrides,
        }, ("conversation_id",)),
        tool("apply_retention_cleanup", "核对 preview 指纹并按用户明确确认的计划清理。", {
            "request_id": ident, "plan_id": ident,
            "fingerprint": field("string", minLength=64, maxLength=64), "confirmed": field("boolean"),
        }, ("request_id", "plan_id", "fingerprint", "confirmed")),
    ]


def _task_runner(book, actor, channel, validate):
    def run(action: str, arguments: dict[str, Any], stable_request_id: str, context: JobContext):
        context.check_cancelled()
        params = {**arguments, "request_id": stable_request_id}
        validate(action, params, channel)
        token = _CURRENT_JOB_CONTEXT.set(context)
        trace = runtime(book).traces
        internal_request_id = _actor_scoped_id(actor, "request", stable_request_id)
        try:
            with trace.span(internal_request_id, "async_operation", action, actor) as span:
                result = book.dispatch(action, params, channel=channel, actor=actor, request_id=stable_request_id)
                if result.get("status") != "ok":
                    span.set_result(result["status"], _safe_error_code(result.get("error", {})))
                return result
        finally:
            _CURRENT_JOB_CONTEXT.reset(token)
    return run


def _handle_submit(book, db, params, channel, validate, now, actor):
    if actor is None:
        raise MCP24OpsError("forbidden", "异步任务需要具名宿主。")
    if "request_id" in params["arguments"]:
        raise MCP24OpsError("invalid_params", "arguments 不接受 request_id；任务会生成稳定的内部请求 ID。")
    validation_params = {**params["arguments"], "request_id": "mcp24-validation"}
    validate(params["action"], validation_params, channel)
    privacy_guard.check_action(db, params["action"], params["arguments"])
    ledger = runtime(book).ledger
    return ledger.submit(actor, params["action"], params["arguments"], params["request_id"],
                         _task_runner(book, actor, channel, validate))


def _handle_get(book, db, params, channel, validate, now, actor):
    if actor is None:
        raise MCP24OpsError("forbidden", "异步任务需要具名宿主。")
    return runtime(book).ledger.get(actor, params["job_id"])


def _handle_cancel(book, db, params, channel, validate, now, actor):
    if actor is None:
        raise MCP24OpsError("forbidden", "异步任务需要具名宿主。")
    return runtime(book).ledger.cancel(actor, params["job_id"])


def _handle_resume(book, db, params, channel, validate, now, actor):
    if actor is None:
        raise MCP24OpsError("forbidden", "异步任务需要具名宿主。")
    return runtime(book).ledger.resume_explicit(
        actor, params["job_id"], _task_runner(book, actor, channel, validate)
    )


def _handle_diagnostics(book, db, params, channel, validate, now, actor):
    if actor is None:
        raise MCP24OpsError("forbidden", "诊断查询需要具名宿主。")
    return {"request_id": params["request_id"], "spans": runtime(book).ledger.diagnostics(actor, params["request_id"])}


def _handle_restart(book, db, params, channel, validate, now, actor):
    if actor is None:
        raise MCP24OpsError("forbidden", "服务重启需要已认证的宿主身份。")
    return _begin_restart(book, actor, params, retry=False)


def _handle_get_restart(book, db, params, channel, validate, now, actor):
    if actor is None:
        raise MCP24OpsError("forbidden", "维护状态查询需要已认证的宿主身份。")
    return ServiceMaintenanceStore(_ledger_path(book)).get(actor, params["operation_id"])


def _handle_retry_restart(book, db, params, channel, validate, now, actor):
    if actor is None:
        raise MCP24OpsError("forbidden", "服务重启需要已认证的宿主身份。")
    return _begin_restart(book, actor, params, retry=True)


def _handle_preview_cleanup(book, db, params, channel, validate, now, actor):
    return runtime(book).cleanup.preview(
        db, actor=actor, retention_days=params.get("retention_days"),
        retention_overrides=params.get("retention_overrides"),
        conversation_id=params["conversation_id"], now=now,
    )


def _handle_apply_cleanup(book, db, params, channel, validate, now, actor):
    return runtime(book).cleanup.apply(
        db, actor=actor, plan_id=params["plan_id"], fingerprint=params["fingerprint"],
        confirmed=params["confirmed"], request_id=params["request_id"], now=now,
    )


HANDLERS = {
    "submit_async_job": _handle_submit,
    "get_async_job": _handle_get,
    "cancel_async_job": _handle_cancel,
    "resume_async_job": _handle_resume,
    "get_request_diagnostics": _handle_diagnostics,
    "restart_service": _handle_restart,
    "get_service_restart": _handle_get_restart,
    "retry_service_restart": _handle_retry_restart,
    "preview_retention_cleanup": _handle_preview_cleanup,
    "apply_retention_cleanup": _handle_apply_cleanup,
}

WRITE_ACTIONS = frozenset({
    "submit_async_job", "cancel_async_job", "resume_async_job", "apply_retention_cleanup",
    "restart_service", "retry_service_restart",
})


def handle(book, db, action, params, channel, validate, now, actor):
    try:
        handler = HANDLERS[action]
    except KeyError:
        raise MCP24OpsError("invalid_action", "不支持的 MCP 运行或清理操作。") from None
    return handler(book, db, params, channel, validate, now, actor)


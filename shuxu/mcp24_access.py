from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


LEGACY_HOST_ID = "legacy-stdio"
LOCAL_HOST_ID = "local-web"
ACCESS_SCHEMA = "mcp24_access"
PREVIEW_TTL = timedelta(minutes=5)
HANDOFF_TTL = timedelta(minutes=5)
EXCERPT_REQUEST_TTL = timedelta(minutes=10)
_HOST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class AccessError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _schema_tables(db: sqlite3.Connection, name: str) -> None:
    q = f'"{name}".'
    required_tables = {"hosts", "bound_previews", "excerpt_requests", "excerpt_permits", "handoffs", "idempotency"}
    existing_tables = {row[0] for row in db.execute(f"SELECT name FROM {q}sqlite_master WHERE type = 'table'")}
    if required_tables.issubset(existing_tables):
        required_columns = {
            "handoffs": {"prompt_version"},
            "idempotency": {"profile", "authorization_revision", "row_key_hash", "tombstoned_at"},
        }
        if all(columns.issubset({row[1] for row in db.execute(f"PRAGMA {name}.table_info({table})")})
               for table, columns in required_columns.items()):
            return
    statements = (
        f"""CREATE TABLE IF NOT EXISTS {q}hosts (
            host_id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            scopes_json TEXT NOT NULL,
            bearer_sha256 TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            authorization_revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS {q}bound_previews (
            preview_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            authorization_revision INTEGER NOT NULL,
            action TEXT NOT NULL,
            parameters_hash TEXT NOT NULL,
            object_versions_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS {q}excerpt_requests (
            host_id TEXT NOT NULL,
            upstream_message_id TEXT NOT NULL,
            authorization_revision INTEGER NOT NULL,
            parameters_hash TEXT NOT NULL,
            kind TEXT NOT NULL,
            object_id TEXT NOT NULL,
            field TEXT NOT NULL,
            mode TEXT NOT NULL,
            start_offset INTEGER,
            requested_length INTEGER,
            selection_sha256 TEXT,
            last_sentence INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'granted', 'consumed', 'expired')),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (host_id, upstream_message_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS {q}excerpt_permits (
            permit_id TEXT PRIMARY KEY,
            host_id TEXT NOT NULL,
            authorization_revision INTEGER NOT NULL,
            upstream_message_id TEXT NOT NULL,
            parameters_hash TEXT NOT NULL,
            kind TEXT NOT NULL,
            object_id TEXT NOT NULL,
            field TEXT NOT NULL,
            mode TEXT NOT NULL,
            start_offset INTEGER,
            requested_length INTEGER,
            selection_sha256 TEXT,
            last_sentence INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS {q}handoffs (
            handoff_id TEXT PRIMARY KEY,
            source_host_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL,
            target_host_id TEXT NOT NULL,
            target_revision INTEGER NOT NULL,
            prompt_id TEXT NOT NULL,
            prompt_version INTEGER NOT NULL,
            clues_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        )""",
        f"""CREATE TABLE IF NOT EXISTS {q}idempotency (
            host_id TEXT NOT NULL,
            profile TEXT NOT NULL,
            authorization_revision INTEGER NOT NULL,
            message_hash TEXT NOT NULL,
            row_key_hash TEXT,
            action TEXT NOT NULL,
            parameters_hash TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending', 'committed')),
            result_ref TEXT,
            result_hash TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            tombstoned_at TEXT,
            PRIMARY KEY (host_id, message_hash)
        )""",
    )
    for statement in statements:
        db.execute(statement)
    handoff_columns = {row[1] for row in db.execute(f"PRAGMA {name}.table_info(handoffs)")}
    if "prompt_version" not in handoff_columns:
        db.execute(f"ALTER TABLE {q}handoffs ADD COLUMN prompt_version INTEGER NOT NULL DEFAULT 0")
    idempotency_columns = {row[1] for row in db.execute(f"PRAGMA {name}.table_info(idempotency)")}
    if "tombstoned_at" not in idempotency_columns:
        db.execute(f"ALTER TABLE {q}idempotency ADD COLUMN tombstoned_at TEXT")
    if "row_key_hash" not in idempotency_columns:
        db.execute(f"ALTER TABLE {q}idempotency ADD COLUMN row_key_hash TEXT")
    if "profile" not in idempotency_columns:
        db.execute(f"ALTER TABLE {q}idempotency ADD COLUMN profile TEXT NOT NULL DEFAULT ''")
    if "authorization_revision" not in idempotency_columns:
        db.execute(f"ALTER TABLE {q}idempotency ADD COLUMN authorization_revision INTEGER NOT NULL DEFAULT 0")
    for row in db.execute(f"SELECT host_id, message_hash FROM {q}idempotency WHERE row_key_hash IS NULL").fetchall():
        row_key_hash = hashlib.sha256(f"{row[0]}\0{row[1]}".encode("utf-8")).hexdigest()
        db.execute(f"UPDATE {q}idempotency SET row_key_hash = ? WHERE host_id = ? AND message_hash = ?", (row_key_hash, row[0], row[1]))


def _database_file(db: sqlite3.Connection) -> str:
    row = next(item for item in db.execute("PRAGMA database_list") if item[1] == "main")
    return row[2]


def sidecar_path(book_or_path: Any) -> Path | None:
    db_path = book_or_path if isinstance(book_or_path, (str, Path)) else book_or_path.db_path
    value = str(db_path)
    if value == ":memory:" or (value.startswith("file:") and "mode=memory" in value):
        return None
    path = Path(value)
    return path.with_name(path.stem + ".mcp24-access.sqlite3")


def _is_sidecar(path: str) -> bool:
    return bool(path) and Path(path).name.endswith(".mcp24-access.sqlite3")


def schema(db: sqlite3.Connection) -> None:
    attached = {row[1] for row in db.execute("PRAGMA database_list")}
    if ACCESS_SCHEMA in attached:
        name = ACCESS_SCHEMA
    else:
        main_file = _database_file(db)
        path = sidecar_path(main_file) if main_file else None
        if path is None or _is_sidecar(main_file):
            name = "main"
        else:
            db.execute(f"ATTACH DATABASE ? AS {ACCESS_SCHEMA}", (str(path),))
            name = ACCESS_SCHEMA
    _schema_tables(db, name)


def _schema_name(db: sqlite3.Connection) -> str:
    attached = {row[1] for row in db.execute("PRAGMA database_list")}
    if ACCESS_SCHEMA in attached:
        return ACCESS_SCHEMA
    main_file = _database_file(db)
    if not main_file or _is_sidecar(main_file):
        _schema_tables(db, "main")
        return "main"
    schema(db)
    return ACCESS_SCHEMA


def _table(db: sqlite3.Connection, name: str) -> str:
    return f'"{_schema_name(db)}"."{name}"'


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _actor_host(actor: dict[str, Any]) -> str:
    return actor["host_id"]


def _local_actor(actor: dict[str, Any]) -> bool:
    return (actor["host_id"], actor["profile"]) in {
        (LEGACY_HOST_ID, "legacy_stdio"), (LOCAL_HOST_ID, "local_web")
    }


def legacy_actor() -> dict[str, Any]:
    return {"host_id": LEGACY_HOST_ID, "profile": "legacy_stdio", "scopes": ["tool:*"], "authorization_revision": 0}


def local_actor() -> dict[str, Any]:
    return {"host_id": LOCAL_HOST_ID, "profile": "local_web", "scopes": ["tool:*"], "authorization_revision": 0}


def authenticate(book: Any, host_id: str | None, bearer: str | None) -> dict[str, Any]:
    if host_id is None and bearer is None:
        return legacy_actor()
    if not isinstance(host_id, str) or not isinstance(bearer, str) or not _HOST_ID.fullmatch(host_id):
        raise AccessError("unauthorized", "Host credentials are invalid.")
    if host_id in {LEGACY_HOST_ID, LOCAL_HOST_ID}:
        raise AccessError("unauthorized", "Host credentials are invalid.")
    try:
        with sidecar_connection(book) as db:
            row = db.execute("SELECT * FROM hosts WHERE host_id = ?", (host_id,)).fetchone()
    except (OSError, sqlite3.Error, FileNotFoundError):
        raise AccessError("unauthorized", "Host credentials are invalid.") from None
    supplied = hashlib.sha256(bearer.encode("utf-8")).hexdigest()
    if row is None or not row["enabled"] or not hmac.compare_digest(supplied, row["bearer_sha256"]):
        raise AccessError("unauthorized", "Host credentials are invalid.")
    return {
        "host_id": row["host_id"],
        "profile": row["profile"],
        "scopes": json.loads(row["scopes_json"]),
        "authorization_revision": row["authorization_revision"],
    }


def connect_sidecar(book_or_path: Any, *, create: bool = False) -> sqlite3.Connection:
    path = sidecar_path(book_or_path)
    if path is None:
        connect = getattr(book_or_path, "_connect", None)
        if connect is None:
            raise ValueError("An in-memory BookService connection is required.")
        db = connect()
        _schema_tables(db, "main")
        return db
    if not create and not path.is_file():
        raise FileNotFoundError(path)
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 5000")
    _schema_tables(db, "main")
    return db


@contextmanager
def sidecar_connection(book_or_path: Any, *, create: bool = False):
    db = connect_sidecar(book_or_path, create=create)
    try:
        yield db
    finally:
        db.close()


def _host_row(db: sqlite3.Connection, host_id: str) -> sqlite3.Row | None:
    return db.execute(f"SELECT * FROM {_table(db, 'hosts')} WHERE host_id = ?", (host_id,)).fetchone()


def _assert_current_actor(db: sqlite3.Connection, actor: dict[str, Any]) -> None:
    if _local_actor(actor):
        return
    row = _host_row(db, _actor_host(actor))
    if row is None or not row["enabled"] or row["authorization_revision"] != actor["authorization_revision"]:
        raise AccessError("authorization_changed", "Host authorization changed; authenticate again.")
    if row["profile"] != actor["profile"] or json.loads(row["scopes_json"]) != actor["scopes"]:
        raise AccessError("authorization_changed", "Host authorization changed; authenticate again.")


def has_scope(actor: dict[str, Any] | None, scope: str) -> bool:
    if actor is None:
        return False
    if has_full_access(actor):
        return True
    return scope in actor["scopes"]


def has_full_access(actor: dict[str, Any] | None) -> bool:
    return actor is not None and (_local_actor(actor) or "tool:*" in actor["scopes"])


def allows(actor: dict[str, Any] | None, tool_name: str) -> bool:
    if actor is None:
        return False
    if has_full_access(actor):
        return True
    scopes = actor["scopes"]
    return f"tool:{tool_name}" in scopes


def host_namespace(actor: dict[str, Any]) -> str:
    value = f"{actor['host_id']}\0{actor['profile']}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:24]


def namespace_id(actor: dict[str, Any], kind: str, external_id: str) -> str:
    identifier = hashlib.sha256(str(external_id).encode("utf-8")).hexdigest()[:32]
    return f"{host_namespace(actor)}:{kind}:{identifier}"


def canonical_params_hash(action: str, effect_params: dict[str, Any]) -> str:
    return hashlib.sha256(_json({"action": action, "params": effect_params}).encode("utf-8")).hexdigest()


def create_bound_preview(
    db: sqlite3.Connection,
    *,
    actor: dict[str, Any],
    action: str,
    canonical_params_hash: str,
    object_versions: dict[str, Any],
    now: datetime,
    expires_at: datetime,
) -> dict[str, Any]:
    if expires_at <= now or expires_at > now + PREVIEW_TTL:
        raise AccessError("invalid_params", "Preview expiry must be within five minutes.")
    if not _HASH.fullmatch(canonical_params_hash.lower()):
        raise AccessError("invalid_params", "Preview parameter hash is invalid.")
    _assert_current_actor(db, actor)
    host_id = _actor_host(actor)
    authorization_revision = actor["authorization_revision"]
    versions_json = _json(object_versions)
    params_hash = canonical_params_hash.lower()
    preview_id = secrets.token_urlsafe(24)
    table = _table(db, "bound_previews")
    db.execute(
        f"""UPDATE {table} SET consumed_at = ?
            WHERE host_id = ? AND authorization_revision = ? AND action = ?
              AND parameters_hash = ? AND object_versions_json = ? AND consumed_at IS NULL""",
        (_iso(now), host_id, authorization_revision, action, params_hash, versions_json),
    )
    db.execute(
        f"INSERT INTO {table}(preview_id, host_id, authorization_revision, action, parameters_hash, object_versions_json, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (preview_id, host_id, authorization_revision, action, params_hash, versions_json, _iso(now), _iso(expires_at)),
    )
    return {"preview_id": preview_id, "action": action, "object_versions": object_versions, "expires_at": _iso(expires_at)}


def guard(
    db: sqlite3.Connection,
    *,
    actor: dict[str, Any],
    action: str,
    canonical_params_hash: str,
    object_versions: dict[str, Any],
    now: datetime,
    preview_id: str | None = None,
) -> dict[str, Any]:
    _assert_current_actor(db, actor)
    if not allows(actor, action):
        raise AccessError("forbidden", "This host cannot perform the requested action.")
    if not _HASH.fullmatch(canonical_params_hash.lower()):
        raise AccessError("invalid_params", "Preview parameter hash is invalid.")
    table = _table(db, "bound_previews")
    match = (_actor_host(actor), actor["authorization_revision"], action, canonical_params_hash.lower(), _json(object_versions), _iso(now))
    if preview_id is None:
        rows = db.execute(
            f"""SELECT * FROM {table}
                WHERE host_id = ? AND authorization_revision = ? AND action = ?
                  AND parameters_hash = ? AND object_versions_json = ? AND expires_at > ? AND consumed_at IS NULL
                ORDER BY created_at DESC LIMIT 1""",
            match,
        ).fetchall()
    else:
        rows = db.execute(
            f"""SELECT * FROM {table}
                WHERE preview_id = ? AND host_id = ? AND authorization_revision = ? AND action = ?
                  AND parameters_hash = ? AND object_versions_json = ? AND expires_at > ? AND consumed_at IS NULL""",
            (preview_id, *match),
        ).fetchall()
    if not rows:
        raise AccessError("preview_required", "A current matching preview is required.")
    row = rows[0]
    consumed = db.execute(
        f"UPDATE {table} SET consumed_at = ? WHERE preview_id = ? AND consumed_at IS NULL AND expires_at > ?",
        (_iso(now), row["preview_id"], _iso(now)),
    )
    if consumed.rowcount != 1:
        raise AccessError("preview_consumed", "This preview was already consumed or expired.")
    return {"preview_id": row["preview_id"], "action": row["action"], "consumed_at": _iso(now)}


def _excerpt_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: params[key] for key in ("kind", "id", "field", "start", "length", "selection", "last_sentence") if key in params}


def _request_id(params: dict[str, Any]) -> str:
    return params.get("upstream_message_id") or params.get("_upstream_message_id") or ""


_IDEMPOTENCY_METADATA = {
    "request_id", "upstream_message_id", "_upstream_message_id",
    "operation_slot", "_operation_slot", "transport_metadata",
}


def _idempotency_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if key not in _IDEMPOTENCY_METADATA}


def _idempotency_message_hash(
    *, upstream_message_id: str | None, operation_slot: str | None, action: str, params: dict[str, Any]
) -> str | None:
    if upstream_message_id:
        identity = {"upstream_message_id": upstream_message_id, "operation_slot": operation_slot or action}
    else:
        request_id = params.get("request_id")
        if request_id is None or request_id == "":
            return None
        identity = {"action": action, "request_id": request_id}
    return hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()


def register_excerpt_request(db: sqlite3.Connection, *, actor: dict[str, Any], params: dict[str, Any], now: datetime) -> dict[str, Any]:
    if _local_actor(actor):
        raise AccessError("invalid_params", "Local requests do not need an excerpt permit.")
    _assert_current_actor(db, actor)
    if not allows(actor, "share_excerpt"):
        raise AccessError("forbidden", "This host cannot request private excerpts.")
    upstream_message_id = _request_id(params)
    if not upstream_message_id:
        raise AccessError("invalid_params", "A stable upstream message ID is required for an excerpt request.")
    effect_params = _excerpt_params(params)
    params_hash = canonical_params_hash("share_excerpt", effect_params)
    use_slice = "start" in effect_params or "length" in effect_params
    use_match = "selection" in effect_params
    use_tail = effect_params.get("last_sentence") is True
    if sum((use_slice, use_match, use_tail)) != 1 or (use_slice and not ("start" in effect_params and "length" in effect_params)):
        raise AccessError("invalid_params", "Specify exactly one excerpt range.")
    mode = "slice" if use_slice else "selection" if use_match else "last_sentence"
    request_table = _table(db, "excerpt_requests")
    existing = db.execute(
        f"SELECT parameters_hash, status FROM {request_table} WHERE host_id = ? AND upstream_message_id = ?",
        (_actor_host(actor), upstream_message_id),
    ).fetchone()
    if existing:
        if existing["parameters_hash"] != params_hash:
            raise AccessError("idempotency_conflict", "This message ID was already used for a different excerpt request.")
        return {"request_id": upstream_message_id, "status": existing["status"]}
    expires_at = now + EXCERPT_REQUEST_TTL
    selection = effect_params.get("selection")
    db.execute(
        f"""INSERT INTO {request_table}(
            host_id, upstream_message_id, authorization_revision, parameters_hash, kind, object_id, field,
            mode, start_offset, requested_length, selection_sha256, last_sentence, status, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (_actor_host(actor), upstream_message_id, actor["authorization_revision"], params_hash,
         effect_params["kind"], effect_params["id"], effect_params["field"], mode,
         effect_params.get("start"), effect_params.get("length"),
         hashlib.sha256(selection.encode("utf-8")).hexdigest() if selection is not None else None,
         int(use_tail), _iso(now), _iso(expires_at)),
    )
    return {"request_id": upstream_message_id, "status": "pending", "expires_at": _iso(expires_at)}


def consume_excerpt_permit(db: sqlite3.Connection, *, actor: dict[str, Any], params: dict[str, Any], now: datetime) -> dict[str, Any]:
    if _local_actor(actor):
        return {"authorized": True, "source": "local"}
    _assert_current_actor(db, actor)
    upstream_message_id = _request_id(params)
    if not upstream_message_id:
        raise AccessError("consent_required", "A current host-bound excerpt permit is required.")
    effect_params = _excerpt_params(params)
    params_hash = canonical_params_hash("share_excerpt", effect_params)
    permit_table = _table(db, "excerpt_permits")
    row = db.execute(
        f"""SELECT * FROM {permit_table}
            WHERE host_id = ? AND authorization_revision = ? AND upstream_message_id = ?
              AND parameters_hash = ? AND expires_at > ? AND consumed_at IS NULL""",
        (_actor_host(actor), actor["authorization_revision"], upstream_message_id, params_hash, _iso(now)),
    ).fetchone()
    if row is None:
        register_excerpt_request(db, actor=actor, params=params, now=now)
        raise AccessError("consent_required", "A local operator must approve this exact excerpt request.")
    request_table = _table(db, "excerpt_requests")
    updated = db.execute(
        f"UPDATE {permit_table} SET consumed_at = ? WHERE permit_id = ? AND consumed_at IS NULL AND expires_at > ?",
        (_iso(now), row["permit_id"], _iso(now)),
    )
    if updated.rowcount != 1:
        raise AccessError("consent_required", "This excerpt permit was already used or expired.")
    db.execute(
        f"UPDATE {request_table} SET status = 'consumed' WHERE host_id = ? AND upstream_message_id = ? AND parameters_hash = ?",
        (_actor_host(actor), upstream_message_id, params_hash),
    )
    return {"authorized": True, "source": "single_use_permit"}


def begin_idempotency(
    db: sqlite3.Connection,
    *,
    actor: dict[str, Any],
    upstream_message_id: str | None,
    operation_slot: str | None = None,
    action: str,
    params: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    message_hash = _idempotency_message_hash(
        upstream_message_id=upstream_message_id, operation_slot=operation_slot, action=action, params=params
    )
    if message_hash is None and _local_actor(actor):
        return {"state": "untracked"}
    _assert_current_actor(db, actor)
    if not allows(actor, action):
        raise AccessError("forbidden", "This host cannot perform the requested action.")
    if message_hash is None:
        return {"state": "untracked"}
    params_hash = canonical_params_hash(action, _idempotency_params(params))
    table = _table(db, "idempotency")
    row = db.execute(f"SELECT * FROM {table} WHERE host_id = ? AND message_hash = ?", (_actor_host(actor), message_hash)).fetchone()
    if row:
        if row["profile"] != actor["profile"] or row["authorization_revision"] != actor["authorization_revision"]:
            raise AccessError("authorization_changed", "This request receipt belongs to an older host authorization revision.")
        if row["action"] != action or row["parameters_hash"] != params_hash:
            raise AccessError("idempotency_conflict", "This host message ID was already used with different parameters.")
        if row["state"] == "committed":
            return {"state": "replay", "result_ref": row["result_ref"], "result_hash": row["result_hash"], "tombstoned": row["result_ref"] is None}
        raise AccessError("submission_unknown", "The original request is still unresolved; it was not run again.")
    db.execute(
        f"INSERT INTO {table}(host_id, profile, authorization_revision, message_hash, row_key_hash, action, parameters_hash, state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (_actor_host(actor), actor["profile"], actor["authorization_revision"], message_hash,
         _idempotency_row_key(_actor_host(actor), message_hash), action, params_hash, _iso(now), _iso(now)),
    )
    return {"state": "new", "parameters_hash": params_hash}


def complete_idempotency(
    db: sqlite3.Connection,
    *,
    actor: dict[str, Any],
    upstream_message_id: str | None,
    operation_slot: str | None = None,
    action: str,
    params: dict[str, Any],
    result_ref: str,
    result_hash: str | None,
    now: datetime,
) -> None:
    _assert_current_actor(db, actor)
    message_hash = _idempotency_message_hash(
        upstream_message_id=upstream_message_id, operation_slot=operation_slot, action=action, params=params
    )
    if message_hash is None:
        raise AccessError("invalid_params", "A stable operation ID is required to complete an idempotency record.")
    params_hash = canonical_params_hash(action, _idempotency_params(params))
    updated = db.execute(
        f"""UPDATE {_table(db, 'idempotency')} SET state = 'committed', result_ref = ?, result_hash = ?, updated_at = ?
            WHERE host_id = ? AND profile = ? AND authorization_revision = ? AND message_hash = ?
              AND action = ? AND parameters_hash = ? AND state = 'pending'""",
        (result_ref, result_hash, _iso(now), _actor_host(actor), actor["profile"],
         actor["authorization_revision"], message_hash, action, params_hash),
    )
    if updated.rowcount != 1:
        raise AccessError("idempotency_conflict", "The request idempotency record could not be committed.")


def tombstone_idempotency(
    db: sqlite3.Connection,
    *,
    actor: dict[str, Any],
    action: str,
    params: dict[str, Any],
    upstream_message_id: str | None = None,
    operation_slot: str | None = None,
) -> None:
    message_hash = _idempotency_message_hash(
        upstream_message_id=upstream_message_id, operation_slot=operation_slot, action=action, params=params
    )
    if message_hash is None:
        raise AccessError("invalid_params", "A stable operation ID is required to tombstone an idempotency record.")
    db.execute(
        f"UPDATE {_table(db, 'idempotency')} SET result_ref = NULL, result_hash = NULL, tombstoned_at = ? WHERE host_id = ? AND profile = ? AND authorization_revision = ? AND message_hash = ? AND state = 'committed'",
        (_iso(datetime.now(timezone.utc)), _actor_host(actor), actor["profile"], actor["authorization_revision"], message_hash),
    )


def _idempotency_row_key(host_id: str, message_hash: str) -> str:
    return hashlib.sha256(f"{host_id}\0{message_hash}".encode("utf-8")).hexdigest()


def preview_idempotency_tombstones(db: sqlite3.Connection, *, actor: dict[str, Any], cutoff: datetime) -> list[dict[str, Any]]:
    _assert_current_actor(db, actor)
    table = _table(db, "idempotency")
    criteria = "state = 'committed' AND result_ref IS NOT NULL AND updated_at <= ?"
    parameters: tuple[Any, ...] = (_iso(cutoff),)
    if not _local_actor(actor):
        criteria += " AND host_id = ? AND profile = ? AND authorization_revision = ?"
        parameters += (_actor_host(actor), actor["profile"], actor["authorization_revision"])
    rows = db.execute(
        f"SELECT row_key_hash, action, result_hash, updated_at FROM {table} WHERE {criteria} ORDER BY updated_at, host_id, message_hash",
        parameters,
    ).fetchall()
    return [
        {"row_key_hash": row["row_key_hash"],
         "action": row["action"], "updated_at": row["updated_at"], "result_hash": row["result_hash"]}
        for row in rows
    ]


def tombstone_idempotency_by_key(
    db: sqlite3.Connection,
    *,
    actor: dict[str, Any],
    row_key_hash: str,
    expected_updated_at: str,
    expected_result_hash: str | None,
    now: datetime,
) -> dict[str, Any]:
    _assert_current_actor(db, actor)
    table = _table(db, "idempotency")
    row = db.execute(f"SELECT * FROM {table} WHERE row_key_hash = ?", (row_key_hash,)).fetchone()
    if row is None:
        raise AccessError("version_conflict", "The idempotency receipt changed after preview.")
    if not _local_actor(actor) and (row["host_id"], row["profile"], row["authorization_revision"]) != (
        _actor_host(actor), actor["profile"], actor["authorization_revision"]
    ):
        raise AccessError("forbidden", "This host cannot clean another host's receipt.")
    if row["state"] == "committed" and row["result_ref"] is None and row["tombstoned_at"] is not None:
        return {"tombstoned": False, "already_tombstoned": True, "row_key_hash": row_key_hash}
    if (row["state"] != "committed" or row["result_ref"] is None
            or row["updated_at"] != expected_updated_at or row["result_hash"] != expected_result_hash):
        raise AccessError("version_conflict", "The idempotency receipt changed after preview.")
    updated = db.execute(
        f"UPDATE {table} SET result_ref = NULL, result_hash = NULL, tombstoned_at = ? WHERE row_key_hash = ? AND state = 'committed' AND result_ref IS NOT NULL AND updated_at = ? AND result_hash IS ?",
        (_iso(now), row_key_hash, expected_updated_at, expected_result_hash),
    )
    if updated.rowcount != 1:
        raise AccessError("version_conflict", "The idempotency receipt changed after preview.")
    return {"tombstoned": True, "already_tombstoned": False, "row_key_hash": row_key_hash, "updated_at": expected_updated_at}


_CLUE_FIELDS = {
    "inspiration": {"text", "source", "url", "note"},
    "clipping": {"title", "source", "published_on", "note"},
    "resume": {"title", "question", "note"},
}


def _current_host_revision(db: sqlite3.Connection, host_id: str) -> int:
    row = _host_row(db, host_id)
    if row is None or not row["enabled"]:
        raise AccessError("forbidden", "The target host is unavailable.")
    return row["authorization_revision"]


def _create_handoff(book: Any, db: sqlite3.Connection, params: dict[str, Any], channel: str, validate, now: datetime, actor: dict[str, Any]) -> dict[str, Any]:
    _assert_current_actor(db, actor)
    if not allows(actor, "create_handoff"):
        raise AccessError("forbidden", "This host cannot create a handoff.")
    target_host_id = params["target_host_id"]
    target_revision = _current_host_revision(db, target_host_id)
    if target_host_id == _actor_host(actor) or not has_scope(actor, f"handoff:{target_host_id}"):
        raise AccessError("forbidden", "This host cannot hand off to the selected destination.")
    prompt_id = params["prompt_id"]
    if not has_scope(actor, f"prompt:{prompt_id}"):
        raise AccessError("forbidden", "This host cannot hand off the selected prompt.")
    row = db.execute("SELECT id, version FROM prompts WHERE id = ?", (prompt_id,)).fetchone()
    if row is None:
        raise AccessError("not_found", "The selected prompt is unavailable.")
    clues = params.get("clues", [])
    if len(clues) > 5:
        raise AccessError("invalid_params", "A handoff can include at most five selected clues.")
    selected = []
    seen = set()
    for clue in clues:
        kind, identifier, field = clue["kind"], clue["id"], clue["field"]
        if field not in _CLUE_FIELDS[kind] or not has_scope(actor, f"material:{identifier}:{field}"):
            raise AccessError("forbidden", "This host cannot hand off the selected clue field.")
        key = (kind, identifier, field)
        if key in seen:
            raise AccessError("invalid_params", "A clue field can only be selected once.")
        seen.add(key)
        material = db.execute("SELECT collection, data_json, version FROM materials WHERE id = ?", (identifier,)).fetchone()
        if material is None or material["collection"] != kind:
            raise AccessError("not_found", "The selected clue is unavailable.")
        value = json.loads(material["data_json"]).get(field)
        if not isinstance(value, str) or not value.strip():
            raise AccessError("not_found", "The selected clue field is unavailable.")
        selected.append({"kind": kind, "id": identifier, "field": field, "version": material["version"]})
    handoff_id = secrets.token_urlsafe(32)
    expires_at = now + HANDOFF_TTL
    db.execute(
        f"""INSERT INTO {_table(db, 'handoffs')}(
            handoff_id, source_host_id, source_revision, target_host_id, target_revision,
            prompt_id, prompt_version, clues_json, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (handoff_id, _actor_host(actor), actor["authorization_revision"], target_host_id,
         target_revision, prompt_id, row["version"], _json(selected), _iso(now), _iso(expires_at)),
    )
    return {"request_id": params.get("request_id"), "committed": True,
            "handoff_id": handoff_id, "target_host_id": target_host_id, "expires_at": _iso(expires_at)}


def _receive_handoff(book: Any, db: sqlite3.Connection, params: dict[str, Any], channel: str, validate, now: datetime, actor: dict[str, Any]) -> dict[str, Any]:
    if _local_actor(actor):
        raise AccessError("forbidden", "This host cannot receive a handoff.")
    _assert_current_actor(db, actor)
    if not allows(actor, "receive_handoff"):
        raise AccessError("forbidden", "This host cannot receive a handoff.")
    table = _table(db, "handoffs")
    row = db.execute(
        f"SELECT * FROM {table} WHERE handoff_id = ? AND target_host_id = ? AND target_revision = ? AND expires_at > ? AND consumed_at IS NULL",
        (params["handoff_id"], _actor_host(actor), actor["authorization_revision"], _iso(now)),
    ).fetchone()
    if row is None:
        raise AccessError("handoff_unavailable", "This handoff is unavailable or expired.")
    source_revision = 0 if row["source_host_id"] in {LEGACY_HOST_ID, LOCAL_HOST_ID} else _current_host_revision(db, row["source_host_id"])
    if source_revision != row["source_revision"]:
        raise AccessError("handoff_unavailable", "This handoff is unavailable or expired.")
    prompt = db.execute("SELECT id, text, version FROM prompts WHERE id = ?", (row["prompt_id"],)).fetchone()
    if prompt is None or prompt["version"] != row["prompt_version"]:
        raise AccessError("handoff_stale", "The selected prompt is no longer available.")
    clues = []
    for clue in json.loads(row["clues_json"]):
        material = db.execute("SELECT collection, data_json, version FROM materials WHERE id = ?", (clue["id"],)).fetchone()
        if material is None or material["collection"] != clue["kind"] or material["version"] != clue["version"]:
            raise AccessError("handoff_stale", "A selected clue is no longer available.")
        value = json.loads(material["data_json"]).get(clue["field"])
        if not isinstance(value, str) or not value.strip():
            raise AccessError("handoff_stale", "A selected clue field is no longer available.")
        clues.append({"kind": clue["kind"], "id": clue["id"], "field": clue["field"], "value": value})
    consumed = db.execute(
        f"UPDATE {table} SET consumed_at = ? WHERE handoff_id = ? AND consumed_at IS NULL AND expires_at > ?",
        (_iso(now), row["handoff_id"], _iso(now)),
    )
    if consumed.rowcount != 1:
        raise AccessError("handoff_consumed", "This handoff was already used.")
    return {"request_id": params.get("request_id"), "committed": True,
            "handoff_id": row["handoff_id"], "prompt": {"id": prompt["id"], "text": prompt["text"]},
            "clues": clues, "expires_at": row["expires_at"]}


def specs(tool, field, ident, direction, version, include_private):
    clue = field("object", properties={
        "kind": field("string", enum=sorted(_CLUE_FIELDS)),
        "id": ident,
        "field": field("string", minLength=1, maxLength=32),
    }, required=["kind", "id", "field"], additionalProperties=False)
    return [
        tool("create_handoff", "Create a five-minute handoff for one selected prompt and up to five selected clue fields.", {
            "request_id": ident, "target_host_id": field("string", minLength=1, maxLength=128),
            "prompt_id": ident, "clues": field("array", items=clue, maxItems=5),
        }, ("request_id", "target_host_id", "prompt_id")),
        tool("receive_handoff", "Receive only the prompt and clue fields selected for this host in a live handoff.", {
            "request_id": ident, "handoff_id": field("string", minLength=32, maxLength=128),
        }, ("request_id", "handoff_id")),
    ]


HANDLERS = {"create_handoff": _create_handoff, "receive_handoff": _receive_handoff}
WRITE_ACTIONS = frozenset(HANDLERS)


def handle(book, db, action, params, channel, validate, now, actor):
    try:
        handler = HANDLERS[action]
    except KeyError:
        raise AccessError("not_found", "Access action not found.") from None
    return handler(book, db, params, channel, validate, now, actor)

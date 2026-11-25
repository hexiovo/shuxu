from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import re
import secrets
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from shuxu import mcp24_access as access


_TOOL_SCOPE = re.compile(r"tool:(?:\*|[a-z][a-z0-9_]{0,63})\Z")
_PROMPT_SCOPE = re.compile(r"prompt:[A-Za-z0-9._:-]{1,128}\Z")
_MATERIAL_SCOPE = re.compile(r"material:[A-Za-z0-9._:-]{1,128}:[a-z][a-z0-9_]{0,31}\Z")
_DIRECTION_SCOPE = re.compile(r"direction:[A-Za-z0-9._:-]{1,128}\Z")
_TOPIC_SCOPE = re.compile(r"topic:[A-Za-z0-9._:-]{1,128}\Z")
_HANDOFF_SCOPE = re.compile(r"handoff:[A-Za-z0-9._:-]{1,128}\Z")
_PRIVATE_SCOPE = re.compile(r"private:[a-z][a-z0-9_]{0,31}:[A-Za-z0-9._:-]{1,128}:[a-z][a-z0-9_]{0,31}\Z")
_RESERVED_HOSTS = {access.LEGACY_HOST_ID, access.LOCAL_HOST_ID}


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _transaction(book_or_path: Any, *, create: bool = False):
    db = access.connect_sidecar(book_or_path, create=create)
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def normalize_scopes(scopes: Iterable[str]) -> list[str]:
    values = sorted(set(scopes))
    for scope in values:
        if not any(pattern.fullmatch(scope) for pattern in (
            _TOOL_SCOPE, _PROMPT_SCOPE, _MATERIAL_SCOPE, _DIRECTION_SCOPE, _TOPIC_SCOPE, _HANDOFF_SCOPE, _PRIVATE_SCOPE,
        )):
            raise access.AccessError("invalid_params", f"Unsupported authorization scope: {scope}")
    return values


def create_host(
    book_or_path: Any,
    *,
    host_id: str,
    bearer: str,
    profile: str,
    scopes: Iterable[str],
    now: datetime | None = None,
) -> dict[str, Any]:
    if not access._HOST_ID.fullmatch(host_id) or host_id in _RESERVED_HOSTS:
        raise access.AccessError("invalid_params", "The host ID is invalid or reserved.")
    if not access._PROFILE.fullmatch(profile):
        raise access.AccessError("invalid_params", "The profile name is invalid.")
    if len(bearer.encode("utf-8")) < 32:
        raise access.AccessError("invalid_params", "Bearer credentials must contain at least 32 UTF-8 bytes.")
    normalized = normalize_scopes(scopes)
    stamp = _timestamp(now or datetime.now(timezone.utc))
    digest = hashlib.sha256(bearer.encode("utf-8")).hexdigest()
    with _transaction(book_or_path, create=True) as db:
        row = db.execute("SELECT authorization_revision, enabled FROM hosts WHERE host_id = ?", (host_id,)).fetchone()
        if row and row["enabled"]:
            raise access.AccessError("host_exists", "The host is already configured.")
        revision = row["authorization_revision"] + 1 if row else 1
        db.execute(
            """INSERT INTO hosts(host_id, profile, scopes_json, bearer_sha256, enabled, authorization_revision, created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?, ?)
               ON CONFLICT(host_id) DO UPDATE SET profile = excluded.profile, scopes_json = excluded.scopes_json,
                 bearer_sha256 = excluded.bearer_sha256, enabled = 1,
                 authorization_revision = excluded.authorization_revision, updated_at = excluded.updated_at""",
            (host_id, profile, access._json(normalized), digest, revision, stamp, stamp),
        )
    return {"host_id": host_id, "profile": profile, "scopes": normalized, "authorization_revision": revision, "enabled": True}


def configure_host(book_or_path: Any, *, host_id: str, profile: str, scopes: Iterable[str], now: datetime | None = None) -> dict[str, Any]:
    if not access._PROFILE.fullmatch(profile):
        raise access.AccessError("invalid_params", "The profile name is invalid.")
    normalized = normalize_scopes(scopes)
    stamp = _timestamp(now or datetime.now(timezone.utc))
    with _transaction(book_or_path) as db:
        row = db.execute("SELECT profile, scopes_json, authorization_revision, enabled FROM hosts WHERE host_id = ?", (host_id,)).fetchone()
        if row is None or not row["enabled"]:
            raise access.AccessError("not_found", "The host is not active.")
        encoded = access._json(normalized)
        revision = row["authorization_revision"] + int(row["profile"] != profile or row["scopes_json"] != encoded)
        db.execute(
            "UPDATE hosts SET profile = ?, scopes_json = ?, authorization_revision = ?, updated_at = ? WHERE host_id = ?",
            (profile, encoded, revision, stamp, host_id),
        )
    return {"host_id": host_id, "profile": profile, "scopes": normalized, "authorization_revision": revision, "enabled": True}


def revoke_host(book_or_path: Any, *, host_id: str, now: datetime | None = None) -> dict[str, Any]:
    stamp = _timestamp(now or datetime.now(timezone.utc))
    with _transaction(book_or_path) as db:
        row = db.execute("SELECT authorization_revision, enabled FROM hosts WHERE host_id = ?", (host_id,)).fetchone()
        if row is None:
            raise access.AccessError("not_found", "The host is not configured.")
        revision = row["authorization_revision"] + int(bool(row["enabled"]))
        db.execute("UPDATE hosts SET enabled = 0, authorization_revision = ?, updated_at = ? WHERE host_id = ?", (revision, stamp, host_id))
    return {"host_id": host_id, "authorization_revision": revision, "enabled": False}


def excerpt_request(book_or_path: Any, *, host_id: str, upstream_message_id: str) -> dict[str, Any]:
    with access.sidecar_connection(book_or_path) as db:
        row = db.execute(
            """SELECT host_id, upstream_message_id, authorization_revision, kind, object_id, field, mode,
                      start_offset, requested_length, status, expires_at
                 FROM excerpt_requests WHERE host_id = ? AND upstream_message_id = ?""",
            (host_id, upstream_message_id),
        ).fetchone()
    if row is None:
        raise access.AccessError("not_found", "The excerpt request is unavailable.")
    return dict(row)


def grant_excerpt_permit(
    book_or_path: Any,
    *,
    host_id: str,
    upstream_message_id: str,
    now: datetime | None = None,
    ttl: timedelta = timedelta(minutes=5),
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    expires_at = current + ttl
    with _transaction(book_or_path) as db:
        host = db.execute("SELECT authorization_revision, enabled FROM hosts WHERE host_id = ?", (host_id,)).fetchone()
        request = db.execute(
            "SELECT * FROM excerpt_requests WHERE host_id = ? AND upstream_message_id = ?",
            (host_id, upstream_message_id),
        ).fetchone()
        if host is None or not host["enabled"] or request is None:
            raise access.AccessError("not_found", "The exact excerpt request is unavailable.")
        if (request["status"] != "pending" or request["authorization_revision"] != host["authorization_revision"]
                or request["expires_at"] <= _timestamp(current)):
            raise access.AccessError("consent_expired", "The exact excerpt request has expired or changed.")
        request_expiry = datetime.fromisoformat(request["expires_at"])
        if ttl <= timedelta(0) or ttl > timedelta(minutes=5) or expires_at > request_expiry:
            raise access.AccessError("invalid_params", "Permit expiry must be within five minutes and before the request expires.")
        permit_id = secrets.token_urlsafe(24)
        db.execute(
            """INSERT INTO excerpt_permits(
                permit_id, host_id, authorization_revision, upstream_message_id, parameters_hash,
                kind, object_id, field, mode, start_offset, requested_length, selection_sha256,
                last_sentence, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (permit_id, host_id, host["authorization_revision"], upstream_message_id, request["parameters_hash"],
             request["kind"], request["object_id"], request["field"], request["mode"], request["start_offset"],
             request["requested_length"], request["selection_sha256"], request["last_sentence"],
             _timestamp(current), _timestamp(expires_at)),
        )
        db.execute(
            "UPDATE excerpt_requests SET status = 'granted' WHERE host_id = ? AND upstream_message_id = ? AND status = 'pending'",
            (host_id, upstream_message_id),
        )
    return {"host_id": host_id, "upstream_message_id": upstream_message_id, "granted": True, "expires_at": _timestamp(expires_at)}


def _database(args: argparse.Namespace) -> Path:
    database = Path(args.database).expanduser().resolve()
    if not database.is_file():
        raise access.AccessError("not_found", "The selected Shuxu database does not exist.")
    return database


def _scopes_arg(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trusted local operator controls for Shuxu host access.")
    parser.add_argument("--database", required=True, help="Existing Shuxu SQLite database path.")
    commands = parser.add_subparsers(dest="resource", required=True)
    host = commands.add_parser("host", help="Manage named host credentials and scopes.")
    host_commands = host.add_subparsers(dest="host_action", required=True)
    create = host_commands.add_parser("create")
    create.add_argument("--host-id", required=True)
    create.add_argument("--profile", required=True)
    create.add_argument("--scopes", default="")
    configure = host_commands.add_parser("configure")
    configure.add_argument("--host-id", required=True)
    configure.add_argument("--profile", required=True)
    configure.add_argument("--scopes", required=True)
    revoke = host_commands.add_parser("revoke")
    revoke.add_argument("--host-id", required=True)
    excerpt = commands.add_parser("excerpt", help="Approve one pending excerpt request.")
    excerpt_commands = excerpt.add_subparsers(dest="excerpt_action", required=True)
    grant = excerpt_commands.add_parser("grant")
    grant.add_argument("--host-id", required=True)
    grant.add_argument("--upstream-message-id", required=True)
    grant.add_argument("--ttl-seconds", type=int, default=300)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    book = _database(args)
    try:
        if args.resource == "host" and args.host_action == "create":
            bearer = getpass.getpass("Bearer supplied by the trusted operator (not displayed or generated): ")
            result = create_host(book, host_id=args.host_id, bearer=bearer, profile=args.profile, scopes=_scopes_arg(args.scopes))
        elif args.resource == "host" and args.host_action == "configure":
            result = configure_host(book, host_id=args.host_id, profile=args.profile, scopes=_scopes_arg(args.scopes))
        elif args.resource == "host" and args.host_action == "revoke":
            result = revoke_host(book, host_id=args.host_id)
        else:
            request = excerpt_request(book, host_id=args.host_id, upstream_message_id=args.upstream_message_id)
            print(json.dumps({key: request[key] for key in (
                "host_id", "upstream_message_id", "kind", "object_id", "field", "mode",
                "start_offset", "requested_length", "expires_at",
            )}, ensure_ascii=False))
            if input("Approve this exact one-time excerpt? [y/N] ").strip().casefold() != "y":
                return 1
            result = grant_excerpt_permit(book, host_id=args.host_id, upstream_message_id=args.upstream_message_id,
                                          ttl=timedelta(seconds=args.ttl_seconds))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (access.AccessError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"{getattr(exc, 'code', 'admin_error')}: {getattr(exc, 'message', str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

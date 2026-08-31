"""Persistent web session storage for the FastAPI UI.

The Text2SQL graph remains stateless between HTTP requests, so the web layer
stores conversation state outside the worker process. PostgreSQL is the
production source of truth; the in-memory store is kept for local demos/tests
when no session database is configured.
"""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from text2sql_agent.pii_masker import mask_pii
from text2sql_agent.time_policy import KST


class SessionOwnershipError(Exception):
    """Raised when a session id exists but belongs to another user."""


class LRUCache(OrderedDict):
    def __init__(self, maxsize: int = 1000):
        super().__init__()
        self._maxsize = maxsize

    def __getitem__(self, key):
        self.move_to_end(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in self:
            self.move_to_end(key)
            return super().__getitem__(key)
        return default

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._maxsize:
            self.popitem(last=False)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        # 서버가 UTC로 돌아도 화면과 DB에는 한국시간으로 남긴다.
        if value.tzinfo is not None:
            value = value.astimezone(KST)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _now() -> datetime:
    return datetime.now(KST)


def _now_iso() -> str:
    return _now().isoformat()


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return default


def _env_int(*names: str, default: int) -> int:
    raw = _env(*names, default=str(default))
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _session_postgres_schema() -> str:
    schema = _env("WEBAPP_POSTGRES_SCHEMA", "SESSION_POSTGRES_SCHEMA", default="corporate_sales").strip()
    if schema.lower() in ("none", "-", "null"):
        return ""
    return schema


def _to_naive_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(KST).replace(tzinfo=None)


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _to_naive_kst(value)
    if not value:
        return None
    try:
        return _to_naive_kst(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except ValueError:
        return None


def _is_expired(value: Any, retention_days: int) -> bool:
    if retention_days <= 0:
        return False
    parsed = _parse_datetime(value)
    return bool(parsed and parsed < _now().replace(tzinfo=None) - timedelta(days=retention_days))


class RetentionPolicy:
    """Storage limits for web sessions.

    A value of 0 disables that specific retention rule.
    """

    def __init__(
        self,
        *,
        max_sessions_per_user: int | None = None,
        max_messages_per_session: int | None = None,
        result_retention_days: int | None = None,
        file_retention_days: int | None = None,
        session_retention_days: int | None = None,
    ) -> None:
        self.max_sessions_per_user = max_sessions_per_user if max_sessions_per_user is not None else _env_int("WEBAPP_MAX_SESSIONS_PER_USER", default=50)
        self.max_messages_per_session = (
            max_messages_per_session if max_messages_per_session is not None else _env_int("WEBAPP_MAX_MESSAGES_PER_SESSION", default=80)
        )
        self.result_retention_days = result_retention_days if result_retention_days is not None else _env_int("WEBAPP_RESULT_RETENTION_DAYS", default=7)
        self.file_retention_days = file_retention_days if file_retention_days is not None else _env_int("WEBAPP_FILE_RETENTION_DAYS", default=7)
        self.session_retention_days = session_retention_days if session_retention_days is not None else _env_int("WEBAPP_SESSION_RETENTION_DAYS", default=60)

    def status(self) -> dict[str, int]:
        return {
            "max_sessions_per_user": self.max_sessions_per_user,
            "max_messages_per_session": self.max_messages_per_session,
            "result_retention_days": self.result_retention_days,
            "file_retention_days": self.file_retention_days,
            "session_retention_days": self.session_retention_days,
        }


class RedisJsonCache:
    """Best-effort JSON cache. PostgreSQL stays authoritative."""

    def __init__(self) -> None:
        self.url = _env("WEBAPP_REDIS_URL", "REDIS_URL", default="")
        self.prefix = _env("WEBAPP_REDIS_KEY_PREFIX", default="text2sql:webapp")
        self.ttl_seconds = int(_env("WEBAPP_REDIS_TTL_SECONDS", default="86400"))
        self.client = None
        self.error = ""
        if not self.url:
            return
        try:
            import redis  # type: ignore

            self.client = redis.Redis.from_url(self.url, decode_responses=True)
        except Exception as exc:  # pragma: no cover - optional runtime dependency
            self.error = str(exc)

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def get_json(self, key: str) -> Any | None:
        if self.client is None:
            return None
        try:
            raw = self.client.get(self._key(key))
            return json.loads(raw) if raw else None
        except Exception as exc:  # pragma: no cover - cache failure must not break requests
            self.error = str(exc)
            return None

    def set_json(self, key: str, value: Any) -> None:
        if self.client is None:
            return
        try:
            self.client.setex(self._key(key), self.ttl_seconds, json.dumps(_jsonable(value), ensure_ascii=False))
        except Exception as exc:  # pragma: no cover
            self.error = str(exc)

    def status(self) -> dict[str, Any]:
        return {
            "configured": bool(self.url),
            "enabled": self.client is not None,
            "error": self.error,
            "ttl_seconds": self.ttl_seconds if self.client is not None else None,
        }


class InMemorySessionStore:
    kind = "memory"
    delete_enabled = True

    def __init__(self, policy: RetentionPolicy | None = None) -> None:
        self.policy = policy or RetentionPolicy()
        self.sessions: LRUCache = LRUCache(maxsize=200)
        self.results: LRUCache = LRUCache(maxsize=500)
        self.files: LRUCache = LRUCache(maxsize=1000)
        self.saved_queries: LRUCache = LRUCache(maxsize=1000)
        self._conversation_seq = 0

    def close(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "persistent": False,
            "redis": {"configured": False, "enabled": False},
            "retention": self.policy.status(),
        }

    def _delete_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        for result_id, record in list(self.results.items()):
            if record.get("session_id") == session_id:
                self.results.pop(result_id, None)
        for token, record in list(self.files.items()):
            if record.get("session_id") == session_id:
                self.files.pop(token, None)

    def _cleanup_expired(self) -> None:
        for session_id, session in list(self.sessions.items()):
            if _is_expired(session.get("updated_at"), self.policy.session_retention_days):
                self._delete_session(session_id)
        for result_id, record in list(self.results.items()):
            if _is_expired(record.get("created_at"), self.policy.result_retention_days):
                self.results.pop(result_id, None)
        for token, record in list(self.files.items()):
            if _is_expired(record.get("created_at"), self.policy.file_retention_days):
                self.files.pop(token, None)

    def _prune_sessions_for_user(self, user_id: str) -> None:
        limit = self.policy.max_sessions_per_user
        if limit <= 0:
            return
        user_sessions = [session for session in self.sessions.values() if session.get("user_id") == user_id]
        user_sessions.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        for session in user_sessions[limit:]:
            self._delete_session(session["id"])

    def _prune_messages(self, session: dict[str, Any]) -> None:
        limit = self.policy.max_messages_per_session
        if limit <= 0:
            return
        messages = session.get("messages", [])
        if len(messages) > limit:
            session["messages"] = messages[-limit:]
            session["message_count"] = len(session["messages"])

    def delete_session(self, session_id: str, user_id: str) -> bool:
        self._cleanup_expired()
        session = self.sessions.get(session_id)
        if not session or session.get("user_id") != user_id:
            return False
        self._delete_session(session_id)
        return True

    def prune_empty_sessions(self, user_id: str, agent_name: str | None = None) -> int:
        self._cleanup_expired()
        session_ids = [
            session["id"]
            for session in self.sessions.values()
            if session.get("user_id") == user_id
            and (agent_name is None or session.get("agent_name") == agent_name)
            and not session.get("messages")
        ]
        for session_id in session_ids:
            self._delete_session(session_id)
        return len(session_ids)

    def get_or_create_session(self, session_id: str | None, user_id: str, agent_name: str) -> dict[str, Any]:
        self._cleanup_expired()
        resolved_id = session_id or f"sess_{os.urandom(16).hex()}"
        if resolved_id in self.sessions:
            session = self.sessions[resolved_id]
            if session.get("user_id") != user_id:
                raise SessionOwnershipError(resolved_id)
            session["updated_at"] = _now_iso()
            return session

        self._conversation_seq += 1
        now = _now_iso()
        session = {
            "id": resolved_id,
            "conversation_id": self._conversation_seq,
            "user_id": user_id,
            "agent_name": agent_name,
            "title": "새 대화",
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "last_result_id": "",
            "pending_continuation": None,
            "message_count": 0,
        }
        self.sessions[resolved_id] = session
        self._prune_sessions_for_user(user_id)
        return session

    def get_session(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        self._cleanup_expired()
        session = self.sessions.get(session_id)
        if not session or session.get("user_id") != user_id:
            return None
        self._prune_messages(session)
        session["message_count"] = len(session.get("messages", []))
        return session

    def list_sessions(self, user_id: str, agent_name: str | None = None) -> list[dict[str, Any]]:
        self._cleanup_expired()
        self._prune_sessions_for_user(user_id)
        sessions = [
            session
            for session in self.sessions.values()
            if session.get("user_id") == user_id and (agent_name is None or session.get("agent_name") == agent_name)
        ]
        return sorted(sessions, key=lambda item: item.get("updated_at", ""), reverse=True)

    def append_message(self, session: dict[str, Any], message: dict[str, Any]) -> None:
        session.setdefault("messages", []).append(dict(message))
        if message.get("role") == "user" and session.get("title") == "새 대화" and message.get("content"):
            session["title"] = str(message.get("content", ""))[:40]
        session["updated_at"] = _now_iso()
        self._prune_messages(session)
        session["message_count"] = len(session.get("messages", []))
        self.sessions[session["id"]] = session

    def update_session_state(
        self,
        session: dict[str, Any],
        *,
        last_result_id: str | None = None,
        pending_continuation: dict[str, Any] | None | object = ...,
    ) -> None:
        if last_result_id is not None:
            session["last_result_id"] = last_result_id
        if pending_continuation is not ...:
            session["pending_continuation"] = pending_continuation
        session["updated_at"] = _now_iso()
        self.sessions[session["id"]] = session
        self._cleanup_expired()

    def save_result(self, result_id: str, session: dict[str, Any], payload: dict[str, Any]) -> None:
        self._cleanup_expired()
        self.results[result_id] = {
            "user_id": session.get("user_id"),
            "session_id": session.get("id"),
            "payload": _jsonable(payload),
            "created_at": _now_iso(),
        }

    def get_result(self, result_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        self._cleanup_expired()
        record = self.results.get(result_id)
        if not record:
            return None
        if user_id is not None and record.get("user_id") != user_id:
            return None
        return dict(record.get("payload") or {})

    def save_file(self, token: str, path: str, session: dict[str, Any] | None = None, user_id: str | None = None) -> None:
        self.files[token] = {
            "path": path,
            "session_id": (session or {}).get("id"),
            "user_id": user_id or (session or {}).get("user_id"),
            "created_at": _now_iso(),
        }
        self._cleanup_expired()

    def get_file(self, token: str) -> str | None:
        self._cleanup_expired()
        record = self.files.get(token)
        return str(record.get("path")) if record else None

    def list_saved_queries(self, user_id: str, agent_name: str | None = None) -> list[dict[str, Any]]:
        records = [
            record
            for record in self.saved_queries.values()
            if record.get("user_id") == user_id and (agent_name is None or record.get("agent_name") == agent_name)
        ]
        return [dict(record) for record in sorted(records, key=lambda item: item.get("updated_at", ""), reverse=True)]

    def get_saved_query(self, query_id: str, user_id: str) -> dict[str, Any] | None:
        record = self.saved_queries.get(query_id)
        if not record or record.get("user_id") != user_id:
            return None
        return dict(record)

    def save_saved_query(self, user_id: str, agent_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        query_id = str(payload.get("id") or f"sq_{os.urandom(16).hex()}")
        existing = self.saved_queries.get(query_id)
        if existing and existing.get("user_id") != user_id:
            raise SessionOwnershipError(query_id)
        now = _now_iso()
        record = {
            "id": query_id,
            "user_id": user_id,
            "agent_name": agent_name,
            "name": str(payload.get("name") or "저장 쿼리"),
            "description": str(payload.get("description") or ""),
            "query_template": str(payload.get("query_template") or ""),
            "parameters": _jsonable(payload.get("parameters") or []),
            "defaults": _jsonable(payload.get("defaults") or {}),
            "created_at": existing.get("created_at") if existing else now,
            "updated_at": now,
        }
        self.saved_queries[query_id] = record
        return dict(record)

    def delete_saved_query(self, query_id: str, user_id: str) -> bool:
        record = self.saved_queries.get(query_id)
        if not record or record.get("user_id") != user_id:
            return False
        self.saved_queries.pop(query_id, None)
        return True


class PostgresSessionStore:
    kind = "postgres"

    def __init__(self, policy: RetentionPolicy | None = None) -> None:
        self.policy = policy or RetentionPolicy()
        self.cache = RedisJsonCache()
        self.schema = _session_postgres_schema()
        self.delete_enabled = _env("WEBAPP_POSTGRES_DELETE_ENABLED", default="false").strip().lower() in {"1", "true", "yes", "on"}
        self._pool = None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.closeall()

    def status(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "persistent": True,
            "schema": self.schema or "default",
            "redis": self.cache.status(),
            "retention": self.policy.status(),
        }

    def _get_pool(self):
        import psycopg2.pool
        from . import config as agent_config

        if self._pool is None or self._pool.closed:
            if agent_config.DB_DSN_ERROR:
                raise RuntimeError("PostgreSQL 접속 정보를 Secrets Manager에서 불러오지 못했습니다.")
            dsn = _env("WEBAPP_POSTGRES_DSN", "SESSION_POSTGRES_DSN", "DATABASE_URL", "DB_DSN", "POSTGRES_DSN", "KBCARD_POSTGRES_DSN")
            maxconn = int(_env("WEBAPP_DB_POOL_MAX", "DB_POOL_MAX", default=str(agent_config.DB_POOL_MAX)))
            if dsn:
                self._pool = psycopg2.pool.ThreadedConnectionPool(1, maxconn, dsn)
            else:
                self._pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=maxconn,
                    host=agent_config.DB_HOST,
                    port=agent_config.DB_PORT,
                    dbname=agent_config.DB_NAME,
                    user=agent_config.DB_USER,
                    password=agent_config.DB_PASSWORD,
                )
        return self._pool

    def _conn(self):
        conn = self._get_pool().getconn()
        try:
            from psycopg2 import sql

            with conn.cursor() as cur:
                # DB 서버가 UTC여도 now()와 컬럼 default가 한국시간으로 기록되게 한다.
                cur.execute("SET TIME ZONE 'Asia/Seoul'")
                if self.schema:
                    cur.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(self.schema)))
                    cur.execute("SELECT current_schema()")
                    current_schema = cur.fetchone()[0]
                    if current_schema != self.schema:
                        raise RuntimeError(f"PostgreSQL schema '{self.schema}' is not available for web session storage")
            return conn
        except Exception:
            try:
                conn.rollback()
            finally:
                self._get_pool().putconn(conn)
            raise

    def _put(self, conn) -> None:
        try:
            if not conn.closed:
                conn.rollback()
            self._get_pool().putconn(conn)
        except Exception:
            pass

    def _delete_expired_rows(self, cur) -> None:
        if not self.delete_enabled:
            return
        if self.policy.result_retention_days > 0:
            cur.execute(
                "DELETE FROM webapp_results WHERE created_at < now() - (%s * INTERVAL '1 day')",
                (self.policy.result_retention_days,),
            )
        if self.policy.file_retention_days > 0:
            cur.execute(
                "DELETE FROM webapp_files WHERE created_at < now() - (%s * INTERVAL '1 day')",
                (self.policy.file_retention_days,),
            )
        if self.policy.session_retention_days > 0:
            cur.execute(
                "DELETE FROM webapp_sessions WHERE updated_at < now() - (%s * INTERVAL '1 day')",
                (self.policy.session_retention_days,),
            )

    def _prune_sessions_for_user(self, cur, user_id: str) -> None:
        if not self.delete_enabled or self.policy.max_sessions_per_user <= 0:
            return
        cur.execute(
            """
            DELETE FROM webapp_sessions
            WHERE id IN (
                SELECT id
                FROM (
                    SELECT id, row_number() OVER (ORDER BY updated_at DESC, conversation_id DESC) AS rn
                    FROM webapp_sessions
                    WHERE user_id = %s
                ) ranked
                WHERE ranked.rn > %s
            )
            """,
            (user_id, self.policy.max_sessions_per_user),
        )

    def _prune_messages_for_session(self, cur, session_id: str) -> None:
        if not self.delete_enabled or self.policy.max_messages_per_session <= 0:
            return
        cur.execute(
            """
            DELETE FROM webapp_messages
            WHERE session_id = %s
              AND id NOT IN (
                  SELECT id
                  FROM webapp_messages
                  WHERE session_id = %s
                  ORDER BY id DESC
                  LIMIT %s
              )
            """,
            (session_id, session_id, self.policy.max_messages_per_session),
        )

    def _session_is_expired(self, session: dict[str, Any]) -> bool:
        return _is_expired(session.get("updated_at"), self.policy.session_retention_days)

    def _row_to_session(self, row: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        message_count = int(row.get("message_count") or (len(messages) if messages is not None else 0))
        return {
            "id": row["id"],
            "conversation_id": row.get("conversation_id"),
            "user_id": row.get("user_id", ""),
            "agent_name": row.get("agent_name", "corporate_sales"),
            "title": row.get("title") or "새 대화",
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at")),
            "last_result_id": row.get("last_result_id") or "",
            "pending_continuation": row.get("pending_continuation"),
            "messages": messages or [],
            "message_count": message_count,
        }

    def _row_to_message(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = dict(row.get("payload") or {})
        message = {
            "role": row.get("role", ""),
            "content": row.get("content", ""),
            "text": row.get("content", ""),
            "created_at": _iso(row.get("created_at")),
        }
        message.update(payload)
        return message

    def _row_to_saved_query(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row.get("user_id", ""),
            "agent_name": row.get("agent_name", "corporate_sales"),
            "name": row.get("name") or "저장 쿼리",
            "description": row.get("description") or "",
            "query_template": row.get("query_template") or "",
            "parameters": _jsonable(row.get("parameters") or []),
            "defaults": _jsonable(row.get("defaults") or {}),
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at")),
        }

    def delete_session(self, session_id: str, user_id: str) -> bool:
        if not self.delete_enabled:
            return False
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM webapp_files WHERE session_id = %s AND user_id = %s", (session_id, user_id))
            cur.execute("DELETE FROM webapp_sessions WHERE id = %s AND user_id = %s RETURNING id", (session_id, user_id))
            deleted = cur.fetchone() is not None
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def prune_empty_sessions(self, user_id: str, agent_name: str | None = None) -> int:
        if not self.delete_enabled:
            return 0
        conn = self._conn()
        try:
            cur = conn.cursor()
            self._delete_expired_rows(cur)
            params: list[Any] = [user_id]
            agent_filter = ""
            if agent_name:
                agent_filter = "AND s.agent_name = %s"
                params.append(agent_name)
            cur.execute(
                f"""
                DELETE FROM webapp_files
                WHERE session_id IN (
                    SELECT s.id
                    FROM webapp_sessions s
                    LEFT JOIN webapp_messages m ON m.session_id = s.id
                    WHERE s.user_id = %s {agent_filter}
                    GROUP BY s.id
                    HAVING count(m.id) = 0
                )
                """,
                tuple(params),
            )
            cur.execute(
                f"""
                DELETE FROM webapp_sessions s
                WHERE s.id IN (
                    SELECT s2.id
                    FROM webapp_sessions s2
                    LEFT JOIN webapp_messages m ON m.session_id = s2.id
                    WHERE s2.user_id = %s {agent_filter.replace("s.", "s2.")}
                    GROUP BY s2.id
                    HAVING count(m.id) = 0
                )
                RETURNING s.id
                """,
                tuple(params),
            )
            deleted = cur.fetchall()
            conn.commit()
            return len(deleted)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def get_or_create_session(self, session_id: str | None, user_id: str, agent_name: str) -> dict[str, Any]:
        resolved_id = session_id or f"sess_{os.urandom(16).hex()}"
        conn = self._conn()
        try:
            from psycopg2.extras import RealDictCursor

            cur = conn.cursor(cursor_factory=RealDictCursor)
            self._delete_expired_rows(cur)
            cur.execute("SELECT * FROM webapp_sessions WHERE id = %s", (resolved_id,))
            row = cur.fetchone()
            if row:
                if row["user_id"] != user_id:
                    raise SessionOwnershipError(resolved_id)
                cur.execute("UPDATE webapp_sessions SET updated_at = now() WHERE id = %s", (resolved_id,))
                self._prune_messages_for_session(cur, resolved_id)
                cur.execute(
                    """
                    SELECT role, content, payload, message_id, created_at
                    FROM webapp_messages
                    WHERE session_id = %s AND user_id = %s
                    ORDER BY id
                    """,
                    (resolved_id, user_id),
                )
                messages = [self._row_to_message(dict(message_row)) for message_row in cur.fetchall()]
                conn.commit()
                row["updated_at"] = _now()
                row["message_count"] = len(messages)
                return self._row_to_session(dict(row), messages)

            cur.execute(
                """
                INSERT INTO webapp_sessions (id, user_id, agent_name)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (resolved_id, user_id, agent_name),
            )
            row = dict(cur.fetchone())
            self._prune_sessions_for_user(cur, user_id)
            conn.commit()
            return self._row_to_session(row, [])
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def get_session(self, session_id: str, user_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            from psycopg2.extras import RealDictCursor

            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(
                """
                SELECT s.*,
                       (SELECT count(*) FROM webapp_messages m WHERE m.session_id = s.id) AS message_count
                FROM webapp_sessions s
                WHERE s.id = %s AND s.user_id = %s
                """,
                (session_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                return None
            if self._session_is_expired(dict(row)):
                return None
            cur.execute(
                """
                SELECT role, content, payload, message_id, created_at
                FROM webapp_messages
                WHERE session_id = %s AND user_id = %s
                ORDER BY id
                """,
                (session_id, user_id),
            )
            messages = [self._row_to_message(dict(message_row)) for message_row in cur.fetchall()]
            return self._row_to_session(dict(row), messages)
        finally:
            self._put(conn)

    def list_sessions(self, user_id: str, agent_name: str | None = None) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            from psycopg2.extras import RealDictCursor

            cur = conn.cursor(cursor_factory=RealDictCursor)
            if agent_name:
                cur.execute(
                    """
                    SELECT s.*,
                           (SELECT count(*) FROM webapp_messages m WHERE m.session_id = s.id) AS message_count
                    FROM webapp_sessions s
                    WHERE s.user_id = %s AND s.agent_name = %s
                    ORDER BY s.updated_at DESC
                    LIMIT 200
                    """,
                    (user_id, agent_name),
                )
            else:
                cur.execute(
                    """
                    SELECT s.*,
                           (SELECT count(*) FROM webapp_messages m WHERE m.session_id = s.id) AS message_count
                    FROM webapp_sessions s
                    WHERE s.user_id = %s
                    ORDER BY s.updated_at DESC
                    LIMIT 200
                    """,
                    (user_id,),
                )
            sessions = [self._row_to_session(dict(row), []) for row in cur.fetchall() if not self._session_is_expired(dict(row))]
            if self.policy.max_sessions_per_user > 0:
                return sessions[: self.policy.max_sessions_per_user]
            return sessions
        finally:
            self._put(conn)

    def append_message(self, session: dict[str, Any], message: dict[str, Any]) -> None:
        payload = {k: _jsonable(v) for k, v in message.items() if k not in {"role", "content", "text", "created_at"} and v is not None}
        created_at = message.get("created_at") or _now_iso()
        try:
            stored_content = mask_pii(message.get("content", ""))
        except Exception:
            # 마스커가 죽어도 대화 저장까지 막지는 않는다.
            stored_content = message.get("content", "")
        # 제목은 마스킹한 본문에서 잘라야 40자에서 끊긴 번호가 그대로 남지 않는다.
        title = session.get("title", "새 대화")
        if message.get("role") == "user" and title == "새 대화" and stored_content:
            title = stored_content[:40]

        conn = self._conn()
        try:
            from psycopg2.extras import Json

            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO webapp_messages (session_id, user_id, role, content, payload, message_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session["id"],
                    session["user_id"],
                    message.get("role", ""),
                    stored_content,
                    Json(payload),
                    message.get("message_id"),
                    created_at,
                ),
            )
            cur.execute(
                """
                UPDATE webapp_sessions
                SET title = %s, updated_at = now()
                WHERE id = %s AND user_id = %s
                """,
                (title, session["id"], session["user_id"]),
            )
            self._prune_messages_for_session(cur, session["id"])
            self._delete_expired_rows(cur)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

        session.setdefault("messages", []).append(dict(message))
        session["title"] = title
        session["updated_at"] = _now_iso()
        if self.policy.max_messages_per_session > 0:
            session["messages"] = session["messages"][-self.policy.max_messages_per_session :]
        session["message_count"] = len(session.get("messages", []))

    def update_session_state(
        self,
        session: dict[str, Any],
        *,
        last_result_id: str | None = None,
        pending_continuation: dict[str, Any] | None | object = ...,
    ) -> None:
        if last_result_id is not None:
            session["last_result_id"] = last_result_id
        if pending_continuation is not ...:
            session["pending_continuation"] = pending_continuation

        conn = self._conn()
        try:
            from psycopg2.extras import Json

            cur = conn.cursor()
            cur.execute(
                """
                UPDATE webapp_sessions
                SET last_result_id = %s,
                    pending_continuation = %s,
                    updated_at = now()
                WHERE id = %s AND user_id = %s
                """,
                (
                    session.get("last_result_id", ""),
                    Json(_jsonable(session.get("pending_continuation"))) if session.get("pending_continuation") is not None else None,
                    session["id"],
                    session["user_id"],
                ),
            )
            self._delete_expired_rows(cur)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)
        session["updated_at"] = _now_iso()

    def save_result(self, result_id: str, session: dict[str, Any], payload: dict[str, Any]) -> None:
        record = {"user_id": session.get("user_id"), "session_id": session.get("id"), "payload": _jsonable(payload), "created_at": _now_iso()}
        conn = self._conn()
        try:
            from psycopg2.extras import Json

            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO webapp_results (result_id, session_id, user_id, payload)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (result_id)
                DO UPDATE SET payload = EXCLUDED.payload, created_at = now()
                """,
                (result_id, session["id"], session["user_id"], Json(record["payload"])),
            )
            self._delete_expired_rows(cur)
            conn.commit()
            self.cache.set_json(f"result:{result_id}", record)
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def get_result(self, result_id: str, user_id: str | None = None) -> dict[str, Any] | None:
        cached = self.cache.get_json(f"result:{result_id}")
        if cached and (user_id is None or cached.get("user_id") == user_id):
            if _is_expired(cached.get("created_at"), self.policy.result_retention_days):
                return None
            return dict(cached.get("payload") or {})

        conn = self._conn()
        try:
            from psycopg2.extras import RealDictCursor

            cur = conn.cursor(cursor_factory=RealDictCursor)
            if user_id is None:
                cur.execute("SELECT session_id, user_id, payload, created_at FROM webapp_results WHERE result_id = %s", (result_id,))
            else:
                cur.execute(
                    "SELECT session_id, user_id, payload, created_at FROM webapp_results WHERE result_id = %s AND user_id = %s",
                    (result_id, user_id),
                )
            row = cur.fetchone()
            if not row:
                return None
            if _is_expired(row.get("created_at"), self.policy.result_retention_days):
                return None
            record = {"session_id": row["session_id"], "user_id": row["user_id"], "payload": row["payload"], "created_at": _iso(row.get("created_at"))}
            self.cache.set_json(f"result:{result_id}", record)
            return dict(row["payload"] or {})
        finally:
            self._put(conn)

    def save_file(self, token: str, path: str, session: dict[str, Any] | None = None, user_id: str | None = None) -> None:
        resolved_user_id = user_id or (session or {}).get("user_id")
        session_id = (session or {}).get("id")
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO webapp_files (token, path, session_id, user_id)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (token)
                DO UPDATE SET path = EXCLUDED.path, session_id = EXCLUDED.session_id, user_id = EXCLUDED.user_id, created_at = now()
                """,
                (token, path, session_id, resolved_user_id),
            )
            self._delete_expired_rows(cur)
            conn.commit()
            self.cache.set_json(f"file:{token}", {"path": path, "session_id": session_id, "user_id": resolved_user_id, "created_at": _now_iso()})
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def get_file(self, token: str) -> str | None:
        cached = self.cache.get_json(f"file:{token}")
        if cached and cached.get("path"):
            if _is_expired(cached.get("created_at"), self.policy.file_retention_days):
                return None
            return str(cached["path"])

        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT path, session_id, user_id, created_at FROM webapp_files WHERE token = %s", (token,))
            row = cur.fetchone()
            if not row:
                return None
            path, session_id, user_id, created_at = row
            if _is_expired(created_at, self.policy.file_retention_days):
                return None
            self.cache.set_json(f"file:{token}", {"path": path, "session_id": session_id, "user_id": user_id, "created_at": _iso(created_at)})
            return str(path)
        finally:
            self._put(conn)

    def list_saved_queries(self, user_id: str, agent_name: str | None = None) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            from psycopg2.extras import RealDictCursor

            cur = conn.cursor(cursor_factory=RealDictCursor)
            if agent_name:
                cur.execute(
                    """
                    SELECT *
                    FROM webapp_saved_queries
                    WHERE user_id = %s AND agent_name = %s
                    ORDER BY updated_at DESC
                    LIMIT 200
                    """,
                    (user_id, agent_name),
                )
            else:
                cur.execute(
                    """
                    SELECT *
                    FROM webapp_saved_queries
                    WHERE user_id = %s
                    ORDER BY updated_at DESC
                    LIMIT 200
                    """,
                    (user_id,),
                )
            return [self._row_to_saved_query(dict(row)) for row in cur.fetchall()]
        finally:
            self._put(conn)

    def get_saved_query(self, query_id: str, user_id: str) -> dict[str, Any] | None:
        conn = self._conn()
        try:
            from psycopg2.extras import RealDictCursor

            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT * FROM webapp_saved_queries WHERE id = %s AND user_id = %s", (query_id, user_id))
            row = cur.fetchone()
            return self._row_to_saved_query(dict(row)) if row else None
        finally:
            self._put(conn)

    def save_saved_query(self, user_id: str, agent_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        query_id = str(payload.get("id") or f"sq_{os.urandom(16).hex()}")
        conn = self._conn()
        try:
            from psycopg2.extras import Json, RealDictCursor

            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute(
                """
                INSERT INTO webapp_saved_queries (
                    id, user_id, agent_name, name, description, query_template, parameters, defaults
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id)
                DO UPDATE SET
                    name = EXCLUDED.name,
                    description = EXCLUDED.description,
                    query_template = EXCLUDED.query_template,
                    parameters = EXCLUDED.parameters,
                    defaults = EXCLUDED.defaults,
                    updated_at = now()
                WHERE webapp_saved_queries.user_id = EXCLUDED.user_id
                RETURNING *
                """,
                (
                    query_id,
                    user_id,
                    agent_name,
                    str(payload.get("name") or "저장 쿼리"),
                    str(payload.get("description") or ""),
                    str(payload.get("query_template") or ""),
                    Json(_jsonable(payload.get("parameters") or [])),
                    Json(_jsonable(payload.get("defaults") or {})),
                ),
            )
            row = cur.fetchone()
            if not row:
                raise SessionOwnershipError(query_id)
            conn.commit()
            return self._row_to_saved_query(dict(row))
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)

    def delete_saved_query(self, query_id: str, user_id: str) -> bool:
        if not self.delete_enabled:
            return False
        conn = self._conn()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM webapp_saved_queries WHERE id = %s AND user_id = %s RETURNING id", (query_id, user_id))
            deleted = cur.fetchone() is not None
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            self._put(conn)


def _postgres_configured() -> bool:
    names = ("WEBAPP_POSTGRES_DSN", "SESSION_POSTGRES_DSN", "DATABASE_URL", "DB_DSN", "POSTGRES_DSN", "KBCARD_POSTGRES_DSN")
    return any(os.getenv(name) for name in names)


def create_session_store():
    mode = _env("WEBAPP_SESSION_STORE", "SESSION_STORE", default="auto").strip().lower()
    if mode == "memory":
        return InMemorySessionStore()
    if mode == "postgres":
        return PostgresSessionStore()
    if mode == "auto":
        return PostgresSessionStore() if _postgres_configured() else InMemorySessionStore()
    raise ValueError("WEBAPP_SESSION_STORE must be one of: auto, postgres, memory")

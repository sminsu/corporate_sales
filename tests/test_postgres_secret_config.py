from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import web_service
from text2sql_agent import db


ROOT = Path(__file__).resolve().parents[1]


def test_athena_with_postgres_sessions_loads_connection_fields_from_secret(tmp_path: Path) -> None:
    fake_boto3 = tmp_path / "boto3.py"
    fake_boto3.write_text(
        """
class Client:
    def get_secret_value(self, **kwargs):
        assert kwargs == {"SecretId": "keyscr-aihub-dev-ane2-agentifo"}
        return {"SecretString": '{"POSTGRES_HOST":"db.example","POSTGRES_PORT":"5433","POSTGRES_DB":"sales","POSTGRES_USER":"app","POSTGRES_PASSWORD":"secret"}'}

class Session:
    def client(self, **kwargs):
        assert kwargs == {"service_name": "secretsmanager", "region_name": "ap-northeast-2"}
        return Client()

class session:
    Session = Session
""",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "DB_BACKEND": "athena",
        "WEBAPP_SESSION_STORE": "postgres",
        "PYTHONPATH": f"{tmp_path}{os.pathsep}{ROOT}",
    }
    for name in ("AWS_EXECUTION_ENV", "DATABASE_URL", "DB_DSN", "POSTGRES_DSN", "KBCARD_POSTGRES_DSN"):
        env.pop(name, None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from text2sql_agent.config import "
                "DB_DSN,DB_DSN_SOURCE,DB_HOST,DB_PORT,DB_NAME,DB_USER,DB_PASSWORD;"
                "print('|'.join((DB_HOST,str(DB_PORT),DB_NAME,DB_USER,str(bool(DB_PASSWORD)),"
                "DB_DSN_SOURCE,str(bool(DB_DSN)))))"
            ),
        ],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "db.example|5433|sales|app|True|secrets_manager|False"


def test_database_probe_reports_success_without_exposing_failures() -> None:
    with (
        patch.object(db, "DB_BACKEND", "athena"),
        patch.object(db, "SESSION_STORE_BACKEND", "postgres"),
        patch.object(db, "DB_DSN_ERROR", ""),
        patch.object(db, "DB_DSN_SOURCE", "secrets_manager"),
        patch.object(db, "_execute_postgres", return_value=(["?column?"], [(1,)])),
    ):
        assert db.probe_database() == {
            "database_ready": True,
            "database_status": "ready",
            "database_source": "secrets_manager",
        }

    with (
        patch.object(db, "DB_BACKEND", "athena"),
        patch.object(db, "SESSION_STORE_BACKEND", "postgres"),
        patch.object(db, "DB_DSN_ERROR", ""),
        patch.object(db, "DB_DSN_SOURCE", "secrets_manager"),
        patch.object(db, "_execute_postgres", side_effect=RuntimeError("password=do-not-expose")),
    ):
        result = db.probe_database()

    assert result == {
        "database_ready": False,
        "database_status": "unavailable",
        "database_source": "secrets_manager",
        "database_error_type": "RuntimeError",
    }


def test_postgres_pool_uses_individual_connection_fields() -> None:
    with (
        patch.object(db, "_pool", None),
        patch.object(db, "DB_DSN", ""),
        patch.object(db, "DB_DSN_ERROR", ""),
        patch.object(db, "DB_HOST", "db.example"),
        patch.object(db, "DB_PORT", 5433),
        patch.object(db, "DB_NAME", "sales"),
        patch.object(db, "DB_USER", "app"),
        patch.object(db, "DB_PASSWORD", "secret"),
        patch("psycopg2.pool.ThreadedConnectionPool") as pool_class,
    ):
        db._get_pool()

    pool_class.assert_called_once_with(
        minconn=1,
        maxconn=db.DB_POOL_MAX,
        host="db.example",
        port=5433,
        dbname="sales",
        user="app",
        password="secret",
    )


def test_startup_logs_database_connection_status() -> None:
    events: list[tuple[int, str, dict[str, object]]] = []

    def capture(level: int, event: str, **fields: object) -> None:
        events.append((level, event, fields))

    async def run_lifespan() -> None:
        async with web_service._lifespan(web_service.app):
            pass

    with (
        patch.object(
            web_service.agent_db,
            "probe_database",
            return_value={
                "database_ready": True,
                "database_status": "ready",
                "database_source": "secrets_manager",
            },
        ),
        patch.object(web_service, "_stream_log", side_effect=capture),
        patch.object(web_service.agent, "close_common_clients"),
        patch.object(web_service._SESSION_STORE, "close"),
    ):
        asyncio.run(run_lifespan())

    _, _, fields = next(item for item in events if item[1] == "database_connection_check")
    assert fields == {
        "database_ready": True,
        "database_status": "ready",
        "database_source": "secrets_manager",
        "database_error_type": None,
    }

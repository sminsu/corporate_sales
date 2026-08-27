from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from text2sql_agent import config as agent_config
from text2sql_agent import session_store


ROOT = Path(__file__).resolve().parents[1]


def test_postgres_session_store_loads_connection_fields_from_secret(tmp_path: Path) -> None:
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


def test_session_pool_uses_individual_connection_fields() -> None:
    dsn_names = {
        name: ""
        for name in (
            "WEBAPP_POSTGRES_DSN",
            "SESSION_POSTGRES_DSN",
            "DATABASE_URL",
            "DB_DSN",
            "POSTGRES_DSN",
            "KBCARD_POSTGRES_DSN",
            "WEBAPP_DB_POOL_MAX",
            "DB_POOL_MAX",
        )
    }
    store = session_store.PostgresSessionStore()
    with (
        patch.dict(os.environ, dsn_names),
        patch.object(agent_config, "DB_DSN_ERROR", ""),
        patch.object(agent_config, "DB_HOST", "db.example"),
        patch.object(agent_config, "DB_PORT", 5433),
        patch.object(agent_config, "DB_NAME", "sales"),
        patch.object(agent_config, "DB_USER", "app"),
        patch.object(agent_config, "DB_PASSWORD", "secret"),
        patch("psycopg2.pool.ThreadedConnectionPool") as pool_class,
    ):
        store._get_pool()

    pool_class.assert_called_once_with(
        minconn=1,
        maxconn=agent_config.DB_POOL_MAX,
        host="db.example",
        port=5433,
        dbname="sales",
        user="app",
        password="secret",
    )

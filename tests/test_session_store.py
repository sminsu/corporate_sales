from __future__ import annotations

import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch


SESSION_STORE_PATH = Path(__file__).resolve().parents[1] / "text2sql_agent" / "session_store.py"
spec = spec_from_file_location("session_store_module", SESSION_STORE_PATH)
assert spec and spec.loader
session_store_module = module_from_spec(spec)
spec.loader.exec_module(session_store_module)

InMemorySessionStore = session_store_module.InMemorySessionStore
PostgresSessionStore = session_store_module.PostgresSessionStore
RetentionPolicy = session_store_module.RetentionPolicy
SessionOwnershipError = session_store_module.SessionOwnershipError


class SessionStoreTest(unittest.TestCase):
    def test_postgres_status_creates_missing_schema_and_tables(self) -> None:
        statements: list[str] = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def execute(self, query, *_):
                statements.append(str(query))

            def fetchone(self):
                return ("corporate_sales",)

            def close(self):
                return None

        class Connection:
            closed = False

            def cursor(self, **_):
                return Cursor()

            def commit(self):
                return None

            def rollback(self):
                return None

        class Pool:
            closed = False

            def __init__(self):
                self.connection = Connection()

            def getconn(self):
                return self.connection

            def putconn(self, *_):
                return None

        store = PostgresSessionStore()
        pool = Pool()

        with patch.object(store, "_get_pool", return_value=pool):
            status = store.status()

        rendered = "\n".join(statements)
        self.assertEqual(status["schema"], "corporate_sales")
        self.assertIn("CREATE SCHEMA IF NOT EXISTS", rendered)
        self.assertIn("CREATE TABLE IF NOT EXISTS webapp_sessions", rendered)
        self.assertIn("CREATE TABLE IF NOT EXISTS webapp_saved_queries", rendered)

    def test_sessions_are_scoped_by_user(self) -> None:
        store = InMemorySessionStore()
        session = store.get_or_create_session(None, "user-a", "manual")

        with self.assertRaises(SessionOwnershipError):
            store.get_or_create_session(session["id"], "user-b", "manual")

        self.assertEqual(store.get_session(session["id"], "user-b"), None)
        self.assertEqual([item["id"] for item in store.list_sessions("user-a")], [session["id"]])
        self.assertEqual(store.list_sessions("user-b"), [])

    def test_messages_update_summary_and_history(self) -> None:
        store = InMemorySessionStore()
        session = store.get_or_create_session(None, "user-a", "manual")

        store.append_message(session, {"role": "user", "content": "2025년 매출 보여줘", "text": "2025년 매출 보여줘"})
        store.append_message(session, {"role": "assistant", "content": "답변", "text": "답변", "result_id": "r1"})

        loaded = store.get_session(session["id"], "user-a")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["title"], "2025년 매출 보여줘")
        self.assertEqual(loaded["message_count"], 2)
        self.assertEqual([message["role"] for message in loaded["messages"]], ["user", "assistant"])

    def test_result_and_file_records_keep_user_ownership(self) -> None:
        store = InMemorySessionStore()
        session = store.get_or_create_session(None, "user-a", "manual")

        store.save_result("r1", session, {"answer": "답변", "query_rows": [(1,)]})
        store.save_file("t1", "/tmp/report.txt", session=session)

        self.assertEqual(store.get_result("r1", "user-a")["query_rows"], [[1]])
        self.assertIsNone(store.get_result("r1", "user-b"))
        self.assertEqual(store.get_file("t1"), "/tmp/report.txt")

    def test_message_retention_keeps_recent_messages_only(self) -> None:
        store = InMemorySessionStore(policy=RetentionPolicy(max_messages_per_session=2, max_sessions_per_user=0, result_retention_days=0, file_retention_days=0, session_retention_days=0))
        session = store.get_or_create_session(None, "user-a", "manual")

        for idx in range(4):
            store.append_message(session, {"role": "user", "content": f"m{idx}", "text": f"m{idx}"})

        loaded = store.get_session(session["id"], "user-a")

        self.assertIsNotNone(loaded)
        self.assertEqual([message["content"] for message in loaded["messages"]], ["m2", "m3"])
        self.assertEqual(loaded["message_count"], 2)

    def test_session_retention_keeps_recent_sessions_only(self) -> None:
        store = InMemorySessionStore(policy=RetentionPolicy(max_sessions_per_user=2, max_messages_per_session=0, result_retention_days=0, file_retention_days=0, session_retention_days=0))

        first = store.get_or_create_session("sess-1", "user-a", "manual")
        second = store.get_or_create_session("sess-2", "user-a", "manual")
        third = store.get_or_create_session("sess-3", "user-a", "manual")

        sessions = store.list_sessions("user-a")

        self.assertEqual({session["id"] for session in sessions}, {second["id"], third["id"]})
        self.assertIsNone(store.get_session(first["id"], "user-a"))

    def test_result_and_file_retention_expire_old_records(self) -> None:
        store = InMemorySessionStore(policy=RetentionPolicy(max_sessions_per_user=0, max_messages_per_session=0, result_retention_days=1, file_retention_days=1, session_retention_days=0))
        session = store.get_or_create_session(None, "user-a", "manual")

        store.save_result("r1", session, {"answer": "답변"})
        store.save_file("t1", "/tmp/report.txt", session=session)
        store.results["r1"]["created_at"] = "2000-01-01T00:00:00"
        store.files["t1"]["created_at"] = "2000-01-01T00:00:00"

        self.assertIsNone(store.get_result("r1", "user-a"))
        self.assertIsNone(store.get_file("t1"))


if __name__ == "__main__":
    unittest.main()

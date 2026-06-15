from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from text2sql_agent import common_services


class FakeKBCardLogger:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def _log(self, level: str, message: str, **metadata: Any) -> dict[str, Any]:
        payload = {"level": level, "message": message, "metadata": metadata}
        self.records.append(payload)
        return payload


class CommonServicesLoggingTest(unittest.TestCase):
    def test_emit_execution_log_returns_common_logger_payload(self) -> None:
        logger = FakeKBCardLogger()
        context = common_services.create_trace_context(
            session_id="sess-1",
            message_id=123,
            request_id="req-1",
            trace_id="trace-1",
        )

        with patch.object(common_services, "_LOGGER", logger):
            payload = common_services.emit_execution_log(
                context=context,
                user_id="user-1",
                agent_name="manual",
                status="SUCCESS",
                total_latency_ms=42,
                question="매출 알려줘",
                result={
                    "status": "complete",
                    "rows": [(1,)],
                    "columns": ["amount"],
                    "sql": "SELECT 1",
                },
            )

        self.assertEqual(payload, logger.records[0])
        self.assertEqual(payload["level"], "INFO")
        self.assertEqual(payload["message"], "agent execution completed")
        self.assertEqual(payload["metadata"]["event_type"], "agent_execution")
        self.assertEqual(payload["metadata"]["request_id"], "req-1")
        self.assertEqual(payload["metadata"]["trace_id"], "trace-1")
        self.assertEqual(payload["metadata"]["result"]["row_count"], 1)
        self.assertIs(payload["metadata"]["result"]["has_sql"], True)

    def test_emit_module_event_uses_current_observability_context(self) -> None:
        logger = FakeKBCardLogger()
        context = common_services.create_trace_context(
            session_id="sess-2",
            message_id=456,
            request_id="req-2",
            trace_id="trace-2",
        )

        with patch.object(common_services, "_LOGGER", logger):
            with common_services.observability_context(
                context=context,
                agent_name="manual",
                user_id="user-2",
            ):
                payload = common_services.emit_module_event(
                    module="db",
                    event_type="db_query",
                    status="ERROR",
                    latency_ms=7,
                    result_count=0,
                    error_message="boom",
                    log_level="ERROR",
                )

        self.assertEqual(payload, logger.records[0])
        self.assertEqual(payload["level"], "ERROR")
        self.assertEqual(payload["message"], "db db_query error")
        self.assertEqual(payload["metadata"]["agent_name"], "manual")
        self.assertEqual(payload["metadata"]["user_id"], "user-2")
        self.assertEqual(payload["metadata"]["message_id"], "456")
        self.assertEqual(payload["metadata"]["request_id"], "req-2")
        self.assertEqual(payload["metadata"]["error_message"], "boom")

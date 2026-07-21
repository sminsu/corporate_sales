from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import web_service


def _event_name(chunk: str) -> str:
    return next(line.split(":", 1)[1].strip() for line in chunk.splitlines() if line.startswith("event:"))


def _event_data(chunk: str) -> dict:
    data = next(line.split(":", 1)[1].strip() for line in chunk.splitlines() if line.startswith("data:"))
    return json.loads(data)


def test_stream_log_is_single_line_json_with_correlation_fields(capsys) -> None:
    token = web_service._STREAM_REQUEST_ID.set("request-json-1")
    try:
        web_service._stream_log(logging.INFO, "stream_test_event", message_id="message-json-1")
    finally:
        web_service._STREAM_REQUEST_ID.reset(token)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["event"] == "stream_test_event"
    assert payload["stream_request_id"] == "request-json-1"
    assert payload["message_id"] == "message-json-1"
    assert payload["app_release"] == web_service.APP_RELEASE
    assert payload["container_id"] == web_service.CONTAINER_ID


def test_observed_stream_converts_iterator_failure_to_terminal_error() -> None:
    def broken_stream():
        yield web_service._sse("response", {"message": "답변 생성 중"})
        raise RuntimeError("stream tail failed")

    chunks = list(web_service._observed_sse_stream(broken_stream(), session_id="session-1", message_id=101))

    assert [_event_name(chunk) for chunk in chunks] == ["response", "error"]
    assert _event_data(chunks[-1])["data"]["message_id"] == 101


def test_stream_query_still_emits_done_when_execution_logger_fails() -> None:
    request = web_service.CompatibleQueryRequest(query="매출 알려줘")
    session = {
        "id": "session-1",
        "user_id": "user-1",
        "messages": [],
        "title": "새 대화",
    }
    result_data = {
        "answer": "답변",
        "status": "complete",
        "result_id": "result-1",
        "rows": [[100]],
        "columns": ["amount"],
        "messages": [],
        "sql": "SELECT 100",
    }

    with (
        patch.object(web_service.agent, "create_trace_context", return_value=object()),
        patch.object(web_service, "_state_from_request", return_value={"question": request.query}),
        patch.object(web_service, "_stream_graph", return_value=iter([("generate_answer", {"answer": "답변"})])),
        patch.object(web_service, "_result_payload", return_value=dict(result_data)),
        patch.object(web_service, "_finalize_assistant_message", side_effect=lambda _session, data, _message_id: data),
        patch.object(web_service.agent, "emit_execution_log", side_effect=RuntimeError("logger unavailable")),
    ):
        chunks = list(web_service._stream_query(request, session, 102))

    assert _event_name(chunks[-1]) == "done"
    assert _event_data(chunks[-1])["data"]["answer"] == "답변"


def test_transport_middleware_logs_completed_response_and_adds_correlation_headers() -> None:
    diagnostic_events: list[tuple[str, dict]] = []
    stream_app = FastAPI()
    stream_app.add_middleware(web_service.StreamTransportDiagnosticsMiddleware)
    chunk = web_service._sse("done", {"data": {"answer": "완료"}})

    @stream_app.post("/api/query/stream")
    def stream_endpoint():
        return StreamingResponse(
            iter([chunk]),
            media_type="text/event-stream",
            headers=web_service._stream_response_headers(303),
        )

    def capture_log(_level: int, event: str, **fields: object) -> None:
        diagnostic_events.append((event, fields))

    with patch.object(web_service, "_stream_log", side_effect=capture_log), TestClient(stream_app) as client:
        response = client.post("/api/query/stream")

    assert response.status_code == 200
    assert response.text == chunk
    assert response.headers["x-stream-message-id"] == "303"
    assert response.headers["x-stream-request-id"]
    assert response.headers["x-app-release"] == web_service.APP_RELEASE

    events = {event: fields for event, fields in diagnostic_events}
    assert events["stream_http_response_started"]["message_id"] == "303"
    assert events["stream_http_response_completed"]["message_id"] == "303"
    assert events["stream_http_response_completed"]["body_bytes"] == len(chunk.encode("utf-8"))
    assert "stream_http_response_aborted" not in events


def test_transport_middleware_logs_aborted_response_after_app_exception() -> None:
    diagnostic_events: list[tuple[str, dict]] = []
    sent_messages: list[dict] = []

    async def failing_app(_scope, _receive, send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream"), (b"x-stream-message-id", b"404")],
            }
        )
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise RuntimeError("stream worker stopped")

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent_messages.append(message)

    def capture_log(_level: int, event: str, **fields: object) -> None:
        diagnostic_events.append((event, fields))

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/query/stream",
        "asgi": {"spec_version": "2.3"},
    }
    middleware = web_service.StreamTransportDiagnosticsMiddleware(failing_app)
    with patch.object(web_service, "_stream_log", side_effect=capture_log), pytest.raises(RuntimeError):
        asyncio.run(middleware(scope, receive, send))

    response_headers = dict(sent_messages[0]["headers"])
    assert response_headers[b"x-stream-request-id"]
    events = {event: fields for event, fields in diagnostic_events}
    assert events["stream_http_app_exception"]["message_id"] == "404"
    assert events["stream_http_app_exception"]["error_type"] == "RuntimeError"
    assert events["stream_http_response_aborted"]["body_bytes"] == len(b"partial")
    assert "stream_http_response_completed" not in events

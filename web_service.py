import asyncio
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web" / "static"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import text2sql_agent as agent  # noqa: E402
import webapp_compatible_api as webapp_api  # noqa: E402
from text2sql_agent import config as agent_config  # noqa: E402
from text2sql_agent.followup_ops import build_chart_spec, plan_followup  # noqa: E402
from text2sql_agent.managed_scope import (  # noqa: E402
    MANAGED_SCOPE_PARAMETER,
    MAX_MANAGED_SCOPE_UPLOAD_BYTES,
    ManagedScopeParseError,
    parse_business_number_list,
    parse_managed_scope_upload,
)
from text2sql_agent.query_frame import (  # noqa: E402
    build_query_frame,
    ensure_query_frame,
    infer_result_scope,
    query_frame_prompt,
)
from text2sql_agent.schema import SCHEMA, _extract_schema_tables, _is_semantic_table_visible  # noqa: E402
from text2sql_agent.session_store import SessionOwnershipError, create_session_store  # noqa: E402
from text2sql_agent.workflow import (  # noqa: E402
    _get_app as _get_compiled_graph,
    _parse_llm_json,
    _table_details as _bounded_table_details,
)


@asynccontextmanager
async def _lifespan(_: FastAPI):
    session_status: dict[str, Any] = {}
    try:
        session_status = _SESSION_STORE.status()
    except Exception as exc:
        _stream_log(
            logging.ERROR,
            "service_startup_status_failed",
            exc_info=True,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
    _stream_log(
        logging.INFO,
        "service_started",
        session_store_kind=session_status.get("kind", "unknown"),
        session_store_persistent=bool(session_status.get("persistent")),
        heartbeat_seconds=PROGRESS_HEARTBEAT_SECONDS,
    )
    try:
        yield
    finally:
        _stream_log(logging.INFO, "service_stopping")
        agent.close_common_clients()
        _SESSION_STORE.close()


app = FastAPI(
    title="KB Card Corporate Sales Text2SQL - WebApp API v4",
    description="Text2SQL service using kbcard-agent-common LLM and embedding modules.",
    version="4.0.0",
    lifespan=_lifespan,
)

_GRAPH = None
_SESSION_STORE = create_session_store()
_CONVERSATION_IDS: dict[str, int] = {}
_CONVERSATION_SEQ = 0
_LLM_HEALTH_CACHE: dict[str, Any] = {"checked_at": 0.0, "data": None}
_LLM_HEALTH_TTL_SECONDS = 20.0
DEFAULT_WEBAPP_USER_ID = os.getenv("WEBAPP_DEFAULT_USER_ID", "ui")
DEFAULT_WEBAPP_AGENT_NAME = os.getenv("WEBAPP_DEFAULT_AGENT_NAME", "corporate_sales")
MANAGED_SCOPE_TEMPLATE_PATH = (
    BASE_DIR / "outputs" / "managed_company_scope_20260714" / "관리기업목록_업로드_예시.xlsx"
)


NODE_LABELS = {
    "classify_question": ("question_analysis", "질문 분석", "업무 범위와 질문 유형을 분류했습니다."),
    "prepare_direct_sql": ("sql_generation", "입력 SQL 준비", "사용자가 입력한 SQL을 읽기 전용 검증 경로로 준비했습니다."),
    "route_domain": ("domain_routing", "도메인 라우팅", "질문에 맞는 업무 도메인을 선택했습니다."),
    "select_tool": ("capability_selection", "Capability 선택", "사용 가능한 Tool과 검증 쿼리 경로를 판단했습니다."),
    "check_tool_params": ("parameter_check", "파라미터 확인", "Tool 실행에 필요한 입력값을 확인했습니다."),
    "execute_tool": ("tool_execution", "Tool 실행", "선택된 Tool을 실행했습니다."),
    "run_tool_query": ("sql_execution", "SQL 실행", "Tool 기반 SQL 조회를 실행했습니다."),
    "match_verified_query": ("verified_query_matching", "검증 쿼리 매칭", "사전 검증된 쿼리와 질문을 비교했습니다."),
    "extract_and_apply_params": ("parameter_extraction", "파라미터 적용", "질문에서 추출한 조건을 쿼리에 반영했습니다."),
    "run_matched_query": ("sql_execution", "SQL 실행", "검증 쿼리를 실행했습니다."),
    "analyze_question": ("table_selection", "테이블 분석", "질문과 관련된 테이블/컬럼을 선별했습니다."),
    "check_sql_gen_params": ("parameter_check", "조건 확인", "SQL 생성 전 필수 조건을 확인했습니다."),
    "generate_sql": ("sql_generation", "SQL 생성", "질문에 맞는 SQL을 생성했습니다."),
    "validate_sql": ("sql_validation", "SQL 검증", "생성된 SQL의 안전성과 실행 가능성을 검토했습니다."),
    "run_query": ("sql_execution", "SQL 실행", "DB 조회를 실행했습니다."),
    "generate_answer": ("answer_generation", "답변 생성", "조회 결과를 요약 답변으로 정리했습니다."),
    "direct_answer": ("answer_generation", "직접 답변", "SQL 없이 직접 답변을 생성했습니다."),
    "reject_answer": ("rejected", "답변 불가", "지원 범위를 벗어난 질문으로 판단했습니다."),
    "handle_error": ("error", "오류 처리", "실행 중 발생한 오류를 정리했습니다."),
    "followup_route": ("followup_route", "후속 의도 판단", "이전 결과 기반 분석/시각화인지 새 SQL 실행인지 분류했습니다."),
}


class ConversationMessage(BaseModel):
    role: str
    content: str


class CompatibleQueryRequest(BaseModel):
    query: str
    agent_name: str | None = None
    top_k: int = 10
    session_id: str | None = None
    conversation_history: list[ConversationMessage] = Field(default_factory=list)
    params: dict[str, Any] | None = None
    continuation: dict[str, Any] | None = None
    result_id: str | None = None


class LegacyQueryRequest(BaseModel):
    question: str
    session_id: str | None = None
    params: dict[str, Any] | None = None
    continuation: dict[str, Any] | None = None


class FollowupRequest(BaseModel):
    result_id: str
    question: str
    session_id: str | None = None


class ExportRequest(BaseModel):
    result_id: str
    format: str = "all"


class SavedQueryRequest(BaseModel):
    id: str | None = None
    name: str
    query_template: str
    description: str = ""
    parameters: list[str] | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)


class SavedQueryRenderRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)


def _get_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = _get_compiled_graph()
    return _GRAPH


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


TEMPLATE_PARAM_RE = re.compile(r"{{\s*([A-Za-z0-9_.가-힣-]+)\s*}}")
SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
STREAM_EXPOSE_HEADERS = "X-Stream-Request-ID, X-Stream-Message-ID, X-App-Release"
PROGRESS_HEARTBEAT_SECONDS = 2.5
APP_RELEASE = (
    os.getenv("APP_RELEASE")
    or os.getenv("IMAGE_TAG")
    or os.getenv("GIT_SHA")
    or "unknown"
)
CONTAINER_ID = os.getenv("HOSTNAME") or os.getenv("ECS_CONTAINER_NAME") or "unknown"
_STREAM_REQUEST_ID: ContextVar[str] = ContextVar("stream_request_id", default="")


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(_jsonable(data), ensure_ascii=False)}\n\n"


def _stream_log(level: int, event: str, *, exc_info: bool = False, **fields: Any) -> None:
    """Write one searchable JSON diagnostic line to ECS stdout/stderr."""

    payload = {
        "event": event,
        "stream_request_id": _STREAM_REQUEST_ID.get(),
        "app_release": APP_RELEASE,
        "container_id": CONTAINER_ID,
        **{key: _jsonable(value) for key, value in fields.items() if value is not None},
    }
    if exc_info:
        exception_trace = traceback.format_exc()
        if exception_trace and exception_trace.strip() != "NoneType: None":
            payload["exception_trace"] = exception_trace
    payload = {key: value for key, value in payload.items() if value not in (None, "")}
    line = json.dumps(payload, ensure_ascii=False, default=str)
    try:
        output = sys.stderr if level >= logging.ERROR else sys.stdout
        print(line, file=output, flush=True)
    except Exception:
        # Keep a last-resort path even if stdout itself is replaced by a faulty wrapper.
        print(line, file=sys.stderr, flush=True)


def _stream_response_headers(message_id: int) -> dict[str, str]:
    """Expose only non-sensitive identifiers needed to correlate browser and ECS logs."""

    return {
        **SSE_HEADERS,
        "X-Stream-Message-ID": str(message_id),
        "X-App-Release": APP_RELEASE,
        "Access-Control-Expose-Headers": STREAM_EXPOSE_HEADERS,
    }


def _asgi_header(headers: list[tuple[bytes, bytes]], name: bytes) -> str:
    lowered_name = name.lower()
    for key, value in headers:
        if key.lower() == lowered_name:
            return value.decode("latin-1", errors="replace")
    return ""


class StreamTransportDiagnosticsMiddleware:
    """Record whether an SSE response completed at the ASGI transport boundary.

    Generator-level logs show whether the application prepared an SSE event.  This
    middleware adds the missing boundary: whether Starlette/Uvicorn emitted the
    final ``http.response.body`` frame.  If CloudWatch contains
    ``stream_http_response_completed`` but the browser still reports an incomplete
    chunk, the connection was truncated after the application (ALB/proxy/client).
    """

    def __init__(self, asgi_app: Any):
        self.asgi_app = asgi_app

    @staticmethod
    def _is_stream_request(scope: dict[str, Any]) -> bool:
        if scope.get("type") != "http" or str(scope.get("method") or "").upper() != "POST":
            return False
        path = str(scope.get("path") or "")
        return path.endswith("/query/stream") or path.endswith("/followup/stream")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not self._is_stream_request(scope):
            await self.asgi_app(scope, receive, send)
            return

        stream_request_id = uuid.uuid4().hex
        token = _STREAM_REQUEST_ID.set(stream_request_id)
        started = time.monotonic()
        path = str(scope.get("path") or "")
        status_code = 0
        message_id = ""
        response_started = False
        response_completed = False
        client_disconnected = False
        body_frames = 0
        body_bytes = 0
        first_body_logged = False
        failure_type = ""

        async def tracked_receive() -> dict[str, Any]:
            nonlocal client_disconnected
            message = await receive()
            if message.get("type") == "http.disconnect" and not client_disconnected:
                client_disconnected = True
                _stream_log(
                    logging.WARNING,
                    "stream_http_client_disconnected",
                    path=path,
                    status_code=status_code or None,
                    message_id=message_id or None,
                    body_frames=body_frames,
                    body_bytes=body_bytes,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            return message

        async def tracked_send(message: dict[str, Any]) -> None:
            nonlocal status_code, message_id, response_started, response_completed
            nonlocal body_frames, body_bytes, first_body_logged

            message_type = message.get("type")
            if message_type == "http.response.start":
                response_started = True
                status_code = int(message.get("status") or 0)
                headers = list(message.get("headers") or [])
                if not _asgi_header(headers, b"x-stream-request-id"):
                    headers.append((b"x-stream-request-id", stream_request_id.encode("ascii")))
                if not _asgi_header(headers, b"x-app-release"):
                    headers.append((b"x-app-release", APP_RELEASE.encode("latin-1", errors="replace")))
                if not _asgi_header(headers, b"access-control-expose-headers"):
                    headers.append((b"access-control-expose-headers", STREAM_EXPOSE_HEADERS.encode("ascii")))
                message_id = _asgi_header(headers, b"x-stream-message-id")
                content_type = _asgi_header(headers, b"content-type")
                outgoing = dict(message)
                outgoing["headers"] = headers
                await send(outgoing)
                _stream_log(
                    logging.INFO,
                    "stream_http_response_started",
                    path=path,
                    status_code=status_code,
                    message_id=message_id or None,
                    content_type=content_type,
                    asgi_spec=(scope.get("asgi") or {}).get("spec_version", ""),
                )
                return

            if message_type == "http.response.body":
                body = message.get("body") or b""
                body_frames += 1
                body_bytes += len(body)
                await send(message)
                if body and not first_body_logged:
                    first_body_logged = True
                    _stream_log(
                        logging.INFO,
                        "stream_http_first_body_sent",
                        path=path,
                        status_code=status_code or None,
                        message_id=message_id or None,
                        first_body_bytes=len(body),
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                if not bool(message.get("more_body", False)):
                    response_completed = True
                    _stream_log(
                        logging.INFO,
                        "stream_http_response_completed",
                        path=path,
                        status_code=status_code or None,
                        message_id=message_id or None,
                        body_frames=body_frames,
                        body_bytes=body_bytes,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                    )
                return

            await send(message)

        _stream_log(logging.INFO, "stream_http_request_started", path=path)
        try:
            await self.asgi_app(scope, tracked_receive, tracked_send)
        except BaseException as exc:
            failure_type = type(exc).__name__
            is_disconnect = isinstance(exc, (asyncio.CancelledError, ConnectionError, OSError))
            _stream_log(
                logging.WARNING if is_disconnect else logging.ERROR,
                "stream_http_app_exception",
                exc_info=not is_disconnect,
                path=path,
                status_code=status_code or None,
                message_id=message_id or None,
                response_started=response_started,
                client_disconnected=client_disconnected,
                body_frames=body_frames,
                body_bytes=body_bytes,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error_type=failure_type,
                error_message=str(exc),
            )
            raise
        finally:
            if not response_completed:
                _stream_log(
                    logging.WARNING,
                    "stream_http_response_aborted",
                    path=path,
                    status_code=status_code or None,
                    message_id=message_id or None,
                    response_started=response_started,
                    client_disconnected=client_disconnected,
                    body_frames=body_frames,
                    body_bytes=body_bytes,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                    error_type=failure_type or None,
                )
            _STREAM_REQUEST_ID.reset(token)


app.add_middleware(StreamTransportDiagnosticsMiddleware)


def _payload_diagnostics(data: Any) -> dict[str, Any]:
    """Return payload sizes/counts without logging answer, SQL, rows, or messages."""

    if not isinstance(data, dict):
        return {"payload_type": type(data).__name__}
    rows = data.get("rows")
    columns = data.get("columns")
    messages = data.get("messages")
    return {
        "status": data.get("status", ""),
        "result_id": data.get("result_id", ""),
        "row_count": len(rows) if isinstance(rows, list) else 0,
        "column_count": len(columns) if isinstance(columns, list) else 0,
        "message_count": len(messages) if isinstance(messages, list) else 0,
        "answer_chars": len(str(data.get("answer") or "")),
        "sql_chars": len(str(data.get("sql") or "")),
    }


def _sse_event_name(chunk: Any) -> str:
    if isinstance(chunk, bytes):
        text = chunk.decode("utf-8", errors="replace")
    else:
        text = str(chunk)
    for line in text.splitlines():
        if line.startswith("event:"):
            return line.split(":", 1)[1].strip()
    return "message"


def _chunk_size_bytes(chunk: Any) -> int:
    if isinstance(chunk, bytes):
        return len(chunk)
    return len(str(chunk).encode("utf-8"))


def _safe_emit_execution_log(
    *,
    stream_stage: str,
    session_id: str,
    message_id: int,
    **kwargs: Any,
) -> bool:
    """Keep an observability backend failure from breaking an SSE response."""

    try:
        agent.emit_execution_log(**kwargs)
        return True
    except Exception as exc:
        _stream_log(
            logging.ERROR,
            "stream_execution_log_failed",
            exc_info=True,
            stream_stage=stream_stage,
            session_id=session_id,
            message_id=message_id,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        return False


def _observed_sse_stream(chunks: Any, *, session_id: str, message_id: int):
    """Log SSE delivery boundaries and convert iterator failures to an error event."""

    started = time.monotonic()
    last_event = ""
    terminal_sent = False
    try:
        for chunk in chunks:
            event_name = _sse_event_name(chunk)
            last_event = event_name
            chunk_bytes = _chunk_size_bytes(chunk)
            if event_name != "heartbeat":
                _stream_log(
                    logging.INFO,
                    "sse_event_prepared",
                    session_id=session_id,
                    message_id=message_id,
                    sse_event=event_name,
                    chunk_bytes=chunk_bytes,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            yield chunk
            if event_name != "heartbeat":
                _stream_log(
                    logging.INFO,
                    "sse_event_sent",
                    session_id=session_id,
                    message_id=message_id,
                    sse_event=event_name,
                    chunk_bytes=chunk_bytes,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
            if event_name in {"done", "result", "error"}:
                terminal_sent = True
    except Exception as exc:
        _stream_log(
            logging.ERROR,
            "sse_stream_iteration_failed",
            exc_info=True,
            session_id=session_id,
            message_id=message_id,
            last_sse_event=last_event,
            terminal_sent=terminal_sent,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        if not terminal_sent:
            error_chunk = _sse(
                "error",
                {
                    "message": "스트림 응답을 마무리하지 못했습니다.",
                    "data": {
                        "error": "응답 전송 중 오류가 발생했습니다. 서버 로그에서 message_id를 확인해 주세요.",
                        "message_id": message_id,
                    },
                },
            )
            yield error_chunk
            terminal_sent = True
            _stream_log(
                logging.INFO,
                "sse_fallback_error_sent",
                session_id=session_id,
                message_id=message_id,
                last_sse_event=last_event,
            )
    finally:
        _stream_log(
            logging.INFO if terminal_sent else logging.WARNING,
            "sse_stream_closed",
            session_id=session_id,
            message_id=message_id,
            last_sse_event=last_event,
            terminal_sent=terminal_sent,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def _message_id() -> int:
    return time.time_ns() // 1_000


def _request_user_id(value: str | None) -> str:
    return (value or DEFAULT_WEBAPP_USER_ID).strip() or DEFAULT_WEBAPP_USER_ID


def _request_agent_name(value: str | None) -> str:
    return (value or DEFAULT_WEBAPP_AGENT_NAME).strip() or DEFAULT_WEBAPP_AGENT_NAME


def _extract_template_parameters(query_template: str) -> list[str]:
    seen: set[str] = set()
    parameters: list[str] = []
    for match in TEMPLATE_PARAM_RE.finditer(query_template or ""):
        name = match.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            parameters.append(name)
    return parameters


def _saved_query_summary(saved_query: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": saved_query.get("id"),
        "name": saved_query.get("name") or "저장 쿼리",
        "description": saved_query.get("description") or "",
        "query_template": saved_query.get("query_template") or "",
        "parameters": _jsonable(saved_query.get("parameters") or []),
        "defaults": _jsonable(saved_query.get("defaults") or {}),
        "created_at": saved_query.get("created_at"),
        "updated_at": saved_query.get("updated_at"),
    }


def _saved_query_payload(req: SavedQueryRequest) -> dict[str, Any]:
    query_template = req.query_template.strip()
    if not query_template:
        raise HTTPException(status_code=400, detail="저장할 쿼리를 입력해주세요.")
    parameters = req.parameters if req.parameters is not None else _extract_template_parameters(query_template)
    normalized_parameters = []
    seen: set[str] = set()
    for parameter in parameters:
        name = str(parameter).strip()
        if name and name not in seen:
            seen.add(name)
            normalized_parameters.append(name)
    name = req.name.strip() or query_template[:40] or "저장 쿼리"
    return {
        "id": req.id,
        "name": name[:80],
        "description": req.description.strip(),
        "query_template": query_template,
        "parameters": normalized_parameters,
        "defaults": req.defaults or {},
    }


def _render_saved_query(saved_query: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    query_template = str(saved_query.get("query_template") or "")
    merged_params = {**dict(saved_query.get("defaults") or {}), **dict(params or {})}
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        value = merged_params.get(name)
        if value in (None, ""):
            missing.append(name)
            return match.group(0)
        return str(value)

    query = TEMPLATE_PARAM_RE.sub(replace, query_template)
    if missing:
        unique_missing = []
        for name in missing:
            if name not in unique_missing:
                unique_missing.append(name)
        raise HTTPException(
            status_code=400,
            detail={"message": "저장 쿼리 실행에 필요한 파라미터를 입력해주세요.", "missing_parameters": unique_missing},
        )
    return {"query": query, "params": _jsonable(merged_params)}


def _conversation_id(session_or_id: dict[str, Any] | str) -> int:
    if isinstance(session_or_id, dict):
        stored_id = session_or_id.get("conversation_id")
        if stored_id:
            return int(stored_id)
        session_id = session_or_id["id"]
    else:
        session_id = session_or_id
    global _CONVERSATION_SEQ
    if session_id not in _CONVERSATION_IDS:
        _CONVERSATION_SEQ += 1
        _CONVERSATION_IDS[session_id] = _CONVERSATION_SEQ
    return _CONVERSATION_IDS[session_id]


def _validate_agent(agent_name: str, header_agent_name: str, body_agent_name: str | None) -> None:
    if header_agent_name and header_agent_name != agent_name:
        raise HTTPException(status_code=400, detail="X-Agent-Name 헤더와 URL agent_name이 다릅니다.")
    if body_agent_name and body_agent_name != agent_name:
        raise HTTPException(status_code=400, detail="본문 agent_name과 URL agent_name이 다릅니다.")


def _raise_api_error(exc: Exception) -> None:
    raise HTTPException(
        status_code=agent.common_http_status(exc),
        detail=_public_error_detail(exc),
    ) from exc


def _public_error_detail(exc: BaseException) -> str:
    """Return a stable client message without leaking provider/DB internals."""
    error_name = agent.common_error_name(exc)
    if error_name == "ConfigurationError":
        return "서비스 설정을 확인해 주세요."
    if error_name == "RetryableProviderError":
        return "모델 서비스가 일시적으로 응답하지 않습니다. 잠시 후 다시 시도해 주세요."
    if error_name in {"ProviderError", "LLMHTTPError"}:
        return "모델 서비스 응답을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."
    if error_name == "CapabilityNotSupportedError":
        return "현재 모델에서 지원하지 않는 요청입니다."
    return "요청 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."


def _public_result_error(error: Any) -> str:
    """Hide SQL/provider diagnostics kept in state from normal API consumers."""
    if not str(error or "").strip():
        return ""
    return "SQL 처리에 실패했습니다. 아래 실패 원인 분석과 마지막 SQL을 확인해 주세요."


def _public_sql_failure_reason(error: Any) -> tuple[str, str]:
    """Classify DB/validation failures without returning raw infrastructure details."""
    detail = str(error or "").strip()
    lowered = detail.lower()
    patterns = [
        (
            r"column_not_found|column .*does not exist|cannot be resolved|unknown column|컬럼.*(?:없|찾)",
            "SQL에 작성한 컬럼을 찾을 수 없습니다.",
            "테이블 상세에서 실제 컬럼명을 확인하고 별칭과 철자를 수정해 주세요.",
        ),
        (
            r"table_not_found|relation .*does not exist|table .*does not exist|unknown table|스키마에 없는 테이블|테이블.*(?:없|찾)",
            "SQL에 작성한 테이블을 찾을 수 없습니다.",
            "테이블 탭에서 물리 테이블명과 스키마 prefix를 확인해 주세요.",
        ),
        (
            r"syntax error|mismatched input|parse error|sql 파싱|구문",
            "SQL 구문을 해석하지 못했습니다.",
            "괄호, 쉼표, 따옴표, SELECT/FROM/GROUP BY 구문을 확인해 주세요.",
        ),
        (
            r"access denied|permission denied|not authorized|권한|접근이 제한",
            "해당 데이터에 대한 조회 권한이 없습니다.",
            "접근 가능한 테이블인지 확인하거나 데이터 권한 담당자에게 문의해 주세요.",
        ),
        (
            r"timeout|timed out|time limit|시간.*초과",
            "SQL 실행 시간이 제한을 초과했습니다.",
            "조회 기간, 대상 컬럼, JOIN 범위를 줄여 다시 실행해 주세요.",
        ),
        (
            r"type mismatch|cannot cast|invalid cast|operator does not exist|자료형|타입",
            "SQL의 컬럼 자료형 또는 연산 방식이 맞지 않습니다.",
            "비교·집계 대상 컬럼의 자료형과 CAST 구문을 확인해 주세요.",
        ),
        (
            r"read-only|select.*only|select 문만|읽기 전용",
            "읽기 전용 SELECT/WITH SQL만 실행할 수 있습니다.",
            "INSERT, UPDATE, DELETE, DDL 문을 제거하고 조회 SQL로 작성해 주세요.",
        ),
    ]
    for pattern, reason, suggestion in patterns:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return reason, suggestion
    return (
        "SQL을 검증하거나 실행하는 과정에서 오류가 발생했습니다.",
        "아래 SQL과 선택 테이블을 확인하고 조건을 단순화해 다시 실행해 주세요.",
    )


def _sql_failure_details(result: dict[str, Any]) -> dict[str, Any] | None:
    raw_error = result.get("query_error") or result.get("error_message") or result.get("validation_result")
    if not str(raw_error or "").strip():
        return None
    if result.get("query_error"):
        stage = "sql_execution"
        stage_label = "SQL 실행"
        analysis_source = result.get("query_error")
    elif result.get("generated_sql") or result.get("final_sql"):
        stage = "sql_validation"
        stage_label = "SQL 검증"
        analysis_source = result.get("validation_result") or raw_error
    else:
        stage = "sql_generation"
        stage_label = "SQL 생성"
        analysis_source = raw_error
    reason, suggestion = _public_sql_failure_reason(analysis_source)
    validation_summary = ""
    if not result.get("query_error"):
        validation_summary = " ".join(str(result.get("validation_result") or "").split())[:1200]
    return {
        "stage": stage,
        "stage_label": stage_label,
        "reason": reason,
        "suggestion": suggestion,
        "retry_count": int(result.get("retry_count") or 0),
        "selected_domain": str(result.get("selected_domain") or ""),
        "selected_tables": list(result.get("selected_tables") or []),
        "validation_summary": validation_summary,
        "sql": str(result.get("final_sql") or result.get("generated_sql") or ""),
    }


def _get_or_create_session(session_id: str | None, user_id: str = "ui", agent_name: str = "corporate_sales") -> dict[str, Any]:
    try:
        return _SESSION_STORE.get_or_create_session(session_id, user_id, agent_name)
    except SessionOwnershipError as exc:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.") from exc


def _append_message(session: dict[str, Any], role: str, content: str, **extra: Any) -> None:
    message = {
        "role": role,
        "content": content,
        "text": content,
        "created_at": datetime.now().isoformat(),
    }
    message.update({k: _jsonable(v) for k, v in extra.items() if v is not None})
    _SESSION_STORE.append_message(session, message)


def _user_message_text(question: str, params: dict[str, Any] | None = None) -> str:
    if not params:
        return question.strip()
    managed_scope_value = params.get(MANAGED_SCOPE_PARAMETER)
    if managed_scope_value not in (None, ""):
        try:
            count = len(parse_business_number_list(managed_scope_value))
            return f"추가 입력: 관리기업목록 {count:,}개"
        except ManagedScopeParseError:
            return "추가 입력: 관리기업목록 형식 확인 필요"
    natural_input = str(params.get("_natural_input") or "").strip()
    if natural_input:
        try:
            count = len(parse_business_number_list(natural_input))
            return f"추가 입력: 관리기업목록 {count:,}개"
        except ManagedScopeParseError:
            pass
        return f"추가 입력: {natural_input}"
    return "추가 입력: " + json.dumps(_jsonable(params), ensure_ascii=False)


def _session_summary(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session["id"],
        "title": session.get("title", "새 대화"),
        "created_at": session.get("created_at", ""),
        "updated_at": session.get("updated_at", ""),
        "last_result_id": session.get("last_result_id", ""),
        "message_count": int(session.get("message_count") or len(session.get("messages", []))),
    }


def _build_continuation(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "question": result.get("question", ""),
        "selected_domain": result.get("selected_domain", ""),
        "domain_candidates": result.get("domain_candidates", []),
        "domain_routing_trace": result.get("domain_routing_trace", ""),
        "domain_context": result.get("domain_context", ""),
        "selected_capability_type": result.get("selected_capability_type", ""),
        "selected_capability_name": result.get("selected_capability_name", ""),
        "selected_tool": result.get("selected_tool", ""),
        "tool_params": result.get("tool_params", {}),
        "matched_query_name": result.get("matched_query_name", ""),
        "matched_query_sql": result.get("matched_query_sql", ""),
        "matched_query_params": result.get("matched_query_params", {}),
        "extracted_params": result.get("extracted_params", {}),
        "selected_tables": result.get("selected_tables", []),
        "table_details": result.get("table_details", ""),
        "user_provided_params": result.get("user_provided_params", {}),
        "missing_params": result.get("missing_params", []),
    }


def _param_name(param: Any) -> str:
    if isinstance(param, dict):
        return str(param.get("name") or param.get("label") or "")
    return str(param or "")


def _normalize_korean_ym(text: str) -> str:
    import re

    stripped = text.strip()
    match = re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월", stripped)
    if match:
        return f"{match.group(1)}{int(match.group(2)):02d}"
    match = re.search(r"(20\d{2})[-./\s](\d{1,2})(?!\d)", stripped)
    if match:
        return f"{match.group(1)}{int(match.group(2)):02d}"
    match = re.search(r"\b(20\d{2})(0[1-9]|1[0-2])\b", stripped)
    if match:
        return match.group(0)
    return stripped


def _normalize_korean_year(text: str) -> str:
    import re

    match = re.search(r"(20\d{2})\s*년?", text)
    return match.group(1) if match else text.strip()


def _extract_named_value(name: str, text: str) -> str:
    """'이름=값' / '이름은 값' / '이름 값' / '이름: 값' 패턴에서 값을 뽑는다.

    LS/IS처럼 정형 규칙이 없는 숫자 파라미터를 LLM 없이 결정적으로 파싱하기 위함.
    """
    import re

    escaped = re.escape(name)
    # 값은 숫자(소수/퍼센트) 또는 공백 전까지의 토큰을 우선 시도한다.
    patterns = [
        rf"{escaped}\s*[=:]\s*(-?\d+(?:\.\d+)?)",
        rf"{escaped}\s*(?:은|는|이|가|을|를|로|으로)?\s*(-?\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _natural_params_by_rule(natural_input: str, missing_params: list[Any]) -> dict[str, Any]:
    import re

    natural_input = natural_input.strip()
    names = [_param_name(param) for param in missing_params if _param_name(param)]
    param_info = {
        _param_name(param): param
        for param in missing_params
        if isinstance(param, dict) and _param_name(param)
    }
    parsed: dict[str, Any] = {}
    current_ym = f"{datetime.now().year}{datetime.now().month:02d}"
    current_index = datetime.now().year * 12 + datetime.now().month - 1

    def shifted_ym(months: int) -> str:
        target = current_index + months
        return f"{target // 12:04d}{target % 12 + 1:02d}"

    is_previous = bool(re.search(r"전월|지난\s*달|저번\s*달", natural_input))
    is_current = bool(re.search(r"이번\s*달|이번\s*월|현재\s*월|금월", natural_input))
    recent_span = re.search(r"최근\s*(\d+)\s*개월", natural_input)
    is_recent = is_current or bool(re.search(r"최근", natural_input))
    if is_previous:
        ym_value = shifted_ym(-1)
    elif is_recent:
        ym_value = current_ym
    else:
        ym_value = _normalize_korean_ym(natural_input)
    year_value = _normalize_korean_year(natural_input)

    for name in names:
        info = param_info.get(name, {})
        if str(info.get("type") or "").lower() == "business_number_list" or name == MANAGED_SCOPE_PARAMETER:
            try:
                parsed[name] = parse_business_number_list(natural_input)
            except ManagedScopeParseError:
                pass
            continue
        # 1) '이름=값' 등 명시적 패턴이 있으면 최우선 채택 (LS/IS 같은 자유 숫자 파라미터 포함).
        named = _extract_named_value(name, natural_input)
        if named:
            parsed[name] = named
            continue

        lowered = name.lower()
        if any(key in lowered for key in ["ym", "년월", "기준월", "기준년월", "base_ym"]):
            parsed[name] = ym_value
        elif any(key in lowered for key in ["기간_시작", "start", "from", "시작"]):
            if recent_span:
                parsed[name] = shifted_ym(-(max(int(recent_span.group(1)), 1) - 1))
            elif is_recent or is_previous:
                parsed[name] = ym_value
            elif re.search(r"(20\d{2})\s*년(?!\s*\d)", natural_input):
                parsed[name] = f"{year_value}01"
            else:
                parsed[name] = ym_value
        elif any(key in lowered for key in ["기간_종료", "end", "to", "종료"]):
            if is_recent or is_previous:
                parsed[name] = ym_value
            elif re.search(r"(20\d{2})\s*년(?!\s*\d)", natural_input):
                parsed[name] = f"{year_value}12"
            else:
                parsed[name] = ym_value
        elif any(key in lowered for key in ["year", "년도", "연도"]):
            parsed[name] = year_value

    if names:
        unfilled_names = [name for name in names if name not in parsed]
        numeric_names = [
            name
            for name in unfilled_names
            if name in {"LS", "IS"} or any(key in name.lower() for key in ["rate", "ratio", "율", "비율", "스프레드"])
        ]
        numeric_values = re.findall(r"-?\d+(?:\.\d+)?", natural_input)
        if numeric_names and len(numeric_values) >= len(numeric_names):
            for name, value in zip(numeric_names, numeric_values[-len(numeric_names):]):
                parsed[name] = value

    # 날짜와 요청 표현을 걷어낸 나머지가 하나의 명사구이면 기업/가맹점명으로 사용한다.
    # 여러 엔티티 파라미터가 동시에 비어 있으면 임의로 고르지 않고 LLM fallback에 맡긴다.
    entity_names = [
        name
        for name in names
        if name not in parsed
        and any(key in name.lower() for key in ["가맹점명", "기업명", "회사명", "상호명", "업체명", "대상명", "고객명"])
    ]
    if len(entity_names) == 1:
        entity_value = natural_input
        entity_value = re.sub(r"20\d{2}\s*년(?:\s*\d{1,2}\s*월)?", " ", entity_value)
        entity_value = re.sub(r"\b20\d{2}(?:0[1-9]|1[0-2])\b", " ", entity_value)
        entity_value = re.sub(
            r"(?:전월|지난\s*달|저번\s*달|이번\s*(?:달|월)|현재\s*월|금월|최근\s*\d*\s*개월)",
            " ",
            entity_value,
        )
        entity_value = re.sub(r"(?:으로|로|은|는|이|가|을|를)?\s*(?:해줘|알려줘|보여줘|조회해줘)$", " ", entity_value.strip())
        entity_value = re.sub(r"\s+", " ", entity_value).strip(" \t\r\n,:'\"")
        if entity_value and len(entity_value) <= 100:
            parsed[entity_names[0]] = entity_value

    if len(names) == 1 and names[0] not in parsed:
        parsed[names[0]] = ym_value if ym_value != natural_input else natural_input

    return {key: value for key, value in parsed.items() if value != "" and value is not None}


def _natural_params_by_llm(natural_input: str, missing_params: list[Any], continuation: dict[str, Any]) -> dict[str, Any]:
    if not missing_params:
        return {}
    prompt = f"""사용자의 자연어 답변에서 누락 파라미터 값을 추출하세요.

원 질문:
{continuation.get("question", "")}

필요한 파라미터:
{json.dumps(_jsonable(missing_params), ensure_ascii=False)}

사용자 답변:
{natural_input}

규칙:
- JSON object만 반환하세요.
- key는 필요한 파라미터의 name을 그대로 사용하세요.
- 기준년월/년월은 YYYYMM 형식으로 변환하세요. 예: 2025년 12월 -> 202512
- "최근", "이번달", "이번 월"은 이번달({datetime.now().year}{datetime.now().month:02d})로 변환하세요.
- 기간 시작/종료가 연도만 주어졌으면 시작은 YYYY01, 종료는 YYYY12로 변환하세요.
- 모르는 값은 포함하지 마세요.

JSON:"""
    try:
        raw = agent._call_llm(prompt, max_tokens=512).strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # 모델이 설명 문장과 함께 JSON을 흘려보낸 경우 첫 번째 JSON object만 추출한다.
            match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
            if not match:
                return {}
            parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _coerce_natural_params(params: dict[str, Any], continuation: dict[str, Any]) -> dict[str, Any]:
    natural_input = str(params.get("_natural_input") or "").strip()
    if not natural_input:
        return params
    missing_params = continuation.get("missing_params") or []
    parsed = _natural_params_by_rule(natural_input, missing_params)
    explicit = {key: value for key, value in params.items() if not key.startswith("_")}
    explicit.update(parsed)
    unresolved = [
        item
        for item in missing_params
        if _param_name(item) and explicit.get(_param_name(item)) in (None, "")
    ]
    llm_unresolved = [
        item
        for item in unresolved
        if not (
            isinstance(item, dict)
            and str(item.get("type") or "").lower() == "business_number_list"
        )
        and _param_name(item) != MANAGED_SCOPE_PARAMETER
    ]
    if llm_unresolved:
        llm_values = _natural_params_by_llm(natural_input, llm_unresolved, continuation)
        allowed = {_param_name(item) for item in llm_unresolved}
        explicit.update({key: value for key, value in llm_values.items() if key in allowed and value not in (None, "")})
    return explicit


def _state_from_request(req: CompatibleQueryRequest, session: dict[str, Any]) -> dict[str, Any]:
    question = req.query.strip()
    if not question:
        raise HTTPException(status_code=400, detail="query를 입력해주세요.")

    params = req.params or {}
    pending = session.get("pending_continuation")
    if not req.continuation and pending and not params:
        params = {"_natural_input": question}
    continuation = req.continuation or (pending if params else None)
    if continuation and params:
        params = _coerce_natural_params(params, continuation)
    if not continuation:
        return agent._new_initial_state(question)

    state = agent._new_initial_state(continuation.get("question") or question)
    state["question_type"] = "need_sql"
    state["selected_domain"] = continuation.get("selected_domain", "")
    state["domain_candidates"] = continuation.get("domain_candidates", [])
    state["domain_routing_trace"] = continuation.get("domain_routing_trace", "")
    state["domain_context"] = continuation.get("domain_context", "")
    state["selected_capability_type"] = continuation.get("selected_capability_type", "")
    state["selected_capability_name"] = continuation.get("selected_capability_name", "")
    selected_tool = continuation.get("selected_tool", "")
    matched_query_name = continuation.get("matched_query_name", "")
    if selected_tool:
        merged = dict(continuation.get("tool_params") or {})
        merged.update(params)
        state["selected_tool"] = selected_tool
        state["tool_params"] = merged
    elif matched_query_name:
        merged = dict(continuation.get("extracted_params") or {})
        merged.update(continuation.get("user_provided_params") or {})
        merged.update(params)
        state["matched_query_name"] = matched_query_name
        state["matched_query_sql"] = continuation.get("matched_query_sql", "")
        state["matched_query_params"] = continuation.get("matched_query_params", {})
        state["user_provided_params"] = merged
    else:
        merged = dict(continuation.get("user_provided_params") or {})
        merged.update(params)
        state["user_provided_params"] = merged
        state["selected_tables"] = continuation.get("selected_tables") or []
        state["table_details"] = continuation.get("table_details") or ""
    return state


def _source_label(result: dict[str, Any], source_override: str | None = None) -> str:
    return source_override or agent._get_source_label(result)


def _documents_from_result(result: dict[str, Any], top_k: int, source_override: str | None = None) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    if result.get("matched_query_name"):
        documents.append({"title": f"검증 쿼리: {result['matched_query_name']}", "source_url": "", "score": 1.0})
    for table in _result_data_sources(result):
        documents.append({"title": f"테이블: {table}", "source_url": "", "score": 1.0})
    if not documents:
        documents.append({"title": _source_label(result, source_override), "source_url": "", "score": 1.0})
    return documents[: max(top_k, 0)]


def _result_conditions(result: dict[str, Any]) -> dict[str, Any]:
    conditions: dict[str, Any] = {}
    for key in ("tool_params", "extracted_params", "user_provided_params"):
        value = result.get(key)
        if isinstance(value, dict):
            for condition_name, condition_value in value.items():
                if condition_value in ("", None):
                    continue
                if condition_name == MANAGED_SCOPE_PARAMETER:
                    try:
                        count = len(parse_business_number_list(condition_value))
                        conditions[condition_name] = f"{count:,}개 (요청별 입력)"
                    except ManagedScopeParseError:
                        conditions[condition_name] = "형식 확인 필요"
                else:
                    conditions[condition_name] = condition_value
    return conditions


def _result_data_sources(result: dict[str, Any]) -> list[str]:
    sql_tables = _table_names_from_sql(result.get("final_sql", ""))
    return sql_tables or list(result.get("selected_tables", []) or [])


def _result_meta(result: dict[str, Any], source_override: str | None = None) -> dict[str, Any]:
    rows = result.get("query_rows", []) or []
    local_scope = result.get("local_result_scope", {}) or {}
    result_scope = infer_result_scope(result)
    sql_tables = _table_names_from_sql(result.get("final_sql", ""))
    planned_tables = list(result.get("selected_tables", []) or [])
    return {
        "execution_path": _source_label(result, source_override),
        "data_sources": sql_tables or planned_tables,
        "sql_data_sources": sql_tables,
        "planned_data_sources": planned_tables,
        "conditions": _result_conditions(result),
        "displayed_rows": len(rows),
        "display_row_limit": int(result_scope.get("display_limit") or 100),
        "rows_may_be_limited": not bool(result_scope.get("is_complete", True)),
        "result_complete": bool(result_scope.get("is_complete", True)),
        "completeness_reason": str(result_scope.get("reason") or ""),
        "result_scope": result_scope,
        "selected_domain": result.get("selected_domain", ""),
        "selected_tool": result.get("selected_tool", ""),
        "matched_query_name": result.get("matched_query_name", ""),
        "selected_capability_type": result.get("selected_capability_type", ""),
        "selected_capability_name": result.get("selected_capability_name", ""),
        "followup_operations": result.get("followup_operations", []),
        "local_result_scope": local_scope,
        "has_chart": bool(result.get("chart")),
    }


def _register_file(path: str, session: dict[str, Any] | None = None, user_id: str | None = None) -> dict[str, str] | None:
    if not path:
        return None
    file_path = Path(path).resolve()
    if not file_path.exists() or not file_path.is_file():
        return None
    allowed_roots = [BASE_DIR.resolve(), Path(agent.BAD_DEBT_OUTPUT_DIR).resolve(), Path(agent.REPORT_DIR).resolve()]
    if not any(file_path.is_relative_to(root) for root in allowed_roots):
        return None
    token = uuid.uuid4().hex
    _SESSION_STORE.save_file(token, str(file_path), session=session, user_id=user_id)
    return {"token": token, "filename": file_path.name, "url": f"api/files/{token}"}


def _static_followups(result: dict[str, Any]) -> list[str]:
    columns = [str(col).lower() for col in result.get("query_columns", [])]
    rows = result.get("query_rows", [])
    suggestions: list[str] = []
    if rows:
        suggestions.extend(
            [
                "이 결과에서 이상치가 있는지 찾아줘",
                "상위/하위 항목의 차이를 비교해줘",
                "추가 확인이 필요한 구간을 골라줘",
                "금액 기준 내림차순으로 다시 정렬해서 보여줘",
            ]
        )
    if any(key in col for col in columns for key in ["월", "일", "date", "month", "ym", "yyyymm", "기간"]):
        suggestions.insert(1, "기간별 트렌드와 변곡점을 요약해줘")
    elif rows:
        suggestions.insert(1, "분포나 패턴이 눈에 띄는지 요약해줘")
    if any(key in col for col in columns for key in ["비율", "율", "rate", "ratio", "증감", "growth"]):
        suggestions.append("비율이 높은 구간의 근거 데이터를 요약해줘")
    return list(dict.fromkeys(suggestions))[:5]


def _parse_suggestion_list(raw: str) -> list[str]:
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if not match:
            parsed = []
        else:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = []
    if isinstance(parsed, dict):
        parsed = parsed.get("suggestions") or parsed.get("questions") or []
    suggestions: list[str] = []
    items = parsed if isinstance(parsed, list) else []
    for item in items:
        value = str(item).strip()
        value = re.sub(r"^\d+[\).\s-]+", "", value).strip()
        if value:
            suggestions.append(value)
    return list(dict.fromkeys(suggestions))


def _llm_followups(result: dict[str, Any]) -> list[str]:
    columns = [str(col) for col in result.get("query_columns", [])]
    rows = result.get("query_rows", []) or []
    if not rows:
        return []
    min_count = agent_config.FOLLOWUP_SUGGESTION_MIN_COUNT
    max_count = agent_config.FOLLOWUP_SUGGESTION_MAX_COUNT
    prompt = f"""조회 결과를 바탕으로 사용자가 다음에 물어볼 만한 후속 분석 질문을 {min_count}~{max_count}개 생성하세요.

규칙:
- 한국어 질문만 작성합니다.
- 각 질문은 현재 결과에 이어서 바로 실행할 수 있어야 합니다.
- SQL, 테이블명, 내부 컬럼명을 노출하지 않습니다.
- 너무 일반적인 질문이나 현재 결과와 무관한 질문은 제외합니다.
- 출력은 JSON 배열만 반환합니다. 예: ["질문1", "질문2"]

[원 질문]
{result.get("question", "")}

[답변 요약]
{result.get("answer", "")[:1200]}

[결과 컬럼]
{", ".join(columns)}

[결과 미리보기]
{_compact_table_text(columns, rows, limit=agent_config.FOLLOWUP_SUGGESTION_ROW_LIMIT)}
"""
    suggestions = _parse_suggestion_list(agent._call_llm(prompt, max_tokens=640))
    return suggestions[:max_count]


def _suggest_followups(result: dict[str, Any]) -> list[str]:
    mode = agent_config.FOLLOWUP_SUGGESTION_MODE
    fallback = _static_followups(result)
    if mode == "static":
        return fallback[: agent_config.FOLLOWUP_SUGGESTION_MAX_COUNT]
    try:
        suggestions = _llm_followups(result)
    except Exception:
        suggestions = []
    if len(suggestions) >= agent_config.FOLLOWUP_SUGGESTION_MIN_COUNT:
        return suggestions[: agent_config.FOLLOWUP_SUGGESTION_MAX_COUNT]
    if mode == "llm":
        return suggestions[: agent_config.FOLLOWUP_SUGGESTION_MAX_COUNT]
    merged = list(dict.fromkeys(suggestions + fallback))
    return merged[: agent_config.FOLLOWUP_SUGGESTION_MAX_COUNT]


def _format_prompt_value(value: Any) -> str:
    value = _jsonable(value)
    return "" if value is None else str(value)


def _compact_table_text(columns: list[Any], rows: list[Any], limit: int = 80) -> str:
    if not columns or not rows:
        return "(조회 데이터 없음)"
    headers = [str(col) for col in columns]
    lines = ["\t".join(headers)]
    for row in rows[:limit]:
        row_values = list(row) if isinstance(row, (list, tuple)) else [row]
        cells = [_format_prompt_value(row_values[idx] if idx < len(row_values) else "") for idx in range(len(headers))]
        lines.append("\t".join(cells))
    if len(rows) > limit:
        lines.append(f"... 총 {len(rows)}건 중 {limit}건만 표시")
    return "\n".join(lines)


def _param_example_value(name: str) -> str:
    """누락된 파라미터 이름에 맞는 예시 입력값을 만든다 (고정 문구 대신 동적 안내용)."""
    lowered = name.lower()
    if name == MANAGED_SCOPE_PARAMETER or "사업자등록번호" in lowered:
        return "123-45-67890, 234-56-78901"
    if any(key in lowered for key in ["년월", "기준월", "ym", "base_ym"]):
        return f"{name}={datetime.now().year}{datetime.now().month:02d}"
    if any(key in lowered for key in ["기간_시작", "start", "시작", "from"]):
        return f"{name}={datetime.now().year}{datetime.now().month:02d}"
    if any(key in lowered for key in ["기간_종료", "end", "종료", "to"]):
        return f"{name}={datetime.now().year}{datetime.now().month:02d}"
    if any(key in lowered for key in ["year", "년도", "연도"]):
        return f"{datetime.now().year}년"
    if any(key in lowered for key in ["가맹점", "기업", "고객", "상호", "대상"]):
        return f"{name}=한빛테크놀로지"
    if name in ("LS", "IS") or "스프레드" in lowered:
        return f"{name}=0.3"
    if any(key in lowered for key in ["limit", "행수", "개수", "top", "상위"]):
        return f"{name}=10"
    return f"{name}=값"


def _param_instruction_line(item: Any) -> str:
    if not isinstance(item, dict):
        return f"- {item}"
    name = str(item.get("name") or item.get("label") or "").strip()
    label = str(item.get("label") or name or "필수 값").strip()
    description = str(item.get("description") or "").strip()
    example = _param_example_value(name) if name else ""
    title = label
    if name and name != label and name not in label:
        title += f" ({name})"
    detail = f": {description}" if description else ""
    example_text = f" (예: {example})" if example else ""
    return f"- {title}{detail}{example_text}"


def _requires_params_answer(result: dict[str, Any]) -> str:
    missing = result.get("missing_params", []) or []
    if not missing:
        return "추가 입력이 필요해요. 진행 영역의 입력값을 확인해 주세요."
    names = [_param_name(item) for item in missing if _param_name(item)]
    example = ", ".join(_param_example_value(name) for name in names) or "필요한 값"
    managed_scope_help = ""
    if any(
        _param_name(item) == MANAGED_SCOPE_PARAMETER
        or (isinstance(item, dict) and str(item.get("type") or "").lower() == "business_number_list")
        for item in missing
    ):
        managed_scope_help = (
            "\n\n관리기업목록은 사업자등록번호를 쉼표 또는 줄바꿈으로 구분해 입력해 주세요. "
            "하이픈은 있어도 됩니다."
        )
    return (
        "추가 입력이 필요해요.\n\n"
        "필요한 값:\n"
        + "\n".join(_param_instruction_line(item) for item in missing)
        + "\n\n"
        f"예: `{example}`\n"
        "진행 영역의 입력값을 확인하거나 수정한 뒤 `계속`을 누르면 같은 질문을 이어서 실행합니다."
        + managed_scope_help
    )


def _result_payload(
    result: dict[str, Any],
    session: dict[str, Any],
    message_id: int,
    top_k: int,
    source_override: str | None = None,
) -> dict[str, Any]:
    requires_params = result.get("param_stage") == "need_params"
    if requires_params:
        status = "requires_params"
        result_id = ""
        answer = _requires_params_answer(result)
        documents = []
        continuation = _build_continuation(result)
        _SESSION_STORE.update_session_state(session, pending_continuation=continuation)
    else:
        status = "complete"
        result_id = uuid.uuid4().hex
        result["result_scope"] = infer_result_scope(result)
        result["query_frame"] = build_query_frame(
            result,
            previous_frame=result.get("query_frame") or None,
            last_question=str(result.get("followup_question") or ""),
        )
        result["suggestions"] = _suggest_followups(result)
        _SESSION_STORE.save_result(result_id, session, dict(result))
        _SESSION_STORE.update_session_state(session, last_result_id=result_id, pending_continuation=None)
        answer = result.get("answer", "") or result.get("error_message", "")
        documents = _documents_from_result(result, top_k, source_override)
        continuation = None

    result_error = result.get("error_message", "") or result.get("query_error", "")
    error = _public_result_error(result_error)
    failure_details = _sql_failure_details(result) if error else None
    result_sql = result.get("final_sql", "") or (result.get("generated_sql", "") if error else "")
    excel_file = _register_file(result.get("bad_debt_excel_path", ""), session=session)
    data = {
        "answer": answer,
        "documents": documents,
        "session_id": session["id"],
        "message_id": message_id,
        "conversation_id": _conversation_id(session),
        "images": [],
        "insufficient_evidence": bool(requires_params or error),
        "status": status,
        "result_id": result_id,
        "question": result.get("question", ""),
        "followup_question": result.get("followup_question", ""),
        "question_type": result.get("question_type", ""),
        "source": _source_label(result, source_override),
        "selected_tool": result.get("selected_tool", ""),
        "matched_query_name": result.get("matched_query_name", ""),
        "selected_tables": result.get("selected_tables", []),
        "sql": result_sql,
        "columns": result.get("query_columns", []),
        "rows": result.get("query_rows", []),
        "query_frame": result.get("query_frame", {}),
        "result_scope": result.get("result_scope", {}),
        "result_meta": _result_meta(result, source_override),
        "error": error,
        "failure_details": failure_details,
        "excel_file": excel_file,
        "suggestions": result.get("suggestions", []),
        "original_answer": result.get("original_answer", result.get("answer", "")),
        "analysis_history": result.get("analysis_history", []),
        "followup_mode": result.get("followup_mode", ""),
        "followup_route": result.get("followup_route", {}),
        "followup_operations": result.get("followup_operations", []),
        "chart": result.get("chart"),
        "parent_result_id": result.get("parent_result_id", ""),
        "messages": session.get("messages", []),
        "session": _session_summary(session),
    }
    if requires_params:
        data["missing_params"] = result.get("missing_params", [])
        data["continuation"] = continuation
    return _jsonable(data)


def _finalize_assistant_message(session: dict[str, Any], data: dict[str, Any], message_id: int) -> dict[str, Any]:
    _append_message(
        session,
        "assistant",
        data.get("answer", ""),
        message_id=message_id,
        status="error" if data.get("error") else data.get("status"),
        error=data.get("error"),
        failure_details=data.get("failure_details"),
        missing_params=data.get("missing_params"),
        continuation=data.get("continuation"),
        result_id=data.get("result_id"),
        source=data.get("source"),
        sql=data.get("sql"),
        columns=data.get("columns"),
        rows=data.get("rows"),
        result_meta=data.get("result_meta"),
        query_frame=data.get("query_frame"),
        result_scope=data.get("result_scope"),
        followup_mode=data.get("followup_mode"),
        followup_route=data.get("followup_route"),
        followup_operations=data.get("followup_operations"),
        chart=data.get("chart"),
        row_count=len(data.get("rows", []) or []),
    )
    data["messages"] = _jsonable(session.get("messages", []))
    data["session"] = _session_summary(session)
    return data


def _last_result_payload(session: dict[str, Any], top_k: int = 10) -> dict[str, Any] | None:
    result_id = session.get("last_result_id", "")
    if not result_id:
        return None
    result = _SESSION_STORE.get_result(result_id, session.get("user_id"))
    fallback_message = None
    for message in reversed(session.get("messages", [])):
        if message.get("role") == "assistant" and message.get("result_id") == result_id:
            fallback_message = message
            break
    if not result and not fallback_message:
        return None

    result = result or {}
    error = _public_result_error(
        result.get("error_message", "")
        or result.get("query_error", "")
        or (fallback_message or {}).get("error", "")
    )
    message_id = (fallback_message or {}).get("message_id")
    answer = result.get("answer", "") or result.get("error_message", "") or (fallback_message or {}).get("text", "")
    columns = result.get("query_columns", []) or (fallback_message or {}).get("columns", [])
    rows = result.get("query_rows", []) or (fallback_message or {}).get("rows", [])
    sql = (
        result.get("final_sql", "")
        or (result.get("generated_sql", "") if error else "")
        or (fallback_message or {}).get("sql", "")
    )
    failure_details = (
        _sql_failure_details(result)
        if error and result
        else (fallback_message or {}).get("failure_details")
    )

    data = {
        "answer": answer,
        "documents": _documents_from_result(result, top_k) if result else [],
        "session_id": session["id"],
        "message_id": message_id,
        "conversation_id": _conversation_id(session),
        "images": [],
        "insufficient_evidence": bool(error),
        "status": "complete",
        "result_id": result_id,
        "question": result.get("question", ""),
        "followup_question": result.get("followup_question", ""),
        "question_type": result.get("question_type", ""),
        "source": _source_label(result) if result else (fallback_message or {}).get("source", ""),
        "selected_tool": result.get("selected_tool", ""),
        "matched_query_name": result.get("matched_query_name", ""),
        "selected_tables": result.get("selected_tables", []),
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "query_frame": result.get("query_frame", {}),
        "result_scope": result.get("result_scope", {}),
        "result_meta": _result_meta(result) if result else (fallback_message or {}).get("result_meta", {}),
        "error": error,
        "failure_details": failure_details,
        "excel_file": _register_file(result.get("bad_debt_excel_path", ""), session=session),
        "suggestions": result.get("suggestions") or _suggest_followups(result),
        "original_answer": result.get("original_answer", result.get("answer", "")),
        "analysis_history": result.get("analysis_history", []),
        "followup_mode": result.get("followup_mode", ""),
        "followup_route": result.get("followup_route", {}),
        "followup_operations": result.get("followup_operations", []),
        "chart": result.get("chart") or (fallback_message or {}).get("chart"),
        "parent_result_id": result.get("parent_result_id", ""),
        "messages": session.get("messages", []),
        "session": _session_summary(session),
    }
    return _jsonable(data)


def _progress_payload(node_name: str, req: CompatibleQueryRequest, result: dict[str, Any]) -> dict[str, Any]:
    phase, title, message = NODE_LABELS.get(node_name, ("processing", node_name, f"{node_name} 단계를 처리하고 있습니다."))
    return {
        "message": message,
        "data": {
            "operation": "text2sql_progress",
            "text2sql_step": node_name,
            "phase": phase,
            "title": title,
            "query": req.query,
            "question_type": result.get("question_type", ""),
            "selected_domain": result.get("selected_domain", ""),
            "domain_candidates": result.get("domain_candidates", []),
            "selected_tool": result.get("selected_tool", ""),
            "matched_query_name": result.get("matched_query_name", ""),
            "selected_capability_type": result.get("selected_capability_type", ""),
            "selected_capability_name": result.get("selected_capability_name", ""),
            "selected_tables": result.get("selected_tables", []),
            "param_stage": result.get("param_stage", ""),
            "missing_params": result.get("missing_params", []),
            "sql": result.get("final_sql", ""),
            "validation_result": result.get("validation_result", ""),
            "columns": result.get("query_columns", []),
            "row_count": len(result.get("query_rows", []) or []),
            "source": _source_label(result),
        },
    }


_NEXT_PROGRESS_STEP = {
    "": "classify_question",
    "classify_question": "route_domain",
    "prepare_direct_sql": "validate_sql",
    "route_domain": "select_tool",
    "select_tool": "match_verified_query",
    "check_tool_params": "execute_tool",
    "execute_tool": "run_tool_query",
    "extract_and_apply_params": "run_matched_query",
    "analyze_question": "check_sql_gen_params",
    "check_sql_gen_params": "generate_sql",
    "generate_sql": "validate_sql",
    "validate_sql": "run_query",
    "run_tool_query": "generate_answer",
    "run_matched_query": "generate_answer",
    "run_query": "generate_answer",
}


def _progress_heartbeat(last_node_name: str, wait_started_at: float) -> dict[str, Any]:
    step = last_node_name or "processing"
    waiting_step = _NEXT_PROGRESS_STEP.get(last_node_name, "")
    phase, title, _ = NODE_LABELS.get(step, ("processing", "처리 중", "작업을 계속 처리하고 있습니다."))
    _, waiting_title, _ = NODE_LABELS.get(waiting_step, ("processing", "다음 단계", "작업을 계속 처리하고 있습니다."))
    elapsed = max(1, int(time.monotonic() - wait_started_at))
    if waiting_step in {"run_query", "run_matched_query", "run_tool_query"}:
        message = f"쿼리 실행 결과를 기다리고 있습니다. Athena는 결과 준비까지 시간이 걸릴 수 있습니다. ({elapsed}초)"
    else:
        message = f"{waiting_title} 응답을 기다리고 있습니다. ({elapsed}초)"
    return {
        "heartbeat": True,
        "step": step,
        "phase": phase,
        "title": "처리 대기" if last_node_name else title,
        "message": message,
        "waiting_step": waiting_step,
        "waiting_title": waiting_title,
        "elapsed_seconds": elapsed,
    }


def _stream_graph(
    req: CompatibleQueryRequest,
    state: dict[str, Any],
    *,
    context: Any | None = None,
    agent_name: str = "corporate_sales",
    user_id: str = "ui",
):
    result = dict(state)
    updates_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
    done_marker = object()

    def produce_updates() -> None:
        updates = _get_graph().stream(state, stream_mode="updates")
        while True:
            try:
                if context is None:
                    update = next(updates)
                else:
                    # Keep the common ContextVar binding only around graph work.
                    # Holding it across SSE yields can reset the token in another context.
                    with agent.observability_context(context=context, agent_name=agent_name, user_id=user_id):
                        update = next(updates)
            except StopIteration:
                updates_queue.put(("done", done_marker))
                return
            except BaseException as exc:
                updates_queue.put(("error", exc))
                return
            updates_queue.put(("update", update))

    worker = threading.Thread(target=produce_updates, daemon=True)
    worker.start()
    last_node_name = ""
    wait_started_at = time.monotonic()
    while True:
        try:
            item_type, update = updates_queue.get(timeout=PROGRESS_HEARTBEAT_SECONDS)
        except queue.Empty:
            yield "__heartbeat__", _progress_heartbeat(last_node_name, wait_started_at)
            continue
        wait_started_at = time.monotonic()
        if item_type == "done":
            break
        if item_type == "error":
            raise update
        if not isinstance(update, dict):
            continue
        for node_name, payload in update.items():
            if isinstance(payload, dict):
                result.update(payload)
            last_node_name = node_name
            yield node_name, dict(result)


def _legacy_query_request(req: LegacyQueryRequest) -> CompatibleQueryRequest:
    return CompatibleQueryRequest(
        query=req.question,
        agent_name="corporate_sales",
        session_id=req.session_id,
        params=req.params,
        continuation=req.continuation,
    )


def _run_query(
    req: CompatibleQueryRequest,
    session: dict[str, Any],
    message_id: int,
    *,
    agent_name: str = "corporate_sales",
    user_id: str = "ui",
) -> dict[str, Any]:
    started = time.monotonic()
    context = agent.create_trace_context(session_id=session["id"], message_id=message_id)
    try:
        with agent.observability_context(context=context, agent_name=agent_name, user_id=user_id):
            result = _get_graph().invoke(_state_from_request(req, session))
            data = _result_payload(result, session, message_id, req.top_k)
        agent.emit_execution_log(
            context=context,
            user_id=user_id,
            agent_name=agent_name,
            status="SUCCESS",
            total_latency_ms=int((time.monotonic() - started) * 1000),
            question=req.query,
            result=data,
        )
        return data
    except Exception as exc:
        agent.emit_execution_log(
            context=context,
            user_id=user_id,
            agent_name=agent_name,
            status="ERROR",
            total_latency_ms=int((time.monotonic() - started) * 1000),
            question=req.query,
            result={},
            error=str(exc),
        )
        raise


def _stream_query(
    req: CompatibleQueryRequest,
    session: dict[str, Any],
    message_id: int,
    *,
    agent_name: str = "corporate_sales",
    user_id: str = "ui",
):
    started = time.monotonic()
    context = agent.create_trace_context(session_id=session["id"], message_id=message_id)
    current_stage = "start"
    last_node_name = ""
    _stream_log(
        logging.INFO,
        "stream_query_started",
        session_id=session["id"],
        message_id=message_id,
        agent_name=agent_name,
    )
    yield _sse("start", {"message": "질문을 분석 중입니다...", "data": {"session_id": session["id"], "message_id": message_id, "conversation_id": _conversation_id(session)}})
    try:
        current_stage = "build_state"
        state = _state_from_request(req, session)
        final_result = dict(state)
        current_stage = "graph_stream"
        for node_name, result in _stream_graph(
            req,
            state,
            context=context,
            agent_name=agent_name,
            user_id=user_id,
        ):
            final_result = result
            if node_name == "__heartbeat__":
                yield _sse("heartbeat", result)
                continue
            last_node_name = node_name
            current_stage = f"serialize_progress:{node_name}"
            _stream_log(
                logging.INFO,
                "stream_node_completed",
                session_id=session["id"],
                message_id=message_id,
                node=node_name,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                row_count=len(final_result.get("query_rows", []) or []),
            )
            yield _sse("text2sql_progress", _progress_payload(node_name, req, final_result))
            current_stage = "graph_stream"
        current_stage = "build_result_payload"
        data = _result_payload(final_result, session, message_id, req.top_k)
        _stream_log(
            logging.INFO,
            "stream_result_payload_built",
            session_id=session["id"],
            message_id=message_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            **_payload_diagnostics(data),
        )
        if data.get("status") == "requires_params":
            current_stage = "serialize_parameter_required"
            yield _sse(
                "parameter_required",
                {
                    "message": "추가 입력이 필요합니다...",
                    "data": {
                        "operation": "parameter_required",
                        "session_id": session["id"],
                        "message_id": message_id,
                        "missing_params": data.get("missing_params", []),
                        "selected_tables": data.get("selected_tables", []),
                        "selected_tool": final_result.get("selected_tool", ""),
                    },
                },
            )
        current_stage = "finalize_assistant_message"
        _stream_log(
            logging.INFO,
            "stream_assistant_finalize_started",
            session_id=session["id"],
            message_id=message_id,
            **_payload_diagnostics(data),
        )
        data = _finalize_assistant_message(session, data, message_id)
        _stream_log(
            logging.INFO,
            "stream_assistant_finalize_completed",
            session_id=session["id"],
            message_id=message_id,
            **_payload_diagnostics(data),
        )

        current_stage = "execution_log"
        _safe_emit_execution_log(
            stream_stage=current_stage,
            session_id=session["id"],
            message_id=message_id,
            context=context,
            user_id=user_id,
            agent_name=agent_name,
            status="SUCCESS",
            total_latency_ms=int((time.monotonic() - started) * 1000),
            question=req.query,
            result=data,
        )

        current_stage = "serialize_done"
        done_chunk = _sse("done", {"message": "", "data": data})
        _stream_log(
            logging.INFO,
            "stream_done_serialized",
            session_id=session["id"],
            message_id=message_id,
            chunk_bytes=_chunk_size_bytes(done_chunk),
            elapsed_ms=int((time.monotonic() - started) * 1000),
            **_payload_diagnostics(data),
        )
        current_stage = "yield_done"
        yield done_chunk
        _stream_log(
            logging.INFO,
            "stream_query_completed",
            session_id=session["id"],
            message_id=message_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        _stream_log(
            logging.ERROR,
            "stream_query_failed",
            exc_info=True,
            session_id=session["id"],
            message_id=message_id,
            stream_stage=current_stage,
            last_node=last_node_name,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )
        _safe_emit_execution_log(
            stream_stage=f"error_log:{current_stage}",
            session_id=session["id"],
            message_id=message_id,
            context=context,
            user_id=user_id,
            agent_name=agent_name,
            status="ERROR",
            total_latency_ms=int((time.monotonic() - started) * 1000),
            question=req.query,
            result={},
            error=str(exc),
        )
        yield _sse(
            "error",
            {
                "message": "오류가 발생했습니다.",
                "data": {"error": _public_error_detail(exc), "message_id": message_id},
            },
        )
        return


def _classify_followup_intent(question: str, columns: list[Any] | None = None, rows: list[Any] | None = None) -> str:
    """Backward-compatible intent label backed by the result-aware planner."""

    return str(plan_followup(question, columns or [], rows or [])["mode"])


def _fallback_followup_context(question: str, plan: dict[str, Any]) -> dict[str, Any]:
    mode = str(plan.get("mode") or "")
    return {
        "relation": "refine_query" if plan.get("requires_sql") else "existing_result",
        "source_strategy": (
            "rediscover"
            if mode.startswith("new_sql")
            else "same_source"
            if mode.startswith("rewrite_sql")
            else "current_result"
        ),
        "resolved_question": question,
        "reason": "deterministic_fallback",
        "used_llm": False,
    }


def _resolve_followup_context(
    base_result: dict[str, Any],
    question: str,
    preliminary_plan: dict[str, Any],
) -> dict[str, Any]:
    """Resolve an elliptical follow-up before deciding how to execute it."""

    frame = ensure_query_frame(base_result)
    history = list(base_result.get("analysis_history") or [])
    recent_history = "\n".join(
        (
            f"- 사용자: {str(item.get('question') or '')[:400]}\n"
            f"  처리: {str(item.get('mode') or '')}\n"
            f"  답변: {str(item.get('answer') or '')[:700]}"
        )
        for item in history[-5:]
    )
    prompt = f"""당신은 기업영업 데이터 에이전트의 대화 문맥 라우터입니다.
현재 사용자의 말을 최근 대화와 실제 조회 상태에 연결해 해석하고 JSON object 하나만 반환하세요.

## 현재 조회를 만든 질문
{str(base_result.get("question") or "")[:1000]}

## 현재 구조화된 조회 상태
{query_frame_prompt(frame)}

## 현재 조회 컬럼
{", ".join(str(value) for value in base_result.get("query_columns", []) or []) or "(없음)"}

## 직전 답변
{str(base_result.get("answer") or "")[:1400] or "(없음)"}

## 최근 대화
{recent_history or "(없음)"}

## 새 사용자 입력
{question}

판단 기준:
- relation="existing_result": 현재 저장된 행의 설명·요약·정렬·필터·계산·시각화만 요청합니다.
- relation="refine_query": 이전 대상·기간·지표·조건 일부를 유지하면서 변경하거나, 현재 행에 없는 데이터를 추가 조회해야 합니다.
- relation="new_query": 이전 업무 대상·조건을 이어받지 않는 독립된 새 데이터 질문입니다.
- source_strategy="current_result": 현재 행만 사용합니다.
- source_strategy="same_source": 같은 도메인·테이블에서 SQL 조건을 바꾸면 됩니다.
- source_strategy="rediscover": 새 도메인·지표·테이블 또는 현재 소스에 없는 시계열이 필요합니다.
- 대명사와 생략된 대상·기간·지표는 조회 상태와 최근 대화에서만 복원하세요.
- 사용자가 명시적으로 바꾼 조건은 교체하고, 말하지 않은 조건은 refine_query일 때 유지하세요.
- 새 질문에 없는 조건이나 사실을 만들지 마세요.
- resolved_question은 SQL 담당자가 과거 대화를 보지 않아도 이해할 수 있는 완전한 한국어 질문으로 작성하세요.

반환 형식:
{{"relation":"existing_result|refine_query|new_query","source_strategy":"current_result|same_source|rediscover","resolved_question":"완전한 질문","reason":"짧은 판단 근거"}}
"""
    fallback = _fallback_followup_context(question, preliminary_plan)
    try:
        parsed = _parse_llm_json(agent._call_llm(prompt, max_tokens=700))
    except Exception:
        return fallback

    relation = str(parsed.get("relation") or "")
    source_strategy = str(parsed.get("source_strategy") or "")
    resolved_question = str(parsed.get("resolved_question") or "").strip()
    if (
        relation not in {"existing_result", "refine_query", "new_query"}
        or source_strategy not in {"current_result", "same_source", "rediscover"}
        or not resolved_question
    ):
        return fallback
    if relation == "existing_result":
        source_strategy = "current_result"
    elif relation == "new_query":
        source_strategy = "rediscover"
    return {
        "relation": relation,
        "source_strategy": source_strategy,
        "resolved_question": resolved_question[:2000],
        "reason": str(parsed.get("reason") or "")[:500],
        "used_llm": True,
    }


def _table_names_from_sql(sql: str) -> list[str]:
    used_tables = _extract_schema_tables(sql or "")
    if not used_tables:
        return []

    names: list[str] = []
    for table in SCHEMA.get("tables", []):
        logical = str(table.get("name") or "").strip()
        physical = str(table.get("physical_table") or "").strip()
        physical_short = physical.rsplit(".", 1)[-1] if physical else ""
        candidates = {logical.lower(), physical.lower(), physical_short.lower()}
        if used_tables.intersection(candidates) and logical and logical not in names:
            names.append(logical)
    return names


def _table_details_for_names(table_names: list[str]) -> str:
    if not table_names:
        return ""
    return _bounded_table_details(table_names)


def _followup_query_state(
    base_result: dict[str, Any],
    question: str,
    followup_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frame = ensure_query_frame(base_result)
    plan = followup_plan or plan_followup(
        question,
        base_result.get("query_columns", []),
        base_result.get("query_rows", []),
        query_frame=frame,
        result_scope=frame.get("result_scope"),
    )
    mode = str(plan.get("mode") or "")
    reroute_sources = mode.startswith("new_sql")
    relation = str(plan.get("context_relation") or "")
    resolved_question = str(plan.get("resolved_question") or question)
    state = agent._new_initial_state(resolved_question)
    state["question_type"] = "need_sql"
    state["skip_tool_selection"] = relation != "new_query"
    state["skip_verified_query_matching"] = relation != "new_query"
    state["query_frame"] = plan.get("next_query_frame") or frame
    if relation != "new_query":
        history = list(base_result.get("analysis_history") or [])
        previous_turn = history[-1] if history else {}
        state["previous_question"] = str(
            previous_turn.get("question")
            or base_result.get("followup_question")
            or base_result.get("question")
            or ""
        )
        state["previous_sql"] = str(base_result.get("final_sql") or "")
        state["previous_answer"] = str(base_result.get("answer") or "")
    state["followup_question"] = question
    if not reroute_sources:
        state["selected_domain"] = base_result.get("selected_domain", "")
        state["domain_candidates"] = base_result.get("domain_candidates", [])
        state["domain_routing_trace"] = base_result.get("domain_routing_trace", "")
        state["domain_context"] = base_result.get("domain_context", "")

        selected_tables = base_result.get("selected_tables") or _table_names_from_sql(base_result.get("final_sql", ""))
        if selected_tables:
            state["selected_tables"] = selected_tables
            state["table_details"] = base_result.get("table_details", "") or _table_details_for_names(selected_tables)
    return state


def _followup_analysis(base_result: dict[str, Any], question: str) -> str:
    columns = base_result.get("query_columns", [])
    rows = base_result.get("query_rows", [])
    history = base_result.get("analysis_history", [])
    original_answer = base_result.get("original_answer") or base_result.get("answer", "")
    previous_answer = base_result.get("answer", "")
    history_text = "\n".join(
        f"- 질문: {str(item.get('question', ''))[:300]}\n  답변: {str(item.get('answer', ''))[:700]}"
        for item in history[-3:]
    )
    prompt = f"""당신은 KB카드 기업영업 데이터 분석가입니다.
아래는 이미 실행된 SQL 결과입니다. 새 SQL을 만들거나 외부 데이터를 가정하지 말고, 제공된 결과 안에서만 후속 질문에 답하세요.
후속 질문이 단순 정리/설명/요약이면 핵심만 정리하고, 그래프/그림/차트/도식 요청이면 현재 결과를 바탕으로 텍스트 표, 막대 표현, 간단한 흐름도처럼 화면에서 바로 읽을 수 있는 형태로 답하세요.

[원 질문]
{base_result.get("question", "")}

[후속 질문]
{question}

[실행 SQL]
{base_result.get("final_sql", "")}

[최초 답변]
{str(original_answer)[:1800]}

[직전 답변]
{str(previous_answer)[:1800]}

[이전 후속 분석]
{history_text or "(없음)"}

[조회 컬럼]
{", ".join(str(col) for col in columns)}

[조회 결과 샘플]
{_compact_table_text(columns, rows, limit=30)}

답변 방식:
- 요청에 맞게 `### 핵심 요약`, `### 시각화`, `### 분석` 중 필요한 섹션만 사용
- 정리만 요청한 경우에는 5줄 이내의 요약 또는 작은 Markdown 표로 답변
- 그래프/그림/차트 요청이면 Mermaid/HTML/SVG/코드블록 대신 Markdown 표나 텍스트 막대(예: `항목 | 값 | 막대`)로 표현
- 숫자는 가능한 한 조회 결과의 값에 근거해서 언급
- 제공된 결과만으로 답할 수 없으면 필요한 추가 조회 기준을 짧게 말하고 값을 지어내지 말 것
- 다음 질문 추천은 유용할 때만 최대 2개 제안
"""
    return agent._call_llm(prompt, max_tokens=1600).strip()


def _followup_cell(value: Any) -> str:
    value = _jsonable(value)
    if value is None:
        return "-"
    if isinstance(value, float):
        if not value.is_integer():
            return f"{value:,.2f}"
        return f"{value:,.0f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value).replace("|", "\\|").replace("\n", " ")


def _local_followup_answer(transform: dict[str, Any], chart: dict[str, Any] | None = None) -> str:
    operations = list(transform.get("operations") or [])
    original_count = int(transform.get("original_row_count") or 0)
    row_count = int(transform.get("row_count") or 0)
    columns = [str(column) for column in transform.get("columns") or []]
    rows = transform.get("rows") or []
    operation_text = " → ".join(operations) if operations else "표현 변경 없음"
    lines = [
        "### 기존 결과 재가공",
        f"직전 조회 결과 {original_count:,}건에 **{operation_text}**을 적용해 {row_count:,}건으로 정리했습니다.",
    ]
    if chart:
        lines.extend(["", "### 시각화", f"**{chart.get('title', '차트')}**를 생성했습니다. {chart.get('note', '')}".strip()])
    if columns and rows:
        visible_columns = columns[:8]
        lines.extend(
            [
                "",
                "| " + " | ".join(visible_columns) + " |",
                "| " + " | ".join("---" for _ in visible_columns) + " |",
            ]
        )
        for row in rows[:15]:
            values = list(row) if isinstance(row, (list, tuple)) else [row]
            lines.append("| " + " | ".join(_followup_cell(values[index] if index < len(values) else None) for index in range(len(visible_columns))) + " |")
        if len(rows) > 15:
            lines.append(f"\n표에는 앞의 15건만 미리 표시했으며, 전체 {len(rows):,}건은 데이터 탭에서 확인할 수 있습니다.")
    lines.append("\n> 새 SQL을 실행하지 않고, 직전 결과로 저장된 행 범위 안에서만 처리했습니다.")
    return "\n".join(lines)


def _chart_followup_answer(chart: dict[str, Any] | None) -> str:
    if not chart:
        return (
            "### 시각화\n"
            "직전 결과에서 차트의 값 축으로 사용할 숫자 컬럼을 찾지 못했습니다. "
            "차트에 사용할 숫자 컬럼을 지정하거나 필요한 지표를 새로 조회해 주세요."
        )
    return (
        "### 시각화\n"
        f"직전 SQL을 다시 실행하지 않고 **{chart.get('title', '차트')}**를 생성했습니다. "
        f"{chart.get('note', '')}\n\n"
        "> 차트와 데이터 탭은 동일한 직전 조회 결과를 사용합니다."
    )


def _finalize_followup_result(
    base_result: dict[str, Any],
    question: str,
    followup_result: dict[str, Any],
    answer: str,
    mode: str,
    source: str,
    followup_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    plan = followup_plan or {}
    history = list(base_result.get("analysis_history", []))
    uses_sql = mode.startswith("rewrite_sql") or mode.startswith("new_sql") or mode == "query"
    resolved_question = str(plan.get("resolved_question") or question)
    context_relation = str(plan.get("context_relation") or "")
    history.append(
        {
            "question": question,
            "resolved_question": resolved_question,
            "context_relation": context_relation,
            "context_reason": str(plan.get("context_reason") or ""),
            "answer": answer,
            "mode": mode,
            "sql": followup_result.get("final_sql", "") if uses_sql else "",
            "base_sql": "" if uses_sql else base_result.get("final_sql", ""),
            "columns": _jsonable(followup_result.get("query_columns", [])),
            "row_count": len(followup_result.get("query_rows", [])),
            "source": source,
            "operations": _jsonable(followup_result.get("followup_operations", [])),
            "has_chart": bool(followup_result.get("chart")),
            "route_reason": str(plan.get("route_reason") or ""),
            "result_complete": bool(infer_result_scope(followup_result).get("is_complete", True)),
        }
    )
    followup_result.update(
        {
            "question": resolved_question if uses_sql else base_result.get("question", ""),
            "original_answer": base_result.get("original_answer", base_result.get("answer", "")),
            "answer": answer,
            "followup_question": question,
            "analysis_history": history,
            "followup_mode": mode,
            "followup_route": {
                "mode": mode,
                "reason": str(plan.get("route_reason") or ""),
                "confidence": str(plan.get("route_confidence") or ""),
                "requested_metrics": list(plan.get("requested_metrics") or []),
                "new_metrics": list(plan.get("new_metrics") or []),
                "context_relation": context_relation,
                "source_strategy": str(plan.get("source_strategy") or ""),
                "resolved_question": resolved_question,
                "context_reason": str(plan.get("context_reason") or ""),
                "context_resolved_by_llm": bool(plan.get("context_resolved_by_llm")),
            },
            "error_message": "",
        }
    )
    previous_frame = plan.get("next_query_frame") or ensure_query_frame(base_result)
    followup_result["result_scope"] = infer_result_scope(followup_result)
    followup_result["query_frame"] = build_query_frame(
        followup_result,
        previous_frame=previous_frame,
        last_question=question,
    )
    return followup_result


def _stream_followup(
    req: FollowupRequest,
    session: dict[str, Any],
    message_id: int,
    *,
    agent_name: str = "corporate_sales",
    user_id: str = "ui",
):
    started = time.monotonic()
    context = agent.create_trace_context(session_id=session["id"], message_id=message_id)
    base_result = _SESSION_STORE.get_result(req.result_id, session.get("user_id"))
    if not base_result:
        yield _sse("error", {"detail": "이전 결과를 찾을 수 없습니다."})
        return
    if not base_result.get("query_columns") or not base_result.get("query_rows"):
        yield _sse("error", {"detail": "후속 분석에 사용할 조회 데이터가 없습니다."})
        return

    yield _sse("progress", {"step": "start", "title": "요청 접수", "message": "이전 결과와 대화 이력을 불러왔습니다.", "query": req.question})
    try:
        base_frame = ensure_query_frame(base_result)
        yield _sse(
            "progress",
            {
                "step": "followup_context",
                "title": "대화 맥락 해석",
                "message": "이전 질의의 대상·기간·지표와 새 질문의 변경점을 함께 확인합니다.",
                "query": req.question,
            },
        )
        with agent.observability_context(context=context, agent_name=agent_name, user_id=user_id):
            preliminary_plan = plan_followup(
                req.question,
                base_result.get("query_columns", []),
                base_result.get("query_rows", []),
                query_frame=base_frame,
                result_scope=base_frame.get("result_scope"),
            )
            context_resolution = _resolve_followup_context(
                base_result,
                req.question,
                preliminary_plan,
            )
            followup_plan = plan_followup(
                req.question,
                base_result.get("query_columns", []),
                base_result.get("query_rows", []),
                query_frame=base_frame,
                result_scope=base_frame.get("result_scope"),
                context_resolution=context_resolution,
            )
            intent = str(followup_plan["mode"])
            resolved_question = str(followup_plan.get("resolved_question") or req.question)
            route_progress = {
                "step": "followup_route",
                "title": "후속 의도 판단",
                "query": req.question,
                "followup_mode": intent,
                "requires_sql": bool(followup_plan["requires_sql"]),
                "context_relation": followup_plan.get("context_relation", ""),
            }

        if followup_plan["requires_sql"]:
            sql_intent = "new_sql" if intent.startswith("new_sql") else "rewrite_sql"
            wants_chart = bool(followup_plan["visualize"])
            if followup_plan.get("route_reason") == "incomplete_result_requires_sql":
                route_message = "직전 결과가 일부 범위이므로 정확한 상위 N·집계를 위해 SQL을 다시 실행합니다."
            else:
                route_message = "후속 요청에 새 데이터가 필요해 도메인과 테이블을 다시 선택합니다." if sql_intent == "new_sql" else "기간·대상·조회 조건이 달라져 기존 SQL을 재작성합니다."
            if followup_plan.get("context_relation") == "new_query":
                route_message = "이전 조건을 이어받지 않는 새 질문으로 판단해 도메인과 테이블을 새로 선택합니다."
            if wants_chart:
                route_message += " 조회가 끝나면 새 결과로 차트도 생성합니다."
            yield _sse("progress", {**route_progress, "message": route_message})
            state = _followup_query_state(base_result, req.question, followup_plan)
            compatible = CompatibleQueryRequest(query=req.question, session_id=req.session_id)
            followup_result = dict(state)
            for node_name, result in _stream_graph(
                compatible,
                state,
                context=context,
                agent_name=agent_name,
                user_id=user_id,
            ):
                followup_result = result
                if node_name == "__heartbeat__":
                    yield _sse("heartbeat", followup_result)
                    continue
                payload = _progress_payload(node_name, compatible, followup_result)
                yield _sse(
                    "progress",
                    {
                        **payload["data"],
                        "step": node_name,
                        "title": payload["data"]["title"],
                        "message": payload["message"],
                        "query": req.question,
                    },
                )
            answer = followup_result.get("answer", "")
            chart = build_chart_spec(resolved_question, followup_result.get("query_columns", []), followup_result.get("query_rows", [])) if wants_chart else None
            if chart:
                followup_result["chart"] = chart
                answer = (
                    f"{answer}\n\n### 시각화\n"
                    f"새로 조회한 결과로 **{chart.get('title', '차트')}**를 생성했습니다. {chart.get('note', '')}"
                ).strip()
            else:
                followup_result.pop("chart", None)
            followup_result["followup_operations"] = ["SQL 재조회"] + (["차트 생성"] if chart else [])
            source = "후속 SQL + 시각화" if chart else "후속 SQL"
            final_result = _finalize_followup_result(base_result, req.question, followup_result, answer, intent, source, followup_plan)
        elif intent in {"transform", "transform_visualization"}:
            transform = followup_plan["transform"]
            wants_chart = bool(followup_plan["visualize"])
            route_message = "후속 요청을 직전 결과의 로컬 재가공으로 분류했습니다. SQL은 다시 실행하지 않습니다."
            if wants_chart:
                route_message += " 재가공한 결과로 차트를 생성합니다."
            yield _sse("progress", {**route_progress, "message": route_message})
            yield _sse("progress", {"step": "generate_answer", "title": "결과 재가공", "message": "저장된 결과에 정렬·필터·집계·표현 변경을 안전하게 적용합니다.", "query": req.question})
            local_result = dict(base_result)
            local_result.update(
                {
                    "query_columns": transform["columns"],
                    "query_rows": transform["rows"],
                    "followup_operations": transform["operations"],
                    "local_result_scope": {
                        "input_rows": transform["original_row_count"],
                        "output_rows": transform["row_count"],
                    },
                }
            )
            chart = build_chart_spec(resolved_question, transform["columns"], transform["rows"]) if wants_chart else None
            if chart:
                local_result["chart"] = chart
            else:
                local_result.pop("chart", None)
            answer = _local_followup_answer(transform, chart)
            source = "기존 결과 재가공 + 시각화" if chart else "기존 결과 재가공"
            final_result = _finalize_followup_result(base_result, req.question, local_result, answer, intent, source, followup_plan)
        elif intent == "visualization":
            yield _sse("progress", {**route_progress, "message": "직전 결과만 사용해 차트를 생성합니다. SQL은 다시 실행하지 않습니다."})
            yield _sse("progress", {"step": "generate_answer", "title": "차트 생성", "message": "숫자·범주·시간 컬럼을 판별해 차트 데이터를 구성합니다.", "query": req.question})
            visual_result = dict(base_result)
            chart = build_chart_spec(resolved_question, base_result.get("query_columns", []), base_result.get("query_rows", []))
            if chart:
                visual_result["chart"] = chart
                visual_result["followup_operations"] = ["차트 생성"]
            else:
                visual_result.pop("chart", None)
                visual_result["followup_operations"] = []
            answer = _chart_followup_answer(chart)
            source = "기존 결과 시각화"
            final_result = _finalize_followup_result(base_result, req.question, visual_result, answer, intent, source, followup_plan)
        else:
            source = "기존 결과 분석"
            yield _sse("progress", {**route_progress, "message": "후속 요청을 직전 결과 설명·분석으로 분류했습니다. SQL은 다시 실행하지 않습니다."})
            yield _sse("progress", {"step": "generate_answer", "title": "답변 생성", "message": "기존 SQL 결과와 대화 이력을 기반으로 답변을 생성합니다.", "query": req.question})
            with agent.observability_context(context=context, agent_name=agent_name, user_id=user_id):
                answer = _followup_analysis(base_result, req.question)
            analysis_result = dict(base_result)
            analysis_result.pop("chart", None)
            analysis_result["followup_operations"] = []
            final_result = _finalize_followup_result(base_result, req.question, analysis_result, answer, intent, source, followup_plan)
        final_result["parent_result_id"] = req.result_id
        data = _result_payload(final_result, session, message_id, 10, source_override=source)
        data = _finalize_assistant_message(session, data, message_id)
    except Exception as exc:
        agent.emit_execution_log(
            context=context,
            user_id=user_id,
            agent_name=agent_name,
            status="ERROR",
            total_latency_ms=int((time.monotonic() - started) * 1000),
            question=req.question,
            result={},
            error=str(exc),
        )
        yield _sse("error", {"detail": _public_error_detail(exc)})
        return
    agent.emit_execution_log(
        context=context,
        user_id=user_id,
        agent_name=agent_name,
        status="SUCCESS",
        total_latency_ms=int((time.monotonic() - started) * 1000),
        question=req.question,
        result=data,
    )
    yield _sse("progress", {"step": "complete", "title": "완료", "message": "최종 결과를 화면에 표시합니다.", "query": req.question})
    yield _sse("result", data)


def _legacy_stream_adapter(chunks):
    for chunk in chunks:
        if chunk.startswith("event: text2sql_progress"):
            payload = json.loads(chunk.split("data: ", 1)[1])
            data = payload.get("data", {})
            yield _sse(
                "progress",
                {
                    "step": data.get("text2sql_step", ""),
                    "phase": data.get("phase", ""),
                    "title": data.get("title", ""),
                    "message": payload.get("message", ""),
                },
            )
        elif chunk.startswith("event: parameter_required"):
            continue
        elif chunk.startswith("event: done"):
            payload = json.loads(chunk.split("data: ", 1)[1])
            yield _sse("result", payload.get("data", {}))
        elif chunk.startswith("event: start"):
            payload = json.loads(chunk.split("data: ", 1)[1])
            yield _sse(
                "progress",
                {"step": "start", "title": "요청 접수", "message": payload.get("message", "질문을 분석 중입니다...")},
            )
        else:
            yield chunk


def _llm_health() -> dict[str, Any]:
    """Cached LLM readiness probe via the common client (see agent.probe_llm)."""
    now = time.time()
    cached = _LLM_HEALTH_CACHE.get("data")
    if cached and now - float(_LLM_HEALTH_CACHE.get("checked_at", 0.0)) < _LLM_HEALTH_TTL_SECONDS:
        return dict(cached)

    data = agent.probe_llm()
    _LLM_HEALTH_CACHE["checked_at"] = now
    _LLM_HEALTH_CACHE["data"] = dict(data)
    return data


@app.post("/api/v1/agent/{agent_name}/query")
def query(
    agent_name: str,
    req: CompatibleQueryRequest,
    x_user_id: str = Header(..., alias="X-User-ID"),
    x_agent_name: str = Header(..., alias="X-Agent-Name"),
    x_session_id: str | None = Header(None, alias="X-Session-ID"),
):
    _validate_agent(agent_name, x_agent_name, req.agent_name)
    if req.result_id:
        raise HTTPException(status_code=400, detail="result_id 후속 질문은 스트리밍 API를 사용해주세요.")
    session = _get_or_create_session(req.session_id or x_session_id, x_user_id, agent_name)
    message_id = _message_id()
    user_text = _user_message_text(req.query, req.params)
    _append_message(session, "user", user_text, message_id=message_id)
    try:
        data = _run_query(req, session, message_id, agent_name=agent_name, user_id=x_user_id)
    except Exception as exc:
        _raise_api_error(exc)
    data = _finalize_assistant_message(session, data, message_id)
    return webapp_api.compatible_json_response(data)


@app.post("/api/v1/agent/{agent_name}/query/stream")
def query_stream(
    agent_name: str,
    req: CompatibleQueryRequest,
    x_user_id: str = Header(..., alias="X-User-ID"),
    x_agent_name: str = Header(..., alias="X-Agent-Name"),
    x_session_id: str | None = Header(None, alias="X-Session-ID"),
):
    _validate_agent(agent_name, x_agent_name, req.agent_name)
    session = _get_or_create_session(req.session_id or x_session_id, x_user_id, agent_name)
    message_id = _message_id()
    user_text = _user_message_text(req.query, req.params)
    _append_message(session, "user", user_text, message_id=message_id)
    if req.result_id:
        followup_req = FollowupRequest(result_id=req.result_id, question=req.query, session_id=session["id"])

        def stream_chunks():
            yield _sse(
                "start",
                {
                    "message": "질문을 분석 중입니다...",
                    "data": {
                        "session_id": session["id"],
                        "message_id": message_id,
                        "conversation_id": _conversation_id(session),
                    },
                },
            )
            yield from _stream_followup(followup_req, session, message_id, agent_name=agent_name, user_id=x_user_id)
        stream = stream_chunks()
    else:
        stream = _stream_query(req, session, message_id, agent_name=agent_name, user_id=x_user_id)
    return StreamingResponse(
        _observed_sse_stream(
            webapp_api.adapt_sse_stream(stream),
            session_id=session["id"],
            message_id=message_id,
        ),
        media_type="text/event-stream",
        headers=_stream_response_headers(message_id),
    )


@app.get("/health")
def health():
    llm_health = _llm_health()
    session_status = _SESSION_STORE.status()
    llm_ready = bool(llm_health.get("llm_ready"))
    return {
        "ok": True,
        "llm_ready": llm_ready,
        "llm_status": llm_health.get("llm_status", "unavailable"),
        "llm_detail": "모델 연결 정상" if llm_ready else "모델 서버 연결 또는 인증 상태를 확인해 주세요.",
        "session_store": {
            "kind": session_status.get("kind", "unknown"),
            "persistent": bool(session_status.get("persistent")),
        },
        "semantic_layer": {
            "tables": len(SCHEMA.get("tables", [])),
            "domains": len(SCHEMA.get("canonical_domains", [])),
            "metrics": len(SCHEMA.get("canonical_metrics", [])),
        },
        "verified_queries": {
            "enabled": len(SCHEMA.get("verified_queries", [])),
            "disabled": len(SCHEMA.get("disabled_verified_queries", [])),
        },
    }



@app.get("/api/examples")
def examples():
    return {
        "examples": [
            "2025년 월별 법인카드 이용금액 합계와 전월 대비 증감률을 보여줘",
            "2025년 업종별 매출 상위 10개를 알려줘",
            "2025년 12월 한빛테크놀로지의 대손비용률을 분석해줘",
            "2025년 월별 해외 카드 이용금액 추이를 보여줘",
            "2025년 12월 말 기준 연체 회원 수를 알려줘",
            "2025년 12월 말 기준 여신한도가 큰 기업 상위 10개를 보여줘",
        ]
    }


@app.get("/api/tables")
def table_catalog():
    tables = []
    for table in SCHEMA.get("tables", []):
        if not _is_semantic_table_visible(table):
            continue
        name = str(table.get("name") or "").strip()
        if not name:
            continue
        tables.append(
            {
                "name": name,
                "physical_table": str(table.get("physical_table") or name).strip(),
                "korean_name": str(table.get("korean_name") or name).strip(),
                "description": " ".join(str(table.get("description") or "").split()),
                "grain": " ".join(str(table.get("grain") or "").split()),
            }
        )
    metadata = SCHEMA.get("semantic_layer_metadata") or {}
    return {
        "count": len(tables),
        "semantic_layer_version": str(metadata.get("version") or ""),
        "tables": tables,
    }


@app.post("/api/managed-company-scope/parse")
async def parse_managed_company_scope_upload(
    request: Request,
    filename: str,
    x_user_id: str | None = Header(None, alias="X-User-ID"),
):
    """Validate a scope file in memory and return request-ready identifiers.

    The service deliberately does not persist the upload or create a shared
    staging table.  The caller sends the returned list with the pending query,
    where it becomes a request-scoped Athena ``VALUES`` CTE.
    """

    _request_user_id(x_user_id)  # normalize the boundary without storing content
    try:
        declared_size = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared_size = 0
    if declared_size > MAX_MANAGED_SCOPE_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="파일 크기는 5MB 이하여야 합니다.")
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_MANAGED_SCOPE_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="파일 크기는 5MB 이하여야 합니다.")
        chunks.append(chunk)
    content = b"".join(chunks)
    try:
        return parse_managed_scope_upload(filename, content)
    except ManagedScopeParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/managed-company-scope/template")
def download_managed_company_scope_template():
    if not MANAGED_SCOPE_TEMPLATE_PATH.is_file():
        raise HTTPException(status_code=404, detail="관리기업 목록 예시 파일이 준비되지 않았습니다.")
    return FileResponse(
        str(MANAGED_SCOPE_TEMPLATE_PATH),
        filename=MANAGED_SCOPE_TEMPLATE_PATH.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/sessions")
def create_session(
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_agent_name: str | None = Header(None, alias="X-Agent-Name"),
):
    user_id = _request_user_id(x_user_id)
    agent_name = _request_agent_name(x_agent_name)
    _SESSION_STORE.prune_empty_sessions(user_id, agent_name)
    session = _get_or_create_session(None, user_id, agent_name)
    return {"session": _session_summary(session), "messages": []}


@app.get("/api/sessions")
def list_sessions(
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_agent_name: str | None = Header(None, alias="X-Agent-Name"),
):
    user_id = _request_user_id(x_user_id)
    agent_name = _request_agent_name(x_agent_name)
    pruned = _SESSION_STORE.prune_empty_sessions(user_id, agent_name)
    sessions = _SESSION_STORE.list_sessions(user_id, agent_name)
    return {"sessions": [_session_summary(session) for session in sessions], "pruned_empty_sessions": pruned}


@app.get("/api/sessions/{session_id}")
def get_session(
    session_id: str,
    x_user_id: str | None = Header(None, alias="X-User-ID"),
):
    session = _SESSION_STORE.get_session(session_id, _request_user_id(x_user_id))
    if not session:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return {
        "session": _session_summary(session),
        "messages": _jsonable(session.get("messages", [])),
        "last_result": _last_result_payload(session),
    }


@app.delete("/api/sessions/{session_id}")
def delete_session(
    session_id: str,
    x_user_id: str | None = Header(None, alias="X-User-ID"),
):
    deleted = _SESSION_STORE.delete_session(session_id, _request_user_id(x_user_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="세션을 찾을 수 없습니다.")
    return {"deleted": True, "session_id": session_id}


@app.get("/api/saved-queries")
def list_saved_queries(
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_agent_name: str | None = Header(None, alias="X-Agent-Name"),
):
    user_id = _request_user_id(x_user_id)
    agent_name = _request_agent_name(x_agent_name)
    saved_queries = _SESSION_STORE.list_saved_queries(user_id, agent_name)
    return {"saved_queries": [_saved_query_summary(saved_query) for saved_query in saved_queries]}


@app.post("/api/saved-queries")
def save_saved_query(
    req: SavedQueryRequest,
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_agent_name: str | None = Header(None, alias="X-Agent-Name"),
):
    user_id = _request_user_id(x_user_id)
    agent_name = _request_agent_name(x_agent_name)
    try:
        saved_query = _SESSION_STORE.save_saved_query(user_id, agent_name, _saved_query_payload(req))
    except SessionOwnershipError as exc:
        raise HTTPException(status_code=403, detail="다른 사용자의 저장 쿼리는 수정할 수 없습니다.") from exc
    return {"saved_query": _saved_query_summary(saved_query)}


@app.post("/api/saved-queries/{query_id}/render")
def render_saved_query(
    query_id: str,
    req: SavedQueryRenderRequest,
    x_user_id: str | None = Header(None, alias="X-User-ID"),
):
    user_id = _request_user_id(x_user_id)
    saved_query = _SESSION_STORE.get_saved_query(query_id, user_id)
    if not saved_query:
        raise HTTPException(status_code=404, detail="저장 쿼리를 찾을 수 없습니다.")
    return {"saved_query": _saved_query_summary(saved_query), **_render_saved_query(saved_query, req.params)}


@app.delete("/api/saved-queries/{query_id}")
def delete_saved_query(
    query_id: str,
    x_user_id: str | None = Header(None, alias="X-User-ID"),
):
    deleted = _SESSION_STORE.delete_saved_query(query_id, _request_user_id(x_user_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="저장 쿼리를 찾을 수 없습니다.")
    return {"deleted": True, "saved_query_id": query_id}


@app.post("/api/query")
def legacy_query(
    req: LegacyQueryRequest,
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_agent_name: str | None = Header(None, alias="X-Agent-Name"),
):
    compatible = _legacy_query_request(req)
    user_id = _request_user_id(x_user_id)
    agent_name = _request_agent_name(x_agent_name)
    session = _get_or_create_session(compatible.session_id, user_id, agent_name)
    message_id = _message_id()
    user_text = _user_message_text(req.question, req.params)
    _append_message(session, "user", user_text, message_id=message_id)
    try:
        data = _run_query(compatible, session, message_id, agent_name=agent_name, user_id=user_id)
    except Exception as exc:
        _raise_api_error(exc)
    data = _finalize_assistant_message(session, data, message_id)
    return data


@app.post("/api/query/stream")
def legacy_query_stream(
    req: LegacyQueryRequest,
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_agent_name: str | None = Header(None, alias="X-Agent-Name"),
):
    compatible = _legacy_query_request(req)
    user_id = _request_user_id(x_user_id)
    agent_name = _request_agent_name(x_agent_name)
    session = _get_or_create_session(compatible.session_id, user_id, agent_name)
    message_id = _message_id()
    user_text = _user_message_text(req.question, req.params)
    _append_message(session, "user", user_text, message_id=message_id)
    return StreamingResponse(
        _observed_sse_stream(
            _legacy_stream_adapter(_stream_query(compatible, session, message_id, agent_name=agent_name, user_id=user_id)),
            session_id=session["id"],
            message_id=message_id,
        ),
        media_type="text/event-stream",
        headers=_stream_response_headers(message_id),
    )


@app.post("/api/followup/stream")
def legacy_followup_stream(
    req: FollowupRequest,
    x_user_id: str | None = Header(None, alias="X-User-ID"),
    x_agent_name: str | None = Header(None, alias="X-Agent-Name"),
):
    user_id = _request_user_id(x_user_id)
    agent_name = _request_agent_name(x_agent_name)
    if not _SESSION_STORE.get_result(req.result_id, user_id):
        raise HTTPException(status_code=404, detail="이전 결과를 찾을 수 없습니다.")
    session = _get_or_create_session(req.session_id, user_id, agent_name)
    message_id = _message_id()
    _append_message(session, "user", req.question.strip(), message_id=message_id)
    return StreamingResponse(
        _observed_sse_stream(
            _stream_followup(req, session, message_id, agent_name=agent_name, user_id=user_id),
            session_id=session["id"],
            message_id=message_id,
        ),
        media_type="text/event-stream",
        headers=_stream_response_headers(message_id),
    )


@app.post("/api/export")
def export_result(
    req: ExportRequest,
    x_user_id: str | None = Header(None, alias="X-User-ID"),
):
    user_id = _request_user_id(x_user_id)
    result = _SESSION_STORE.get_result(req.result_id, user_id)
    if not result:
        raise HTTPException(status_code=404, detail="결과를 찾을 수 없습니다.")
    fmt = req.format.lower().strip()
    files: list[dict[str, str]] = []
    if fmt == "all":
        paths = agent.export_all(result)
        for kind, path in paths.items():
            registered = _register_file(path, user_id=user_id)
            if registered:
                registered["kind"] = kind
                files.append(registered)
    elif fmt in {"word", "docx"}:
        registered = _register_file(agent.export_to_word(result), user_id=user_id)
        if registered:
            registered["kind"] = "word"
            files.append(registered)
    elif fmt in {"text", "txt"}:
        registered = _register_file(agent.export_to_text(result), user_id=user_id)
        if registered:
            registered["kind"] = "text"
            files.append(registered)
    elif fmt == "csv":
        registered = _register_file(agent.export_to_csv(result), user_id=user_id)
        if registered:
            registered["kind"] = "csv"
            files.append(registered)
    elif fmt in {"excel", "xlsx"}:
        excel_path = result.get("bad_debt_excel_path", "") or agent.export_to_excel(result)
        registered = _register_file(excel_path, user_id=user_id)
        if registered:
            registered["kind"] = "excel"
            files.append(registered)
    else:
        raise HTTPException(status_code=400, detail="지원하지 않는 export 형식입니다.")
    if not files:
        raise HTTPException(status_code=404, detail="생성된 파일이 없습니다.")
    return {"files": files}


@app.get("/api/files/{token}")
def download_file(token: str):
    path = _SESSION_STORE.get_file(token)
    if not path:
        raise HTTPException(status_code=404, detail="파일을 찾을 수 없습니다.")
    file_path = Path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="파일이 삭제되었거나 이동되었습니다.")
    return FileResponse(str(file_path), filename=file_path.name)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-store"})


@app.head("/")
def index_head():
    return Response(status_code=200, media_type="text/html")


@app.get("/{full_path:path}")
def static_fallback(full_path: str):
    file_path = STATIC_DIR / full_path
    if file_path.is_file():
        headers = {"Cache-Control": "no-store"} if file_path.suffix == ".html" else None
        return FileResponse(str(file_path), headers=headers)
    return FileResponse(str(STATIC_DIR / "index.html"), headers={"Cache-Control": "no-store"})

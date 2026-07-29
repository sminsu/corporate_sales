"""Adapters for the WebApp Compatible API SSE contract.

The Text2SQL service has richer internal progress events than the shared
WebApp UI expects. This module keeps that translation isolated so the local
Text2SQL UI can keep using its existing legacy endpoints while /api/v1 emits
the generic WebApp-compatible event names.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Iterator


SEARCH_PLAN_NODES = {
    "classify_question",
    "route_domain",
    "select_tool",
    "check_tool_params",
    "match_verified_query",
    "analyze_question",
    "check_sql_gen_params",
}

AGGREGATE_REVIEW_NODES = {
    "execute_tool",
    "run_tool_query",
    "extract_and_apply_params",
    "generate_sql",
    "validate_sql",
    "run_matched_query",
    "run_query",
}

RESPONSE_NODES = {
    "generate_answer",
    "direct_answer",
    "reject_answer",
    "handle_error",
}


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: dict[str, Any]


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(_jsonable(data), ensure_ascii=False)}\n\n"


def _parse_sse_chunk(chunk: str) -> SSEEvent | None:
    lines = chunk.strip().splitlines()
    if not lines:
        return None
    event = "message"
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    if not data_lines:
        return SSEEvent(event=event, data={})
    try:
        data = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return SSEEvent(event=event, data=data)


def compatible_json_response(data: dict[str, Any]) -> dict[str, Any]:
    """Return the non-streaming response envelope expected by the WebApp UI."""

    return {"success": True, "data": _jsonable(data)}


def adapt_sse_stream(chunks: Iterable[str]) -> Iterator[str]:
    """Translate Text2SQL SSE chunks into the WebApp Compatible API sequence."""

    emitted = {"search_plan": False, "aggregate_review": False, "response": False}
    latest_query = ""
    pending_response_payload: dict[str, Any] | None = None

    for chunk in chunks:
        parsed = _parse_sse_chunk(chunk)
        if parsed is None:
            yield chunk
            continue

        if parsed.event == "start":
            yield _sse("start", parsed.data)
            continue

        if parsed.event == "error":
            yield _sse("error", parsed.data)
            continue

        if parsed.event == "heartbeat":
            yield _sse("heartbeat", parsed.data)
            continue

        if parsed.event == "parameter_required":
            pending_response_payload = _response_payload(parsed.data.get("data", {}), message=parsed.data.get("message"))
            continue

        if parsed.event == "text2sql_progress":
            payload = parsed.data
            progress = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}
            latest_query = str(progress.get("query") or latest_query)
            mapped_event = _event_name_for_node(str(progress.get("text2sql_step") or ""))
            if mapped_event:
                yield _sse(mapped_event, _payload_for_event(mapped_event, payload, latest_query))
                emitted[mapped_event] = True
            continue

        if parsed.event == "progress":
            progress = _legacy_progress_data(parsed.data)
            latest_query = str(progress.get("followup_question") or progress.get("query") or latest_query)
            mapped_event = _event_name_for_node(str(progress.get("text2sql_step") or ""))
            if progress.get("text2sql_step") == "start":
                continue
            if mapped_event == "search_plan" and not latest_query:
                continue
            if mapped_event:
                payload = _payload_for_event(mapped_event, {"message": parsed.data.get("message"), "data": progress}, latest_query)
                if mapped_event == "response" and not emitted["aggregate_review"]:
                    pending_response_payload = payload
                    continue
                yield _sse(mapped_event, payload)
                emitted[mapped_event] = True
            continue

        if parsed.event in {"done", "result"}:
            data = parsed.data.get("data", parsed.data)
            if isinstance(data, dict):
                latest_query = str(data.get("followup_question") or data.get("question") or latest_query)
                for event_name in ("search_plan", "aggregate_review", "response"):
                    if emitted[event_name]:
                        continue
                    if event_name == "response" and pending_response_payload is not None:
                        yield _sse(event_name, pending_response_payload)
                    else:
                        yield _sse(event_name, _payload_for_final_event(event_name, data, latest_query))
                    emitted[event_name] = True
            yield _sse("done", parsed.data if parsed.event == "done" else {"message": "", "data": parsed.data})
            continue

        yield chunk


def _event_name_for_node(node_name: str) -> str | None:
    if node_name in {"start", "followup_context", "followup_route"}:
        return "search_plan"
    if node_name == "complete":
        return "response"
    if node_name in SEARCH_PLAN_NODES:
        return "search_plan"
    if node_name in AGGREGATE_REVIEW_NODES:
        return "aggregate_review"
    if node_name in RESPONSE_NODES:
        return "response"
    return None


def _legacy_progress_data(data: dict[str, Any]) -> dict[str, Any]:
    step = str(data.get("step") or data.get("text2sql_step") or "")
    return {
        **data,
        "text2sql_step": step,
        "phase": data.get("phase", ""),
        "title": data.get("title", ""),
    }


def _payload_for_event(event_name: str, payload: dict[str, Any], query: str) -> dict[str, Any]:
    progress = payload.get("data", {}) if isinstance(payload.get("data"), dict) else {}
    if event_name == "search_plan":
        return _search_plan_payload(progress, payload.get("message"), query)
    if event_name == "aggregate_review":
        return _aggregate_review_payload(progress, payload.get("message"), query)
    return _response_payload(progress, payload.get("message"))


def _payload_for_final_event(event_name: str, data: dict[str, Any], query: str) -> dict[str, Any]:
    if event_name == "search_plan":
        return _search_plan_payload(data, "질문을 분석하고 실행 계획을 세우고 있습니다...", query)
    if event_name == "aggregate_review":
        return _aggregate_review_payload(data, "조회 결과와 근거를 종합하고 있습니다...", query)
    return _response_payload(data, "답변을 작성 중입니다...")


def _search_plan_payload(data: dict[str, Any], message: Any, query: str) -> dict[str, Any]:
    resolved_query = query or str(data.get("followup_question") or data.get("query") or data.get("question") or "")
    return {
        "message": "질문을 분석하고 SQL 실행 계획을 세우고 있습니다...",
        "data": {
            "operation": "text2sql_plan",
            "question": resolved_query,
            "text2sql_step": data.get("text2sql_step", ""),
            "phase": data.get("phase", ""),
            "title": data.get("title", ""),
            "progress_message": str(message or ""),
            "question_type": data.get("question_type", ""),
            "selected_domain": data.get("selected_domain", ""),
            "domain_candidates": data.get("domain_candidates", []),
            "selected_tool": data.get("selected_tool", ""),
            "matched_query_name": data.get("matched_query_name", ""),
            "selected_capability_type": data.get("selected_capability_type", ""),
            "selected_capability_name": data.get("selected_capability_name", ""),
            "selected_tables": data.get("selected_tables", []),
            "param_stage": data.get("param_stage", ""),
            "missing_params": data.get("missing_params", []),
            "followup_mode": data.get("followup_mode", ""),
            "requires_sql": bool(data.get("requires_sql")),
        },
    }


def _aggregate_review_payload(data: dict[str, Any], message: Any, query: str) -> dict[str, Any]:
    resolved_query = query or str(data.get("followup_question") or data.get("query") or data.get("question") or "")
    sql = str(data.get("sql") or data.get("final_sql") or "")
    columns = data.get("columns") or data.get("query_columns") or []
    return {
        "message": "SQL을 생성/검증하고 조회 결과를 확인하고 있습니다...",
        "data": {
            "operation": "sql_execution_review",
            "question": resolved_query,
            "text2sql_step": data.get("text2sql_step", ""),
            "phase": data.get("phase", ""),
            "title": data.get("title", ""),
            "progress_message": str(message or ""),
            "source": data.get("source", ""),
            "selected_domain": data.get("selected_domain", ""),
            "domain_candidates": data.get("domain_candidates", []),
            "selected_tool": data.get("selected_tool", ""),
            "matched_query_name": data.get("matched_query_name", ""),
            "selected_capability_type": data.get("selected_capability_type", ""),
            "selected_capability_name": data.get("selected_capability_name", ""),
            "selected_tables": data.get("selected_tables", []),
            "sql": sql,
            "has_sql": bool(sql),
            "validation_result": data.get("validation_result", ""),
            "columns": columns,
            "column_count": len(columns) if isinstance(columns, list) else 0,
            "row_count": _row_count(data),
            "param_stage": data.get("param_stage", ""),
            "missing_params": data.get("missing_params", []),
            "error": data.get("error") or data.get("query_error") or data.get("error_message") or "",
        },
    }


def _response_payload(data: dict[str, Any], message: Any = None) -> dict[str, Any]:
    sql = str(data.get("sql") or data.get("final_sql") or "")
    return {
        "message": "조회 결과를 답변으로 정리하고 있습니다...",
        "data": {
            "operation": "answer_generation",
            "text2sql_step": data.get("text2sql_step", ""),
            "phase": data.get("phase", ""),
            "title": data.get("title", ""),
            "progress_message": str(message or ""),
            "status": data.get("status", ""),
            "result_id": data.get("result_id", ""),
            "source": data.get("source", ""),
            "has_sql": bool(sql),
            "row_count": _row_count(data),
            "column_count": _column_count(data),
            "answer_ready": bool(data.get("answer") or data.get("status") == "requires_params"),
            "missing_params": data.get("missing_params", []),
            "suggestions": data.get("suggestions", []),
        },
    }


def _row_count(data: dict[str, Any]) -> int:
    explicit = data.get("row_count")
    if isinstance(explicit, int):
        return explicit
    rows = data.get("rows") or data.get("query_rows") or []
    return len(rows) if isinstance(rows, list) else 0


def _column_count(data: dict[str, Any]) -> int:
    columns = data.get("columns") or data.get("query_columns") or []
    return len(columns) if isinstance(columns, list) else 0

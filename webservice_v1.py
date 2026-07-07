import json
import os
import queue
import re
import sys
import threading
import time
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web" / "static"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import text2sql_agent as agent  # noqa: E402
import webapp_compatible_api as webapp_api  # noqa: E402
from text2sql_agent.schema import SCHEMA, _extract_schema_tables  # noqa: E402
from text2sql_agent.session_store import SessionOwnershipError, create_session_store  # noqa: E402
from text2sql_agent.workflow import _get_app as _get_compiled_graph  # noqa: E402


app = FastAPI(
    title="KB Card Corporate Sales Text2SQL - WebApp API v4",
    description="Text2SQL service using kbcard-agent-common LLM and embedding modules.",
    version="4.0.0",
)


@app.on_event("shutdown")
def _shutdown_common_clients() -> None:
    agent.close_common_clients()
    _SESSION_STORE.close()

_GRAPH = None
_SESSION_STORE = create_session_store()
_CONVERSATION_IDS: dict[str, int] = {}
_CONVERSATION_SEQ = 0
_LLM_HEALTH_CACHE: dict[str, Any] = {"checked_at": 0.0, "data": None}
_LLM_HEALTH_TTL_SECONDS = 20.0
DEFAULT_WEBAPP_USER_ID = os.getenv("WEBAPP_DEFAULT_USER_ID", "ui")
DEFAULT_WEBAPP_AGENT_NAME = os.getenv("WEBAPP_DEFAULT_AGENT_NAME", "manual")


NODE_LABELS = {
    "classify_question": ("question_analysis", "질문 분석", "업무 범위와 질문 유형을 분류했습니다."),
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
    "followup_route": ("followup_route", "후속 의도 판단", "이전 결과 기반 분석인지 새 SQL 실행인지 분류했습니다."),
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


SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
PROGRESS_HEARTBEAT_SECONDS = 2.5


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(_jsonable(data), ensure_ascii=False)}\n\n"


def _message_id() -> int:
    return time.time_ns() // 1_000


def _request_user_id(value: str | None) -> str:
    return (value or DEFAULT_WEBAPP_USER_ID).strip() or DEFAULT_WEBAPP_USER_ID


def _request_agent_name(value: str | None) -> str:
    return (value or DEFAULT_WEBAPP_AGENT_NAME).strip() or DEFAULT_WEBAPP_AGENT_NAME


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
    error_name = agent.common_error_name(exc)
    detail = f"{error_name}: {exc}" if str(exc) else error_name
    raise HTTPException(status_code=agent.common_http_status(exc), detail=detail) from exc


def _get_or_create_session(session_id: str | None, user_id: str = "ui", agent_name: str = "manual") -> dict[str, Any]:
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
    natural_input = str(params.get("_natural_input") or "").strip()
    if natural_input:
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
    parsed: dict[str, Any] = {}
    current_ym = f"{datetime.now().year}{datetime.now().month:02d}"
    is_recent = bool(re.search(r"최근|이번\s*달|이번\s*월|현재\s*월", natural_input))
    ym_value = current_ym if is_recent else _normalize_korean_ym(natural_input)
    year_value = _normalize_korean_year(natural_input)

    for name in names:
        # 1) '이름=값' 등 명시적 패턴이 있으면 최우선 채택 (LS/IS 같은 자유 숫자 파라미터 포함).
        named = _extract_named_value(name, natural_input)
        if named:
            parsed[name] = named
            continue

        lowered = name.lower()
        if any(key in lowered for key in ["ym", "년월", "기준월", "기준년월", "base_ym"]):
            parsed[name] = ym_value
        elif any(key in lowered for key in ["기간_시작", "start", "from", "시작"]):
            if is_recent:
                parsed[name] = current_ym
            elif re.search(r"(20\d{2})\s*년(?!\s*\d)", natural_input):
                parsed[name] = f"{year_value}01"
            else:
                parsed[name] = ym_value
        elif any(key in lowered for key in ["기간_종료", "end", "to", "종료"]):
            if is_recent:
                parsed[name] = current_ym
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

    if len(names) == 1 and names[0] not in parsed:
        parsed[names[0]] = ym_value if ym_value != natural_input else natural_input

    return {key: value for key, value in parsed.items() if value not in {"", None}}


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
        raw = agent._call_llm(prompt).strip()
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
            conditions.update({k: v for k, v in value.items() if v != "" and v is not None})
    return conditions


def _result_data_sources(result: dict[str, Any]) -> list[str]:
    sql_tables = _table_names_from_sql(result.get("final_sql", ""))
    return sql_tables or list(result.get("selected_tables", []) or [])


def _result_meta(result: dict[str, Any], source_override: str | None = None) -> dict[str, Any]:
    rows = result.get("query_rows", []) or []
    sql_tables = _table_names_from_sql(result.get("final_sql", ""))
    planned_tables = list(result.get("selected_tables", []) or [])
    return {
        "execution_path": _source_label(result, source_override),
        "data_sources": sql_tables or planned_tables,
        "sql_data_sources": sql_tables,
        "planned_data_sources": planned_tables,
        "conditions": _result_conditions(result),
        "displayed_rows": len(rows),
        "display_row_limit": 100,
        "rows_may_be_limited": len(rows) >= 100,
        "selected_domain": result.get("selected_domain", ""),
        "selected_tool": result.get("selected_tool", ""),
        "matched_query_name": result.get("matched_query_name", ""),
        "selected_capability_type": result.get("selected_capability_type", ""),
        "selected_capability_name": result.get("selected_capability_name", ""),
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


def _suggest_followups(result: dict[str, Any]) -> list[str]:
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
        return "추가 입력이 필요해요. 팝업 또는 질문창에 필요한 정보를 입력해 주세요."
    names = [_param_name(item) for item in missing if _param_name(item)]
    example = ", ".join(_param_example_value(name) for name in names) or "필요한 값"
    return (
        "추가 입력이 필요해요.\n\n"
        "필요한 값:\n"
        + "\n".join(_param_instruction_line(item) for item in missing)
        + "\n\n"
        f"예: `{example}`\n"
        "팝업의 문장 입력칸이나 아래 질문창에 이어서 입력하면 같은 질문을 계속 실행합니다."
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
        _SESSION_STORE.save_result(result_id, session, dict(result))
        _SESSION_STORE.update_session_state(session, last_result_id=result_id, pending_continuation=None)
        answer = result.get("answer", "") or result.get("error_message", "")
        documents = _documents_from_result(result, top_k, source_override)
        continuation = None

    error = result.get("error_message", "") or result.get("query_error", "")
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
        "sql": result.get("final_sql", ""),
        "columns": result.get("query_columns", []),
        "rows": result.get("query_rows", []),
        "result_meta": _result_meta(result, source_override),
        "error": error,
        "excel_file": excel_file,
        "suggestions": _suggest_followups(result),
        "original_answer": result.get("original_answer", result.get("answer", "")),
        "analysis_history": result.get("analysis_history", []),
        "followup_mode": result.get("followup_mode", ""),
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
        status=data.get("status"),
        missing_params=data.get("missing_params"),
        continuation=data.get("continuation"),
        result_id=data.get("result_id"),
        source=data.get("source"),
        sql=data.get("sql"),
        columns=data.get("columns"),
        rows=data.get("rows"),
        result_meta=data.get("result_meta"),
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
    error = result.get("error_message", "") or result.get("query_error", "") or (fallback_message or {}).get("error", "")
    message_id = (fallback_message or {}).get("message_id")
    answer = result.get("answer", "") or result.get("error_message", "") or (fallback_message or {}).get("text", "")
    columns = result.get("query_columns", []) or (fallback_message or {}).get("columns", [])
    rows = result.get("query_rows", []) or (fallback_message or {}).get("rows", [])
    sql = result.get("final_sql", "") or (fallback_message or {}).get("sql", "")

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
        "result_meta": _result_meta(result) if result else (fallback_message or {}).get("result_meta", {}),
        "error": error,
        "excel_file": _register_file(result.get("bad_debt_excel_path", ""), session=session),
        "suggestions": _suggest_followups(result),
        "original_answer": result.get("original_answer", result.get("answer", "")),
        "analysis_history": result.get("analysis_history", []),
        "followup_mode": result.get("followup_mode", ""),
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
            "selected_tool": result.get("selected_tool", ""),
            "matched_query_name": result.get("matched_query_name", ""),
            "selected_capability_type": result.get("selected_capability_type", ""),
            "selected_capability_name": result.get("selected_capability_name", ""),
            "selected_tables": result.get("selected_tables", []),
            "param_stage": result.get("param_stage", ""),
            "missing_params": result.get("missing_params", []),
            "sql": result.get("final_sql", ""),
            "columns": result.get("query_columns", []),
            "row_count": len(result.get("query_rows", []) or []),
            "source": _source_label(result),
        },
    }


_NEXT_PROGRESS_STEP = {
    "": "classify_question",
    "classify_question": "route_domain",
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
    agent_name: str = "manual",
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
        agent_name="manual",
        session_id=req.session_id,
        params=req.params,
        continuation=req.continuation,
    )


def _run_query(
    req: CompatibleQueryRequest,
    session: dict[str, Any],
    message_id: int,
    *,
    agent_name: str = "manual",
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
    agent_name: str = "manual",
    user_id: str = "ui",
):
    started = time.monotonic()
    context = agent.create_trace_context(session_id=session["id"], message_id=message_id)
    yield _sse("start", {"message": "질문을 분석 중입니다...", "data": {"session_id": session["id"], "message_id": message_id, "conversation_id": _conversation_id(session)}})
    try:
        state = _state_from_request(req, session)
        final_result = dict(state)
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
            yield _sse("text2sql_progress", _progress_payload(node_name, req, final_result))
        data = _result_payload(final_result, session, message_id, req.top_k)
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
        yield _sse("error", {"message": "오류가 발생했습니다.", "data": {"error": str(exc), "message_id": message_id}})
        return
    if data.get("status") == "requires_params":
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
    data = _finalize_assistant_message(session, data, message_id)
    agent.emit_execution_log(
        context=context,
        user_id=user_id,
        agent_name=agent_name,
        status="SUCCESS",
        total_latency_ms=int((time.monotonic() - started) * 1000),
        question=req.query,
        result=data,
    )
    yield _sse("done", {"message": "", "data": data})


def _classify_followup_intent(question: str) -> str:
    lowered = question.lower()
    rewrite_keywords = [
        "sql", "쿼리", "query", "조회", "다시 뽑", "다시 보여", "가져와", "불러와",
        "정렬", "sorting", "sort", "order by", "순으로", "상위", "하위", "top",
        "조건", "필터", "filter", "where", "추가", "빼고", "제외", "포함",
        "그룹", "group by", "집계", "컬럼", "열", "월별로", "연도별로", "업종별로", "기업별로",
    ]
    new_query_keywords = ["합쳐", "join", "붙여", "merge", "비교해서 보여", "같이 보여", "새로", "다른 테이블", "대표자", "심사", "이용금액", "매출", "연체", "한도"]
    analysis_only_keywords = ["이상치", "anomaly", "트렌드 분석", "해석", "요약", "원인", "인사이트"]
    if any(keyword in lowered for keyword in new_query_keywords):
        return "new_sql"
    if any(keyword in lowered for keyword in rewrite_keywords):
        return "rewrite_sql"
    if any(keyword in lowered for keyword in analysis_only_keywords):
        return "analysis"
    return "analysis"


def _conversation_history(base_result: dict[str, Any]) -> str:
    history = base_result.get("analysis_history", [])
    if not history:
        return "(없음)"
    lines = []
    for idx, item in enumerate(history[-5:], start=1):
        lines.append(f"{idx}. mode={item.get('mode', 'analysis')}\n   질문: {item.get('question', '')}\n   답변: {item.get('answer', '')}\n   SQL: {item.get('sql', '')}")
    return "\n".join(lines)


def _rewrite_followup_question(base_result: dict[str, Any], question: str) -> str:
    return f"""이전 분석 결과를 바탕으로 후속 요청을 반영해 새 SQL을 생성하고 실행해줘.

[원 질문]
{base_result.get("question", "")}

[직전 SQL]
{base_result.get("final_sql", "")}

[직전 결과 컬럼]
{", ".join(str(col) for col in base_result.get("query_columns", []))}

[이전 대화 이력]
{_conversation_history(base_result)}

[후속 요청]
{question}

요구사항:
- 직전 SQL을 우선 출발점으로 삼고, 필요한 WHERE/GROUP BY/ORDER BY/LIMIT/SELECT/JOIN만 최소 수정
- 후속 요청이 정렬, 필터, 조건 추가, 비교 대상 추가, 합치기, 그룹핑, 상위/하위 N개 조회라면 새 SQL에 반영
- 기존 질문의 업무 맥락은 유지
- 최종 답변은 표 중심으로 짧게 요약
"""


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
    selected = set(table_names or [])
    if not selected:
        return ""

    details = []
    for table in SCHEMA.get("tables", []):
        if table.get("name") in selected:
            details.append(yaml.dump(table, allow_unicode=True, default_flow_style=False))

    rel_lines = []
    for rel in SCHEMA.get("relationships", []):
        if rel.get("from") in selected or rel.get("to") in selected:
            rel_lines.append(f"- {rel.get('from')} -> {rel.get('to')}: {rel.get('join_expr', '')}")

    table_details = "\n---\n".join(details)
    if rel_lines:
        table_details += "\n\n## Relationships\n" + "\n".join(rel_lines)
    return table_details


def _followup_query_state(base_result: dict[str, Any], question: str) -> dict[str, Any]:
    state = agent._new_initial_state(_rewrite_followup_question(base_result, question))
    state["question_type"] = "need_sql"
    state["skip_tool_selection"] = True
    state["skip_verified_query_matching"] = True
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
    history_text = "\n".join(f"- 질문: {item.get('question', '')}\n  답변: {item.get('answer', '')}" for item in history[-5:])
    prompt = f"""당신은 KB카드 기업영업 데이터 분석가입니다.
아래는 이미 실행된 SQL 결과입니다. 새 SQL을 만들거나 외부 데이터를 가정하지 말고, 제공된 결과 안에서만 후속 질문에 답하세요.

[원 질문]
{base_result.get("question", "")}

[후속 질문]
{question}

[실행 SQL]
{base_result.get("final_sql", "")}

[최초 답변]
{original_answer}

[직전 답변]
{previous_answer}

[이전 후속 분석]
{history_text or "(없음)"}

[조회 컬럼]
{", ".join(str(col) for col in columns)}

[조회 결과 샘플]
{_compact_table_text(columns, rows)}

답변 형식:
### 핵심 요약
| 구분 | 내용 |
|---|---|
| ... | ... |

### 분석
- 5줄 이내로 anomaly, trend, 비교, 원인 추정을 간결하게 정리
- 숫자는 가능한 한 표의 값에 근거해서 언급

### 다음 질문 추천
- 이 결과로 이어서 물어볼 만한 질문 2개
"""
    return agent._call_llm(prompt).strip()


def _finalize_followup_result(base_result: dict[str, Any], question: str, followup_result: dict[str, Any], answer: str, mode: str, source: str) -> dict[str, Any]:
    history = list(base_result.get("analysis_history", []))
    history.append(
        {
            "question": question,
            "answer": answer,
            "mode": mode,
            "sql": followup_result.get("final_sql", ""),
            "columns": _jsonable(followup_result.get("query_columns", [])),
            "row_count": len(followup_result.get("query_rows", [])),
            "source": source,
        }
    )
    followup_result.update(
        {
            "question": base_result.get("question", ""),
            "original_answer": base_result.get("original_answer", base_result.get("answer", "")),
            "answer": answer,
            "followup_question": question,
            "analysis_history": history,
            "followup_mode": mode,
            "error_message": "",
        }
    )
    return followup_result


def _stream_followup(
    req: FollowupRequest,
    session: dict[str, Any],
    message_id: int,
    *,
    agent_name: str = "manual",
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
        with agent.observability_context(context=context, agent_name=agent_name, user_id=user_id):
            intent = _classify_followup_intent(req.question)

        if intent in {"rewrite_sql", "new_sql"}:
            route_message = "후속 요청을 기존 SQL 재작성으로 분류했습니다." if intent == "rewrite_sql" else "후속 요청을 새 SQL 실행으로 분류했습니다."
            yield _sse("progress", {"step": "followup_route", "title": "후속 의도 판단", "message": route_message, "query": req.question})
            state = _followup_query_state(base_result, req.question)
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
                yield _sse("progress", {"step": node_name, "title": payload["data"]["title"], "message": payload["message"], "query": req.question})
            answer = followup_result.get("answer", "")
            final_result = _finalize_followup_result(base_result, req.question, followup_result, answer, intent, "후속 SQL")
            source = "후속 SQL"
        else:
            yield _sse("progress", {"step": "followup_route", "title": "후속 의도 판단", "message": "후속 요청을 기존 결과 분석으로 분류했습니다.", "query": req.question})
            yield _sse("progress", {"step": "generate_answer", "title": "답변 생성", "message": "기존 SQL 결과와 대화 이력을 기반으로 답변을 생성합니다.", "query": req.question})
            with agent.observability_context(context=context, agent_name=agent_name, user_id=user_id):
                answer = _followup_analysis(base_result, req.question)
            final_result = _finalize_followup_result(base_result, req.question, dict(base_result), answer, "analysis", "후속 분석")
            source = "후속 분석"
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
        yield _sse("error", {"detail": str(exc)})
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
        webapp_api.adapt_sse_stream(stream),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@app.get("/health")
def health():
    llm_health = _llm_health()
    return {
        "ok": True,
        "model": agent.LLM_MODEL,
        "llm_base_url": agent.LLM_BASE_URL,
        "llm_endpoint_path": agent.LLM_ENDPOINT_PATH,
        "bad_debt_output_dir": agent.BAD_DEBT_OUTPUT_DIR,
        "common": agent.common_package_status(),
        "common_features": agent.common_feature_status(),
        "session_store": _SESSION_STORE.status(),
        **llm_health,
    }



@app.get("/api/examples")
def examples():
    return {
        "examples": [
            "2025년 월별 법인카드 이용금액 추이를 보여줘",
            "2025년 업종별 매출 상위 10개를 알려줘",
            "2025년 12월 한빛테크놀로지의 대손비용률 분석해줘",
            "2025년 기업카드 심사 승인율을 알려줘",
            "2025년 연체가 발생한 기업 상위 20개를 알려줘",
            "2025년 한도사용률 상위 10개 기업 보여줘",
        ]
    }


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
        _legacy_stream_adapter(_stream_query(compatible, session, message_id, agent_name=agent_name, user_id=user_id)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
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
        _stream_followup(req, session, message_id, agent_name=agent_name, user_id=user_id),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
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

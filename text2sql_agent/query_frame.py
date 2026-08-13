"""Structured multi-turn query context and result-scope guards.

The frame is intentionally compact.  It stores the business shape of the
current query instead of asking a small model to recover every inherited
condition from an ever-growing conversation transcript.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from typing import Any, TypedDict

from .config import DISPLAY_ROW_LIMIT
from .row_constraints import outer_limit, outer_order_by, parse_row_request


QUERY_FRAME_VERSION = 1
DEFAULT_FETCH_ROW_LIMIT = 500

_TIME_EXPRESSION_RE = re.compile(
    r"(?:전월|지난\s*달|저번\s*달|이번\s*(?:달|월)|금월|작년|지난해|올해|금년|"
    r"(?:최근|지난)\s*(?:\d{1,3}\s*(?:개월|달)|(?:\d{1,2}|일|반)\s*년)"
    r"(?:\s*(?:이내|내|간|동안))?|20\d{2}\s*년(?:\s*\d{1,2}\s*월)?|"
    r"20\d{2}(?:0[1-9]|1[0-2])(?:[0-3]\d)?)"
)
_TIME_COLUMN_RE = re.compile(r"(?:년월|일자|날짜|date|month|year|ym)", re.IGNORECASE)
_DIMENSION_COLUMN_RE = re.compile(
    r"(?:명|번호|코드|구분|업종|지역|등급|년월|일자|날짜|date|month|year|ym)$",
    re.IGNORECASE,
)
_METRIC_COLUMN_RE = re.compile(
    r"(?:금액|매출|이용|비용|잔액|한도|건수|명수|고객수|회원수|가맹점수|비율|율|"
    r"평균|합계|최대|최소|증감|count|amount|rate|ratio|total|sum|avg)",
    re.IGNORECASE,
)
_ENTITY_COLUMN_RE = re.compile(
    r"(?:가맹점명|기업명|회사명|업체명|상호명|브랜드명|고객명|회원명)",
    re.IGNORECASE,
)
_BUSINESS_NUMBER_RE = re.compile(r"^\d{3}-?\d{2}-?\d{5}$")


class QueryFrame(TypedDict, total=False):
    version: int
    source_question: str
    last_question: str
    domain: str
    tables: list[str]
    entities: list[dict[str, str]]
    time: dict[str, list[str]]
    metrics: list[str]
    dimensions: list[str]
    sort: dict[str, str]
    limit: int | None
    result_scope: dict[str, Any]
    route_mode: str
    requires_reroute: bool


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _rows_as_lists(columns: list[str], rows: list[Any]) -> list[list[Any]]:
    normalized: list[list[Any]] = []
    for row in rows or []:
        if isinstance(row, dict):
            values = [row.get(column) for column in columns]
        elif isinstance(row, (list, tuple)):
            values = list(row)
        else:
            values = [row]
        normalized.append(values + [None] * max(0, len(columns) - len(values)))
    return normalized


def _numeric_column_indices(columns: list[str], rows: list[Any]) -> set[int]:
    normalized_rows = _rows_as_lists(columns, rows)
    numeric: set[int] = set()
    for index in range(len(columns)):
        present = [row[index] for row in normalized_rows if row[index] not in (None, "")]
        if present and sum(_number(value) is not None for value in present) / len(present) >= 0.7:
            numeric.add(index)
    return numeric


def sql_limit(sql: str) -> int | None:
    return outer_limit(sql)


def build_result_scope(
    sql: str,
    *,
    fetched_row_count: int,
    displayed_row_count: int,
    display_limit: int = DISPLAY_ROW_LIMIT,
    fetch_limit: int = DEFAULT_FETCH_ROW_LIMIT,
) -> dict[str, Any]:
    """Describe whether local follow-up operations see the full SQL result."""

    limit = sql_limit(sql)
    display_truncated = fetched_row_count > displayed_row_count
    fetch_limit_reached = fetch_limit > 0 and fetched_row_count >= fetch_limit
    sql_limit_reached = limit is not None and fetched_row_count >= limit
    is_complete = not (display_truncated or fetch_limit_reached or sql_limit_reached)
    if display_truncated:
        reason = f"화면 처리 범위 {display_limit}행을 초과해 일부 행만 유지했습니다."
    elif fetch_limit_reached:
        reason = f"DB 조회 안전 한도 {fetch_limit}행에 도달해 전체 결과 여부를 확인할 수 없습니다."
    elif sql_limit_reached:
        reason = f"SQL LIMIT {limit}에 도달해 전체 원본 범위가 아닐 수 있습니다."
    else:
        reason = "현재 SQL 결과 전체를 후속 처리에 사용할 수 있습니다."
    return {
        "is_complete": is_complete,
        "reason": reason,
        "fetched_row_count": int(fetched_row_count),
        "displayed_row_count": int(displayed_row_count),
        "display_limit": int(display_limit),
        "fetch_limit": int(fetch_limit),
        "sql_limit": limit,
        "display_truncated": display_truncated,
        "fetch_limit_reached": fetch_limit_reached,
        "sql_limit_reached": sql_limit_reached,
        "ordered": bool(outer_order_by(sql)),
    }


def infer_result_scope(result: dict[str, Any]) -> dict[str, Any]:
    existing = result.get("result_scope")
    if isinstance(existing, dict) and "is_complete" in existing:
        return dict(existing)
    rows = result.get("query_rows", []) or []
    scope = build_result_scope(
        str(result.get("final_sql") or result.get("generated_sql") or ""),
        fetched_row_count=len(rows),
        displayed_row_count=len(rows),
    )
    # Older stored results do not retain the pre-display row count. Reaching
    # the display boundary is therefore unknown rather than provably complete.
    if len(rows) >= DISPLAY_ROW_LIMIT and scope.get("is_complete"):
        scope["is_complete"] = False
        scope["reason"] = (
            f"저장된 결과가 화면 한도 {DISPLAY_ROW_LIMIT}행에 도달해 "
            "전체 결과 여부를 확인할 수 없습니다."
        )
        scope["legacy_display_boundary_reached"] = True
    return scope


def _extract_time_context(question: str, sql: str) -> dict[str, list[str]]:
    expressions = _unique(match.group(0).strip() for match in _TIME_EXPRESSION_RE.finditer(question or ""))
    sql_values = _unique(re.findall(r"['\"]((?:19|20)\d{4}(?:\d{2})?)['\"]", sql or ""))
    return {"expressions": expressions, "sql_values": sql_values}


def _extract_entities(sql: str) -> list[dict[str, str]]:
    entities: list[dict[str, str]] = []
    for match in _ENTITY_COLUMN_RE.finditer(sql or ""):
        segment = (sql or "")[match.end() : match.end() + 180]
        value_match = re.search(
            r"(?:LIKE|=)\s*(?:LOWER\s*\(\s*)?['\"](%?)([^'\"]+?)(%?)['\"]",
            segment,
            flags=re.IGNORECASE,
        )
        if not value_match:
            continue
        value = value_match.group(2).strip().strip("%")
        if not value or _BUSINESS_NUMBER_RE.fullmatch(value):
            continue
        item = {"column": match.group(0).strip('"'), "value": value}
        if item not in entities:
            entities.append(item)
    return entities[:8]


def _extract_parameter_entities(result: dict[str, Any]) -> list[dict[str, str]]:
    entities: list[dict[str, str]] = []
    for key in ("extracted_params", "tool_params", "user_provided_params"):
        params = result.get(key) or {}
        if not isinstance(params, dict):
            continue
        for column, raw_value in params.items():
            value = str(raw_value or "").strip()
            if not value or not _ENTITY_COLUMN_RE.fullmatch(str(column)):
                continue
            item = {"column": str(column), "value": value}
            if item not in entities:
                entities.append(item)
    return entities[:8]


def _extract_sort(sql: str) -> dict[str, str]:
    outer_order = outer_order_by(sql)
    if not outer_order:
        return {}
    clause = " ".join(outer_order.split())[:300]
    direction_match = re.search(r"\b(ASC|DESC)\b", clause, flags=re.IGNORECASE)
    return {
        "expression": clause,
        "direction": direction_match.group(1).lower() if direction_match else "",
    }


def build_query_frame(
    result: dict[str, Any],
    *,
    previous_frame: dict[str, Any] | None = None,
    last_question: str = "",
) -> QueryFrame:
    previous = previous_frame or {}
    columns = [str(column) for column in result.get("query_columns", []) or []]
    rows = result.get("query_rows", []) or []
    numeric = _numeric_column_indices(columns, rows)
    metrics = [
        column
        for index, column in enumerate(columns)
        if (index in numeric and not _TIME_COLUMN_RE.search(column))
        or (_METRIC_COLUMN_RE.search(column) and not _TIME_COLUMN_RE.search(column))
    ]
    dimensions = [
        column
        for index, column in enumerate(columns)
        if column not in metrics and (index not in numeric or _DIMENSION_COLUMN_RE.search(column))
    ]
    sql = str(result.get("final_sql") or result.get("generated_sql") or "")
    question = str(result.get("question") or previous.get("source_question") or "")
    followup_question = str(last_question or result.get("followup_question") or "")
    entities = (
        _extract_parameter_entities(result)
        or _extract_entities(sql)
        or list(previous.get("entities") or [])
    )
    time_context = _extract_time_context(followup_question or question, sql)
    if not time_context["expressions"] and not time_context["sql_values"]:
        time_context = copy.deepcopy(previous.get("time") or {"expressions": [], "sql_values": []})
    return {
        "version": QUERY_FRAME_VERSION,
        "source_question": question,
        "last_question": followup_question or question,
        "domain": str(result.get("selected_domain") or previous.get("domain") or ""),
        "tables": _unique(
            [str(value) for value in result.get("selected_tables", []) or []]
            or [str(value) for value in previous.get("tables", []) or []]
        ),
        "entities": entities,
        "time": time_context,
        "metrics": _unique(metrics or [str(value) for value in previous.get("metrics", []) or []]),
        "dimensions": _unique(dimensions or [str(value) for value in previous.get("dimensions", []) or []]),
        "sort": _extract_sort(sql) or copy.deepcopy(previous.get("sort") or {}),
        "limit": sql_limit(sql) if sql else previous.get("limit"),
        "result_scope": infer_result_scope(result),
        "route_mode": str(result.get("followup_mode") or previous.get("route_mode") or ""),
        "requires_reroute": False,
    }


def ensure_query_frame(result: dict[str, Any]) -> QueryFrame:
    existing = result.get("query_frame")
    if isinstance(existing, dict) and int(existing.get("version") or 0) == QUERY_FRAME_VERSION:
        frame = copy.deepcopy(existing)
        frame["result_scope"] = infer_result_scope(result)
        return frame
    return build_query_frame(result)


def evolve_query_frame(
    frame: dict[str, Any],
    question: str,
    *,
    mode: str,
    requested_metrics: list[str] | None = None,
) -> QueryFrame:
    next_frame: QueryFrame = copy.deepcopy(frame or {})
    next_frame["version"] = QUERY_FRAME_VERSION
    next_frame["last_question"] = str(question or "")
    next_frame["route_mode"] = mode
    next_frame["requires_reroute"] = mode.startswith("new_sql")
    metrics = _unique([str(value) for value in requested_metrics or []])
    if metrics:
        asks_to_add = bool(re.search(r"(?:도|까지)\s*(?:같이|함께|추가|포함|보여|알려|조회)", question or ""))
        inherited = [str(value) for value in next_frame.get("metrics", []) or []]
        next_frame["metrics"] = _unique(inherited + metrics) if asks_to_add else metrics
    time_context = _extract_time_context(question, "")
    if time_context["expressions"]:
        previous_time = copy.deepcopy(next_frame.get("time") or {})
        previous_time["expressions"] = time_context["expressions"]
        next_frame["time"] = previous_time
    row_request = parse_row_request(question)
    if row_request.limit is not None:
        next_frame["limit"] = row_request.limit
    if re.search(r"(?:내림차순|높은\s*순|큰\s*순|상위|desc)", question or "", flags=re.IGNORECASE):
        next_frame["sort"] = {"expression": "", "direction": "desc"}
    elif re.search(r"(?:오름차순|낮은\s*순|작은\s*순|하위|asc)", question or "", flags=re.IGNORECASE):
        next_frame["sort"] = {"expression": "", "direction": "asc"}
    elif row_request.mode in {"latest", "tail"}:
        next_frame["sort"] = {"expression": "", "direction": row_request.mode}
    return next_frame


def query_frame_prompt(frame: dict[str, Any] | None) -> str:
    if not frame:
        return "(구조화된 이전 조회 상태 없음)"
    entities = ", ".join(
        f"{item.get('column', '대상')}={item.get('value', '')}"
        for item in frame.get("entities", []) or []
        if item.get("value")
    )
    time_context = frame.get("time") or {}
    time_values = _unique(
        [str(value) for value in time_context.get("expressions", []) or []]
        + [str(value) for value in time_context.get("sql_values", []) or []]
    )
    scope = frame.get("result_scope") or {}
    reroute = bool(frame.get("requires_reroute"))
    domain_label = "이전 도메인(재탐색 대상)" if reroute else "도메인"
    table_label = "이전 테이블(재탐색 대상)" if reroute else "테이블"
    return "\n".join(
        [
            f"- {domain_label}: {frame.get('domain') or '(없음)'}",
            f"- {table_label}: {', '.join(frame.get('tables', []) or []) or '(없음)'}",
            f"- 소스 재탐색 필요: {'예' if reroute else '아니오'}",
            f"- 대상 조건: {entities or '(없음)'}",
            f"- 기간: {', '.join(time_values) or '(없음)'}",
            f"- 지표: {', '.join(frame.get('metrics', []) or []) or '(없음)'}",
            f"- 차원: {', '.join(frame.get('dimensions', []) or []) or '(없음)'}",
            f"- 정렬: {(frame.get('sort') or {}).get('direction') or '(없음)'}",
            f"- 제한: {frame.get('limit') if frame.get('limit') is not None else '(없음)'}",
            f"- 직전 결과 전체성: {'전체' if scope.get('is_complete', True) else '일부'}",
        ]
    )

"""Safe, deterministic operations for follow-up questions on an existing result.

The database result is the boundary: functions in this module never execute SQL and
never invent rows.  They only reshape the columns and rows supplied by the caller.
"""

from __future__ import annotations

import math
import re
from collections import OrderedDict
from decimal import Decimal
from typing import Any

from .query_frame import evolve_query_frame


VISUAL_KEYWORDS = (
    "그래프", "차트", "chart", "plot", "시각화", "막대", "bar", "라인", "line",
    "선그래프", "원형", "분포",
)
PIE_VISUAL_RE = re.compile(
    r"(?:^|\s)파이(?=\s|[?!.,]|$|로(?:\s|[?!.,]|$))|\bpie\b",
    re.IGNORECASE,
)
CURRENT_RESULT_KEYWORDS = (
    "이 결과", "위 결과", "방금 결과", "직전 결과", "현재 결과", "조회 결과",
    "이 내용", "위 내용", "표에서", "데이터에서", "그 중", "그중",
)
TIME_VALUE_RE = re.compile(
    r"(?:전월|지난\s*달|저번\s*달|이번\s*(?:달|월)|금월|작년|지난해|올해|금년|"
    r"(?:최근|지난)\s*(?:\d{1,3}\s*(?:개월|달)|(?:\d{1,2}|일|반)\s*년)"
    r"(?:\s*(?:이내|내|간|동안))?|20\d{2}\s*년(?:\s*\d{1,2}\s*월)?|20\d{2}(?:0[1-9]|1[0-2]))"
)
METRIC_ALIASES = (
    ("매출", ("매출", "매출금액", "매출액")),
    ("이용금액", ("이용금액", "사용금액")),
    ("연체", ("연체", "연체금액", "연체율")),
    ("한도", ("한도", "여신한도")),
    ("대손", ("대손", "대손비용", "대손비용률")),
    ("건수", ("건수", "거래건수", "승인건수")),
    ("가맹점수", ("가맹점수", "가맹점개수")),
    ("회원수", ("회원수", "고객수", "명수")),
    ("승인율", ("승인율",)),
    ("잔액", ("잔액",)),
    ("수수료", ("수수료",)),
)


def _normalized(value: Any) -> str:
    return re.sub(r"[\s_\-()/\[\]]+", "", str(value or "")).lower()


def _rows_as_lists(columns: list[Any], rows: list[Any]) -> list[list[Any]]:
    width = len(columns)
    normalized: list[list[Any]] = []
    for row in rows or []:
        if isinstance(row, dict):
            values = [row.get(column) for column in columns]
        elif isinstance(row, (list, tuple)):
            values = list(row[:width])
        else:
            values = [row]
        normalized.append(values + [None] * max(0, width - len(values)))
    return normalized


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    is_percent = text.endswith("%")
    text = text[:-1].strip() if is_percent else text
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _numeric_indices(columns: list[str], rows: list[list[Any]]) -> list[int]:
    numeric: list[int] = []
    for index in range(len(columns)):
        present = [row[index] for row in rows if index < len(row) and not _is_missing(row[index])]
        if present and sum(_number(value) is not None for value in present) / len(present) >= 0.7:
            numeric.append(index)
    return numeric


def _column_mentions(question: str, columns: list[str]) -> list[int]:
    normalized_question = _normalized(question)
    positions: list[tuple[int, int]] = []
    for index, column in enumerate(columns):
        normalized_column = _normalized(column)
        if normalized_column and normalized_column in normalized_question:
            positions.append((normalized_question.find(normalized_column), index))
    if positions:
        return [index for _, index in sorted(positions)]

    aliases = (
        (("가맹점명", "기업명", "업체명", "상호명", "브랜드명", "이름"), ("가맹점", "기업", "업체", "상호", "브랜드", "이름", "명칭")),
        (("매출금액", "이용금액", "금액", "비용", "잔액", "한도"), ("매출", "이용금액", "금액", "비용", "잔액", "한도")),
        (("비율", "증감률", "증가율", "율", "rate", "ratio"), ("비율", "증감", "증가율", "rate", "ratio")),
        (("건수", "명수", "회원수", "업체수", "가맹점수"), ("건수", "명수", "몇명", "회원수", "업체수", "가맹점수")),
        (("기준년월", "년월", "월", "일자", "날짜", "기간", "date"), ("년월", "월", "일자", "날짜", "기간", "date")),
    )
    matched: list[int] = []
    for column_aliases, question_aliases in aliases:
        if not any(alias in normalized_question for alias in map(_normalized, question_aliases)):
            continue
        for index, column in enumerate(columns):
            normalized_column = _normalized(column)
            if any(_normalized(alias) in normalized_column for alias in column_aliases) and index not in matched:
                matched.append(index)
                break
    return matched


def _metric_index(question: str, columns: list[str], rows: list[list[Any]], *, exclude: set[int] | None = None) -> int | None:
    excluded = exclude or set()
    numeric = _numeric_indices(columns, rows)
    for index in _column_mentions(question, columns):
        if index in numeric and index not in excluded:
            return index
    preferred_tokens = ("금액", "매출", "이용", "비용", "잔액", "한도", "건수", "명수", "수", "비율", "율")
    for index in numeric:
        if index not in excluded and any(token in columns[index] for token in preferred_tokens):
            return index
    return next((index for index in numeric if index not in excluded), None)


def _dimension_index(question: str, columns: list[str], rows: list[list[Any]]) -> int | None:
    numeric = set(_numeric_indices(columns, rows))
    mentions = _column_mentions(question, columns)
    for index in mentions:
        if index not in numeric or any(token in columns[index].lower() for token in ("년월", "월", "일자", "날짜", "date", "기간")):
            return index

    normalized_question = _normalized(question)
    dimension_aliases = (
        (("년월", "월별", "월", "기간"), ("기준년월", "년월", "월", "기간", "month", "ym")),
        (("연도", "연도별", "년별"), ("연도", "년도", "year")),
        (("업종", "업종별"), ("업종",)),
        (("기업", "기업별", "회사별"), ("기업명", "회사명", "기업")),
        (("가맹점", "가맹점별", "매장별"), ("가맹점명", "매장명", "가맹점")),
        (("일자", "날짜", "일별"), ("일자", "날짜", "date")),
    )
    for question_aliases, column_aliases in dimension_aliases:
        if not any(_normalized(alias) in normalized_question for alias in question_aliases):
            continue
        for index, column in enumerate(columns):
            if any(_normalized(alias) in _normalized(column) for alias in column_aliases):
                return index
    return next((index for index in range(len(columns)) if index not in numeric), None)


def _korean_number(value: str, unit: str) -> float:
    number = float(value.replace(",", ""))
    return number * {"": 1, "천": 1_000, "만": 10_000, "억": 100_000_000, "조": 1_000_000_000_000}.get(unit, 1)


def _sortable(value: Any) -> tuple[int, Any]:
    if _is_missing(value):
        return (2, "")
    number = _number(value)
    if number is not None:
        return (0, number)
    return (1, str(value).lower())


def _apply_grouping(question: str, columns: list[str], rows: list[list[Any]]) -> tuple[list[str], list[list[Any]], str] | None:
    normalized_question = _normalized(question)
    has_group_shape = any(token in normalized_question for token in ("월별", "연도별", "년별", "업종별", "기업별", "회사별", "가맹점별", "매장별", "일별", "날짜별"))
    has_aggregate = any(token in normalized_question for token in ("합계", "총합", "평균", "건수", "개수", "최대", "최소", "집계", "묶"))
    if not (has_group_shape and has_aggregate):
        return None
    dimension = _dimension_index(question, columns, rows)
    if dimension is None:
        return None
    operation = "avg" if "평균" in normalized_question else "max" if "최대" in normalized_question else "min" if "최소" in normalized_question else "count" if any(token in normalized_question for token in ("건수", "개수")) else "sum"
    metric = _metric_index(question, columns, rows, exclude={dimension})
    if operation != "count" and metric is None:
        return None

    grouped: OrderedDict[str, list[float]] = OrderedDict()
    display_values: dict[str, Any] = {}
    for row in rows:
        key_value = row[dimension]
        key = str(key_value)
        display_values.setdefault(key, key_value)
        grouped.setdefault(key, [])
        if operation == "count":
            grouped[key].append(1.0)
        else:
            number = _number(row[metric])
            if number is not None:
                grouped[key].append(number)

    output_rows: list[list[Any]] = []
    for key, values in grouped.items():
        if operation == "count":
            aggregate: Any = len(values)
        elif not values:
            aggregate = None
        elif operation == "avg":
            aggregate = sum(values) / len(values)
        elif operation == "max":
            aggregate = max(values)
        elif operation == "min":
            aggregate = min(values)
        else:
            aggregate = sum(values)
        output_rows.append([display_values[key], aggregate])
    operation_label = {"sum": "합계", "avg": "평균", "max": "최대", "min": "최소", "count": "건수"}[operation]
    metric_name = "건수" if operation == "count" else f"{columns[metric]} {operation_label}"
    return [columns[dimension], metric_name], output_rows, f"{columns[dimension]}별 {operation_label} 집계"


def apply_local_transform(question: str, raw_columns: list[Any], raw_rows: list[Any]) -> dict[str, Any]:
    """Apply only transformations that can be determined safely from the wording."""

    columns = [str(column) for column in raw_columns or []]
    rows = _rows_as_lists(columns, raw_rows or [])
    original_count = len(rows)
    operations: list[str] = []
    normalized_question = _normalized(question)

    grouped = _apply_grouping(question, columns, rows)
    if grouped:
        columns, rows, label = grouped
        operations.append(label)

    mentioned = _column_mentions(question, columns)
    target = _metric_index(question, columns, rows)

    if any(token in normalized_question for token in ("중복제거", "중복을제거", "중복없이", "고유값", "distinct")):
        keys = mentioned or list(range(len(columns)))
        seen: set[tuple[str, ...]] = set()
        unique_rows: list[list[Any]] = []
        for row in rows:
            key = tuple(repr(row[index]) for index in keys)
            if key not in seen:
                seen.add(key)
                unique_rows.append(row)
        rows = unique_rows
        operations.append("중복 행 제거")

    asks_missing = any(token in normalized_question for token in ("결측", "빈값", "null", "none", "누락"))
    if asks_missing and any(token in normalized_question for token in ("제외", "제거", "빼", "삭제")):
        keys = mentioned or list(range(len(columns)))
        rows = [row for row in rows if all(not _is_missing(row[index]) for index in keys)]
        operations.append("결측값 행 제외")
    elif asks_missing and any(token in normalized_question for token in ("0으로", "제로로", "영으로")):
        keys = mentioned or list(range(len(columns)))
        for row in rows:
            for index in keys:
                if _is_missing(row[index]):
                    row[index] = 0
        operations.append("결측값을 0으로 대체")

    condition = re.search(r"([0-9][0-9,]*(?:\.\d+)?)\s*(천|만|억|조)?\s*(?:원|%|퍼센트)?\s*(이상|이하|초과|미만)", question)
    if condition and target is not None:
        threshold = _korean_number(condition.group(1), condition.group(2) or "")
        comparator = condition.group(3)
        comparisons = {
            "이상": lambda value: value >= threshold,
            "이하": lambda value: value <= threshold,
            "초과": lambda value: value > threshold,
            "미만": lambda value: value < threshold,
        }
        rows = [row for row in rows if (number := _number(row[target])) is not None and comparisons[comparator](number)]
        operations.append(f"{columns[target]} {condition.group(0).strip()} 필터")

    text_filter = re.search(r"([^\s,]+?)(?:이|가)?\s*(?:포함된|포함한|포함하는|들어간)\s*(?:것|행|항목)?만?", question)
    if text_filter:
        value = text_filter.group(1).strip()
        text_columns = mentioned or [index for index in range(len(columns)) if index not in _numeric_indices(columns, rows)]
        if value and text_columns:
            rows = [row for row in rows if any(value.lower() in str(row[index] or "").lower() for index in text_columns)]
            operations.append(f"'{value}' 포함 행 필터")

    growth_request = any(token in normalized_question for token in ("증감률", "증가율", "전월대비", "전년대비"))
    if growth_request and rows:
        dimension = _dimension_index(question, columns, rows)
        metric = _metric_index(question, columns, rows, exclude={dimension} if dimension is not None else set())
        time_dimension = dimension is not None and any(
            token in _normalized(columns[dimension]) for token in ("년월", "월", "일자", "날짜", "date", "기간", "year", "month")
        )
        if time_dimension and metric is not None and len({str(row[dimension]) for row in rows}) == len(rows):
            rows.sort(key=lambda row: _sortable(row[dimension]))
            previous: float | None = None
            for row in rows:
                current = _number(row[metric])
                rate = None if current is None or previous in (None, 0) else (current - previous) / abs(previous) * 100
                row.append(rate)
                previous = current
            columns.append(f"{columns[metric]} 증감률(%)")
            operations.append(f"{columns[metric]} 이전 구간 대비 증감률 계산")

    if "비중" in normalized_question and not any("비중" in column for column in columns):
        metric = _metric_index(question, columns, rows)
        if metric is not None:
            total = sum(number for row in rows if (number := _number(row[metric])) is not None)
            if total:
                for row in rows:
                    number = _number(row[metric])
                    row.append(None if number is None else number / total * 100)
                columns.append(f"{columns[metric]} 비중(%)")
                operations.append(f"{columns[metric]} 비중 계산")

    unit_match = re.search(r"(천|만|억)\s*원(?:\s*단위(?:로|으로)?|(?:로|으로))", question)
    if unit_match:
        metric = _metric_index(question, columns, rows)
        if metric is not None:
            unit = unit_match.group(1)
            divisor = {"천": 1_000, "만": 10_000, "억": 100_000_000}[unit]
            for row in rows:
                number = _number(row[metric])
                row[metric] = None if number is None else number / divisor
            columns[metric] = f"{columns[metric]}({unit}원)"
            operations.append(f"{unit}원 단위 변환")

    decimal_match = re.search(r"소수점\s*(\d+|첫|한|둘|두|셋|세|넷|네)\s*(?:째)?\s*자리", question)
    if decimal_match:
        digit_value = decimal_match.group(1)
        digits = min(8, int(digit_value) if digit_value.isdigit() else {"첫": 1, "한": 1, "둘": 2, "두": 2, "셋": 3, "세": 3, "넷": 4, "네": 4}[digit_value])
        keys = [index for index in (mentioned or _numeric_indices(columns, rows)) if index < len(columns)]
        for row in rows:
            for index in keys:
                number = _number(row[index])
                if number is not None:
                    row[index] = round(number, digits)
        if keys:
            operations.append(f"소수점 {digits}자리 반올림")

    top_match = re.search(r"(?:상위|top)\s*(\d+)", normalized_question)
    bottom_match = re.search(r"(?:하위|bottom)\s*(\d+)", normalized_question)
    handled_extremes = False
    if top_match and bottom_match and rows:
        sort_index = _metric_index(question, columns, rows)
        if sort_index is not None:
            present_rows = [row for row in rows if not _is_missing(row[sort_index])]
            missing_rows = [row for row in rows if _is_missing(row[sort_index])]
            ascending = sorted(present_rows, key=lambda row: _sortable(row[sort_index]))
            bottom_count = max(1, min(1_000, int(bottom_match.group(1))))
            top_count = max(1, min(1_000, int(top_match.group(1))))
            selected = list(reversed(ascending[-top_count:])) + ascending[:bottom_count]
            seen_ids: set[int] = set()
            rows = []
            for row in selected:
                row_id = id(row)
                if row_id not in seen_ids:
                    seen_ids.add(row_id)
                    rows.append(row)
            if len(rows) < top_count + bottom_count:
                rows.extend(missing_rows[: max(0, top_count + bottom_count - len(rows))])
            operations.append(f"{columns[sort_index]} 상위 {top_count}건·하위 {bottom_count}건 추출")
            handled_extremes = True

    mentions_both_extremes = any(token in normalized_question for token in ("상위", "top")) and any(token in normalized_question for token in ("하위", "bottom"))
    sort_requested = not handled_extremes and not mentions_both_extremes and any(token in normalized_question for token in ("정렬", "오름차순", "내림차순", "순으로", "상위", "하위", "top", "bottom"))
    if sort_requested and rows:
        sort_index = _metric_index(question, columns, rows)
        if mentioned:
            sort_index = mentioned[0]
        if sort_index is not None:
            descending = not any(token in normalized_question for token in ("오름차순", "낮은순", "낮은", "작은순", "작은", "하위", "bottom", "asc"))
            present_rows = [row for row in rows if not _is_missing(row[sort_index])]
            missing_rows = [row for row in rows if _is_missing(row[sort_index])]
            present_rows.sort(key=lambda row: _sortable(row[sort_index]), reverse=descending)
            rows = present_rows + missing_rows
            operations.append(f"{columns[sort_index]} {'내림차순' if descending else '오름차순'} 정렬")

    limit_match = None if handled_extremes else re.search(r"(?:상위|하위|top|bottom)\s*(\d+)", normalized_question)
    if not limit_match and sort_requested:
        limit_match = re.search(r"(\d+)\s*(?:개|건|명)만", normalized_question)
    if limit_match:
        limit = max(1, min(1_000, int(limit_match.group(1))))
        rows = rows[:limit]
        range_name = "하위" if any(token in normalized_question for token in ("하위", "bottom")) else "상위" if any(token in normalized_question for token in ("상위", "top")) else "표시 범위"
        operations.append(f"{range_name} {limit}건만 표시")

    if not sort_requested and any(token in normalized_question for token in ("역순", "반대로", "순서뒤집", "뒤집어")):
        rows.reverse()
        operations.append("행 순서 반전")

    select_requested = any(token in normalized_question for token in ("컬럼만", "열만", "항목만", "컬럼순서", "열순서")) or (
        any(token in normalized_question for token in ("컬럼", "열", "항목"))
        and any(token in normalized_question for token in ("만보여", "만남겨"))
    )
    if select_requested and mentioned:
        ordered = list(dict.fromkeys(mentioned))
        only = any(token in normalized_question for token in ("만보여", "만남겨", "컬럼만", "열만", "항목만"))
        selected = ordered if only else ordered + [index for index in range(len(columns)) if index not in ordered]
        columns = [columns[index] for index in selected]
        rows = [[row[index] for index in selected] for row in rows]
        operations.append("표시 컬럼 선택" if only else "컬럼 순서 변경")

    return {
        "applied": bool(operations),
        "columns": columns,
        "rows": rows,
        "operations": operations,
        "original_row_count": original_count,
        "row_count": len(rows),
    }


def _requested_metrics(question: str) -> list[str]:
    normalized = _normalized(question)
    return [
        canonical
        for canonical, aliases in METRIC_ALIASES
        if any(_normalized(alias) in normalized for alias in aliases)
    ]


def _metric_is_available(metric: str, columns: list[Any], query_frame: dict[str, Any]) -> bool:
    known = _normalized(
        " ".join(
            [str(column) for column in columns or []]
            + [str(value) for value in query_frame.get("metrics", []) or []]
        )
    )
    aliases = next((aliases for canonical, aliases in METRIC_ALIASES if canonical == metric), (metric,))
    return any(_normalized(alias) in known for alias in aliases)


def _transform_requires_complete_result(operations: list[str]) -> bool:
    """Return True when applying the transform to a partial result could lie."""

    safe_display_operations = (
        "단위 변환",
        "반올림",
        "표시 컬럼 선택",
        "컬럼 순서 변경",
        "결측값을 0으로 대체",
    )
    return any(
        operation and not any(safe in operation for safe in safe_display_operations)
        for operation in operations
    )


def plan_followup(
    question: str,
    columns: list[Any],
    rows: list[Any],
    *,
    query_frame: dict[str, Any] | None = None,
    result_scope: dict[str, Any] | None = None,
    context_resolution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Route a follow-up to SQL, local transform, visualization, or analysis."""

    frame = query_frame or {}
    scope = result_scope or frame.get("result_scope") or {}
    resolution = context_resolution or {}
    context_relation = str(resolution.get("relation") or "")
    source_strategy = str(resolution.get("source_strategy") or "")
    lowered = " ".join(str(question or "").lower().split())
    normalized_question = _normalized(lowered)
    asks_visual = any(keyword in lowered for keyword in VISUAL_KEYWORDS) or bool(
        PIE_VISUAL_RE.search(lowered)
    )
    transform = apply_local_transform(question, columns, rows)
    refers_to_current_result = any(keyword in lowered for keyword in CURRENT_RESULT_KEYWORDS) or bool(
        re.search(r"(?:그|거기|같은|동일한|이전|방금|이번에는|그럼)", lowered)
    )

    explicit_sql = any(keyword in lowered for keyword in ("sql", "쿼리", "query", "where", "order by", "group by", "join"))
    new_data_phrases = any(keyword in lowered for keyword in ("새로 조회", "다른 테이블", "조인", "join", "합쳐", "붙여", "merge"))
    requested_metrics = _requested_metrics(question)
    derived_growth = any(keyword in normalized_question for keyword in ("증감률", "증가율", "전월대비", "전년대비")) and transform["applied"]
    new_metrics = [
        metric
        for metric in requested_metrics
        if not _metric_is_available(metric, columns, frame)
    ]
    if derived_growth:
        new_metrics = [metric for metric in new_metrics if metric not in {"건수"}]
    changes_time = bool(TIME_VALUE_RE.search(lowered)) and not derived_growth
    asks_time_series = changes_time and any(
        keyword in normalized_question for keyword in ("추이", "변동", "변화", "시계열")
    )
    frame_dimensions = _normalized(" ".join(str(value) for value in frame.get("dimensions", []) or []))
    has_time_dimension = any(
        keyword in frame_dimensions for keyword in ("년월", "월", "일자", "날짜", "date", "month", "year", "ym")
    )
    needs_time_series_source = asks_time_series and not has_time_dimension
    result_complete = bool(scope.get("is_complete", True))
    unsafe_partial_transform = (
        transform["applied"]
        and not result_complete
        and _transform_requires_complete_result(transform["operations"])
    )

    if new_data_phrases:
        base_mode = "new_sql"
        route_reason = "new_data_requested"
    elif new_metrics:
        base_mode = "new_sql"
        route_reason = "new_metric_requires_reroute"
    elif needs_time_series_source:
        base_mode = "new_sql"
        route_reason = "new_time_series_requires_reroute"
    elif explicit_sql or changes_time:
        base_mode = "rewrite_sql"
        route_reason = "query_definition_changed"
    elif unsafe_partial_transform:
        base_mode = "rewrite_sql"
        route_reason = "incomplete_result_requires_sql"
    elif transform["applied"]:
        base_mode = "transform_visualization" if asks_visual else "transform"
        route_reason = "complete_result_local_transform"
    elif asks_visual:
        base_mode = "visualization"
        route_reason = "existing_result_visualization"
    elif any(keyword in lowered for keyword in ("조회", "조건", "필터", "다시 뽑", "가져와", "불러와", "월별", "연도별", "업종별", "기업별", "만 보여", "만 남겨", "제외", "포함")):
        base_mode = "rewrite_sql"
        route_reason = "filter_or_grain_changed"
    elif refers_to_current_result and re.search(r"(?:만|빼|제외|추가|바꿔|변경)", lowered):
        base_mode = "rewrite_sql"
        route_reason = "contextual_condition_changed"
    else:
        base_mode = "analysis"
        route_reason = "existing_result_analysis"

    # The context resolver only escalates work; deterministic safety rules
    # above still win when they already require a fresh SQL result.
    if context_relation == "new_query":
        base_mode = "new_sql"
        route_reason = "independent_question_requires_new_query"
    elif context_relation == "refine_query" and base_mode not in {"new_sql", "rewrite_sql"}:
        base_mode = "new_sql" if source_strategy == "rediscover" else "rewrite_sql"
        route_reason = (
            "context_change_requires_reroute"
            if base_mode == "new_sql"
            else "context_change_requires_sql"
        )
    elif (
        context_relation == "refine_query"
        and source_strategy == "rediscover"
        and base_mode == "rewrite_sql"
    ):
        base_mode = "new_sql"
        route_reason = "context_change_requires_reroute"

    if base_mode in {"rewrite_sql", "new_sql"} and asks_visual:
        mode = f"{base_mode}_visualization"
    else:
        mode = base_mode
    frame_to_evolve = {} if context_relation == "new_query" else frame
    next_query_frame = evolve_query_frame(
        frame_to_evolve,
        question,
        mode=mode,
        requested_metrics=requested_metrics,
    )
    return {
        "mode": mode,
        "requires_sql": base_mode in {"rewrite_sql", "new_sql"},
        "visualize": asks_visual,
        "transform": transform,
        "refers_to_current_result": refers_to_current_result,
        "route_reason": route_reason,
        "route_confidence": "high" if route_reason != "existing_result_analysis" else "medium",
        "requested_metrics": requested_metrics,
        "new_metrics": new_metrics,
        "result_complete": result_complete,
        "next_query_frame": next_query_frame,
        "context_relation": context_relation,
        "source_strategy": source_strategy,
        "resolved_question": str(resolution.get("resolved_question") or question),
        "context_reason": str(resolution.get("reason") or ""),
        "context_resolved_by_llm": bool(resolution.get("used_llm")),
    }


def build_chart_spec(question: str, raw_columns: list[Any], raw_rows: list[Any], *, max_points: int = 30) -> dict[str, Any] | None:
    """Build a small dependency-free chart contract for the web client."""

    columns = [str(column) for column in raw_columns or []]
    rows = _rows_as_lists(columns, raw_rows or [])
    if not columns or not rows:
        return None
    numeric = _numeric_indices(columns, rows)
    if not numeric:
        return None

    lowered = str(question or "").lower()
    chart_type = "pie" if any(token in lowered for token in ("원형", "파이", "pie", "구성비")) else "line" if any(token in lowered for token in ("라인", "line", "선그래프", "추이", "시계열")) else "bar"
    time_indices = [
        index
        for index, column in enumerate(columns)
        if any(token in _normalized(column) for token in ("년월", "월", "일자", "날짜", "date", "기간", "year", "month"))
    ]
    dimension = time_indices[0] if chart_type == "line" and time_indices else _dimension_index(question, columns, rows)
    if chart_type != "line" and time_indices and dimension not in _column_mentions(question, columns):
        dimension = time_indices[0]
    if dimension is None:
        dimension = next((index for index in range(len(columns)) if index not in numeric), 0)
    mentioned = _column_mentions(question, columns)
    metrics = [index for index in mentioned if index in numeric and index != dimension]
    metrics.extend(index for index in numeric if index != dimension and index not in metrics)
    if not metrics and dimension in numeric:
        metrics = [dimension]
        dimension = -1
    if not metrics:
        return None
    metrics = metrics[:1] if chart_type == "pie" else metrics[:3]

    valid_rows = [row for row in rows if any(_number(row[index]) is not None for index in metrics)]
    if chart_type == "line" and dimension >= 0:
        valid_rows.sort(key=lambda row: _sortable(row[dimension]))
    elif chart_type in {"bar", "pie"}:
        primary = metrics[0]
        valid_rows.sort(key=lambda row: _number(row[primary]) if _number(row[primary]) is not None else float("-inf"), reverse=True)
    point_limit = min(max_points, 12 if chart_type == "pie" else 20 if chart_type == "bar" else max_points)
    truncated = len(valid_rows) > point_limit
    valid_rows = valid_rows[:point_limit]
    labels = [str(row[dimension]) if dimension >= 0 else str(index + 1) for index, row in enumerate(valid_rows)]
    datasets = [
        {
            "label": columns[index],
            "data": [_number(row[index]) for row in valid_rows],
        }
        for index in metrics
    ]
    if not labels or not any(any(value is not None for value in dataset["data"]) for dataset in datasets):
        return None
    dimension_name = columns[dimension] if dimension >= 0 else "행"
    chart_name = {"bar": "막대", "line": "선", "pie": "원형"}[chart_type]
    return {
        "type": chart_type,
        "title": f"{dimension_name}별 {datasets[0]['label']} {chart_name} 차트",
        "x_label": dimension_name,
        "y_label": datasets[0]["label"],
        "labels": labels,
        "datasets": datasets,
        "source": "existing_result",
        "point_count": len(labels),
        "truncated": truncated,
        "note": f"직전 결과 중 {len(labels):,}개 항목을 표시합니다." + (" 값이 큰 항목부터 일부만 표시했습니다." if truncated else ""),
    }

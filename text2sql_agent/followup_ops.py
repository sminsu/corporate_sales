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
from .row_constraints import parse_row_request


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
    # 기업 수(고객식별자)와 회원 수(회원일련번호)는 1:N이라 서로 대체할 수 없다.
    ("고객수", ("고객수", "기업고객수", "법인고객수", "업체수")),
    ("회원수", ("회원수", "명수")),
    ("승인율", ("승인율",)),
    ("잔액", ("잔액",)),
    ("수수료", ("수수료",)),
)
# 축·계열을 고를 때 쓰는 컬럼 성격 판별용 토큰.  접미사로 보는 이유는
# "월평균매출"처럼 시간 단어를 품고도 지표인 컬럼을 시간축으로 오해하지 않기 위함이다.
TIME_COLUMN_SUFFIXES = (
    "년월", "년월일", "월", "일자", "날짜", "일", "년", "연도", "년도", "분기", "기간",
    "date", "month", "year", "ym", "dt",
)
IDENTIFIER_TOKENS = ("번호", "코드", "일련", "사업자등록", "식별자")
NON_ADDITIVE_TOKENS = ("율", "비율", "비중", "평균", "단가", "지수", "점수", "rate", "ratio")
METRIC_PREFERENCE_TOKENS = ("금액", "매출", "이용", "비용", "잔액", "한도", "건수", "수", "비율", "율")
# "지역별"처럼 질문이 부르는 이름과 실제 컬럼명(한글시도명)을 잇는 표.
DIMENSION_ALIASES = (
    (("년월", "월별", "월", "기간"), ("기준년월", "년월", "월", "기간", "month", "ym")),
    (("연도", "연도별", "년별"), ("연도", "년도", "year")),
    (("업종", "업종별"), ("업종",)),
    (("기업", "기업별", "회사별"), ("기업명", "회사명", "기업")),
    (("가맹점", "가맹점별", "매장별"), ("가맹점명", "매장명", "가맹점")),
    (("일자", "날짜", "일별"), ("일자", "날짜", "date")),
    (("지역", "지역별", "시도", "시도별", "권역", "광역", "소재지"), ("시도", "지역", "권역")),
    (("시군구", "시군구별", "구별", "군별"), ("시군구",)),
)
GROUP_SHAPE_RE = re.compile(r"([가-힣A-Za-z0-9]{1,12})\s*별")
ENTITY_COUNT_RE = re.compile(r"([가-힣]{2,8})\s*(?:수|개수|건수)(?:는|은|를|을|가|이|만)?(?:\s|[?!.,]|$)")
# 무엇을 세는지에 따라 세는 컬럼이 다르다.  기업 수는 고객식별자, 회원 수는 회원일련번호로만
# 셀 수 있다(1:N이라 서로 대체하면 숫자가 달라진다).
ENTITY_IDENTIFIER_ALIASES = (
    (("기업", "법인", "업체", "회사", "고객", "거래처"), ("기업고객식별자", "고객식별자", "사업자등록번호", "기업명", "회사명")),
    (("가맹점", "매장", "점포"), ("가맹점번호", "가맹점명")),
    (("회원",), ("회원일련번호", "회원번호")),
    (("카드",), ("카드번호", "카드일련번호")),
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
        (("시도", "지역", "권역", "시군구"), ("지역", "시도", "권역", "시군구", "광역", "소재지")),
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


def _alias_candidates(token: str) -> tuple[str, ...]:
    """붙여 쓴 말("전체지역별")에서도 뒤쪽 두 글자를 다시 본다."""

    token = str(token or "").strip()
    return (token, token[-2:]) if len(token) > 2 else (token,)


def _resolve_columns(token: str, columns: list[str]) -> list[int]:
    """Columns a word in the question ("지역") can name, most direct first.

    한 글자 토큰은 이름 대조에서 뺀다.  "특별히"의 "특"이 아무 컬럼이나 잡으면
    질문에 없는 기준으로 묶어 버린다.  한 글자는 별칭표를 통해서만 해석한다.
    """

    normalized = _normalized(token)
    if not normalized:
        return []
    matches = [
        index
        for index, column in enumerate(columns)
        if len(normalized) > 1 and normalized in _normalized(column)
    ]
    for question_aliases, column_aliases in DIMENSION_ALIASES:
        if normalized not in [_normalized(alias) for alias in question_aliases]:
            continue
        for alias in column_aliases:
            matches.extend(
                index
                for index, column in enumerate(columns)
                if _normalized(alias) in _normalized(column) and index not in matches
            )
    return matches


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
        if index not in numeric or _is_time_column(columns[index]):
            return index

    normalized_question = _normalized(question)
    for question_aliases, column_aliases in DIMENSION_ALIASES:
        if not any(_normalized(alias) in normalized_question for alias in question_aliases):
            continue
        for index, column in enumerate(columns):
            # 축은 라벨이 될 수 있는 컬럼이어야 한다.  "기업" 별칭이 "기업 수"라는
            # 지표를 잡아 x축에 올리면 막대가 한 칸으로 뭉개진다.
            if index in numeric and not _is_time_column(column):
                continue
            if any(_normalized(alias) in _normalized(column) for alias in column_aliases):
                return index
    return next((index for index in range(len(columns)) if index not in numeric), None)


def _is_time_column(column: str) -> bool:
    normalized = _normalized(column)
    return bool(normalized) and (
        normalized.endswith(TIME_COLUMN_SUFFIXES) or "년월" in normalized or "일자" in normalized
    )


def _is_identifier_column(column: str) -> bool:
    normalized = _normalized(column)
    return any(token in normalized for token in IDENTIFIER_TOKENS) or normalized.endswith(("id", "no", "key"))


def _is_additive_column(column: str) -> bool:
    return not any(token in _normalized(column) for token in NON_ADDITIVE_TOKENS)


def _metric_rank(column: str) -> int:
    normalized = _normalized(column)
    return next(
        (rank for rank, token in enumerate(METRIC_PREFERENCE_TOKENS) if token in normalized),
        len(METRIC_PREFERENCE_TOKENS),
    )


def _distinct_values(rows: list[list[Any]], index: int) -> int:
    if index is None or index < 0:
        return 0
    return len({str(row[index]) for row in rows if index < len(row)})


def _series_scale(rows: list[list[Any]], index: int) -> float:
    values = [abs(number) for number in (_number(row[index]) for row in rows) if number]
    return max(values) if values else 0.0


def _non_negative(rows: list[list[Any]], indices: list[int]) -> bool:
    return all(
        (_number(row[index]) or 0) >= 0
        for row in rows
        for index in indices
    )


def _comparable_scale(primary: float, other: float) -> bool:
    """True when two series can share one y axis without one of them flattening."""

    if not primary or not other:
        return True
    return max(primary, other) / min(primary, other) <= 100


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


def _group_dimension(question: str, columns: list[str], rows: list[list[Any]]) -> int | None:
    """"<무엇>별"이 가리키는 컬럼.  질문이 부르지 않은 컬럼으로는 묶지 않는다."""

    numeric = set(_numeric_indices(columns, rows))

    def usable(index: int | None) -> bool:
        return index is not None and (index not in numeric or _is_time_column(columns[index]))

    for token in GROUP_SHAPE_RE.findall(str(question or "")):
        for candidate in _alias_candidates(token):
            index = next((index for index in _resolve_columns(candidate, columns) if usable(index)), None)
            if index is not None:
                return index
    if any(token in _normalized(question) for token in ("분포", "구성")):
        return next((index for index in _column_mentions(question, columns) if usable(index)), None)
    return None


def _entity_count_target(question: str, columns: list[str]) -> tuple[str, int] | None:
    """"기업 수"를 세려면 어느 컬럼의 고유값을 세야 하는지 찾는다."""

    for entity in ENTITY_COUNT_RE.findall(str(question or "")):
        for candidate in _alias_candidates(entity):
            for entity_aliases, column_aliases in ENTITY_IDENTIFIER_ALIASES:
                if _normalized(candidate) not in [_normalized(alias) for alias in entity_aliases]:
                    continue
                for alias in column_aliases:
                    for index, column in enumerate(columns):
                        if _normalized(alias) in _normalized(column):
                            return candidate, index
    return None


def _count_distinct_by_group(
    columns: list[str],
    rows: list[list[Any]],
    dimension: int,
    entity: str,
    target: int,
) -> tuple[list[str], list[list[Any]], str]:
    """그룹별로 식별자의 고유값을 센다.  한 기업이 여러 행이어도 한 번만 센다."""

    seen: OrderedDict[str, set[str]] = OrderedDict()
    display_values: dict[str, Any] = {}
    for row in rows:
        key = str(row[dimension])
        display_values.setdefault(key, row[dimension])
        seen.setdefault(key, set()).add(str(row[target]))
    metric_name = f"{entity} 수"
    output_rows = [[display_values[key], len(values)] for key, values in seen.items()]
    label = f"{columns[dimension]}별 {metric_name} 집계 ({columns[target]} 고유값 기준)"
    return [columns[dimension], metric_name], output_rows, label


def _apply_grouping(question: str, columns: list[str], rows: list[list[Any]]) -> tuple[list[str], list[list[Any]], str] | None:
    normalized_question = _normalized(question)
    dimension = _group_dimension(question, columns, rows)
    if dimension is None:
        return None
    # "지역별 기업 수"는 행 수가 아니라 식별자의 고유값 수를 물어보는 질문이다.
    counted = _entity_count_target(question, columns)
    if counted and counted[1] != dimension:
        return _count_distinct_by_group(columns, rows, dimension, counted[0], counted[1])
    has_aggregate = any(token in normalized_question for token in ("합계", "총합", "평균", "건수", "개수", "최대", "최소", "집계", "묶", "분포"))
    if not has_aggregate:
        return None
    operation = "avg" if "평균" in normalized_question else "max" if "최대" in normalized_question else "min" if "최소" in normalized_question else "count" if any(token in normalized_question for token in ("건수", "개수", "분포")) else "sum"
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
    row_request = parse_row_request(question)

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
    sort_requested = not handled_extremes and not mentions_both_extremes and (
        row_request.mode == "latest"
        or any(token in normalized_question for token in ("정렬", "오름차순", "내림차순", "순으로", "상위", "하위", "top", "bottom"))
    )
    if sort_requested and rows:
        if row_request.mode == "latest":
            sort_index = next(
                (
                    index
                    for index, column in enumerate(columns)
                    if any(token in _normalized(column) for token in ("기준년월", "년월", "일자", "날짜", "date", "month"))
                ),
                _dimension_index(question, columns, rows),
            )
        else:
            sort_index = _metric_index(question, columns, rows)
        if mentioned and row_request.mode != "latest":
            numeric_mentions = [index for index in mentioned if index in _numeric_indices(columns, rows)]
            if numeric_mentions:
                sort_index = numeric_mentions[0]
        if sort_index is not None:
            descending = row_request.mode == "latest" or not any(token in normalized_question for token in ("오름차순", "낮은순", "낮은", "작은순", "작은", "하위", "bottom", "asc"))
            present_rows = [row for row in rows if not _is_missing(row[sort_index])]
            missing_rows = [row for row in rows if _is_missing(row[sort_index])]
            present_rows.sort(key=lambda row: _sortable(row[sort_index]), reverse=descending)
            rows = present_rows + missing_rows
            operations.append(f"{columns[sort_index]} {'내림차순' if descending else '오름차순'} 정렬")

    if not handled_extremes and row_request.limit is not None:
        limit = max(1, min(1_000, row_request.limit))
        if row_request.mode == "tail":
            rows = rows[-limit:]
            range_name = "마지막"
        else:
            rows = rows[:limit]
            range_name = (
                "하위" if row_request.mode == "bottom"
                else "상위" if row_request.mode == "top"
                else "최근" if row_request.mode == "latest"
                else "앞"
            )
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


def _label_dimension(
    columns: list[str],
    rows: list[list[Any]],
    numeric: list[int],
    mentioned: list[int],
    current: int | None,
) -> int | None:
    """Pick an x axis whose values actually differ, so each point gets its own label."""

    candidates = [index for index in range(len(columns)) if index not in numeric or _is_time_column(columns[index])]
    varying = [index for index in candidates if _distinct_values(rows, index) > 1]
    preferred = [index for index in mentioned if index in varying]
    if preferred:
        return preferred[0]
    if varying:
        return max(varying, key=lambda index: _distinct_values(rows, index))
    if current is not None:
        return current
    return candidates[0] if candidates else None


def _merge_duplicate_labels(
    rows: list[list[Any]],
    dimension: int,
    metrics: list[int],
    columns: list[str],
) -> tuple[list[list[Any]], str]:
    """Collapse rows that share one x label so a label draws a single bar or point."""

    if dimension < 0 or _distinct_values(rows, dimension) == len(rows):
        return rows, ""
    if not all(_is_additive_column(columns[index]) for index in metrics):
        return rows, f"같은 {columns[dimension]} 값이 여러 번 나타나 합산하지 않고 그대로 표시했습니다."

    merged: OrderedDict[str, list[Any]] = OrderedDict()
    for row in rows:
        key = str(row[dimension])
        if key not in merged:
            merged[key] = list(row)
            continue
        target = merged[key]
        for index in metrics:
            addition = _number(row[index])
            base = _number(target[index])
            if addition is not None:
                target[index] = addition if base is None else base + addition
    return list(merged.values()), f"같은 {columns[dimension]} 값은 합계로 묶었습니다."


def build_chart_spec(
    question: str,
    raw_columns: list[Any],
    raw_rows: list[Any],
    *,
    max_points: int = 30,
    shape_question: str | None = None,
) -> dict[str, Any] | None:
    """Build a small dependency-free chart contract for the web client.

    ``question`` 은 축·계열을 찾는 데 쓰는 질문(맥락이 채워진 문장)이고,
    ``shape_question`` 은 사용자가 실제로 입력한 문장이다.  맥락 보강이 "파이차트로"
    같은 표현을 지워 버려도 사용자가 말한 차트 모양은 그대로 지켜야 한다.
    """

    columns = [str(column) for column in raw_columns or []]
    rows = _rows_as_lists(columns, raw_rows or [])
    if not columns or not rows:
        return None
    numeric = _numeric_indices(columns, rows)
    if not numeric:
        return None

    lowered = " ".join(str(part or "") for part in (question, shape_question)).lower()
    # 질문이 모양을 직접 말하면("막대") 그 말이 이긴다.  "추이"·"구성비"는 모양을
    # 지정하지 않았을 때만 쓰는 힌트다. 안 그러면 "매출 추이를 막대로"가 선 차트가 된다.
    named_type = (
        "pie" if any(token in lowered for token in ("원형", "파이", "pie")) else
        "bar" if any(token in lowered for token in ("막대", "bar")) else
        "line" if any(token in lowered for token in ("라인", "line", "선그래프", "꺾은")) else ""
    )
    implied_type = (
        "pie" if "구성비" in lowered else
        "line" if any(token in lowered for token in ("추이", "시계열")) else
        "bar"
    )
    chart_type = named_type or implied_type
    notes: list[str] = []
    time_indices = [index for index, column in enumerate(columns) if _is_time_column(column)]
    mentioned = _column_mentions(question, columns)
    # 막대·원형은 질문이 가리키는 축을 그대로 쓴다.  시간 컬럼을 무조건 x축으로 올리면
    # "가맹점별 승인율"처럼 범주를 지정한 질문에서 축이 년월로 바뀌어 라벨이 겹친다.
    dimension = time_indices[0] if chart_type == "line" and time_indices else _dimension_index(question, columns, rows)
    if dimension is None or _distinct_values(rows, dimension) < 2:
        dimension = _label_dimension(columns, rows, numeric, mentioned, dimension)
    if dimension is None:
        dimension = next((index for index in range(len(columns)) if index not in numeric), 0)

    metrics = [index for index in mentioned if index in numeric and index != dimension]
    # 년월·번호 컬럼은 숫자여도 지표가 아니다.  같은 축에 올리면 실제 지표가 눌려 보이지 않는다.
    extras = [
        index
        for index in numeric
        if index != dimension
        and index not in metrics
        and not _is_time_column(columns[index])
        and not _is_identifier_column(columns[index])
    ]
    metrics.extend(sorted(extras, key=lambda index: (_metric_rank(columns[index]), index)))
    if not metrics and dimension in numeric and not _is_identifier_column(columns[dimension]):
        metrics = [dimension]
        dimension = -1
    if not metrics:
        return None
    metrics = metrics[:1] if chart_type == "pie" else metrics[:3]
    # 축을 지배하는 계열이 첫 계열이 되도록 지표성이 강한 순서로 세운다.
    metrics.sort(key=lambda index: (_metric_rank(columns[index]), index))
    right_axis: list[int] = []
    if len(metrics) > 1:
        primary_scale = _series_scale(rows, metrics[0])
        shared = [metrics[0]] + [index for index in metrics[1:] if _comparable_scale(primary_scale, _series_scale(rows, index))]
        rest = [index for index in metrics if index not in shared]
        # 크기 차이가 큰 계열이 하나면 오른쪽 축으로 살린다.  축을 셋 이상으로 늘리거나
        # 음수가 섞여 0선이 어긋나는 경우는 눌려 보이는 계열을 제외하고 이유를 남긴다.
        if len(shared) == 1 and len(rest) == 1 and _non_negative(rows, shared + rest):
            right_axis = rest
            notes.append(f"{columns[rest[0]]}은(는) 값의 크기가 달라 오른쪽 축에 표시했습니다.")
        elif rest:
            notes.append(f"{', '.join(columns[index] for index in rest)}은(는) 값의 크기 차이가 커서 축을 공유할 수 없어 제외했습니다.")
        metrics = shared + right_axis

    valid_rows = [row for row in rows if any(_number(row[index]) is not None for index in metrics)]
    valid_rows, merge_note = _merge_duplicate_labels(valid_rows, dimension, metrics, columns)
    if merge_note:
        notes.append(merge_note)
    if chart_type == "pie":
        values = [_number(row[metrics[0]]) for row in valid_rows]
        if any(value is not None and value < 0 for value in values):
            chart_type = "bar"
            notes.append("음수가 섞여 구성비로 나눌 수 없어 막대 차트로 표시했습니다.")
        else:
            valid_rows = [row for row in valid_rows if (_number(row[metrics[0]]) or 0) > 0]

    time_axis = dimension >= 0 and _is_time_column(columns[dimension])
    if dimension >= 0 and (chart_type == "line" or time_axis):
        valid_rows.sort(key=lambda row: _sortable(row[dimension]))
    else:
        primary = metrics[0]
        valid_rows.sort(key=lambda row: _number(row[primary]) if _number(row[primary]) is not None else float("-inf"), reverse=True)
    point_limit = min(max_points, 12 if chart_type == "pie" else 20 if chart_type == "bar" else max_points)
    truncated = len(valid_rows) > point_limit
    if truncated and chart_type == "pie" and dimension >= 0 and _is_additive_column(columns[metrics[0]]):
        # 원형 차트의 조각 비중은 전체를 뜻한다.  상위 몇 개만 남기고 자르면 남은
        # 조각들이 100%를 차지한 것처럼 보이므로, 나머지는 기타로 합쳐 전체를 지킨다.
        tail = valid_rows[point_limit - 1:]
        others: list[Any] = [None] * len(columns)
        others[dimension] = f"기타 {len(tail):,}개"
        others[metrics[0]] = sum(_number(row[metrics[0]]) or 0 for row in tail)
        valid_rows = valid_rows[: point_limit - 1] + [others]
        notes.append(f"상위 {point_limit - 1}개 외 {len(tail):,}개는 기타로 합쳐 비중을 유지했습니다.")
    elif truncated:
        # 시간축은 값 순서로 자르면 기간이 뒤섞이므로 최근 구간을 남긴다.
        valid_rows = valid_rows[-point_limit:] if time_axis else valid_rows[:point_limit]
        notes.append("최근 구간만 표시했습니다." if time_axis else "값이 큰 항목부터 일부만 표시했습니다.")
    labels = [str(row[dimension]) if dimension >= 0 else str(index + 1) for index, row in enumerate(valid_rows)]
    datasets = [
        {
            "label": columns[index],
            "data": [_number(row[index]) for row in valid_rows],
            **({"axis": "right"} if index in right_axis else {}),
        }
        for index in metrics
    ]
    if not labels or not any(any(value is not None for value in dataset["data"]) for dataset in datasets):
        return None
    dimension_name = columns[dimension] if dimension >= 0 else "행"
    chart_name = {"bar": "막대", "line": "선", "pie": "원형"}[chart_type]
    return {
        "type": chart_type,
        "title": f"{dimension_name}별 {', '.join(dataset['label'] for dataset in datasets)} {chart_name} 차트",
        "x_label": dimension_name,
        "y_label": datasets[0]["label"],
        "labels": labels,
        "datasets": datasets,
        "source": "existing_result",
        "point_count": len(labels),
        "truncated": truncated,
        "note": " ".join([f"직전 결과 중 {len(labels):,}개 항목을 표시합니다."] + notes),
    }

"""Deterministic parsing for user-requested result row limits and direction."""

from __future__ import annotations

import re
from typing import Literal, NamedTuple

import sqlparse
from sqlparse.sql import Where


RowMode = Literal["", "head", "top", "bottom", "latest", "tail"]


class RowRequest(NamedTuple):
    limit: int | None
    mode: RowMode


_DIRECTIONAL_ROW_RE = re.compile(
    r"(?P<direction>상위|하위|TOP|톱|BOTTOM|앞(?:에서|쪽|에)?|처음|"
    r"뒤(?:에서|쪽|에)?|마지막|끝(?:에서|쪽)?|최근|최신|FIRST|LAST|LATEST)"
    r"\s*(?P<count>\d[\d,]*)(?![\d,])\s*"
    r"(?P<unit>개사|개(?!월)|건|명|곳|행|좌|업체|회사|가맹점)?"
    r"(?!\s*(?:개월|달|년|주|일|시간|분|초|분기|원|천|만|억|조|%))",
    re.IGNORECASE,
)
_PLAIN_ROW_RE = re.compile(
    r"(?P<count>\d[\d,]*)\s*(?:개사|개(?!월)|건|명|곳|행|좌|업체|회사|가맹점)"
    r"\s*(?:까지만|만(?=\s*(?:$|[.!?]))|(?:만\s*)?(?:을|를)?\s*(?:뽑|추출|표시|보여|알려|조회|가져|출력|만들|생성|정리))",
    re.IGNORECASE,
)


def _row_mode(direction: str) -> RowMode:
    compact = re.sub(r"\s+", "", direction or "").lower()
    if compact in {"상위", "top", "톱"}:
        return "top"
    if compact in {"하위", "bottom"}:
        return "bottom"
    if compact in {"최근", "최신", "latest"}:
        return "latest"
    if compact.startswith(("뒤", "마지막", "끝")) or compact == "last":
        return "tail"
    return "head"


def _direction_in_text(text: str) -> RowMode:
    if re.search(r"(?:하위|BOTTOM|낮은(?:\s*순)?|적은(?:\s*순)?|작은\s*순|오름차순|\bASC\b)", text, re.IGNORECASE):
        return "bottom"
    if re.search(r"(?:상위|TOP|톱|높은(?:\s*순)?|많은(?:\s*순)?|큰\s*순|내림차순|\bDESC\b)", text, re.IGNORECASE):
        return "top"
    if re.search(r"(?:뒤(?:에서|쪽|에)|마지막|끝(?:에서|쪽)|\bLAST\b)", text, re.IGNORECASE):
        return "tail"
    if re.search(r"(?:최신(?!\s*기준)|\bLATEST\b)", text, re.IGNORECASE):
        return "latest"
    return ""


def parse_row_request(question: str) -> RowRequest:
    """Return an explicit row count and its ordering meaning, if present.

    Time spans such as ``최근 20개월`` are intentionally not row requests.
    """

    text = str(question or "")
    match = _DIRECTIONAL_ROW_RE.search(text)
    if match:
        return RowRequest(
            max(1, int(match.group("count").replace(",", ""))),
            _row_mode(match.group("direction")),
        )

    match = _PLAIN_ROW_RE.search(text)
    if match:
        return RowRequest(
            max(1, int(match.group("count").replace(",", ""))),
            _direction_in_text(text) or "head",
        )

    return RowRequest(None, _direction_in_text(text))


def _top_level_keyword(sql: str, keyword: str) -> tuple[list, int] | None:
    """Find a keyword token in the outer SELECT, excluding CTE/subquery text."""
    statements = sqlparse.parse(str(sql or ""))
    if len(statements) != 1:
        return None
    tokens = statements[0].tokens
    wanted = keyword.upper()
    for index, token in enumerate(tokens):
        if token.value.strip().upper() == wanted:
            return tokens, index
    return None


def outer_order_by(sql: str) -> str:
    """Return only the outermost ORDER BY clause text."""
    found = _top_level_keyword(sql, "ORDER BY")
    if not found:
        return ""
    tokens, index = found
    values: list[str] = []
    stops = {"LIMIT", "OFFSET", "FETCH", "FOR", "UNION", "EXCEPT", "INTERSECT"}
    for token in tokens[index + 1 :]:
        normalized = token.value.strip().upper()
        if normalized in stops or token.value.strip() == ";":
            break
        values.append(token.value)
    return "".join(values).strip()


def first_order_key(sql: str) -> str:
    """Return the first expression in the outermost ORDER BY clause."""
    order_by = outer_order_by(sql)
    if not order_by:
        return ""
    depth = 0
    quote = ""
    for index, char in enumerate(order_by):
        if quote:
            if char == quote and (index == 0 or order_by[index - 1] != "\\"):
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            return order_by[:index].strip()
    return order_by.strip()


def outer_limit(sql: str) -> int | None:
    """Return the numeric outer LIMIT, ignoring limits inside CTEs/subqueries."""
    found = _top_level_keyword(sql, "LIMIT")
    if not found:
        return None
    tokens, index = found
    tail = "".join(token.value for token in tokens[index + 1 :])
    match = re.match(r"\s*(\d+)\b", tail)
    return int(match.group(1)) if match else None


def outer_select_list(sql: str) -> str:
    """Return the outermost SELECT projection, ignoring CTE and subquery lists."""
    statements = sqlparse.parse(str(sql or ""))
    if len(statements) != 1:
        return ""
    select_parts: list[str] = []
    in_select = False
    for token in statements[0].tokens:
        normalized = token.value.strip().upper()
        if normalized == "SELECT":
            in_select = True
            continue
        if in_select and normalized == "FROM":
            break
        if in_select:
            select_parts.append(token.value)
    return "".join(select_parts).strip()


def is_scalar_aggregate_query(sql: str) -> bool:
    """Return whether the outer SELECT is a single aggregate result."""
    statements = sqlparse.parse(str(sql or ""))
    if len(statements) != 1:
        return False
    select_text = outer_select_list(sql)
    depth = 0
    quote = ""
    compact: list[str] = []
    index = 0
    while index < len(select_text):
        char = select_text[index]
        if quote:
            if char == quote and (index == 0 or select_text[index - 1] != "\\"):
                quote = ""
            if depth == 0:
                compact.append(char)
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            if depth == 0:
                compact.append(char)
        elif char == "(":
            if re.match(r"(?is)\(\s*SELECT\b", select_text[index:]):
                nested = 1
                index += 1
                while index < len(select_text) and nested:
                    nested += (select_text[index] == "(") - (select_text[index] == ")")
                    index += 1
                compact.append(" NULL ")
                continue
            depth += 1
            compact.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            compact.append(char)
        else:
            compact.append(char)
        index += 1
    outer_projection = "".join(compact)
    has_aggregate = bool(
        re.search(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", outer_projection, re.IGNORECASE)
        and not re.search(r"\bOVER\s*\(", outer_projection, re.IGNORECASE)
    )
    if not has_aggregate:
        return False

    group_found = _top_level_keyword(sql, "GROUP BY")
    if not group_found:
        return True
    tokens, group_index = group_found
    group_parts: list[str] = []
    stops = {"HAVING", "ORDER BY", "LIMIT", "OFFSET", "FETCH"}
    for token in tokens[group_index + 1 :]:
        if token.value.strip().upper() in stops or token.value.strip() == ";":
            break
        group_parts.append(token.value)
    group_key = first_order_key("SELECT 1 ORDER BY " + "".join(group_parts))
    where_text = next(
        (token.value for token in statements[0].tokens if isinstance(token, Where)),
        "",
    )
    compact_group = re.sub(r"[\s\"']", "", group_key).lower()
    compact_where = re.sub(r"[\s\"']", "", where_text).lower()
    return bool(compact_group and re.search(re.escape(compact_group) + r"=", compact_where))


def apply_outer_limit(sql: str, limit: int) -> str:
    """Replace a numeric outer LIMIT or append one without touching inner limits."""
    text = str(sql or "")
    found = _top_level_keyword(text, "LIMIT")
    if found:
        tokens, index = found
        value_start = sum(len(token.value) for token in tokens[: index + 1])
        match = re.match(r"(?P<space>\s*)(?P<value>\d+)\b", text[value_start:])
        if match:
            start = value_start + match.start("value")
            end = value_start + match.end("value")
            return text[:start] + str(limit) + text[end:]

    stripped = text.rstrip()
    trailing = text[len(stripped) :]
    if stripped.endswith(";"):
        return stripped[:-1].rstrip() + f"\nLIMIT {limit};" + trailing
    return stripped + f"\nLIMIT {limit}" + trailing

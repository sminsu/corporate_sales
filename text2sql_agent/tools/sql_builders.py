"""Shared parameter helpers and deterministic SQL builders."""

import re
from calendar import monthrange
from functools import lru_cache

import yaml

from ..config import DB_BACKEND, DEFAULT_QUERY_ROW_LIMIT, MAX_QUERY_ROW_LIMIT, SCHEMA_PATH
from ..managed_scope import render_athena_business_number_values
from ..row_constraints import apply_outer_limit
from ..time_policy import kst_today, previous_day_ymd

# ---------------------------------------------------------------------------
# 4. verified query 공통 유틸 및 확정적 SQL 생성
# ---------------------------------------------------------------------------

def _sanitize_param(value: str) -> str:
    if not isinstance(value, str):
        return str(value)
    value = value.replace("'", "").replace(";", "").replace("--", "").replace("/*", "").replace("*/", "")
    value = value.replace("\\", "")
    if len(value) > 200:
        value = value[:200]
    return value


def _escape_like(value: str) -> str:
    value = _sanitize_param(value)
    value = value.replace("%", "\\%").replace("_", "\\_")
    return value


def _name_like_pattern(value: str) -> str:
    """Let ordered name tokens match with or without text between them."""
    return re.sub(r"\s+", "%", value.strip())


_ENTITY_NAME_PARAMS = (
    "가맹점명",
    "기업명",
    "기업검색명",
    "상호명",
    "회사명",
    "업체명",
    "고객명",
    "브랜드명",
    "대상명",
)


def _exact_name_match(params: dict) -> bool:
    value = params.get("이름정확일치")
    return value is True or str(value).strip().lower() in {"1", "true", "yes", "y"}


def _escape_name_like(value: str, params: dict) -> str:
    escaped = _escape_like(value)
    return escaped if _exact_name_match(params) else _name_like_pattern(escaped)


def _apply_name_placeholder_mode(sql: str, params: dict) -> str:
    """Remove template wildcards only for an explicit exact-name request."""
    if not _exact_name_match(params):
        return sql
    for name in _ENTITY_NAME_PARAMS:
        placeholder = "{" + name + "}"
        if placeholder not in sql:
            continue
        sql = sql.replace(f"'%{placeholder}%'", f"'{placeholder}'")
        sql = re.sub(
            rf"CONCAT\(\s*'%'\s*,\s*'{re.escape(placeholder)}'\s*,\s*'%'\s*\)",
            f"'{placeholder}'",
            sql,
            flags=re.IGNORECASE,
        )
    return sql


def _coerce_sql_param(name: str, value, param_info: dict | None = None):
    """Validate a verified-query value before interpolating it into SQL.

    Verified queries intentionally use templates, so type validation is the
    injection boundary.  Stripping a handful of characters is not sufficient for
    unquoted numeric/list placeholders (``1 OR 1=1`` would otherwise survive).
    """
    info = param_info or {}
    param_type = str(info.get("type") or "string").lower()
    if param_type == "integer":
        raw = str(value).replace(",", "").strip()
        if not re.fullmatch(r"[+-]?\d+", raw):
            raise ValueError(f"{name}은(는) 정수여야 합니다.")
        number = int(raw)
        if name == "limit":
            return min(max(number, 1), MAX_QUERY_ROW_LIMIT)
        return number
    if param_type in {"number", "float", "decimal"}:
        raw = str(value).replace(",", "").strip()
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw):
            raise ValueError(f"{name}은(는) 숫자여야 합니다.")
        return float(raw)
    if param_type == "list":
        raw_items = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
        items: list[str] = []
        for item in raw_items:
            token = str(item).strip().strip("'\"")
            if not token or not re.fullmatch(r"[0-9A-Za-z가-힣_-]+", token):
                raise ValueError(f"{name} 목록에 허용되지 않는 값이 있습니다.")
            items.append(token)
        if not items:
            raise ValueError(f"{name} 목록이 비어 있습니다.")
        return ",".join(f"'{item}'" for item in items)
    if param_type == "business_number_list":
        return render_athena_business_number_values(value)
    if param_type == "like_string":
        return _escape_like(str(value))
    return _sanitize_param(str(value))


def _current_date_context() -> str:
    today = kst_today()
    prev = today.month - 1
    prev_year = today.year
    if prev <= 0:
        prev = 12
        prev_year -= 1
    return (
        f"KST 현재 {today.year}년 {today.month}월 {today.day}일 기준, "
        f"올해 시작: {today.year}01, 현재월: {today.year}{today.month:02d}, "
        f"최근/이번달: {today.year}{today.month:02d}, 지난달: {prev_year}{prev:02d}, "
        f"전일: {previous_day_ymd(today)}"
    )


def _calc_months_back(yyyymm: str, months: int) -> str:
    year = int(yyyymm[:4])
    month = int(yyyymm[4:6])
    month -= months
    while month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}{month:02d}"


def _month_end_yyyymmdd(yyyymm: str) -> str:
    yyyymm = _sanitize_param(str(yyyymm))
    year = int(yyyymm[:4])
    month = int(yyyymm[4:6])
    return f"{year:04d}{month:02d}{monthrange(year, month)[1]:02d}"


VQ_PARAM_SPECS: dict[str, list[dict]] = {
    "corporate_card_active_no_usage_members": [
        {"name": "기준년월", "type": "string", "description": "카드 보유 여부와 무실적을 판단할 기준년월 (YYYYMM)"},
        {"name": "조회개월수", "type": "integer", "description": "기준월을 포함한 무실적 판정 개월 수"},
        {"name": "limit", "type": "integer", "description": "상세 목록 제한 건수"},
    ],
    "corporate_check_card_only_high_monthly_avg": [
        {"name": "기준년월", "type": "string", "description": "보유·이용과 월평균을 판단할 기준년월 (YYYYMM)"},
        {"name": "월평균금액", "type": "integer", "description": "월평균 체크카드 이용금액 기준"},
        {"name": "limit", "type": "integer", "description": "상세 목록 제한 건수"},
    ],
    "monthly_corporate_card_usage": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM)"},
    ],
    "enterprise_size_corporate_card_usage_by_current_size": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM)"},
        {
            "name": "기업규모구분코드",
            "type": "string",
            "description": "enterprise_size 코드북의 기업규모 코드",
            "semantic_attribute": "enterprise_size",
        },
    ],
    "daily_sales_amount": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM) 또는 기준년월일 (YYYYMMDD)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM) 또는 기준년월일 (YYYYMMDD)"},
        {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
    ],
    "sales_by_industry": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM) 또는 기준년월일 (YYYYMMDD)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM) 또는 기준년월일 (YYYYMMDD)"},
        {"name": "업종", "type": "string", "description": "업종대분류명 또는 가맹점업종명 (부분일치)"},
        {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
    ],
    "top_merchants_by_revenue": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM)"},
        {"name": "업종", "type": "string", "description": "업종명 필터 (부분일치)"},
        {"name": "가맹점명", "type": "string", "description": "가맹점명 필터 (부분일치)"},
        {"name": "limit", "type": "integer", "description": "상위 N개 (기본 10)"},
    ],
    "merchant_monthly_trend": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM)"},
        {"name": "가맹점명", "type": "string", "description": "특정 가맹점명 (부분일치)"},
    ],
    "merchant_yoy_growth": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM)"},
        {"name": "가맹점명", "type": "string", "description": "특정 가맹점명 (부분일치)"},
        {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
    ],
    "review_approval_rate": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM) 또는 기준년월일 (YYYYMMDD)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM) 또는 기준년월일 (YYYYMMDD)"},
    ],
    "review_by_credit_grade": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM) 또는 기준년월일 (YYYYMMDD)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM) 또는 기준년월일 (YYYYMMDD)"},
        {"name": "신용등급", "type": "string", "description": "특정 신용평가등급코드 필터"},
    ],
    "delinquency_status": [
        {"name": "기간_시작", "type": "string", "description": "시작 작업기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "종료 작업기준년월 (YYYYMM)"},
        {"name": "기업명", "type": "string", "description": "기업 상호명 (부분일치)"},
        {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
    ],
    "credit_utilization": [
        {"name": "기간_시작", "type": "string", "description": "시작 작업기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "종료 작업기준년월 (YYYYMM)"},
        {"name": "기업명", "type": "string", "description": "기업 상호명 (부분일치)"},
        {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
    ],
    "fee_rate_by_industry": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM)"},
        {"name": "업종", "type": "string", "description": "업종명 필터 (부분일치)"},
        {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
    ],
    "corporate_customer_card_holdings": [
        {"name": "기업명", "type": "string", "description": "기업검색명 (부분일치)"},
        {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
    ],
    "sales_target_individual_merchant_no_personal_card": [
        {"name": "업종코드목록", "type": "list", "description": "대상 업종코드 (쉼표 구분, 예: '7005','5605')"},
        {"name": "기간_시작", "type": "string", "description": "기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "기준년월 (YYYYMM)"},
    ],
    "merchant_card_possession_performance": [
        {"name": "기준년월", "type": "string", "description": "가맹점 실적 기준년월 (YYYYMM)"},
        {"name": "카드만료기준일", "type": "string", "description": "유효 카드 판단 기준일 (YYYYMMDD). 미입력 시 기준년월의 월말일"},
        {"name": "가맹점명", "type": "string", "description": "가맹점명 필터 (부분일치)"},
        {"name": "업종", "type": "string", "description": "가맹점업종명 또는 업종대분류코드명 필터"},
        {"name": "보유구분", "type": "string", "description": "전체, 개인카드보유, 개인카드미보유, 기업카드보유, 기업카드미보유, 둘다보유, 둘다미보유"},
        {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
    ],
    "card_delinquency_by_grade": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM)"},
    ],
    "recent_closed_brand_merchant_count": [
        {"name": "가맹점명", "type": "like_string", "description": "가맹점명·외부가맹점명·브랜드명 부분일치 검색어"},
        {"name": "조회기간개월수", "type": "integer", "description": "CURRENT_DATE에서 과거로 조회할 개월 수"},
    ],
}


def _apply_params_to_vq(base_sql: str, params: dict, vq_name: str, vq_params_def: dict) -> str:
    sql = _apply_name_placeholder_mode(base_sql, params or {})
    params = dict(params or {})
    if vq_name == "brand_merchants_with_corporate_card":
        card_kind = str(
            params.get("기업카드종류")
            or (vq_params_def.get("기업카드종류") or {}).get("default")
            or "all"
        ).lower()
        card_filter = {
            "credit": 'COALESCE(r."유효기업신용카드수", 0) > 0',
            "check": 'COALESCE(r."유효기업체크카드수", 0) > 0',
            "all": (
                '(COALESCE(r."유효기업신용카드수", 0) + '
                'COALESCE(r."유효기업체크카드수", 0)) > 0'
            ),
        }.get(card_kind)
        if card_filter is None:
            raise ValueError("기업카드종류는 credit, check, all 중 하나여야 합니다.")
        sql = sql.replace("{기업카드보유조건}", card_filter)
    has_explicit_time_placeholders = any(
        placeholder in base_sql
        for placeholder in ("{기준년월}", "{기간_시작}", "{기간_종료}", "{시작일}", "{종료일}")
    )
    if vq_name == "merchant_card_possession_performance":
        yyyymm = params.get("기준년월") or params.get("기간_시작")
        if yyyymm and not params.get("카드만료기준일"):
            try:
                params["카드만료기준일"] = _month_end_yyyymmdd(str(yyyymm))
            except (TypeError, ValueError):
                pass
        params.setdefault("limit", str(DEFAULT_QUERY_ROW_LIMIT))

        possession = str(params.get("보유구분") or "").strip().replace(" ", "")
        possession_clause = {
            "전체": "",
            "개인카드보유": "WHERE 개인카드소지여부 = '1'",
            "개인카드미보유": "WHERE 개인카드소지여부 = '0'",
            "개인미보유": "WHERE 개인카드소지여부 = '0'",
            "기업카드보유": "WHERE 기업카드소지여부 = '1'",
            "기업카드미보유": "WHERE 기업카드소지여부 = '0'",
            "법인카드미보유": "WHERE 기업카드소지여부 = '0'",
            "기업미보유": "WHERE 기업카드소지여부 = '0'",
            "둘다보유": "WHERE 개인카드소지여부 = '1' AND 기업카드소지여부 = '1'",
            "둘다미보유": "WHERE 개인카드소지여부 = '0' AND 기업카드소지여부 = '0'",
        }.get(possession)
        if possession_clause is not None:
            sql = re.sub(
                r"\n\s*WHERE\s+(?:개인|기업)카드소지여부\s*=\s*'[01]'",
                "\n" + possession_clause if possession_clause else "",
                sql,
                flags=re.IGNORECASE,
            )

    daily_columns = (
        "전표매출년월일",
        "최종심사년월일",
        "기준년월일",
        "실적기준년월일",
    )
    daily_column_pattern = "|".join(map(re.escape, daily_columns))
    explicit_daily_period = re.search(
        rf'(?:{daily_column_pattern})"?\s+BETWEEN\s+[\'\"]?\{{기간_시작\}}[\'\"]?'
        rf'\s+AND\s+[\'\"]?\{{기간_종료\}}[\'\"]?',
        base_sql,
        re.IGNORECASE,
    )
    if explicit_daily_period:
        start = _sanitize_param(str(params.get("기간_시작") or ""))
        end = _sanitize_param(str(params.get("기간_종료") or ""))
        if re.fullmatch(r"\d{6}", start):
            params["기간_시작"] = start + "01"
        if re.fullmatch(r"\d{6}", end):
            params["기간_종료"] = _month_end_yyyymmdd(end)

    for pname, pinfo in vq_params_def.items():
        placeholder = "{" + pname + "}"
        if placeholder in sql:
            value = params.get(pname, pinfo.get("default", ""))
            if value not in (None, ""):
                coerced = _coerce_sql_param(pname, value, pinfo)
                if pname in _ENTITY_NAME_PARAMS and not _exact_name_match(params):
                    coerced = _name_like_pattern(str(coerced))
                params[pname] = coerced
                sql = sql.replace(placeholder, str(coerced))
    time_col_map = {
        "기준년월": "monthly", "작업기준년월": "monthly",
        "전표매출년월일": "daily", "최종심사년월일": "daily",
        "기준년월일": "daily", "실적기준년월일": "daily",
    }
    detected_time_col = None
    detected_type = None
    for col, ctype in time_col_map.items():
        if col in sql:
            detected_time_col = col
            detected_type = ctype
            break
    where_additions = []
    start = params.get("기간_시작")
    end = params.get("기간_종료")
    if detected_time_col and (start or end) and not has_explicit_time_placeholders:
        if start:
            start = _sanitize_param(start)
            if detected_type == "daily" and len(start) == 6:
                start = start + "01"
        if end:
            end = _sanitize_param(end)
            if detected_type == "daily" and len(end) == 6:
                end = end + "31"
        if start and end:
            if start == end:
                where_additions.append(f"{detected_time_col} = '{start}'")
            else:
                where_additions.append(f"{detected_time_col} BETWEEN '{start}' AND '{end}'")
        elif start:
            where_additions.append(f"{detected_time_col} >= '{start}'")
        elif end:
            where_additions.append(f"{detected_time_col} <= '{end}'")
    company = params.get("기업명")
    if company and "{기업명}" not in base_sql:
        company = _escape_name_like(company, params)
        company_pattern = company if _exact_name_match(params) else f"%{company}%"
        for col in ["상호명", "기업검색명", "기업명"]:
            if col in sql:
                where_additions.append(f"{col} LIKE '{company_pattern}' ESCAPE '\\'")
                break
    merchant = params.get("가맹점명")
    if merchant and not company and "{가맹점명}" not in base_sql:
        merchant = _escape_name_like(merchant, params)
        merchant_pattern = merchant if _exact_name_match(params) else f"%{merchant}%"
        if "가맹점명" in sql:
            where_additions.append(f"가맹점명 LIKE '{merchant_pattern}' ESCAPE '\\'")
    industry = params.get("업종")
    if industry:
        industry = _escape_like(industry)
        for col in ["업종대분류코드명", "가맹점업종명"]:
            if col in sql:
                where_additions.append(f"{col} LIKE '%{industry}%' ESCAPE '\\'")
                break
    credit_grade = params.get("신용등급")
    if credit_grade and "신용평가등급코드" in sql:
        where_additions.append(f"신용평가등급코드 = '{_sanitize_param(credit_grade)}'")
    if where_additions:
        sql_upper = sql.upper()
        has_where = "WHERE" in sql_upper
        addition_str = " AND ".join(where_additions)
        if has_where:
            for kw in ["GROUP BY", "ORDER BY", "LIMIT", "HAVING"]:
                idx = sql_upper.find(kw)
                if idx > 0:
                    sql = sql[:idx] + f"  AND {addition_str}\n" + sql[idx:]
                    break
            else:
                sql += f"\n  AND {addition_str}"
        else:
            for kw in ["GROUP BY", "ORDER BY", "LIMIT", "HAVING"]:
                idx = sql_upper.find(kw)
                if idx > 0:
                    sql = sql[:idx] + f"WHERE {addition_str}\n" + sql[idx:]
                    break
            else:
                sql += f"\nWHERE {addition_str}"
    limit = params.get("limit")
    if limit:
        limit = _coerce_sql_param("limit", limit, {"type": "integer"})
        sql = apply_outer_limit(sql, limit)
    unresolved = re.findall(r"\{[A-Za-z0-9가-힣_]+\}", sql)
    if unresolved:
        raise ValueError("SQL 생성에 필요한 파라미터가 없습니다: " + ", ".join(sorted(set(unresolved))))
    return sql


def _build_where(conditions: list[str]) -> str:
    if not conditions:
        return ""
    return "WHERE " + " AND ".join(conditions)


def _period_conds(params: dict, col: str) -> list[str]:
    conds = []
    s = params.get("기간_시작")
    e = params.get("기간_종료")
    if s:
        s = _sanitize_param(s)
    if e:
        e = _sanitize_param(e)
    if s and e:
        conds.append(f"{col} BETWEEN '{s}' AND '{e}'" if s != e else f"{col} = '{s}'")
    elif s:
        conds.append(f"{col} >= '{s}'")
    elif e:
        conds.append(f"{col} <= '{e}'")
    return conds


def _period_conds_daily(params: dict, col: str) -> list[str]:
    conds = []
    s = params.get("기간_시작")
    e = params.get("기간_종료")
    if s:
        s = _sanitize_param(s)
        if len(s) == 6:
            s = s + "01"
    if e:
        e = _sanitize_param(e)
        if len(e) == 6:
            e = _month_end_yyyymmdd(e)
    if s and e:
        conds.append(f"{col} BETWEEN '{s}' AND '{e}'" if s != e else f"{col} = '{s}'")
    elif s:
        conds.append(f"{col} >= '{s}'")
    elif e:
        conds.append(f"{col} <= '{e}'")
    return conds


@lru_cache(maxsize=1)
def _athena_partition_index() -> dict[str, dict]:
    """Load optional table-level Athena partition metadata from the semantic schema.

    Expected schema shape:
      athena_partition:
        granularity: month | day
        source_time_dimension: 기준년월
        keys:
        - name: year
          data_type: string
          format: YYYY
        - name: month
          data_type: string
          format: MM
        - name: daty
          data_type: string
          format: DD
    """
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema = yaml.safe_load(f) or {}
    except Exception:
        return {}

    index: dict[str, dict] = {}
    for table in schema.get("tables", []):
        if not isinstance(table, dict):
            continue
        partition = table.get("athena_partition")
        if not isinstance(partition, dict):
            continue
        if partition.get("enabled") is False:
            continue

        logical_name = str(table.get("name") or "").strip()
        physical_name = str(table.get("physical_table") or "").strip()
        physical_short = physical_name.rsplit(".", 1)[-1] if physical_name else ""
        for name in {logical_name, physical_name, physical_short}:
            if name:
                index[name.lower()] = partition
    return index


def _partition_key_def(partition: dict, *, fmt: str, names: tuple[str, ...]) -> dict | None:
    keys = partition.get("keys") or []
    normalized_keys = [{"name": key} if isinstance(key, str) else key for key in keys]
    for key in normalized_keys:
        if isinstance(key, dict) and str(key.get("format", "")).upper() == fmt:
            return key
    for key in normalized_keys:
        if isinstance(key, dict) and str(key.get("name", "")).lower() in names:
            return key
    return None


def _partition_key_expr(key: dict, alias: str = "") -> str:
    name = str(key.get("name") or "").strip()
    if not name:
        return ""
    expr = f'"{name}"' if DB_BACKEND == "athena" or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else name
    return f"{alias}.{expr}" if alias else expr


def _partition_literal(value: str, key: dict) -> str:
    data_type = str(key.get("data_type", "string")).lower()
    if data_type in {"int", "integer", "bigint", "smallint", "tinyint"}:
        return str(int(value))
    return f"'{value}'"


def _normalize_partition_period(value: object) -> str:
    value = _sanitize_param(str(value or ""))
    if len(value) in {6, 8} and value.isdigit():
        return value
    return ""


def _athena_partition_conds(
    table_name: str,
    *,
    start: object = "",
    end: object = "",
    alias: str = "",
) -> list[str]:
    """Return Athena partition predicates for a table and period.

    The helper is intentionally no-op outside Athena or when the semantic schema
    does not define ``athena_partition`` for the table. Month-level periods add
    year/month predicates. Day-level keys add the day predicate only when the
    input period is day-specific.
    """
    try:
        return _athena_partition_conds_inner(table_name, start=start, end=end, alias=alias)
    except Exception:
        return []


def _athena_partition_conds_inner(
    table_name: str,
    *,
    start: object = "",
    end: object = "",
    alias: str = "",
) -> list[str]:
    if DB_BACKEND != "athena":
        return []

    partition = _athena_partition_index().get(str(table_name).lower())
    if not partition:
        return []
    if partition.get("enabled") is False:
        return []

    start_value = _normalize_partition_period(start)
    end_value = _normalize_partition_period(end) or start_value
    if not start_value and end_value:
        start_value = end_value
    if not start_value or not end_value:
        return []
    if int(start_value[:6]) > int(end_value[:6]):
        start_value, end_value = end_value, start_value

    year_key = _partition_key_def(partition, fmt="YYYY", names=("year", "yyyy"))
    month_key = _partition_key_def(partition, fmt="MM", names=("month", "mm", "mnth"))
    day_key = _partition_key_def(partition, fmt="DD", names=("day", "dd", "daty"))
    if not year_key:
        return []

    year_expr = _partition_key_expr(year_key, alias)
    month_expr = _partition_key_expr(month_key, alias) if month_key else ""
    day_expr = _partition_key_expr(day_key, alias) if day_key else ""
    if not year_expr:
        return []

    start_year, start_month = int(start_value[:4]), int(start_value[4:6])
    end_year, end_month = int(end_value[:4]), int(end_value[4:6])

    groups = []
    for year in range(start_year, end_year + 1):
        from_month = start_month if year == start_year else 1
        to_month = end_month if year == end_year else 12
        group = [f"{year_expr} = {_partition_literal(f'{year:04d}', year_key)}"]
        if month_expr and not (from_month == 1 and to_month == 12):
            if from_month == to_month:
                group.append(f"{month_expr} = {_partition_literal(f'{from_month:02d}', month_key)}")
            else:
                group.append(
                    f"{month_expr} BETWEEN {_partition_literal(f'{from_month:02d}', month_key)} "
                    f"AND {_partition_literal(f'{to_month:02d}', month_key)}"
                )
        groups.append(" AND ".join(group))

    condition = groups[0] if len(groups) == 1 else "(" + " OR ".join(f"({group})" for group in groups) + ")"

    if (
        day_expr
        and len(start_value) == 8
        and len(end_value) == 8
        and start_value[:6] == end_value[:6]
    ):
        start_day, end_day = start_value[6:8], end_value[6:8]
        if start_day == end_day:
            condition += f" AND {day_expr} = {_partition_literal(start_day, day_key)}"
        else:
            condition += (
                f" AND {day_expr} BETWEEN {_partition_literal(start_day, day_key)} "
                f"AND {_partition_literal(end_day, day_key)}"
            )

    return [f"({condition})"]


def _partition_conds_from_params(
    table_name: str,
    params: dict,
    *,
    alias: str = "",
    daily: bool = False,
) -> list[str]:
    start = params.get("기간_시작")
    end = params.get("기간_종료")
    if start:
        start = _sanitize_param(start)
        if daily and len(start) == 6:
            start = start + "01"
    if end:
        end = _sanitize_param(end)
        if daily and len(end) == 6:
            end = end + "31"
    return _athena_partition_conds(table_name, start=start, end=end, alias=alias)


def _partition_conds_for_month(table_name: str, yyyymm: str, *, alias: str = "") -> list[str]:
    yyyymm = _sanitize_param(yyyymm)
    return _athena_partition_conds(table_name, start=yyyymm, end=yyyymm, alias=alias)


# -- SQL builder-backed verified query 함수들 --

def _vq_sql_심사승인율(params: dict) -> str:
    conds = _period_conds_daily(params, "최종심사년월일")
    conds.extend(_partition_conds_from_params("tbdaaaf23", params, daily=True))
    if params.get("신용등급"):
        conds.append(f"신용평가등급코드 = '{_sanitize_param(params['신용등급'])}'")
    where = _build_where(conds)
    if params.get("등급별") or params.get("신용등급"):
        return f"""SELECT 신용평가등급코드 AS 신용등급,
    COUNT(DISTINCT 신청서접수번호) AS 총심사건수,
    COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '01' THEN 신청서접수번호 END) AS 승인건수,
    COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '02' THEN 신청서접수번호 END) AS 거절건수,
    ROUND(CAST(COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '01' THEN 신청서접수번호 END) AS DOUBLE)
        / NULLIF(COUNT(DISTINCT 신청서접수번호), 0) * 100, 2) AS 승인율_퍼센트,
    AVG(최종부여한도금액) AS 평균부여한도
FROM tbdaaaf23 {where}
GROUP BY 신용평가등급코드 ORDER BY 신용평가등급코드"""
    else:
        return f"""SELECT
    COUNT(DISTINCT 신청서접수번호) AS 총심사건수,
    COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '01' THEN 신청서접수번호 END) AS 승인건수,
    COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '02' THEN 신청서접수번호 END) AS 거절건수,
    ROUND(CAST(COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '01' THEN 신청서접수번호 END) AS DOUBLE)
        / NULLIF(COUNT(DISTINCT 신청서접수번호), 0) * 100, 2) AS 승인율_퍼센트,
    AVG(최종부여한도금액) AS 평균부여한도
FROM tbdaaaf23 {where}"""


def _vq_sql_월별이용금액(params: dict) -> str:
    conds = ["개인기업구분코드 = '2'"]
    start = _sanitize_param(str(params.get("기간_시작") or ""))
    end = _sanitize_param(str(params.get("기간_종료") or ""))

    # Fetch one extra month so the first requested month can be compared with
    # its actual preceding calendar month. Builder-backed verified queries are
    # initially rendered with symbolic placeholders, so retain the calculation
    # as SQL until _apply_params_to_vq substitutes the concrete value.
    if start:
        if re.fullmatch(r"20\d{2}(?:0[1-9]|1[0-2])", start):
            comparison_start = f"'{_calc_months_back(start, 1)}'"
        else:
            comparison_start = (
                "DATE_FORMAT(DATE_ADD('month', -1, "
                f"DATE_PARSE(CONCAT('{start}', '01'), '%Y%m%d')), '%Y%m')"
            )
        if end:
            conds.append(f"기준년월 BETWEEN {comparison_start} AND '{end}'")
        else:
            conds.append(f"기준년월 >= {comparison_start}")
    elif end:
        conds.append(f"기준년월 <= '{end}'")

    partition_params = dict(params)
    if re.fullmatch(r"20\d{2}(?:0[1-9]|1[0-2])", start):
        partition_params["기간_시작"] = _calc_months_back(start, 1)
    conds.extend(_partition_conds_from_params("tmdaa3e16", partition_params))
    where = _build_where(conds)
    output_where = _build_where(_period_conds(params, "기준년월"))
    return f"""WITH monthly_usage AS (
    SELECT 기준년월,
        SUM(금월이용합계금액) AS 총이용금액,
        SUM(금월일시불이용금액) AS 일시불이용금액,
        SUM(금월할부이용금액) AS 할부이용금액,
        SUM(금월ca이용금액) AS CA이용금액,
        SUM(금월이용합계건수) AS 총이용건수
    FROM tmdaa3e16 {where}
    GROUP BY 기준년월
), usage_with_previous AS (
    SELECT 기준년월,
        총이용금액,
        일시불이용금액,
        할부이용금액,
        CA이용금액,
        총이용건수,
        LAG(총이용금액) OVER (ORDER BY 기준년월) AS 전월이용금액
    FROM monthly_usage
)
SELECT 기준년월,
    총이용금액,
    일시불이용금액,
    할부이용금액,
    CA이용금액,
    총이용건수,
    전월이용금액,
    CASE
        WHEN 전월이용금액 IS NULL OR 전월이용금액 = 0 THEN NULL
        ELSE ROUND(
            (CAST(총이용금액 AS DOUBLE) - CAST(전월이용금액 AS DOUBLE))
            / NULLIF(CAST(전월이용금액 AS DOUBLE), 0) * 100,
            2
        )
    END AS 전월대비증감률_퍼센트
FROM usage_with_previous
{output_where}
ORDER BY 기준년월"""


def _vq_sql_가맹점매출순위(params: dict) -> str:
    conds = _period_conds(params, "a.기준년월")
    conds.extend(_partition_conds_from_params("tmdaa5e11", params, alias="a"))
    if params.get("업종"):
        conds.append(f"b.업종대분류코드명 LIKE '%{_escape_like(params['업종'])}%' ESCAPE '\\'")
    if params.get("가맹점명"):
        merchant = _escape_name_like(params["가맹점명"], params)
        conds.append(f"a.가맹점명 LIKE '%{merchant}%' ESCAPE '\\'")
    where = _build_where(conds)
    limit = int(params.get("limit", 10))
    return f"""SELECT a.가맹점번호, a.가맹점명, b.가맹점업종명, b.업종대분류코드명,
    SUM(a.가맹점일시불매출금액 + a.가맹점할부매출금액) AS 총매출금액,
    SUM(a.가맹점일시불매출건수 + a.가맹점할부매출건수) AS 총매출건수,
    AVG(a.신용카드가맹점수수료율) AS 평균수수료율
FROM tmdaa5e11 a
JOIN tbdaadb17 b ON a.가맹점업종코드 = b.가맹점업종코드
{where}
GROUP BY a.가맹점번호, a.가맹점명, b.가맹점업종명, b.업종대분류코드명
ORDER BY 총매출금액 DESC LIMIT {limit}"""


def _vq_sql_한도사용률(params: dict) -> str:
    conds = _period_conds(params, "작업기준년월")
    conds.extend(_partition_conds_from_params("tbdaaha97", params))
    if params.get("기업명"):
        company = _escape_name_like(params["기업명"], params)
        conds.append(f"상호명 LIKE '%{company}%' ESCAPE '\\'")
    where = _build_where(conds)
    limit = int(params.get("limit", 20))
    return f"""SELECT 상호명, 사업자등록번호,
    SUM(총한도금액) AS 총한도, SUM(카드이용합계금액) AS 총이용금액,
    CASE WHEN SUM(총한도금액) > 0
         THEN ROUND((CAST(SUM(카드이용합계금액) AS DOUBLE) / SUM(총한도금액)) * 100, 2) ELSE 0
    END AS 한도사용률_퍼센트
FROM tbdaaha97 {where}
GROUP BY 상호명, 사업자등록번호 HAVING SUM(총한도금액) > 0
ORDER BY 한도사용률_퍼센트 DESC LIMIT {limit}"""


def _vq_sql_업종별매출(params: dict) -> str:
    conds = ["a.개인기업구분코드 = '2'", "(a.전표취소구분코드 IS NULL OR a.전표취소구분코드 = '0')"]
    conds.extend(_period_conds_daily(params, "a.전표매출년월일"))
    conds.extend(_partition_conds_from_params("tbdaabt30", params, alias="a", daily=True))
    if params.get("업종"):
        conds.append(f"b.업종대분류코드명 LIKE '%{_escape_like(params['업종'])}%' ESCAPE '\\'")
    where = _build_where(conds)
    limit = f"LIMIT {int(params['limit'])}" if params.get("limit") else ""
    return f"""SELECT b.업종대분류코드명, b.가맹점업종명,
    SUM(a.매출금액) AS 총매출금액, COUNT(DISTINCT a.매출전표번호) AS 매출건수,
    AVG(a.매출금액) AS 건당평균금액
FROM card_system.tbdaabt30 a
JOIN card_system.tbdaadb17 b ON a.가맹점업종코드 = b.가맹점업종코드
{where}
GROUP BY b.업종대분류코드명, b.가맹점업종명 ORDER BY 총매출금액 DESC {limit}"""


def _vq_sql_기업별연체현황(params: dict) -> str:
    conds = ["연체금액 > 0"]
    conds.extend(_period_conds(params, "작업기준년월"))
    conds.extend(_partition_conds_from_params("tbdaaha97", params))
    if params.get("기업명"):
        company = _escape_name_like(params["기업명"], params)
        conds.append(f"상호명 LIKE '%{company}%' ESCAPE '\\'")
    where = _build_where(conds)
    limit = int(params.get("limit", 20))
    return f"""SELECT 상호명, 사업자등록번호,
    SUM(총한도금액) AS 총한도, SUM(카드이용합계금액) AS 총이용금액, SUM(연체금액) AS 연체금액,
    CASE WHEN SUM(카드이용합계금액) > 0
         THEN ROUND((CAST(SUM(연체금액) AS DOUBLE) / SUM(카드이용합계금액)) * 100, 2) ELSE 0
    END AS 연체율_퍼센트
FROM tbdaaha97 {where}
GROUP BY 상호명, 사업자등록번호 ORDER BY 연체금액 DESC LIMIT {limit}"""


def _vq_sql_카드등급별연체(params: dict) -> str:
    conds = ["개인기업구분코드 = '2'"]
    conds.extend(_period_conds(params, "기준년월"))
    conds.extend(_partition_conds_from_params("tmdaa3e16", params))
    where = _build_where(conds)
    return f"""SELECT 카드등급구분코드,
    COUNT(DISTINCT 카드구분키번호) AS 카드수, SUM(금월이용합계금액) AS 총이용금액,
    SUM(연체원금) AS 총연체원금,
    CASE WHEN SUM(금월이용합계금액) > 0
         THEN ROUND((CAST(SUM(연체원금) AS DOUBLE) / SUM(금월이용합계금액)) * 100, 2) ELSE 0
    END AS 연체율_퍼센트
FROM tmdaa3e16 {where}
GROUP BY 카드등급구분코드 ORDER BY 총연체원금 DESC"""


def _vq_sql_가맹점카드소지현황(params: dict) -> str:
    yyyymm = _sanitize_param(params["기준년월"])
    card_valid_after = _sanitize_param(params.get("카드만료기준일") or _month_end_yyyymmdd(yyyymm))
    limit = int(params.get("limit", DEFAULT_QUERY_ROW_LIMIT))
    perf_partition_conds = _partition_conds_for_month("tmdaa5e11", yyyymm)
    perf_partition_sql = "".join(f"\n      AND {cond}" for cond in perf_partition_conds)

    base_conds = [
        "a.실적기준년월일 BETWEEN "
        "DATE_FORMAT(DATE_ADD('day', -9, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d') "
        "AND DATE_FORMAT(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul', '%Y%m%d')"
    ]
    if params.get("가맹점명"):
        merchant = _escape_name_like(params["가맹점명"], params)
        base_conds.append(f"a.가맹점명 LIKE '%{merchant}%' ESCAPE '\\'")
    if params.get("업종"):
        industry = _escape_like(params["업종"])
        base_conds.append(f"(b.가맹점업종명 LIKE '%{industry}%' ESCAPE '\\' OR b.업종대분류코드명 LIKE '%{industry}%' ESCAPE '\\')")
    base_where = "WHERE " + " AND ".join(base_conds) if base_conds else ""

    possession = str(params.get("보유구분") or "전체").strip().replace(" ", "")
    possession_where = {
        "전체": "",
        "개인카드보유": "WHERE 개인카드소지여부 = '1'",
        "개인카드미보유": "WHERE 개인카드소지여부 = '0'",
        "개인미보유": "WHERE 개인카드소지여부 = '0'",
        "기업카드보유": "WHERE 기업카드소지여부 = '1'",
        "기업카드미보유": "WHERE 기업카드소지여부 = '0'",
        "법인카드미보유": "WHERE 기업카드소지여부 = '0'",
        "기업미보유": "WHERE 기업카드소지여부 = '0'",
        "둘다보유": "WHERE 개인카드소지여부 = '1' AND 기업카드소지여부 = '1'",
        "둘다미보유": "WHERE 개인카드소지여부 = '0' AND 기업카드소지여부 = '0'",
    }.get(possession, "")

    return f"""WITH upjong AS (
    SELECT
        가맹점업종코드,
        가맹점업종명,
        업종대분류코드명
    FROM tbdaadb17
    WHERE 사용여부 = '1'
),
gm_base AS (
    SELECT
        a.가맹점신규년월일 AS 등록년월일,
        a.가맹점번호,
        a.기업고객식별자,
        a.대표고객식별자 AS 개인고객식별자,
        a.가맹점명,
        a.사업자등록번호,
        a.가맹점업종코드,
        b.가맹점업종명,
        b.업종대분류코드명,
        a.가맹점사업주체구분코드,
        a.현금카드결제기관구분코드 AS 가맹점결제계좌,
        a.신용카드가맹점수수료율,
        a.체크카드가맹점수수료율,
        a.가맹점관리부점코드,
        a.가맹점상태구분코드
    FROM tbdaadt01 a
    LEFT JOIN upjong b
        ON a.가맹점업종코드 = b.가맹점업종코드
    {base_where}
),
gm_perf AS (
    SELECT
        기준년월,
        가맹점번호,
        COALESCE(가맹점일시불매출금액, 0) + COALESCE(가맹점할부매출금액, 0) AS 당월매출금액,
        최근1년가맹점매출금액,
        최근1년가맹점매출건수,
        최근3개월가맹점매출금액,
        최근3개월가맹점매출건수,
        가맹점상태구분코드
    FROM tmdaa5e11
    WHERE 기준년월 = '{yyyymm}'{perf_partition_sql}
),
card_base AS (
    SELECT DISTINCT
        본인고객식별자,
        개인기업구분코드,
        카드결제기관구분코드,
        상품중분류구분코드,
        CASE WHEN 카드결제기관구분코드 = '004' THEN '1' ELSE '0' END AS KB카드계좌여부
    FROM tbdaaat05
    WHERE 실제카드만료년월일 > '{card_valid_after}'
      AND KB카드BC카드구분코드 = '1'
),
joined AS (
    SELECT
        g.등록년월일,
        g.가맹점번호,
        g.기업고객식별자,
        g.개인고객식별자,
        g.가맹점명,
        g.사업자등록번호,
        g.가맹점업종코드,
        g.가맹점업종명,
        g.업종대분류코드명,
        g.가맹점사업주체구분코드,
        g.가맹점결제계좌,
        g.신용카드가맹점수수료율,
        g.체크카드가맹점수수료율,
        g.가맹점관리부점코드,
        COALESCE(p.가맹점상태구분코드, g.가맹점상태구분코드) AS 가맹점상태구분코드,
        p.기준년월,
        p.당월매출금액,
        p.최근3개월가맹점매출금액,
        p.최근3개월가맹점매출건수,
        p.최근1년가맹점매출금액,
        p.최근1년가맹점매출건수,
        CASE WHEN c1.본인고객식별자 IS NOT NULL THEN '1' ELSE '0' END AS 개인카드소지여부,
        CASE WHEN c2.본인고객식별자 IS NOT NULL THEN '1' ELSE '0' END AS 기업카드소지여부
    FROM gm_base g
    LEFT JOIN gm_perf p
        ON g.가맹점번호 = p.가맹점번호
    LEFT JOIN card_base c1
        ON g.개인고객식별자 = c1.본인고객식별자
       AND c1.개인기업구분코드 = '1'
    LEFT JOIN card_base c2
        ON g.기업고객식별자 = c2.본인고객식별자
       AND c2.개인기업구분코드 = '2'
)
SELECT
    등록년월일,
    기준년월,
    가맹점번호,
    가맹점명,
    사업자등록번호,
    가맹점업종코드,
    가맹점업종명,
    업종대분류코드명,
    개인고객식별자,
    기업고객식별자,
    개인카드소지여부,
    기업카드소지여부,
    당월매출금액,
    최근3개월가맹점매출금액,
    최근3개월가맹점매출건수,
    최근1년가맹점매출금액,
    최근1년가맹점매출건수,
    신용카드가맹점수수료율,
    체크카드가맹점수수료율,
    가맹점결제계좌,
    가맹점관리부점코드,
    가맹점상태구분코드
FROM joined
{possession_where}
ORDER BY 당월매출금액 DESC NULLS LAST, 가맹점번호
LIMIT {limit}"""


def _tool_sql_external_verified_query(name: str, params: dict) -> str:
    # Imported lazily because verified_queries imports this module's shared builders.
    from .verified_queries import load_external_verified_queries

    query = next(query for query in load_external_verified_queries() if query.get("name") == name)
    return _apply_params_to_vq(
        query["sql"],
        params,
        name,
        query.get("parameters") or {},
    )


def _tool_sql_corporate_card_active_no_usage_members(params: dict) -> str:
    return _tool_sql_external_verified_query("corporate_card_active_no_usage_members", params)


def _tool_sql_corporate_card_churned_after_usage_members(params: dict) -> str:
    return _tool_sql_external_verified_query("corporate_card_churned_after_usage_members", params)

def _tool_sql_corporate_check_card_only_high_monthly_avg(params: dict) -> str:
    return _tool_sql_external_verified_query("corporate_check_card_only_high_monthly_avg", params)


def _tool_sql_merchant_corporate_sales_target_no_corporate_card(params: dict) -> str:
    return _tool_sql_external_verified_query("merchant_corporate_sales_target_no_corporate_card", params)


def _tool_sql_corporate_limit_low_utilization_members(params: dict) -> str:
    return _tool_sql_external_verified_query("corporate_limit_low_utilization_members", params)


def _tool_sql_new_sales_targets_usage_amount_detail(params: dict) -> str:
    기준년월 = _sanitize_param(params.get("기준년월", "202604"))
    최근6개월_시작 = _sanitize_param(params.get("최근6개월_시작", _calc_months_back(기준년월, 5)))
    교차기간_시작 = _sanitize_param(params.get("교차기간_시작", "202601"))
    월매출금액 = int(params.get("월매출금액", 100000000))
    return f"""WITH target AS (
    SELECT DISTINCT a.고객식별자, '1_6무' AS 구분
    FROM tbdaa1d12 a
    WHERE SUBSTRING(a.기준년월일, 1, 6) = '{기준년월}'
      AND a.기준년월일 <= DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')
      AND a."year" = SUBSTRING('{기준년월}', 1, 4)
      AND a."month" = SUBSTRING('{기준년월}', 5, 2)
      AND (COALESCE(a.유효기업신용카드수, 0) + COALESCE(a.유효기업체크카드수, 0)) > 0
      AND NOT EXISTS (
          SELECT 1
          FROM tbdaa1d12 b
          WHERE b.고객식별자 = a.고객식별자
            AND SUBSTRING(b.기준년월일, 1, 6) BETWEEN '{최근6개월_시작}' AND '{기준년월}'
            AND b.기준년월일 <= DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')
            AND (b."year" || b."month") BETWEEN '{최근6개월_시작}' AND '{기준년월}'
            AND (COALESCE(b.금월신용카드이용금액, 0) + COALESCE(b.금월체크카드이용금액, 0)) > 0
      )
    UNION ALL
    SELECT DISTINCT a.고객식별자, '2_이탈' AS 구분
    FROM tbdaa1d12 a
    WHERE SUBSTRING(a.기준년월일, 1, 6) = '{기준년월}'
      AND a.기준년월일 <= DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')
      AND a."year" = SUBSTRING('{기준년월}', 1, 4)
      AND a."month" = SUBSTRING('{기준년월}', 5, 2)
      AND (COALESCE(a.유효기업신용카드수, 0) + COALESCE(a.유효기업체크카드수, 0)) = 0
      AND EXISTS (
          SELECT 1
          FROM tbdaa1d12 b
          WHERE b.고객식별자 = a.고객식별자
            AND SUBSTRING(b.기준년월일, 1, 6) BETWEEN '{최근6개월_시작}' AND '{기준년월}'
            AND b.기준년월일 <= DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')
            AND (b."year" || b."month") BETWEEN '{최근6개월_시작}' AND '{기준년월}'
            AND (COALESCE(b.금월신용카드이용금액, 0) + COALESCE(b.금월체크카드이용금액, 0)) > 0
      )
    UNION ALL
    SELECT DISTINCT a.고객식별자, '3_교차' AS 구분
    FROM tbdaa1d12 a
    WHERE SUBSTRING(a.기준년월일, 1, 6) BETWEEN '{교차기간_시작}' AND '{기준년월}'
      AND a.기준년월일 <= DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')
      AND (a."year" || a."month") BETWEEN '{교차기간_시작}' AND '{기준년월}'
    GROUP BY a.고객식별자
    HAVING SUM(COALESCE(a.금월신용카드이용금액, 0)) = 0
       AND SUM(COALESCE(a.금월체크카드이용금액, 0)) > 0
    UNION ALL
    SELECT DISTINCT a.기업고객식별자 AS 고객식별자, '4_가맹점' AS 구분
    FROM tbdaaus01 a
    WHERE SUBSTRING(a.기준년월일, 1, 6) = '{기준년월}'
      AND a.기준년월일 <= DATE_FORMAT(
          DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'),
          '%Y%m%d'
      )
      AND a."year" = SUBSTRING('{기준년월}', 1, 4)
      AND a."month" = SUBSTRING('{기준년월}', 5, 2)
      AND COALESCE(a.가맹점총지급금액, 0) >= {월매출금액}
      AND a.가맹점사업주체구분코드 = '2'
      AND COALESCE(a.유효기업신용카드수, 0) = 0
      AND COALESCE(a.유효기업체크카드수, 0) = 0
      AND COALESCE(a.휴폐업여부, '0') = '0'
      AND a.기업고객식별자 IS NOT NULL
),
usage_base AS (
    SELECT
        t.구분,
        a.고객식별자,
        COALESCE(a.금월신용카드이용금액, 0) AS 금월신용카드이용금액,
        COALESCE(a.금월체크카드이용금액, 0) AS 금월체크카드이용금액,
        COALESCE(a.금월해외이용금액, 0) AS 금월해외이용금액,
        COALESCE(a.금월KB페이이용금액, 0) AS 금월KB페이이용금액,
        COALESCE(a.금월국세이용금액, 0) AS 금월국세이용금액,
        COALESCE(a.금월지방세이용금액, 0) AS 금월지방세이용금액,
        COALESCE(a.금월4대보험이용금액, 0) AS 금월4대보험이용금액,
        COALESCE(a.금월일반매출이용금액, 0) AS 금월일반매출이용금액,
        COALESCE(a.금월전략매출이용금액, 0) AS 금월전략매출이용금액
    FROM target t
    JOIN tbdaa1d12 a
      ON t.고객식별자 = a.고객식별자
    WHERE SUBSTRING(a.기준년월일, 1, 6) = '{기준년월}'
      AND a.기준년월일 <= DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')
      AND a."year" = SUBSTRING('{기준년월}', 1, 4)
      AND a."month" = SUBSTRING('{기준년월}', 5, 2)
)
SELECT
    구분,
    COUNT(DISTINCT 고객식별자) AS 기업회원수,
    SUM(금월신용카드이용금액) AS 신용이용금액,
    SUM(금월체크카드이용금액) AS 체크이용금액,
    SUM(금월해외이용금액) AS 해외이용금액,
    SUM(금월KB페이이용금액) AS KB페이이용금액,
    SUM(금월국세이용금액) AS 국세이용금액,
    SUM(금월지방세이용금액) AS 지방세이용금액,
    SUM(금월4대보험이용금액) AS 사대보험이용금액,
    SUM(금월일반매출이용금액) AS 일반매출이용금액,
    SUM(금월전략매출이용금액) AS 전략매출이용금액
FROM usage_base
GROUP BY 구분
ORDER BY 구분"""


def _tool_sql_managed_corporate_usage_trend_monitoring(params: dict) -> str:
    관리기업목록 = render_athena_business_number_values(params.get("관리기업목록"))
    기준년월 = _sanitize_param(params.get("기준년월", "202604"))
    기간_시작 = _sanitize_param(params.get("기간_시작", _calc_months_back(기준년월, 5)))
    limit = _coerce_sql_param("limit", params.get("limit", DEFAULT_QUERY_ROW_LIMIT), {"type": "integer"})
    return f"""WITH managed_list (사업자등록번호) AS (
    VALUES {관리기업목록}
),
base AS (
    SELECT
        a.고객식별자,
        a.사업자등록번호,
        a.기업명,
        SUBSTRING(a.기준년월일, 1, 6) AS 기준년월,
        COALESCE(a.금월신용카드이용금액, 0) + COALESCE(a.금월체크카드이용금액, 0) AS 금월총이용금액
    FROM tbdaa1d12 a
    JOIN managed_list l
      ON a.사업자등록번호 = l.사업자등록번호
    WHERE SUBSTRING(a.기준년월일, 1, 6) BETWEEN '{기간_시작}' AND '{기준년월}'
      AND a.기준년월일 <= DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')
      AND (a."year" || a."month") BETWEEN '{기간_시작}' AND '{기준년월}'
),
pvt AS (
    SELECT
        고객식별자,
        MAX(사업자등록번호) AS 사업자등록번호,
        MAX(기업명) AS 기업명,
        SUM(CASE WHEN 기준년월 = date_format(date_add('month', -5, date_parse('{기준년월}' || '01', '%Y%m%d')), '%Y%m') THEN 금월총이용금액 ELSE 0 END) AS m_minus_5,
        SUM(CASE WHEN 기준년월 = date_format(date_add('month', -4, date_parse('{기준년월}' || '01', '%Y%m%d')), '%Y%m') THEN 금월총이용금액 ELSE 0 END) AS m_minus_4,
        SUM(CASE WHEN 기준년월 = date_format(date_add('month', -3, date_parse('{기준년월}' || '01', '%Y%m%d')), '%Y%m') THEN 금월총이용금액 ELSE 0 END) AS m_minus_3,
        SUM(CASE WHEN 기준년월 = date_format(date_add('month', -2, date_parse('{기준년월}' || '01', '%Y%m%d')), '%Y%m') THEN 금월총이용금액 ELSE 0 END) AS m_minus_2,
        SUM(CASE WHEN 기준년월 = date_format(date_add('month', -1, date_parse('{기준년월}' || '01', '%Y%m%d')), '%Y%m') THEN 금월총이용금액 ELSE 0 END) AS m_minus_1,
        SUM(CASE WHEN 기준년월 = '{기준년월}' THEN 금월총이용금액 ELSE 0 END) AS m_current
    FROM base
    GROUP BY 고객식별자
),
scored AS (
    SELECT
        고객식별자,
        사업자등록번호,
        기업명,
        m_minus_5,
        m_minus_4,
        m_minus_3,
        m_minus_2,
        m_minus_1,
        m_current,
        (m_minus_5 + m_minus_4 + m_minus_3 + m_minus_2 + m_minus_1 + m_current) AS 최근6개월총이용금액,
        (m_minus_3 + m_minus_2 + m_minus_1) / 3 AS 직전3개월평균,
        CASE
            WHEN m_current = 0 THEN '최근월 무실적'
            WHEN (m_minus_3 + m_minus_2 + m_minus_1) > 0
             AND m_current < ((m_minus_3 + m_minus_2 + m_minus_1) / 3) * 0.5 THEN '급감'
            WHEN (m_minus_3 + m_minus_2 + m_minus_1) > 0
             AND m_current >= ((m_minus_3 + m_minus_2 + m_minus_1) / 3) * 2 THEN '급증'
            WHEN m_minus_2 > m_minus_1 AND m_minus_1 > m_current THEN '3개월 연속 감소'
            ELSE '정상'
        END AS 특이사항
    FROM pvt
)
SELECT *
FROM scored
WHERE 특이사항 <> '정상'
ORDER BY 사업자등록번호
LIMIT {limit}"""


def _tool_sql_managed_corporate_delinquency_current(params: dict) -> str:
    관리기업목록 = render_athena_business_number_values(params.get("관리기업목록"))
    기준년월 = _sanitize_param(params.get("기준년월", "202604"))
    limit = _coerce_sql_param("limit", params.get("limit", DEFAULT_QUERY_ROW_LIMIT), {"type": "integer"})
    return f"""WITH managed_list (사업자등록번호) AS (
    VALUES {관리기업목록}
),
ranked AS (
    SELECT
        a.고객식별자,
        a.사업자등록번호,
        a.기업명,
        SUBSTRING(a.기준년월일, 1, 6) AS 기준년월,
        a.기준년월일,
        a.연체여부,
        a.연체일수,
        a.연체금액,
        ROW_NUMBER() OVER (PARTITION BY a.고객식별자 ORDER BY a.기준년월일 DESC) AS rn
    FROM tbdaa1d12 a
    JOIN managed_list l
      ON a.사업자등록번호 = l.사업자등록번호
    WHERE SUBSTRING(a.기준년월일, 1, 6) = '{기준년월}'
      AND a.기준년월일 <= DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')
      AND a."year" = SUBSTRING('{기준년월}', 1, 4)
      AND a."month" = SUBSTRING('{기준년월}', 5, 2)
)
SELECT
    고객식별자,
    사업자등록번호,
    기업명,
    기준년월,
    기준년월일,
    연체여부,
    연체일수,
    연체금액
FROM ranked
WHERE rn = 1
  AND (COALESCE(연체여부, '0') = '1' OR COALESCE(연체일수, 0) > 0)
ORDER BY 연체일수 DESC, 연체금액 DESC
LIMIT {limit}"""


def _tool_sql_managed_corporate_limit_down_monitoring(params: dict) -> str:
    관리기업목록 = render_athena_business_number_values(params.get("관리기업목록"))
    기준년월 = _sanitize_param(params.get("기준년월", "202604"))
    전월기준년월 = _sanitize_param(params.get("전월기준년월", _calc_months_back(기준년월, 1)))
    limit = _coerce_sql_param("limit", params.get("limit", DEFAULT_QUERY_ROW_LIMIT), {"type": "integer"})
    return f"""WITH managed_list (사업자등록번호) AS (
    VALUES {관리기업목록}
),
cur AS (
    SELECT *
    FROM (
        SELECT
            a.고객식별자,
            a.사업자등록번호,
            a.기업명,
            COALESCE(a.기업총한도금액, 0) AS 기업총한도금액,
            ROW_NUMBER() OVER (PARTITION BY a.고객식별자, SUBSTRING(a.기준년월일, 1, 6) ORDER BY a.기준년월일 DESC) AS rn
        FROM tbdaa1d12 a
        WHERE SUBSTRING(a.기준년월일, 1, 6) = '{기준년월}'
          AND a.기준년월일 <= DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')
          AND a."year" = SUBSTRING('{기준년월}', 1, 4)
          AND a."month" = SUBSTRING('{기준년월}', 5, 2)
    )
    WHERE rn = 1
),
pre AS (
    SELECT *
    FROM (
        SELECT
            a.고객식별자,
            a.사업자등록번호,
            a.기업명,
            COALESCE(a.기업총한도금액, 0) AS 기업총한도금액,
            ROW_NUMBER() OVER (PARTITION BY a.고객식별자, SUBSTRING(a.기준년월일, 1, 6) ORDER BY a.기준년월일 DESC) AS rn
        FROM tbdaa1d12 a
        WHERE SUBSTRING(a.기준년월일, 1, 6) = '{전월기준년월}'
          AND a.기준년월일 <= DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')
          AND a."year" = SUBSTRING('{전월기준년월}', 1, 4)
          AND a."month" = SUBSTRING('{전월기준년월}', 5, 2)
    )
    WHERE rn = 1
)
SELECT
    cur.고객식별자,
    cur.사업자등록번호,
    cur.기업명,
    pre.기업총한도금액 AS 전월기업총한도금액,
    cur.기업총한도금액 AS 기준월기업총한도금액,
    pre.기업총한도금액 - cur.기업총한도금액 AS 감액금액
FROM cur
JOIN pre
  ON cur.고객식별자 = pre.고객식별자
JOIN managed_list l
  ON cur.사업자등록번호 = l.사업자등록번호
WHERE cur.기업총한도금액 < pre.기업총한도금액
ORDER BY 감액금액 DESC
LIMIT {limit}"""

# 기존 코드에서 builder 이름으로 직접 접근해야 하는 경우를 위한 선택적 매핑입니다.
SQL_BUILDER_MAP = globals().get("SQL_BUILDER_MAP", {})
SQL_BUILDER_MAP.update({
    "corporate_card_active_no_usage_members": _tool_sql_corporate_card_active_no_usage_members,
    "corporate_card_churned_after_usage_members": _tool_sql_corporate_card_churned_after_usage_members,
    "corporate_check_card_only_high_monthly_avg": _tool_sql_corporate_check_card_only_high_monthly_avg,
    "merchant_corporate_sales_target_no_corporate_card": _tool_sql_merchant_corporate_sales_target_no_corporate_card,
    "corporate_limit_low_utilization_members": _tool_sql_corporate_limit_low_utilization_members,
    "new_sales_targets_usage_amount_detail": _tool_sql_new_sales_targets_usage_amount_detail,
    "managed_corporate_usage_trend_monitoring": _tool_sql_managed_corporate_usage_trend_monitoring,
    "managed_corporate_delinquency_current": _tool_sql_managed_corporate_delinquency_current,
    "managed_corporate_limit_down_monitoring": _tool_sql_managed_corporate_limit_down_monitoring,
})

"""Shared parameter helpers and deterministic SQL builders."""

import re
from datetime import datetime
from calendar import monthrange

# ---------------------------------------------------------------------------
# 4. 공통 Tool 유틸 및 확정적 SQL 생성
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


def _current_date_context() -> str:
    now = datetime.now()
    prev = now.month - 1
    prev_year = now.year
    if prev <= 0:
        prev = 12
        prev_year -= 1
    return (
        f"현재 {now.year}년 {now.month}월 기준, "
        f"올해 시작: {now.year}01, 현재월: {now.year}{now.month:02d}, "
        f"지난달: {prev_year}{prev:02d}"
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
    "monthly_corporate_card_usage": [
        {"name": "기간_시작", "type": "string", "description": "시작 기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM)"},
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
}


def _apply_params_to_vq(base_sql: str, params: dict, vq_name: str, vq_params_def: dict) -> str:
    sql = base_sql
    params = dict(params or {})
    if vq_name == "merchant_card_possession_performance":
        yyyymm = params.get("기준년월") or params.get("기간_시작")
        if yyyymm and not params.get("카드만료기준일"):
            try:
                params["카드만료기준일"] = _month_end_yyyymmdd(str(yyyymm))
            except (TypeError, ValueError):
                pass
        params.setdefault("limit", "100")

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

    for pname, pinfo in vq_params_def.items():
        placeholder = "{" + pname + "}"
        if placeholder in sql:
            value = params.get(pname, pinfo.get("default", ""))
            if value:
                sql = sql.replace(placeholder, _sanitize_param(str(value)))
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
    if detected_time_col and (start or end):
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
    if company:
        company = _escape_like(company)
        for col in ["상호명", "기업검색명"]:
            if col in sql:
                where_additions.append(f"{col} LIKE '%{company}%' ESCAPE '\\'")
                break
    merchant = params.get("가맹점명")
    if merchant and not company:
        merchant = _escape_like(merchant)
        if "가맹점명" in sql:
            where_additions.append(f"가맹점명 LIKE '%{merchant}%' ESCAPE '\\'")
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
        limit = int(limit)
        if re.search(r"LIMIT\s+\d+", sql, re.IGNORECASE):
            sql = re.sub(r"LIMIT\s+\d+", f"LIMIT {limit}", sql, flags=re.IGNORECASE)
        else:
            sql += f"\nLIMIT {limit}"
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
            e = e + "31"
    if s and e:
        conds.append(f"{col} BETWEEN '{s}' AND '{e}'" if s != e else f"{col} = '{s}'")
    elif s:
        conds.append(f"{col} >= '{s}'")
    elif e:
        conds.append(f"{col} <= '{e}'")
    return conds


# -- 기존 Tool SQL 함수들 --

def _tool_sql_심사승인율(params: dict) -> str:
    conds = _period_conds_daily(params, "최종심사년월일")
    if params.get("신용등급"):
        conds.append(f"신용평가등급코드 = '{_sanitize_param(params['신용등급'])}'")
    where = _build_where(conds)
    if params.get("등급별") or params.get("신용등급"):
        return f"""SELECT 신용평가등급코드 AS 신용등급,
    COUNT(DISTINCT 신청서접수번호) AS 총심사건수,
    COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '01' THEN 신청서접수번호 END) AS 승인건수,
    COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '02' THEN 신청서접수번호 END) AS 거절건수,
    ROUND(COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '01' THEN 신청서접수번호 END)::FLOAT
        / NULLIF(COUNT(DISTINCT 신청서접수번호), 0) * 100, 2) AS 승인율_퍼센트,
    AVG(최종부여한도금액) AS 평균부여한도
FROM card_system.tbdaaaf23 {where}
GROUP BY 신용평가등급코드 ORDER BY 신용평가등급코드"""
    else:
        return f"""SELECT
    COUNT(DISTINCT 신청서접수번호) AS 총심사건수,
    COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '01' THEN 신청서접수번호 END) AS 승인건수,
    COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '02' THEN 신청서접수번호 END) AS 거절건수,
    ROUND(COUNT(DISTINCT CASE WHEN 카드처리결과구분코드 = '01' THEN 신청서접수번호 END)::FLOAT
        / NULLIF(COUNT(DISTINCT 신청서접수번호), 0) * 100, 2) AS 승인율_퍼센트,
    AVG(최종부여한도금액) AS 평균부여한도
FROM card_system.tbdaaaf23 {where}"""


def _tool_sql_월별이용금액(params: dict) -> str:
    conds = ["개인기업구분코드 = '2'"]
    conds.extend(_period_conds(params, "기준년월"))
    where = _build_where(conds)
    return f"""SELECT 기준년월,
    SUM(금월이용합계금액) AS 총이용금액, SUM(금월일시불이용금액) AS 일시불이용금액,
    SUM(금월할부이용금액) AS 할부이용금액, SUM(금월ca이용금액) AS CA이용금액,
    SUM(금월이용합계건수) AS 총이용건수
FROM card_system.tmdaa3e16 {where}
GROUP BY 기준년월 ORDER BY 기준년월"""


def _tool_sql_가맹점매출순위(params: dict) -> str:
    conds = _period_conds(params, "a.기준년월")
    if params.get("업종"):
        conds.append(f"b.업종대분류코드명 LIKE '%{_escape_like(params['업종'])}%' ESCAPE '\\'")
    if params.get("가맹점명"):
        conds.append(f"a.가맹점명 LIKE '%{_escape_like(params['가맹점명'])}%' ESCAPE '\\'")
    where = _build_where(conds)
    limit = int(params.get("limit", 10))
    return f"""SELECT a.가맹점번호, a.가맹점명, b.가맹점업종명, b.업종대분류코드명,
    SUM(a.가맹점일시불매출금액 + a.가맹점할부매출금액) AS 총매출금액,
    SUM(a.가맹점일시불매출건수 + a.가맹점할부매출건수) AS 총매출건수,
    AVG(a.신용카드가맹점수수료율) AS 평균수수료율
FROM card_system.tmdaa5e11 a
JOIN card_system.tbdaadb17 b ON a.가맹점업종코드 = b.가맹점업종코드
{where}
GROUP BY a.가맹점번호, a.가맹점명, b.가맹점업종명, b.업종대분류코드명
ORDER BY 총매출금액 DESC LIMIT {limit}"""


def _tool_sql_한도사용률(params: dict) -> str:
    conds = _period_conds(params, "작업기준년월")
    if params.get("기업명"):
        conds.append(f"상호명 LIKE '%{_escape_like(params['기업명'])}%' ESCAPE '\\'")
    where = _build_where(conds)
    limit = int(params.get("limit", 20))
    return f"""SELECT 상호명, 사업자등록번호,
    SUM(총한도금액) AS 총한도, SUM(카드이용합계금액) AS 총이용금액,
    CASE WHEN SUM(총한도금액) > 0
         THEN ROUND((SUM(카드이용합계금액)::FLOAT / SUM(총한도금액)) * 100, 2) ELSE 0
    END AS 한도사용률_퍼센트
FROM card_system.tbdaaha97 {where}
GROUP BY 상호명, 사업자등록번호 HAVING SUM(총한도금액) > 0
ORDER BY 한도사용률_퍼센트 DESC LIMIT {limit}"""


def _tool_sql_업종별매출(params: dict) -> str:
    conds = ["a.개인기업구분코드 = '2'", "(a.전표취소구분코드 IS NULL OR a.전표취소구분코드 = '0')"]
    conds.extend(_period_conds_daily(params, "a.전표매출년월일"))
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


def _tool_sql_기업별연체현황(params: dict) -> str:
    conds = ["연체금액 > 0"]
    conds.extend(_period_conds(params, "작업기준년월"))
    if params.get("기업명"):
        conds.append(f"상호명 LIKE '%{_escape_like(params['기업명'])}%' ESCAPE '\\'")
    where = _build_where(conds)
    limit = int(params.get("limit", 20))
    return f"""SELECT 상호명, 사업자등록번호,
    SUM(총한도금액) AS 총한도, SUM(카드이용합계금액) AS 총이용금액, SUM(연체금액) AS 연체금액,
    CASE WHEN SUM(카드이용합계금액) > 0
         THEN ROUND((SUM(연체금액)::FLOAT / SUM(카드이용합계금액)) * 100, 2) ELSE 0
    END AS 연체율_퍼센트
FROM card_system.tbdaaha97 {where}
GROUP BY 상호명, 사업자등록번호 ORDER BY 연체금액 DESC LIMIT {limit}"""


def _tool_sql_카드등급별연체(params: dict) -> str:
    conds = ["개인기업구분코드 = '2'"]
    conds.extend(_period_conds(params, "기준년월"))
    where = _build_where(conds)
    return f"""SELECT 카드등급구분코드,
    COUNT(DISTINCT 카드구분키번호) AS 카드수, SUM(금월이용합계금액) AS 총이용금액,
    SUM(연체원금) AS 총연체원금,
    CASE WHEN SUM(금월이용합계금액) > 0
         THEN ROUND((SUM(연체원금)::FLOAT / SUM(금월이용합계금액)) * 100, 2) ELSE 0
    END AS 연체율_퍼센트
FROM card_system.tmdaa3e16 {where}
GROUP BY 카드등급구분코드 ORDER BY 총연체원금 DESC"""


def _tool_sql_가맹점카드소지현황(params: dict) -> str:
    yyyymm = _sanitize_param(params["기준년월"])
    card_valid_after = _sanitize_param(params.get("카드만료기준일") or _month_end_yyyymmdd(yyyymm))
    limit = int(params.get("limit", 100))

    base_conds = []
    if params.get("가맹점명"):
        base_conds.append(f"a.가맹점명 LIKE '%{_escape_like(params['가맹점명'])}%' ESCAPE '\\'")
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
    FROM card_system.tbdaadb17
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
    FROM card_system.tbdaadt01 a
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
    FROM card_system.tmdaa5e11
    WHERE 기준년월 = '{yyyymm}'
),
card_base AS (
    SELECT DISTINCT
        본인고객식별자,
        개인기업구분코드,
        카드결제기관구분코드,
        상품중분류구분코드,
        CASE WHEN 카드결제기관구분코드 = '004' THEN '1' ELSE '0' END AS KB카드계좌여부
    FROM card_system.tbdaaat05
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

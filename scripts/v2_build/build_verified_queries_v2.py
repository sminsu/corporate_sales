"""v1 sql_verified_queries.yaml → corporate_sales_fable_v2/sql_verified_queries.yaml.

이 파일의 SQL은 프롬프트의 "참고 SQL 예시"로 그대로 들어가므로, 여기 있는 오류를
모델이 베낀다. goldenset v2 실패 중 아래가 그렇게 생겼다.

    22건  COLUMN_NOT_FOUND: '개인기업구분코드'
          special_debt_by_asset_quality 가 FROM card_system.tmdaaus01(가맹점 월말요약)
          인데 SELECT 하는 컬럼은 전부 tbmaisd06(특수채권) 소속이다. 모델이 이 예시를
          그대로 베껴 존재하지 않는 컬럼을 조회했다.

     2건  mismatched input ':'
          ::FLOAT 캐스트가 8곳 있었고 실패한 agent SQL은 이 표현을 글자 그대로 베꼈다.
          예: ROUND(SUM(금월이용합계금액)::FLOAT / NULLIF(...), 0) → 2345행과 동일.

merchant_risk_combined_customer_month 도 tbmaisd06 에서 가맹점번호·전월매입금액을
읽고 있어 같은 종류의 오류다. 가맹점 월 실적 원천인 tbdaaus01 로 고친다.

고친 뒤 모든 쿼리를 semantic layer 로 검증하고, 미해결 컬럼이 남으면 실패한다.

usage:
    python corporate_sales_fable_v2/scripts/build_verified_queries_v2.py
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from ruamel.yaml import YAML

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # v2 적용본 루트
V1_ROOT = PROJECT_ROOT.parent / "corporate_sales_fable"   # 변환 원본(v1 저장소)
sys.path.insert(0, str(PROJECT_ROOT))

from text2sql_agent.v2.sql_dialect_guard import audit_sql, rewrite_postgres_casts  # noqa: E402
from text2sql_agent.v2.verified_query_audit import unregistered_tables, unresolved_identifiers  # noqa: E402

# 잘못된 원천 테이블. (쿼리 이름, 기존 테이블, 올바른 테이블)
TABLE_FIXES: tuple[tuple[str, str, str], ...] = (
    # 자산건전성분류구분코드·특수채권편입* 는 tbmaisd06(특수채권) 컬럼이다.
    ("special_debt_by_asset_quality", "tmdaaus01", "tbmaisd06"),
    # 월 집계의 가맹점·기업고객·전월매입 컬럼은 월 가맹점 풍성화 원천을 쓴다.
    ("merchant_risk_combined_customer_month", "tbmaisd06", "tmdaaus01"),
)

# 가맹점 마스터(tbdaadt01)를 읽는 질의들. v2 초기에는 이 넷에 최근 10일
# 실적기준년월일 창을 얹었는데 되돌렸다. 마스터는 가맹점번호 1건이 grain 이라
# 창이 없어도 행이 불어나지 않고, 질문이 날짜를 말하지 않았는데 조회 범위를 좁히면
# 묻지 않은 조건이 답에 섞인다(사용자 제공). 날짜를 말한 질문은 실행 직전에
# _apply_tbdaadt01_historical_source 가 그 날짜로 조건을 넣는다.
MERCHANT_MASTER_QUERIES = (
    "brand_active_merchant_count",
    "merchant_detail_by_name",
    "merchant_payment_institution_list",
    "merchant_count_by_payment_institution",
)

HISTORICAL_CORPORATE_MONTH_QUERIES = (
    "corporate_member_with_usage_count",
    "corporate_card_churned_after_usage_members",
    "corporate_check_card_only_high_monthly_avg",
    "corporate_monthly_average_usage",
    "corporate_limit_status_at_month",
    "corporate_limit_low_utilization_members",
    "corporate_target_industry_usage",
    "managed_company_usage_anomalies",
    "managed_company_delinquency",
    "managed_company_limit_reduction",
    "named_corporate_half_year_usage",
)

PREVIOUS_DAY_MERCHANT_QUERIES = (
    "recent_closed_brand_merchant_count",
    "brand_merchants_with_corporate_card",
    "brand_merchant_owner_corporate_card_count",
    "region_top_enterprise_merchants",
    "marketing_industry_card_portfolio",
)

# 가맹점거래정지여부는 '0' 정상영업, '1' 거래정지다(사용자 제공 코드북). v1 예시가
# COALESCE(..., 'N') <> 'Y' 라고 적어 두었는데, 참고 SQL 예시는 프롬프트에 그대로
# 들어가므로 모델이 이 값을 베낀다. 코드북과 같은 값으로 맞춘다.
MERCHANT_SUSPENSION_PREDICATE_FIXES: tuple[tuple[str, str, str], ...] = (
    (
        "region_top_enterprise_merchants",
        "COALESCE(가맹점거래정지여부, 'N') <> 'Y'",
        "COALESCE(가맹점거래정지여부, '0') = '0'",
    ),
)

_PREVIOUS_DAY_EXPRESSION = (
    "DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')"
)

_CORPORATE_DAY_RANGE = re.compile(
    r'a\."기준년월일"\s+BETWEEN\s+CONCAT\((.+?),\s*\'01\'\)'
    r'\s+AND\s+CONCAT\((.+?),\s*\'31\'\)',
    re.DOTALL,
)


def _yaml() -> YAML:
    parser = YAML()
    parser.preserve_quotes = True
    parser.width = 4096
    parser.indent(mapping=2, sequence=2, offset=0)
    return parser


def _query(by_name: dict[str, dict], name: str) -> dict:
    query = by_name.get(name)
    if query is None:
        raise SystemExit(f"쿼리 없음: {name}")
    return query


def _route_corporate_sql_to_monthly(sql: str) -> str:
    sql = sql.replace("card_system.tbdaa1d12", "card_system.tmdaa1d12")
    sql = _CORPORATE_DAY_RANGE.sub(
        lambda match: (
            f'a."기준년월" = {match.group(1)}'
            if match.group(1).strip() == match.group(2).strip()
            else f'a."기준년월" BETWEEN {match.group(1)} AND {match.group(2)}'
        ),
        sql,
    )
    return sql.replace('SUBSTR(a."기준년월일", 1, 6)', 'a."기준년월"')


def _route_historical_corporate_query(query: dict) -> None:
    sql = _route_corporate_sql_to_monthly(str(query.get("sql") or ""))
    if "card_system.tbdaa1d12" in sql or 'a."기준년월"' not in sql:
        raise SystemExit(f"{query.get('name')}: 월 기업고객 원천 변환 실패")
    query["sql"] = sql

    description = str(query.get("description") or "")
    query["description"] = (
        description.replace("tbdaa1d12", "tmdaa1d12")
        .replace("기업고객 일별 스냅샷", "월기업고객 스냅샷")
        .replace("일별 스냅샷", "월 스냅샷")
    )
    query["tags"] = ["tmdaa1d12" if tag == "tbdaa1d12" else tag for tag in query.get("tags") or []]


def _route_mixed_current_history_query(query: dict) -> None:
    sql = str(query.get("sql") or "")
    old_current = """WITH latest_month AS (
  SELECT MAX(SUBSTR(a.\"기준년월일\", 1, 6)) AS \"현재기준년월\"
  FROM card_system.tbdaa1d12 a
),"""
    new_current = """WITH previous_day AS (
  SELECT DATE_FORMAT(
    DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'),
    '%Y%m%d'
  ) AS \"현재기준일\"
),"""
    if old_current in sql:
        sql = sql.replace(old_current, new_current, 1)
        sql = sql.replace("CROSS JOIN latest_month l", "CROSS JOIN previous_day l", 1)
        sql = sql.replace(
            'WHERE SUBSTR(a."기준년월일", 1, 6) = l."현재기준년월"',
            'WHERE a."기준년월일" = l."현재기준일"',
            1,
        )

    prefix, marker, remainder = sql.partition("usage_ranked AS (")
    usage, closing, suffix = remainder.partition("),\nmonthly_usage AS (")
    if not marker or not closing:
        raise SystemExit(f"{query.get('name')}: 이력 이용 CTE를 찾지 못했다")
    usage = _route_corporate_sql_to_monthly(usage)
    sql = prefix + marker + usage + closing + suffix
    if sql.count("card_system.tbdaa1d12") != 1 or sql.count("card_system.tmdaa1d12") != 1:
        raise SystemExit(f"{query.get('name')}: 현재/과거 원천 분리 실패")
    query["sql"] = sql
    query["description"] = (
        "한국 시간 기준 전일에 유효 기업카드를 보유한 기업회원만 대상으로 월기업고객기본의 "
        "고객·월 최신행 이용금액을 두 반기별로 합산하고, 대상 반기 이용금액이 기준 반기보다 "
        "하락한 모든 기업회원을 반환합니다."
    )
    tags = list(query.get("tags") or [])
    if "tmdaa1d12" not in tags:
        tags.insert(tags.index("Athena") if "Athena" in tags else len(tags), "tmdaa1d12")
    query["tags"] = tags


def _keep_current_corporate_snapshot(query: dict) -> None:
    sql = str(query.get("sql") or "")
    marker = "  FROM card_system.tbdaa1d12 a\n"
    if 'WHERE a."기준년월일" = DATE_FORMAT(' not in sql:
        replacement = marker + "  WHERE a.\"기준년월일\" = DATE_FORMAT(\n" \
            "    DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'),\n" \
            "    '%Y%m%d'\n" \
            "  )\n"
        if marker not in sql:
            raise SystemExit(f"{query.get('name')}: 현재 기업고객 원천을 찾지 못했다")
        sql = sql.replace(marker, replacement, 1)
    query["sql"] = sql


def _apply_previous_day_merchant_fix(query: dict) -> None:
    name = str(query.get("name") or "")
    sql = str(query.get("sql") or "")
    if name == "recent_closed_brand_merchant_count":
        sql = sql.replace("CURRENT_DATE", "DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul')")
    elif name == "brand_merchants_with_corporate_card":
        marker = '  WHERE a."기준년월일" BETWEEN CONCAT(\'{기준년월}\', \'01\') AND CONCAT(\'{기준년월}\', \'31\')\n'
        if _PREVIOUS_DAY_EXPRESSION not in sql:
            sql = sql.replace(
                marker,
                marker + '    AND a."기준년월일" <= ' + _PREVIOUS_DAY_EXPRESSION + "\n",
                1,
            )
    elif name == "brand_merchant_owner_corporate_card_count":
        sql = re.sub(
            r'^WITH latest_snapshot AS \(.+?\)\n',
            "",
            sql,
            count=1,
            flags=re.DOTALL,
        )
        sql = sql.replace('(SELECT l."기준년월일" FROM latest_snapshot l)', _PREVIOUS_DAY_EXPRESSION)
        query["description"] = str(query.get("description") or "").replace("최신 가맹점 일별", "한국 시간 기준 전일 가맹점")
    else:
        sql = sql.replace(
            "(SELECT MAX(기준년월일) FROM card_system.tbdaaus01)",
            _PREVIOUS_DAY_EXPRESSION,
        )
    if "CURRENT_DATE" in sql or "MAX(기준년월일) FROM card_system.tbdaaus01" in sql:
        raise SystemExit(f"{name}: 전일 기준 변환 실패")
    query["sql"] = sql


def _fix_merchant_risk_monthly_load(query: dict) -> None:
    sql = str(query.get("sql") or "")
    marker = "  FROM card_system.tmdaaus01\n"
    if "WHERE 기준년월 BETWEEN '{기간_시작}' AND '{기간_종료}'" not in sql:
        if marker not in sql:
            raise SystemExit(f"{query.get('name')}: 월 가맹점 원천을 찾지 못했다")
        sql = sql.replace(
            marker,
            marker + "  WHERE 기준년월 BETWEEN '{기간_시작}' AND '{기간_종료}'\n",
            1,
        )
        sql = sql.replace(
            "  FROM card_system.tbmewcm94\n  WHERE 개인기업구분코드 = '2'",
            "  FROM card_system.tbmewcm94\n"
            "  WHERE 기준년월 BETWEEN '{기간_시작}' AND '{기간_종료}'\n"
            "    AND 개인기업구분코드 = '2'",
            1,
        )
    if sql.count("WHERE 기준년월 BETWEEN '{기간_시작}' AND '{기간_종료}'") != 2:
        raise SystemExit(f"{query.get('name')}: 두 월 원천의 적재 경계 변환 실패")
    query["sql"] = sql
    parameters = query.setdefault("parameters", {})
    parameters.setdefault(
        "기간_시작",
        {
            "type": "string",
            "description": "가맹점 포트폴리오 조회 시작 기준년월(YYYYMM)",
            "required": True,
        },
    )
    parameters.setdefault(
        "기간_종료",
        {
            "type": "string",
            "description": "가맹점 포트폴리오 조회 종료 기준년월(YYYYMM)",
            "required": True,
        },
    )


def apply_fixes(document: dict) -> dict:
    queries = document.get("verified_queries") or []
    by_name = {str(query.get("name")): query for query in queries}
    stats = {"tables_fixed": [], "casts_fixed": [], "cadence_fixed": [], "codebook_fixed": []}

    for name, wrong, right in TABLE_FIXES:
        query = _query(by_name, name)
        sql = str(query.get("sql") or "")
        if wrong in sql:
            query["sql"] = sql.replace(wrong, right)
        elif right not in sql:
            raise SystemExit(f"{name}: '{wrong}' 또는 '{right}' 참조를 찾지 못했다")
        stats["tables_fixed"].append(f"{name}: {wrong} -> {right}")

    _fix_merchant_risk_monthly_load(_query(by_name, "merchant_risk_combined_customer_month"))

    for query in queries:
        sql = str(query.get("sql") or "")
        if "::" not in sql:
            continue
        rewritten = rewrite_postgres_casts(sql)
        if rewritten != sql:
            query["sql"] = rewritten
            stats["casts_fixed"].append(str(query.get("name")))

    for name in MERCHANT_MASTER_QUERIES:
        sql = str(_query(by_name, name).get("sql") or "")
        if "실적기준년월일" in sql:
            raise SystemExit(f"{name}: 가맹점 마스터에 적재 창 조건이 다시 들어왔다")

    for name in HISTORICAL_CORPORATE_MONTH_QUERIES:
        _route_historical_corporate_query(_query(by_name, name))
        stats["cadence_fixed"].append(f"{name}: tmdaa1d12 기준년월")

    _route_mixed_current_history_query(_query(by_name, "current_corporate_half_year_usage_decline"))
    stats["cadence_fixed"].append(
        "current_corporate_half_year_usage_decline: tbdaa1d12 전일 + tmdaa1d12 이력"
    )

    _keep_current_corporate_snapshot(
        _query(by_name, "current_corporate_member_half_year_new_check_card_issuance")
    )
    stats["cadence_fixed"].append(
        "current_corporate_member_half_year_new_check_card_issuance: tbdaa1d12 전일"
    )

    for name in PREVIOUS_DAY_MERCHANT_QUERIES:
        _apply_previous_day_merchant_fix(_query(by_name, name))
        stats["cadence_fixed"].append(f"{name}: tbdaaus01 한국 시간 전일")

    for name, wrong, right in MERCHANT_SUSPENSION_PREDICATE_FIXES:
        query = _query(by_name, name)
        sql = str(query.get("sql") or "")
        if wrong in sql:
            query["sql"] = sql.replace(wrong, right)
        elif right not in sql:
            raise SystemExit(f"{name}: 가맹점거래정지여부 조건을 찾지 못했다")
        stats["codebook_fixed"].append(f"{name}: {wrong} -> {right}")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=V1_ROOT / "text2sql_agent" / "tools" / "sql_verified_queries.yaml"
    )
    parser.add_argument("--schema", type=Path, default=PROJECT_ROOT / "semantic_layer.yaml")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "text2sql_agent" / "tools" / "sql_verified_queries.yaml")
    args = parser.parse_args()

    yaml = _yaml()
    document = yaml.load(args.source.read_text(encoding="utf-8"))
    stats = apply_fixes(document)

    failures: list[str] = []
    warnings: list[str] = []
    for query in document.get("verified_queries") or []:
        name = str(query.get("name"))
        sql = str(query.get("sql") or "")
        if not sql.strip():
            # sql 키가 없는 항목은 tools/sql_builders.py 의 결정적 빌더가 만든다.
            continue
        missing_tables = unregistered_tables(sql, args.schema)
        if missing_tables:
            # 테이블 정의가 없으면 컬럼 검증도 못 하므로 경고만 남기고 통과시킨다.
            warnings.append(f"{name}: semantic layer 미등록 테이블 {missing_tables}")
            continue
        unresolved = unresolved_identifiers(sql, args.schema)
        if unresolved:
            failures.append(f"{name}: 미해결 컬럼 {unresolved}")
        issues = audit_sql(sql)
        if issues:
            failures.append(f"{name}: {issues}")

    print(f"table fixes ({len(stats['tables_fixed'])}):")
    for line in stats["tables_fixed"]:
        print(f"  {line}")
    print(f"postgres cast fixes ({len(stats['casts_fixed'])}): {', '.join(stats['casts_fixed'])}")
    print(f"cadence fixes ({len(stats['cadence_fixed'])}):")
    for line in stats["cadence_fixed"]:
        print(f"  {line}")
    print(f"codebook fixes ({len(stats['codebook_fixed'])}):")
    for line in stats["codebook_fixed"]:
        print(f"  {line}")

    if warnings:
        print(f"\nwarnings ({len(warnings)}):")
        for line in warnings:
            print(f"  {line}")

    if failures:
        print(f"\nFAIL {len(failures)} query issues remain:")
        for line in failures:
            print(f"  {line}")
        raise SystemExit(1)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        yaml.dump(document, handle)
    print(f"\nverified queries v2 -> {args.output} (all {len(document['verified_queries'])} queries validated)")


if __name__ == "__main__":
    main()

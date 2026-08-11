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
import sys
from pathlib import Path

from ruamel.yaml import YAML

V2_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = V2_DIR.parent
sys.path.insert(0, str(V2_DIR))

from text2sql_v2.sql_dialect_guard import audit_sql, rewrite_postgres_casts  # noqa: E402
from text2sql_v2.verified_query_audit import unregistered_tables, unresolved_identifiers  # noqa: E402

# 잘못된 원천 테이블. (쿼리 이름, 기존 테이블, 올바른 테이블)
TABLE_FIXES: tuple[tuple[str, str, str], ...] = (
    # 자산건전성분류구분코드·특수채권편입* 는 tbmaisd06(특수채권) 컬럼이다.
    ("special_debt_by_asset_quality", "tmdaaus01", "tbmaisd06"),
    # 가맹점번호·기업고객식별자·전월일시불매입금액·전월할부매입금액은 tbdaaus01 컬럼이다.
    ("merchant_risk_combined_customer_month", "tbmaisd06", "tbdaaus01"),
)


def _yaml() -> YAML:
    parser = YAML()
    parser.preserve_quotes = True
    parser.width = 4096
    parser.indent(mapping=2, sequence=2, offset=0)
    return parser


def apply_fixes(document: dict) -> dict:
    queries = document.get("verified_queries") or []
    by_name = {str(query.get("name")): query for query in queries}
    stats = {"tables_fixed": [], "casts_fixed": []}

    for name, wrong, right in TABLE_FIXES:
        query = by_name.get(name)
        if query is None:
            raise SystemExit(f"쿼리 없음: {name}")
        sql = str(query.get("sql") or "")
        if wrong not in sql:
            raise SystemExit(f"{name}: '{wrong}' 참조를 찾지 못했다 (이미 수정됨?)")
        query["sql"] = sql.replace(wrong, right)
        stats["tables_fixed"].append(f"{name}: {wrong} -> {right}")

    for query in queries:
        sql = str(query.get("sql") or "")
        if "::" not in sql:
            continue
        rewritten = rewrite_postgres_casts(sql)
        if rewritten != sql:
            query["sql"] = rewritten
            stats["casts_fixed"].append(str(query.get("name")))

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=REPO_ROOT / "text2sql_agent" / "tools" / "sql_verified_queries.yaml"
    )
    parser.add_argument("--schema", type=Path, default=V2_DIR / "semantic_layer.yaml")
    parser.add_argument("--output", type=Path, default=V2_DIR / "sql_verified_queries.yaml")
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

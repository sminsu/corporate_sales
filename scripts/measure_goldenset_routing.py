#!/usr/bin/env python3
"""골든셋으로 라우팅·프롬프트 컬럼·정적 검증을 LLM·Athena 없이 측정한다.

에이전트가 답을 못 내는 원인은 크게 세 군데다. 이 스크립트는 그 세 군데를
모델 호출 없이 센다.

    1. 테이블 라우팅   : 정답 테이블이 규칙 후보에 드는지, 1순위인지
    2. 프롬프트 컬럼   : 정답 SQL이 쓴 컬럼이 프롬프트 컬럼 예산에 살아남는지
    3. 정적 검증       : 정답 SQL 자체가 guardrail 을 통과하는지
                         (통과하지 못하는 규칙은 정답을 반려하고 재시도만 돌린다)

    python scripts/measure_goldenset_routing.py
    python scripts/measure_goldenset_routing.py --cases tests/fixtures/corporate_sales_text2sql_goldenset_v2.jsonl
    python scripts/measure_goldenset_routing.py --failures reports/routing-failures.json

스키마를 바꿔 보고 비교하려면 SEMANTIC_SCHEMA_PATH 로 다른 semantic layer 를
가리키면 된다.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CASES = ROOT / "tests" / "fixtures" / "corporate_sales_text2sql_goldenset_v3.jsonl"
# 스키마 prefix 는 에이전트가 실행 직전에 붙인다. 골든셋 SQL 에는 없으므로 센다는
# 의미가 없다.
_PREFIX_ISSUE_RE = re.compile(r"prefix|card_system")


def _load(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _declared_columns(schema: dict) -> frozenset[str]:
    return frozenset(
        str(column.get("name") or "")
        for table in schema.get("tables", [])
        for section in ("dimensions", "measures", "time_dimensions")
        for column in table.get(section) or []
    )


def _static_issues(workflow, validate_against_schema, question: str, sql: str, tables: list[str]) -> list[str]:
    routed = workflow._apply_accumulation_historical_sources(question, sql)
    issues = [
        issue
        for issue in validate_against_schema(routed, tables)
        if not _PREFIX_ISSUE_RE.search(issue)
    ]
    issues += workflow._validate_required_semantic_tables(question, routed, tables)
    issues += workflow._validate_recent_month_semantics(question, routed)
    issues += workflow._validate_requested_row_constraints(question, routed)
    issues += workflow._validate_corporate_entity_grain(question, routed)
    issues += workflow._validate_sales_slip_net_amount(routed)
    issues += workflow._availability_policy_issues(question, routed, tables)
    return issues


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--failures", type=Path, default=None, help="실패 건 JSON 저장 경로")
    parser.add_argument("--top", type=int, default=15, help="원인별 상위 몇 개를 볼지")
    parser.add_argument(
        "--pair-equivalent",
        action="store_true",
        help=(
            "적재 짝(tbdaa1d12↔tmdaa1d12 등)을 같은 테이블로 세서 비교한다. "
            "골든셋이 짝 중 한쪽만 적어 둔 경우(v1·v2)에 쓴다. v3 는 현재/과거 월을 "
            "정책대로 갈라 적어 두므로 기본값(엄격 비교)이 맞다."
        ),
    )
    args = parser.parse_args()

    from text2sql_agent import workflow
    from text2sql_agent.schema import SCHEMA, _validate_sql_against_schema
    from text2sql_agent.time_policy import TABLE_ACCUMULATION_POLICIES

    # 적재 정책이 이미 짝을 들고 있다. 목록을 또 두면 새 짝을 등록할 때 어긋난다.
    pairs = {
        name.lower(): str(policy["historical_source"]["table"]).lower()
        for name, policy in TABLE_ACCUMULATION_POLICIES.items()
        if isinstance(policy.get("historical_source"), dict)
    }

    def canonical(name: str) -> str:
        lowered = str(name).rsplit(".", 1)[-1].lower()
        return pairs.get(lowered, lowered) if args.pair_equivalent else lowered

    cases = _load(args.cases)
    declared = _declared_columns(SCHEMA)

    counts = collections.Counter()
    single_table_cases = 0
    missing_tables = collections.Counter()
    missing_columns = collections.Counter()
    guard_reasons = collections.Counter()
    failures: list[dict] = []

    for case in cases:
        question = str(case["question"])
        expected = [str(name).rsplit(".", 1)[-1] for name in case["expected_tables"]]
        ranked = workflow._rule_rank_tables(question)
        compared = {canonical(name) for name in ranked}
        details = workflow._table_details(expected, question)
        needed = {
            name for name in re.findall(r'"([^"]+)"', str(case["sql"])) if name in declared
        }
        absent = sorted(name for name in needed if f"- {name} [" not in details)
        issues = _static_issues(
            workflow, _validate_sql_against_schema, question, str(case["sql"]), expected
        )

        table_ok = {canonical(name) for name in expected} <= compared
        counts["table_recall"] += table_ok
        counts["column_recall"] += not absent
        counts["guard_clean"] += not issues
        counts["all_three"] += table_ok and not absent and not issues
        if len(expected) == 1:
            single_table_cases += 1
            counts["rank_1"] += bool(ranked) and canonical(ranked[0]) == canonical(expected[0])

        if not table_ok:
            missing_tables[
                tuple(sorted({canonical(name) for name in expected} - compared))
            ] += 1
        for name in absent:
            missing_columns[name] += 1
        for issue in issues:
            guard_reasons[re.sub(r"'[^']*'|\d+", "*", issue)[:90]] += 1
        if not table_ok or absent or issues:
            failures.append(
                {
                    "id": case.get("id"),
                    "question": question,
                    "expected_tables": expected,
                    "ranked_tables": ranked,
                    "missing_columns": absent,
                    "guard_issues": issues,
                }
            )

    total = len(cases)
    print(f"cases                : {total}  ({args.cases})")
    for key, label in (
        ("table_recall", "정답 테이블이 후보에 있음"),
        ("rank_1", "정답 테이블이 1순위 (단일 테이블 건)"),
        ("column_recall", "정답 SQL 컬럼이 프롬프트에 있음"),
        ("guard_clean", "정답 SQL이 정적 검증 통과"),
        ("all_three", "세 가지 모두"),
    ):
        denominator = single_table_cases if key == "rank_1" else total
        value = counts[key]
        print(f"{label:<34}: {value}/{denominator} ({value / denominator:.1%})")

    for title, counter in (
        ("후보에 못 든 테이블", missing_tables),
        ("프롬프트에서 잘린 컬럼", missing_columns),
        ("정답 SQL을 반려한 규칙", guard_reasons),
    ):
        if not counter:
            continue
        print(f"\n{title}:")
        for key, value in counter.most_common(args.top):
            print(f"  {value:4d} {key}")

    if args.failures:
        args.failures.parent.mkdir(parents=True, exist_ok=True)
        args.failures.write_text(
            json.dumps(failures, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\nfailures -> {args.failures}")


if __name__ == "__main__":
    main()

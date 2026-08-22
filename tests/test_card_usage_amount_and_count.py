"""카드 이용금액은 카드 월실적에서, 이용 기업수는 전표의 기업고객식별자로 센다.

두 질문 모두 카드브랜드 축이라 후보가 같은 8개 테이블로 묶인다. 카드브랜드 속성이
그 8곳에 똑같이 +36 을 얹고 나면 남는 몇 점이 순서를 정하는데, 그 몇 점이 양쪽 다
엉뚱한 곳에 붙어 있었다.

    법인카드 이용금액을 카드브랜드별로 알려줘   -> tbdaabt30(매출전표) 1순위
    법인카드 이용 기업수를 카드브랜드별로 알려줘 -> tbdaaat05(회원카드기본) 1순위

이용금액은 전표가 테이블 동의어로 "이용금액" 을 들고 있어서였다. 전표의 매출금액은
가맹점이 받은 돈이고 취소·정정을 순액으로 풀어야 하는 값이며, 카드가 쓴 돈은 카드
월실적의 금월이용합계금액이다(semantic layer 10번 용어). 이용 기업수는 기업고객식별자를
가진 테이블이 전표뿐인데 그걸 말해 주는 지표가 없어서, 8곳이 동점으로 묶이자 선언
순서가 앞선 회원카드기본이 이겼다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import SCHEMA, _validate_sql_against_schema

ROOT = Path(__file__).resolve().parents[1]

# (질문, 1순위 테이블, 프롬프트에 있어야 하는 컬럼)
CARD_USAGE_QUESTIONS = [
    ("법인카드 이용 기업수를 카드브랜드별로 알려줘", "tbdaabt30", "기업고객식별자"),
    ("2026년 1월 법인카드 이용 기업수를 카드브랜드별로 알려줘", "tbdaabt30", "기업고객식별자"),
    ("법인카드 이용금액을 카드브랜드별로 알려줘", "tmdaa3e16", "금월이용합계금액"),
    (
        "2026년 4월 기업카드 법인카드 이용금액을 카드브랜드별로 보여줘",
        "tmdaa3e16",
        "금월이용합계금액",
    ),
    (
        "2026년 3월 기업카드 이용금액의 카드브랜드별 구성비를 알려줘",
        "tmdaa3e16",
        "금월이용합계금액",
    ),
]


@pytest.mark.parametrize(("question", "table", "column"), CARD_USAGE_QUESTIONS)
def test_amount_and_company_count_pick_their_own_source(
    question: str, table: str, column: str
) -> None:
    del column
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == table, (question, ranked)


@pytest.mark.parametrize(("question", "table", "column"), CARD_USAGE_QUESTIONS)
def test_the_measure_column_reaches_the_prompt(
    question: str, table: str, column: str
) -> None:
    del table
    ranked = workflow._rule_rank_tables(question)

    assert f"- {column} [" in workflow._table_details(ranked, question), question


def test_card_using_company_count_counts_the_corporate_id() -> None:
    """회원일련번호로 세면 한 기업의 부서·카드가 여러 건으로 갈라진다."""
    metric = next(
        item
        for item in SCHEMA["canonical_metrics"]
        if item["name"] == "이용기업수"
    )

    assert metric["source_table"] == "tbdaabt30"
    assert metric["expression"] == 'COUNT(DISTINCT tbdaabt30."기업고객식별자")'
    assert metric["domain"] == "card_usage"


def test_registered_company_count_is_a_different_metric() -> None:
    """기업고객수는 카드를 쓰지 않은 기업까지 센다. 두 지표가 같은 말이 아니다."""
    registered = next(
        item for item in SCHEMA["canonical_metrics"] if item["name"] == "기업고객수"
    )

    assert registered["source_table"] == "tbdaaat01"
    assert "이용기업수" not in (registered.get("synonyms") or [])


def test_slip_only_axes_keep_the_slip_table_as_a_candidate() -> None:
    """업종·결제처는 카드 월실적에 없는 축이라 전표가 후보에 남아야 한다."""
    for question in (
        "2026년 2월 결제처 업종별 이용 기업수 상위 50개를 알려줘",
        "2026년 1월 MCC 업종별 이용 기업수 상위 5개를 알려줘",
    ):
        assert "tbdaabt30" in workflow._rule_rank_tables(question), question


def test_sales_slip_no_longer_answers_to_the_usage_word_by_itself() -> None:
    """전표 테이블 동의어의 "이용금액" 을 그 뜻 그대로인 컬럼으로 옮겼다."""
    slip = next(item for item in SCHEMA["tables"] if item["name"] == "tbdaabt30")
    card_month = next(item for item in SCHEMA["tables"] if item["name"] == "tmdaa3e16")
    usage_column = next(
        column
        for column in card_month["measures"]
        if column["name"] == "금월이용합계금액"
    )

    assert "이용금액" not in (slip.get("synonyms") or [])
    assert "이용금액" in usage_column["synonyms"]
    # 컬럼 동의어는 남긴다. 전표에만 있는 축을 물을 때 후보로 남을 근거다.
    slip_amount = next(
        column for column in slip["measures"] if column["name"] == "매출금액"
    )
    assert "이용금액" in slip_amount["synonyms"]


@pytest.mark.parametrize(
    "case_id", ["cs-golden-v3-0035", "cs-golden-v3-0033", "cs-golden-v3-0192"]
)
def test_goldenset_answers_survive_the_static_guardrails(case_id: str) -> None:
    case = next(item for item in _goldenset_v3() if item["id"] == case_id)
    question = case["question"]
    tables = [str(name).rsplit(".", 1)[-1] for name in case["expected_tables"]]
    sql = workflow._apply_accumulation_historical_sources(question, case["sql"])

    issues = [
        issue
        for issue in _validate_sql_against_schema(sql, tables)
        if not re.search(r"prefix|card_system", issue)
    ]
    issues += workflow._validate_required_semantic_tables(question, sql, tables)
    issues += workflow._validate_recent_month_semantics(question, sql)
    issues += workflow._validate_corporate_entity_grain(question, sql)
    issues += workflow._validate_sales_slip_net_amount(sql)
    issues += workflow._availability_policy_issues(question, sql, tables)

    assert not issues, (question, issues)


def _goldenset_v3() -> list[dict]:
    path = ROOT / "tests" / "fixtures" / "corporate_sales_text2sql_goldenset_v3.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

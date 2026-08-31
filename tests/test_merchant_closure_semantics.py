"""폐업을 물으면 그 낟알의 컬럼을 프롬프트에 남긴다.

"빽다방 가맹점주 중에 2026년 5월 기준으로 폐업처리된 건 몇 개야?" 가 세 군데서
걸렸다.

    1. 휴폐업여부의 동의어가 '휴폐업' 하나뿐이라 "폐업처리된" 이 안 걸렸다.
    2. 1순위가 tmdaa5d01 이었는데 그 테이블엔 휴폐업여부·브랜드명이 아예 없다.
    3. merchant_search_name 의 별칭이 컬럼 이름뿐이라 "빽다방 가맹점주" 의
       브랜드 이름을 못 받아 브랜드명이 프롬프트에서 빠졌다.

'폐업' 은 낟알이 둘이다. 기업고객(사업자)의 폐업여부는 자산건전성 기준으로 '1' 이
폐업이고, 가맹점의 휴폐업여부는 국세청 사업자등록상태 기준으로 '0' 이 정상이다.
부호가 반대다. 가맹점 쪽에 '폐업'·'폐업여부' 를 동의어로 달면 기업고객 컬럼의 이름과
겹쳐서 "기업 국세 이용금액을 폐업 여부별로" 가 tmdaa1d12 대신 tmdaaus01 로 갔다.
그래서 속성이 두 낟알을 함께 들고, 컬럼 동의어는 각자 쪽 말만 갖는다.
"""

from __future__ import annotations

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import SCHEMA

MERCHANT_QUESTION = "빽다방 가맹점주 중에 2026년 5월 기준으로 폐업처리된 건 몇 개야?"
CORPORATE_QUESTION = "2025년 12월 기준 기업 국세 이용금액을 폐업 여부별로 알려줘"


@pytest.mark.parametrize(
    ("question", "table"),
    [(MERCHANT_QUESTION, "tmdaaus01"), (CORPORATE_QUESTION, "tmdaa1d12")],
)
def test_closure_questions_route_to_the_grain_that_holds_the_column(
    question: str, table: str
) -> None:
    ranked = workflow._rule_rank_tables(question)

    assert ranked and ranked[0] == table, (question, ranked)


@pytest.mark.parametrize(
    "column", ["휴폐업여부", "브랜드명", "가맹점명", "대표고객식별자"]
)
def test_the_merchant_question_sees_the_columns_it_needs(column: str) -> None:
    ranked = workflow._rule_rank_tables(MERCHANT_QUESTION)

    assert f"- {column} [" in workflow._table_details(ranked[:1], MERCHANT_QUESTION)


@pytest.mark.parametrize("column", ["폐업여부", "금월국세이용금액"])
def test_the_corporate_question_keeps_its_own_closure_column(column: str) -> None:
    ranked = workflow._rule_rank_tables(CORPORATE_QUESTION)

    assert f"- {column} [" in workflow._table_details(ranked[:1], CORPORATE_QUESTION)


def _closure_attribute() -> dict:
    return next(
        item
        for item in SCHEMA.get("semantic_attributes", [])
        if item.get("name") == "merchant_closure"
    )


@pytest.mark.parametrize(
    ("role", "table", "column"),
    [
        ("current_merchant_closure", "tbdaaus01", "휴폐업여부"),
        ("monthly_merchant_closure", "tmdaaus01", "휴폐업여부"),
        ("current_corporate_closure", "tbdaa1d12", "폐업여부"),
        ("monthly_corporate_closure", "tmdaa1d12", "폐업여부"),
    ],
)
def test_the_attribute_reads_each_grain_from_its_own_table(
    role: str, table: str, column: str
) -> None:
    mapping = next(
        item for item in _closure_attribute()["source_mappings"] if item["role"] == role
    )

    assert mapping["table"] == table
    assert mapping["columns"] == [column]


def test_the_merchant_column_does_not_claim_the_corporate_column_name() -> None:
    """'폐업여부' 는 기업고객 컬럼의 실제 이름이다. 가맹점 동의어로 쓰면 이름이 겹친다."""
    merchant = next(
        column
        for table in SCHEMA["tables"]
        if table["name"] == "tmdaaus01"
        for column in table["dimensions"]
        if column["name"] == "휴폐업여부"
    )

    assert "폐업여부" not in merchant.get("synonyms", [])
    assert "폐업" not in merchant.get("synonyms", [])


def test_the_search_name_does_not_claim_the_bare_brand_word() -> None:
    """'브랜드' 한 낱말은 카드브랜드구분코드 질문에도 걸린다."""
    attribute = next(
        item
        for item in SCHEMA.get("semantic_attributes", [])
        if item.get("name") == "merchant_search_name"
    )

    assert "브랜드" not in attribute["aliases"]
    assert "가맹점주" in attribute["aliases"]

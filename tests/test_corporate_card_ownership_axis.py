"""소유 형태(개별·공용)를 물으면 그 컬럼을 프롬프트에 남긴다.

기업 카드 수는 tmdaa1d12 가 네 컬럼으로 들고 있다. 카드 종류 축이 유효기업신용
카드수·유효기업체크카드수, 소유 형태 축이 유효기업개별카드수·유효기업공용카드수다.
그런데 프롬프트 컬럼을 고르는 구문 매칭은 질문의 표면형과 선언 이름의 띄어쓰기가
어긋나 걸리지 않는다.

    질문: "26년 5월 유효한 기업공용카드 수 알려줘"
    컬럼: 유효기업공용카드수   → 매칭 실패 → 16칸 예산 밖으로 밀림

신용·체크 두 컬럼은 무실적 질의 계약의 근거 문장이 이름을 적어 둬서(+45) 늘 살아
남는다. 그래서 공용카드를 물어도 모델이 볼 수 있는 카드 수 컬럼은 신용·체크뿐이었다.
개별카드는 컬럼 설명이 우연히 "해당 유효한 기업개별 카드 수" 라고 '유효한' 을 그대로
써서 낱말 겹침 몇 점으로 겨우 들어왔다 — 질문이 "유효" 를 빼면 같이 떨어졌다.

고친 자리는 corporate_card_holding 속성의 값 라벨이다. 코드 라벨이 곧 컬럼 근거이므로
(_matched_attribute_columns 가 +70) 네 번째 갈래를 만들지 않았다.
"""

from __future__ import annotations

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import SCHEMA

# (질문, 1순위 테이블, 프롬프트에 있어야 하는 컬럼)
CORPORATE_CARD_COUNT_QUESTIONS = [
    ("26년 5월 유효한 기업개별카드 수 알려줘", "tmdaa1d12", "유효기업개별카드수"),
    ("26년 5월 유효한 기업공용카드 수 알려줘", "tmdaa1d12", "유효기업공용카드수"),
    ("26년 5월 유효한 기업신용카드 수 알려줘", "tmdaa1d12", "유효기업신용카드수"),
    ("26년 5월 유효한 기업체크카드 수 알려줘", "tmdaa1d12", "유효기업체크카드수"),
    # '유효' 를 빼도, 기업을 빼도 같은 컬럼을 물은 것이다.
    ("26년 5월 기업개별카드 수 알려줘", "tmdaa1d12", "유효기업개별카드수"),
    ("26년 5월 유효한 공용카드 수 알려줘", "tmdaa1d12", "유효기업공용카드수"),
    # 현재 보유는 D-1 짝이 답한다.
    ("현재 유효한 기업개별카드 수 알려줘", "tbdaa1d12", "유효기업개별카드수"),
]


@pytest.mark.parametrize(
    ("question", "table", "column"), CORPORATE_CARD_COUNT_QUESTIONS
)
def test_card_count_questions_route_to_the_corporate_snapshot(
    question: str, table: str, column: str
) -> None:
    del column
    ranked = workflow._rule_rank_tables(question)

    assert ranked and ranked[0] == table, (question, ranked)


@pytest.mark.parametrize(
    ("question", "table", "column"), CORPORATE_CARD_COUNT_QUESTIONS
)
def test_the_asked_count_column_reaches_the_prompt(
    question: str, table: str, column: str
) -> None:
    details = workflow._table_details([table], question)

    assert f"- {column} [" in details, question


def _corporate_card_holding() -> dict:
    return next(
        item
        for item in SCHEMA.get("semantic_attributes", [])
        if item.get("name") == "corporate_card_holding"
    )


@pytest.mark.parametrize(
    ("value", "column"),
    [("individual", "유효기업개별카드수"), ("shared", "유효기업공용카드수")],
)
def test_the_ownership_values_read_their_own_column(value: str, column: str) -> None:
    values = _corporate_card_holding()["value_semantics"]

    assert column in values[value]["filter_expression"], value


def test_the_card_level_source_still_splits_by_owner_code() -> None:
    """카드 한 장이 한 행인 원천은 소유 형태를 카드소유자구분코드로 가른다."""
    mapping = next(
        item
        for item in _corporate_card_holding()["source_mappings"]
        if item.get("role") == "card_level_corporate_card_holding"
    )

    assert "카드소유자구분코드" in mapping["columns"]
    assert "유효기업개별카드수" not in mapping["columns"]

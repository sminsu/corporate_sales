"""이번 달을 물었는데 지난 달 월 실적으로 가던 두 자리.

가맹점 신용판매 매출금액은 이번 달(tbdaadt01 일 적재)과 지난 달 이전(tmdaa5e11 월
실적)으로 원천을 갈라 선언해 뒀다(tests/test_merchant_credit_sales_source.py). 그
갈림길을 타지도 못하고 tmdaa5e11 로 직행하던 질문이 두 종류 있었다.

1. 이름 자리에 남은 지표 조각이 가맹점명으로 잡혔다. "오늘 기준 가맹점 신용판매
   매출금액" 에서 기간·지표를 걷어내면 '오늘' 이, "현재 기준 …" 에서는 '기준 가맹점
   신용판매' 가 남는다. 그것이 가맹점명이 되면 가맹점명이 required 인
   named_merchant_monthly_sales 계약이 authoritative 로 잠겨, 점수 계산을 통째로
   건너뛰고 tmdaa5e11 한 곳만 후보로 남는다. merchant_sales_comparison 이 그대로
   실행되면 가맹점명 LIKE '%오늘%' 필터까지 붙는다.

2. 컬럼 이름 안의 '전년' 을 기간으로 읽었다. 전년가맹점신용판매매출금액은 마스터가
   들고 있는 컬럼 이름인데, 속성의 현재/과거 판정이 그 글자를 과거 기간 표현으로
   읽어 월 실적을 골랐다(goldenset v3 의 0103·0104·0105).
"""

from __future__ import annotations

import pytest

from text2sql_agent import workflow


# 이름 없이 "지금 기준" 만 붙은 지표 질문. 각각 다른 표면형에서 같은 자리로 떨어졌다.
OPEN_MONTH_QUESTIONS = [
    "오늘 기준 가맹점 신용판매 매출금액 알려줘",
    "현재 기준 가맹점 신용판매 매출금액 알려줘",
    "최신 기준 가맹점 신용판매 매출금액 알려줘",
    "지금 기준 가맹점 신용판매 매출금액 알려줘",
    "당월 기준 가맹점 신용판매 매출금액 알려줘",
    "오늘 기준으로 신용판매 매출 제일 많은 가맹점 알려줘",
    "이번달 기준 가맹점 신용판매 매출 얼마야",
    "가맹점 신용판매 매출금액 최근 현황 알려줘",
]


@pytest.mark.parametrize("question", OPEN_MONTH_QUESTIONS)
def test_as_of_words_and_metric_fragments_are_not_merchant_names(question: str) -> None:
    assert workflow._extract_merchant_name_by_rule(question) == ""


@pytest.mark.parametrize("question", OPEN_MONTH_QUESTIONS)
def test_open_month_question_reads_the_daily_master(question: str) -> None:
    assert workflow._rule_rank_tables(question)[0] == "tbdaadt01"


@pytest.mark.parametrize("question", OPEN_MONTH_QUESTIONS)
def test_open_month_question_does_not_run_a_named_merchant_query(question: str) -> None:
    """이름을 대지 않은 질문이 이름 필터가 박힌 VQ 로 실행되면 0행이 나온다."""
    assert workflow._match_vq_by_semantic_contract(question) is None


def test_a_real_merchant_name_still_selects_the_named_monthly_query() -> None:
    question = "도미노 피자 최근 6개월 매출액 알려줘"

    assert workflow._extract_merchant_name_by_rule(question) == "도미노 피자"
    matched = workflow._match_vq_by_semantic_contract(question)
    assert matched is not None
    assert matched["matched_query_name"] == "merchant_sales_comparison"
    assert workflow._rule_rank_tables(question) == ["tmdaa5e11"]


@pytest.mark.parametrize(
    "question",
    [
        "가맹점 전년 가맹점 신용판매 매출금액을 가맹점유형별로 알려줘",
        "가맹점 전년 가맹점 신용판매 매출금액을 카드대금지급주기별로 알려줘",
        "전년 가맹점 신용판매 매출금액이 큰 가맹점 알려줘",
        "금년 가맹점 신용판매 매출금액 알려줘",
    ],
)
def test_a_period_word_inside_a_column_name_is_not_a_period(question: str) -> None:
    assert workflow._rule_rank_tables(question)[0] == "tbdaadt01"


@pytest.mark.parametrize(
    "question",
    [
        # 컬럼 이름에 붙지 않은 '전년' 은 그대로 기간이다.
        "전년 대비 가맹점 신용판매 매출금액 알려줘",
        "2026년 5월 가맹점 신용판매 매출금액 알려줘",
    ],
)
def test_a_real_past_period_still_reads_the_monthly_performance_table(
    question: str,
) -> None:
    assert workflow._rule_rank_tables(question)[0] == "tmdaa5e11"


def test_an_unnamed_past_period_question_aggregates_every_merchant() -> None:
    """이름을 대지 않았으면 기간이 과거여도 이름 필터가 붙어서는 안 된다."""
    question = "지난 6개월 가맹점 신용판매 매출 알려줘"

    assert workflow._extract_merchant_name_by_rule(question) == ""
    assert workflow._match_vq_by_semantic_contract(question) is None
    assert workflow._rule_rank_tables(question)[0] == "tmdaa5e11"


@pytest.mark.parametrize("question", OPEN_MONTH_QUESTIONS)
def test_the_monthly_table_is_kept_out_of_the_open_month_candidates(
    question: str,
) -> None:
    """이름 없는 질문에 걸린 계약이 이 배제를 통째로 끄고 있었다."""
    assert "tmdaa5e11" in workflow._attribute_snapshot_exclusions(question)


def test_a_named_merchant_question_keeps_its_contract_table() -> None:
    assert workflow._attribute_snapshot_exclusions("도미노 피자 최근 6개월 매출액 알려줘") == set()

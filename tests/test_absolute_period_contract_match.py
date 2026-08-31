"""연·월을 숫자로 못 박은 브랜드 매출 질의도 가맹점 월실적으로 가야 한다.

    도미노피자 2026년 매출액 알려줘        → 전표(tbdaabt08, tbdaabt30)
    도미노피자 2026년 1월 매출액 알려줘     → 전표
    도미노피자 최근 1년 매출액 알려줘       → tmdaa5e11 (정상)

세 가지가 겹쳐 있었다.

1. 브랜드 월매출 계약(named_merchant_monthly_sales)이 기간 조건으로
   ["최근","지난","월별","개월","년","기준"] 을 요구했다. 계약 match 는 어절
   경계가 아니라 부분 문자열이고 두 글자 미만을 버린다. '년' 은 절대 안 걸리는
   죽은 항목이라 "2026년" 은 그룹 전체를 통과하지 못했다.
2. 가맹점명 추출이 기간을 지운 문장으로 "기간을 말했나" 를 물었다. "2026년 1월"
   은 통째로 지워지므로 늘 기간 없음이 되어 이름을 못 뽑았고, 이름이 required 인
   계약은 authoritative 가 되지 못했다.
3. 계약의 excluded 에 적힌 축 이름은 업종·가맹점·점포·매장 넷뿐이었다. 결과가
   기준년월 한 행인데 지역·카드VAN사·표준산업 대분류로 쪼개는 질문까지 잡아,
   축 테이블(tbdaadb17, tmdaaus01)을 후보에서 떨어뜨렸다.
"""

from __future__ import annotations

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import (
    CONTRACT_MATCH_PREDICATES,
    SCHEMA,
    find_relevant_queries,
    semantic_query_contract_candidates,
)
from text2sql_agent.v2.vq_output_guard import _axis_terms, grouping_axis_terms

CONTRACT = "named_merchant_monthly_sales"
MONTHLY_PERFORMANCE = "tmdaa5e11"
MERCHANT_MASTER = "tbdaadt01"

# 이름 + 기간 + 매출. 절대 기간이든 상대 기간이든 같은 곳으로 가야 한다.
NAMED_SALES_QUESTIONS = [
    "도미노피자 2026년 매출액 알려줘",
    "도미노피자 2026년 1월 매출액 알려줘",
    "스타벅스 2025년 7월 매출금액 보여줘",
    "스타벅스 202601 매출액 알려줘",
    "도미노피자 최근 1년 매출액 알려줘",
    "도미노 피자 최근 6개월 매출액 알려줘",
    "지난 일년 동안 스타벅스 매출금액 추이를 보여줘",
    "도미노피자 월별 매출액 알려줘",
]


@pytest.mark.parametrize("question", NAMED_SALES_QUESTIONS)
def test_named_brand_sales_reads_the_monthly_performance_table(question: str) -> None:
    assert workflow._rule_rank_tables(question) == [MONTHLY_PERFORMANCE], question
    assert [
        item["name"] for item in semantic_query_contract_candidates(SCHEMA, question, max_count=2)
    ] == [CONTRACT], question


@pytest.mark.parametrize("question", NAMED_SALES_QUESTIONS)
def test_the_brand_name_survives_an_explicit_month(question: str) -> None:
    """기간을 지운 문장으로 기간 유무를 물으면 "2026년 1월" 은 늘 기간 없음이 된다."""
    assert workflow._extract_merchant_name_by_rule(question), question


def test_a_period_fragment_is_not_a_brand_name() -> None:
    """앞의 연·월만 지워져 남는 토막("부터 5월까지")이 이름으로 잡히면 안 된다."""
    assert workflow._extract_merchant_name_by_rule(
        "2026년 1월부터 5월까지 가맹점 신용판매 매출금액 추이를 보여줘"
    ) == ""
    assert workflow._extract_merchant_name_by_rule("2026년 3월 가맹점 매출액 알려줘") == ""


def test_a_period_word_inside_a_column_name_still_is_not_a_period() -> None:
    """'전년'·'금년' 은 전년가맹점신용판매매출금액이라는 컬럼 이름의 한 토막이다.

    require_explicit_period 플래그는 그 두 말도 기간으로 보므로 쓰지 않는다.
    """
    for question in (
        "전년 가맹점 신용판매 매출금액이 큰 가맹점 알려줘",
        "금년 가맹점 신용판매 매출금액 알려줘",
    ):
        assert workflow._rule_rank_tables(question)[0] == MERCHANT_MASTER, question
        assert not semantic_query_contract_candidates(SCHEMA, question, max_count=2), question


def test_period_less_sales_question_still_misses_the_contract() -> None:
    question = "도미노피자 매출액 알려줘"

    assert not semantic_query_contract_candidates(SCHEMA, question, max_count=2)
    assert MONTHLY_PERFORMANCE not in workflow._rule_rank_tables(question)


# (질문, 후보에 함께 있어야 하는 축 테이블)
AXIS_SPLIT_QUESTIONS = [
    ("2026년 5월 업종 대분류별 가맹점 일시불 매출건수 상위 15개를 보여줘", "tbdaadb17"),
    ("2026년 4월 표준산업 대분류별 가맹점 일시불 매출건수 상위 20개를 보여줘", "tbdaadb17"),
    ("2026년 1월 금년 가맹점 신용판매 매출금액을 카드VAN사별로 알려줘", "tmdaa5d01"),
    ("2026년 3월 시군구별로 가맹점 매출이 가장 큰 브랜드를 알려줘", "tmdaaus01"),
]


@pytest.mark.parametrize(("question", "axis_table"), AXIS_SPLIT_QUESTIONS)
def test_axis_questions_keep_the_axis_table(question: str, axis_table: str) -> None:
    """결과가 한 행인 계약이 authoritative 로 끼면 축 테이블이 후보에서 사라진다."""
    ranked = workflow._rule_rank_tables(question, max_tables=6)

    assert axis_table in ranked, (question, ranked)
    assert CONTRACT not in [
        item["name"] for item in semantic_query_contract_candidates(SCHEMA, question, max_count=2)
    ], question


def test_grouping_axis_token_ignores_the_time_axis() -> None:
    """"월별·일별" 은 기준년월 한 행인 계약이 이미 낼 수 있는 축이다.

    앞말 규칙이 조사 없는 이름을 축에 붙여 놓으므로('도미노피자 월'), 어구 전체가
    아니라 '별' 바로 앞 낱말로 시간축을 판별한다.
    """
    assert grouping_axis_terms("도미노피자 월별 매출액 알려줘") == []
    assert grouping_axis_terms("스타벅스 일별 매출") == []
    assert grouping_axis_terms("도미노피자 분기별 매출") == []
    assert grouping_axis_terms("2026년 5월 업종 대분류별 매출") == ["업종 대분류"]
    assert grouping_axis_terms("도미노피자 2026년 매출액 광역시도별 증감내역 알려줘") == ["광역시도"]

    # VQ 게이트는 시간축도 축으로 센다. "일별 매출" 을 물었는데 GROUP BY 에 날짜가
    # 없는 참고 SQL 은 그 질문에 못 쓴다. 그 판정은 그대로 둔다.
    assert _axis_terms("도미노피자 월별 매출액 알려줘") == ["도미노피자 월"]
    assert "tbdaabt30" not in find_relevant_queries(
        SCHEMA, "2026년 7월 법인카드 일별 매출금액을 보여줘", domain_name="card_usage"
    )


def test_contract_match_tokens_are_declared_in_the_matcher() -> None:
    """계약이 매처가 모르는 토큰을 적으면 그 그룹은 조용히 늘 실패한다."""
    used = {
        str(value)
        for contract in SCHEMA["semantic_query_contracts"]
        for key in ("required", "optional", "excluded")
        for group in (contract.get("match") or {}).get(key) or []
        for value in (group if isinstance(group, list) else [group])
        if str(value).startswith("@")
    }

    assert used
    assert used <= set(CONTRACT_MATCH_PREDICATES)


def test_no_match_phrase_is_too_short_to_ever_hit() -> None:
    """phrase_hit 은 두 글자 미만을 버린다. required 에 있으면 그룹을 통째로 막는다."""
    dead = [
        (contract["name"], key, value)
        for contract in SCHEMA["semantic_query_contracts"]
        for key in ("required", "optional", "excluded")
        for group in (contract.get("match") or {}).get(key) or []
        for value in (group if isinstance(group, list) else [group])
        if str(value) not in CONTRACT_MATCH_PREDICATES
        and len(str(value).replace(" ", "")) < 2
    ]

    assert dead == []

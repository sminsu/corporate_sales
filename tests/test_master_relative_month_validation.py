"""마스터로 답한 "이번 달"·"최근" 을 검증이 시점 불일치로 반려하던 자리.

tbdaadt01 은 가맹점번호 1건이 grain 인 마스터라 시간 축이 없다. "이번 달 가맹점
신용판매 매출금액" 의 답은 그 마스터의 최신 적재 상태 그 자체인데,
_validate_recent_month_semantics 는 월 팩트를 전제로 "기준년월 = '202608' 조건을
쓰고 전체 MAX 를 쓰지 말라" 고 요구해 그 SQL 을 반려하고 재시도만 돌렸다.

"이번 달" 은 적재 정책이 실적기준년월일 = 최신가용일을 붙여 주는 덕에 우연히
'202608' 이 SQL 에 들어가 통과했지만, "최근"·"최근 현황" 처럼 그 경로를 타지 않는
표현은 그대로 반려됐다.

같이 고친 자리: 서브쿼리에 묶인 별칭까지 바깥 WHERE 에 주입돼
``x."실적기준년월일" = '20260821'`` 처럼 그 스코프에 없는 별칭을 참조하는 SQL 이
나왔다.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from text2sql_agent import workflow

MASTER_SQL = (
    'SELECT SUM(COALESCE(m."금년가맹점신용판매매출금액", 0)) AS "금액"\n'
    "FROM card_system.tbdaadt01 m"
)
MASTER_LATEST_SQL = (
    'SELECT SUM(COALESCE(m."금년가맹점신용판매매출금액", 0)) AS "금액"\n'
    "FROM card_system.tbdaadt01 m\n"
    'WHERE m."실적기준년월일" = (SELECT MAX(x."실적기준년월일") FROM card_system.tbdaadt01 x)'
)
MONTHLY_SQL = (
    'SELECT SUM(a."가맹점일시불매출금액") AS "금액"\n'
    "FROM card_system.tmdaa5e11 a\n"
    'WHERE a."기준년월" = (SELECT MAX(b."기준년월") FROM card_system.tmdaa5e11 b)'
)

RELATIVE_MONTH_QUESTIONS = [
    "최근 가맹점 신용판매 매출금액 알려줘",
    "가맹점 신용판매 매출금액 최근 현황 알려줘",
    "최근 기준 가맹점 상태별 가맹점 수 알려줘",
    "이번 달 가맹점 신용판매 매출금액 알려줘",
]


@pytest.mark.parametrize("question", RELATIVE_MONTH_QUESTIONS)
@pytest.mark.parametrize("sql", [MASTER_SQL, MASTER_LATEST_SQL])
def test_master_answer_is_not_rejected_for_missing_month_condition(
    question: str, sql: str
) -> None:
    assert workflow._validate_recent_month_semantics(question, sql) == []


@pytest.mark.parametrize("question", RELATIVE_MONTH_QUESTIONS)
def test_master_answer_names_its_load_basis(question: str) -> None:
    """검증 LLM 과 사용자 답변이 "무엇을 기준으로 답했는지" 를 받아야 한다.

    질문이 날짜를 찍지 않았으므로 기준도 일자가 아니라 달로 밝힌다.
    """
    note = workflow._implicit_time_basis_note(question, MASTER_SQL)

    assert "tbdaadt01" in note
    assert workflow._TBDAADT01_MONTH_LABEL in note


@pytest.mark.parametrize("question", RELATIVE_MONTH_QUESTIONS)
def test_master_sql_keeps_its_shape(question: str) -> None:
    """월 조건을 얹으면 달이 바뀐 직후 최신 적재분이 잘려 0행이 된다."""
    assert workflow._apply_recent_month_sql_fix(question, MASTER_SQL) == MASTER_SQL


def test_monthly_fact_still_needs_the_month_condition() -> None:
    issues = workflow._validate_recent_month_semantics(
        "최근 가맹점 월매출금액 알려줘", MONTHLY_SQL
    )

    assert issues and workflow._current_ym() in issues[0]


def test_a_past_month_question_still_cannot_be_answered_from_the_master() -> None:
    issues = workflow._validate_recent_month_semantics(
        "저번달 가맹점 신용판매 매출금액 알려줘", MASTER_LATEST_SQL
    )

    assert issues and workflow._previous_ym() in issues[0]


@pytest.mark.parametrize(
    "question",
    ["오늘 기준 가맹점 신용판매 매출금액 알려줘", "2026년 5월 가맹점 신용판매 매출금액 알려줘"],
)
def test_a_subquery_alias_is_not_filtered_in_the_outer_scope(question: str) -> None:
    routed = workflow._apply_accumulation_historical_sources(question, MASTER_LATEST_SQL)

    assert 'x."실적기준년월일" =' not in routed
    assert routed.count("실적기준년월일\" = '") <= 1


def test_two_master_instances_in_one_scope_both_get_the_period() -> None:
    """같은 스코프에 두 번 묶인 마스터는 각 별칭에 조건이 필요하다."""
    sql = (
        'SELECT a."가맹점번호"\n'
        "FROM card_system.tbdaadt01 a\n"
        'JOIN card_system.tbdaadt01 b ON a."가맹점번호" = b."가맹점번호"'
    )

    routed = workflow._apply_accumulation_historical_sources(
        "오늘 기준 가맹점 신용판매 매출금액 알려줘", sql
    )

    assert 'a."실적기준년월일"' in routed
    assert 'b."실적기준년월일"' in routed


@pytest.mark.parametrize("question", RELATIVE_MONTH_QUESTIONS)
def test_validate_sql_accepts_the_master_answer(question: str) -> None:
    state = {
        "question": question,
        "generated_sql": MASTER_LATEST_SQL,
        "selected_tables": ["tbdaadt01"],
        "retry_count": 0,
    }

    with patch.object(workflow, "_call_llm", return_value="VALID"):
        result = workflow.validate_sql(state)

    assert result["is_valid"] is True

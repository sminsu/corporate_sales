from __future__ import annotations

from unittest.mock import patch

from text2sql_agent import workflow
from text2sql_agent.tools.sql_builders import (
    VQ_PARAM_SPECS,
    _apply_params_to_vq,
    _vq_sql_월별이용금액,
)
from text2sql_agent.tools.verified_queries import load_external_verified_queries


QUESTION = "2025년 월별 법인카드 이용금액 합계와 전월 대비 증감률을 보여줘"


def test_explicit_year_is_not_replaced_by_previous_month_comparison() -> None:
    assert workflow._extract_period_by_rule(QUESTION) == ("202501", "202512", "")

    params = workflow._extract_params_by_rule(
        QUESTION,
        VQ_PARAM_SPECS["monthly_corporate_card_usage"],
    )

    assert params == {"기간_시작": "202501", "기간_종료": "202512"}


def test_explicit_month_growth_skips_current_previous_month_validation() -> None:
    question = "2026년 3월 매출액이 전월 대비 가장 많이 증가한 업종 상위 10개를 보여줘"
    sql = "WHERE 기준년월 IN ('202602', '202603')"

    assert workflow._relative_month_target(question) == ("", "")
    assert workflow._validate_recent_month_semantics(question, sql) == []


def test_quantified_recent_period_skips_single_current_month_validation() -> None:
    questions_and_sql = [
        (
            "최근 6개월 이내 문닫은 교촌 치킨 가맹점 개수",
            "WHERE 기준년월일 BETWEEN DATE_ADD('month', -6, CURRENT_DATE) AND CURRENT_DATE",
        ),
        (
            "노랑통닭 점포별의 2026년 6월 기준 최근 3개월 가맹점 매출액 알려줘",
            "WHERE 기준년월 = '202606'",
        ),
        (
            "최근 반년 내 기업카드 이용하였는데 2026년 4월 현재 유효 카드가 없는 법인",
            "WHERE 기준년월 = '202604'",
        ),
    ]

    for question, sql in questions_and_sql:
        assert workflow._relative_month_target(question) == ("", "")
        assert workflow._validate_recent_month_semantics(question, sql) == []

    assert workflow._relative_month_target("최근 기준 가맹점 현황") == (
        workflow._current_ym(),
        "최근/이번달/최근 기준",
    )


def test_standalone_previous_month_still_resolves_as_relative_period() -> None:
    previous = workflow._previous_ym()

    assert workflow._extract_period_by_rule("전월 법인카드 이용금액을 보여줘") == (
        previous,
        previous,
        "",
    )


def test_monthly_usage_builder_fetches_prior_month_and_calculates_growth() -> None:
    sql = _vq_sql_월별이용금액(
        {"기간_시작": "202501", "기간_종료": "202512"}
    )

    assert "기준년월 BETWEEN '202412' AND '202512'" in sql
    assert "WHERE 기준년월 BETWEEN '202501' AND '202512'" in sql
    assert "LAG(총이용금액) OVER (ORDER BY 기준년월) AS 전월이용금액" in sql
    assert "END AS 전월대비증감률_퍼센트" in sql


def test_builder_template_keeps_prior_month_calculation_after_param_application() -> None:
    query = next(
        item
        for item in load_external_verified_queries()
        if item["name"] == "monthly_corporate_card_usage"
    )

    sql = _apply_params_to_vq(
        query["sql"],
        {"기간_시작": "202501", "기간_종료": "202512"},
        query["name"],
        query["parameters"],
    )

    assert "CONCAT('202501', '01')" in sql
    assert "WHERE 기준년월 BETWEEN '202501' AND '202512'" in sql
    assert "{기간_시작}" not in sql
    assert "{기간_종료}" not in sql


def test_rule_period_overrides_incorrect_llm_previous_month_params() -> None:
    query = next(
        item
        for item in load_external_verified_queries()
        if item["name"] == "monthly_corporate_card_usage"
    )
    state = {
        "question": QUESTION,
        "matched_query_name": query["name"],
        "matched_query_sql": query["sql"],
        "matched_query_params": query["parameters"],
        "user_provided_params": {},
    }

    with patch.object(
        workflow,
        "_call_llm",
        return_value='{"기간_시작":"202606","기간_종료":"202606"}',
    ):
        result = workflow.extract_and_apply_params(state)

    assert result["extracted_params"] == {
        "기간_시작": "202501",
        "기간_종료": "202512",
    }
    assert "BETWEEN '202501' AND '202512'" in result["final_sql"]
    assert "BETWEEN '202606' AND '202606'" not in result["final_sql"]

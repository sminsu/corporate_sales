from __future__ import annotations

from unittest.mock import patch

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import _validate_sql_against_schema


@pytest.mark.parametrize(
    ("question", "expected_name"),
    [
        ("도미노 피자 최근 6개월 매출액 알려줘", "도미노 피자"),
        ("최근 6개월 도미노 피자 매출액 알려줘", "도미노 피자"),
        ("도미노피자 매출액 최근 6개월 알려줘", "도미노피자"),
        ("도미노피자 최근 1년 매출액 알려줘", "도미노피자"),
        ("최근 일년 동안 도미노 피자 매출액 알려줘", "도미노 피자"),
    ],
)
def test_named_merchant_sales_is_detected_without_an_explicit_merchant_word(
    question: str,
    expected_name: str,
) -> None:
    assert workflow._extract_merchant_name_by_rule(question) == expected_name
    assert workflow._is_named_merchant_sales_question(question) is True
    assert workflow._reference_domain_by_rule(question) == "merchant_sales"


@pytest.mark.parametrize(
    "question",
    [
        "최근 6개월 가맹점 매출액 알려줘",
        "법인카드 최근 6개월 매출액 알려줘",
        "최근 6개월 업종별 매출액 알려줘",
    ],
)
def test_generic_sales_subject_is_not_mistaken_for_a_merchant_name(question: str) -> None:
    assert workflow._extract_merchant_name_by_rule(question) == ""
    assert workflow._is_named_merchant_sales_question(question) is False


def test_merchant_number_monthly_sales_uses_an_exact_identifier_filter() -> None:
    question = "가맹점번호 595931113의 월별 매출금액을 알려줘"
    capability = workflow._select_verified_query_capability(question, {})

    assert workflow._extract_merchant_number_by_rule(question) == "595931113"
    assert workflow._extract_merchant_name_by_rule(question) == ""
    assert capability is not None
    assert capability["matched_query_name"] == "merchant_monthly_sales_by_number"

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {"question": question, **capability, "user_provided_params": {}}
        )

    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {"가맹점번호": "595931113"}
    assert 'a."가맹점번호" = \'595931113\'' in result["final_sql"]
    assert "LIKE" not in result["final_sql"]
    assert _validate_sql_against_schema(result["final_sql"], ["tmdaa5e11"]) == []


def test_domino_recent_six_month_sales_uses_verified_query_without_llm() -> None:
    question = "도미노 피자 최근 6개월 매출액 알려줘"
    capability = workflow._select_verified_query_capability(question, {})

    assert capability is not None
    assert capability["matched_query_name"] == "merchant_sales_comparison"

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {
                "question": question,
                **capability,
                "user_provided_params": {},
            }
        )

    end_ym = workflow._current_ym()
    start_ym = workflow._months_back_ym(end_ym, 5)
    sql = result["final_sql"]

    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {
        "가맹점명": "도미노 피자",
        "기간_시작": start_ym,
        "기간_종료": end_ym,
    }
    assert "LIKE '%도미노%피자%'" in sql
    assert sql.count("LIKE '%도미노%피자%'") == 1
    assert f"BETWEEN '{start_ym}' AND '{end_ym}'" in sql
    assert (
        'SUM(COALESCE(a."가맹점일시불매출금액", 0) '
        '+ COALESCE(a."가맹점할부매출금액", 0))'
    ) in sql
    assert 'GROUP BY a."기준년월"' in sql
    assert _validate_sql_against_schema(sql, ["tmdaa5e11", "tbdaadb17"]) == []


def test_domino_recent_one_year_sales_uses_sales_vq_and_rolling_twelve_months() -> None:
    question = "도미노피자 최근 1년 매출액 알려줘"
    capability = workflow._select_verified_query_capability(question, {})

    assert capability is not None
    assert capability["matched_query_name"] == "merchant_sales_comparison"

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {
                "question": question,
                **capability,
                "user_provided_params": {},
            }
        )

    end_ym = workflow._current_ym()
    start_ym = workflow._months_back_ym(end_ym, 11)
    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {
        "가맹점명": "도미노피자",
        "기간_시작": start_ym,
        "기간_종료": end_ym,
    }
    assert f"BETWEEN '{start_ym}' AND '{end_ym}'" in result["final_sql"]
    assert "휴폐업여부" not in result["final_sql"]


def test_sales_intent_rejects_recent_closed_brand_vq_even_if_similarity_selects_it() -> None:
    closed_vq = next(
        item
        for item in workflow.VERIFIED_QUERIES
        if item["name"] == "recent_closed_brand_merchant_count"
    )

    assert workflow._verified_query_matches_intent(
        "도미노피자 최근 1년 매출액 알려줘",
        closed_vq,
    ) is False


@pytest.mark.parametrize(
    "question",
    [
        "26년 7월 9일 하루 기준 기업신용매출건수랑 금액",
        "2026년 7월 기업 매출건수",
        "2026년 6월 가맹점 매출금액 알려줘",
    ],
)
def test_nameless_question_is_not_routed_by_the_named_merchant_contract(
    question: str,
) -> None:
    """가맹점명이 required 인 계약은 이름 없는 질문의 라우팅 근거가 못 된다.

    점수 경로의 +28 보너스에 이 가드가 없어서 "기업신용매출건수" 질문이 가맹점
    월매출(tmdaa5e11)을 1순위로 받았다.
    """
    contract = next(
        item
        for item in workflow.SCHEMA["semantic_query_contracts"]
        if item["name"] == "named_merchant_monthly_sales"
    )

    assert workflow._contract_entity_bindings_available(question, contract) is False
    assert workflow._rule_rank_tables(question)[0] != "tmdaa5e11"


@pytest.mark.parametrize(
    "question",
    [
        "2026년 상반기 월별 가맹점매출금액과 전월 대비 증감률을 보여줘",
        "2026년 6월 가맹점매출금액 알려줘",
    ],
)
def test_nameless_merchant_sales_still_reaches_the_monthly_performance_table(
    question: str,
) -> None:
    """이름 없는 가맹점 매출 질문의 원천은 계약 보너스가 아니라 지표 동의어다."""
    assert "tmdaa5e11" in workflow._rule_rank_tables(question)

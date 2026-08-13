from __future__ import annotations

from unittest.mock import patch

import pytest

from text2sql_agent import workflow
import web_service


def _select_and_render(question: str, user_params: dict | None = None) -> tuple[dict, dict]:
    state = workflow._new_initial_state(question)
    state["user_provided_params"] = user_params or {}
    selected = workflow.select_tool(state)
    state.update(selected)
    with patch.object(workflow, "_call_llm", return_value="{}"):
        rendered = workflow.extract_and_apply_params(state)
    return selected, rendered


def test_current_check_card_average_uses_d1_mtd_and_prior_monthly_history() -> None:
    question = "현재 신용카드 없이 기업 체크카드만 갖고 있고 올해 월평균 5천만원 넘게 쓴 기업"

    selected, rendered = _select_and_render(question)
    sql = rendered["final_sql"]

    assert selected["matched_query_name"] == "corporate_check_card_only_high_monthly_avg"
    assert rendered["param_stage"] == "done"
    assert rendered["extracted_params"]["기준년월"] == workflow._current_ym()
    assert workflow._extract_schema_tables(sql) >= {"tbdaa1d12", "tmdaa1d12"}
    assert "DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'ASIA/SEOUL')" in sql
    assert "DATE_ADD('month', -1" in sql
    assert 'c."금월체크카드이용금액"' in sql
    assert not workflow._availability_policy_issues(question, sql)


def test_historical_check_card_average_stays_monthly_only() -> None:
    question = "2026년 4월 현재 신용카드 없이 기업 체크카드만 갖고 있고 월평균 5천만원 넘게 쓴 기업"

    selected, rendered = _select_and_render(question)

    assert selected["matched_query_name"] == "corporate_check_card_only_high_monthly_avg"
    assert workflow._extract_schema_tables(rendered["final_sql"]) == {"tmdaa1d12"}


def test_current_managed_delinquency_needs_only_scope_and_uses_d1() -> None:
    question = "현재 내가 관리하고 있는 기업회원 중 연체 발생한 기업회원을 알려줘"

    selected, rendered = _select_and_render(
        question, {"관리기업목록": ["1234567890"]}
    )
    sql = rendered["final_sql"]

    assert selected["matched_query_name"] == "managed_company_delinquency"
    assert rendered["param_stage"] == "done"
    assert rendered["extracted_params"]["기준년월"] == workflow._current_ym()
    assert workflow._extract_schema_tables(sql) == {"tbdaa1d12"}
    assert "DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'ASIA/SEOUL')" in sql
    assert workflow._rule_rank_tables(question) == ["tbdaa1d12"]
    assert not workflow._availability_policy_issues(question, sql)


def test_historical_managed_delinquency_stays_monthly() -> None:
    question = "2026년 4월 내가 관리하고 있는 기업회원 중 연체 발생한 기업회원을 알려줘"

    selected, rendered = _select_and_render(
        question, {"관리기업목록": ["1234567890"]}
    )

    assert selected["matched_query_name"] == "managed_company_delinquency"
    assert rendered["extracted_params"]["기준년월"] == "202604"
    assert workflow._extract_schema_tables(rendered["final_sql"]) == {"tmdaa1d12"}
    assert workflow._rule_rank_tables(question) == ["tmdaa1d12"]


def test_current_target_industry_uses_d1_population_and_mtd_with_monthly_history() -> None:
    question = "현재 1~5 질문 대상 기업회원의 업종별 이용금액을 알고 싶어"

    selected, rendered = _select_and_render(
        question,
        {"이용기간_시작": "202601", "이용기간_종료": "202607"},
    )
    sql = rendered["final_sql"]
    used_tables = workflow._extract_schema_tables(sql)

    assert selected["matched_query_name"] == "corporate_target_industry_usage"
    assert rendered["param_stage"] == "done"
    assert rendered["extracted_params"]["기준년월"] == workflow._current_ym()
    assert {"tbdaa1d12", "tmdaa1d12", "tbdaabt30"} <= used_tables
    assert "DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'ASIA/SEOUL')" in sql
    assert "DATE_ADD('month', -1" in sql
    assert "FROM current_customers c" in sql
    assert "JOIN customer_monthly m" in sql
    assert 'c."금월신용카드이용금액"' in sql
    ranked = workflow._rule_rank_tables(question, max_tables=8)
    assert "tbdaa1d12" in ranked and "tmdaa1d12" in ranked
    assert not workflow._availability_policy_issues(question, sql)


def test_historical_target_industry_stays_monthly_for_corporate_snapshot() -> None:
    question = "2026년 4월 현재 1~5 질문 대상 기업회원의 업종별 이용금액을 알고 싶어"
    query = next(
        query
        for query in workflow.VERIFIED_QUERIES
        if query["name"] == "corporate_target_industry_usage"
    )

    sql = workflow._route_verified_query_accumulation(
        question, query["name"], query["sql"]
    )

    assert "tmdaa1d12" in workflow._extract_schema_tables(sql)
    assert "tbdaa1d12" not in workflow._extract_schema_tables(sql)


def test_period_omitted_limit_defaults_to_current_d1() -> None:
    question = "기업별 총한도, 잔여한도와 한도소진율을 알려줘"

    selected, rendered = _select_and_render(question)

    assert selected["matched_query_name"] == "corporate_limit_status_at_month"
    assert rendered["param_stage"] == "done"
    assert rendered["extracted_params"]["기준년월"] == workflow._current_ym()
    assert workflow._extract_schema_tables(rendered["final_sql"]) == {"tbdaa1d12"}


@pytest.mark.parametrize(
    ("tool_name", "question", "params"),
    [
        (
            "corporate_check_card_only_high_monthly_avg",
            "현재 신용카드 없이 체크카드만 쓰는 기업 중 월평균 5천만원 이상",
            {"기준년월": "202608", "월평균금액": 50_000_000, "limit": 10},
        ),
        (
            "corporate_card_active_no_usage_members",
            "현재 기업카드 보유회원 중 최근 6개월 무실적 기업",
            {"기준년월": "202608", "조회개월수": 6, "limit": 10},
        ),
    ],
)
def test_direct_tool_execution_uses_the_same_current_cadence_route(
    tool_name: str, question: str, params: dict
) -> None:
    result = workflow.execute_tool(
        {
            "question": question,
            "selected_tool": tool_name,
            "tool_params": params,
        }
    )

    assert not result.get("query_error")
    assert {"tbdaa1d12", "tmdaa1d12"} <= workflow._extract_schema_tables(
        result["final_sql"]
    )
    assert "DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'ASIA/SEOUL')" in result[
        "final_sql"
    ]


def test_natural_current_continuation_preserves_daily_cadence_hint() -> None:
    question = "신용카드 없이 기업 체크카드만 갖고 있고 월평균 5천만원 넘게 쓴 기업"
    selected, missing = _select_and_render(question)
    continuation = {
        "question": question,
        "matched_query_name": selected["matched_query_name"],
        "matched_query_sql": selected["matched_query_sql"],
        "matched_query_params": selected["matched_query_params"],
        "missing_params": missing["missing_params"],
        "extracted_params": missing["extracted_params"],
    }

    current_params = web_service._coerce_natural_params(
        {"_natural_input": "현재"}, continuation
    )
    state = workflow._new_initial_state(question)
    state.update(
        {
            "matched_query_name": selected["matched_query_name"],
            "matched_query_sql": selected["matched_query_sql"],
            "matched_query_params": selected["matched_query_params"],
            "user_provided_params": current_params,
            "retrieval_query": "현재",
        }
    )
    with patch.object(workflow, "_call_llm", return_value="{}"):
        rendered = workflow.extract_and_apply_params(state)

    assert current_params["_cadence_hint"] == "current"
    assert {"tbdaa1d12", "tmdaa1d12"} <= workflow._extract_schema_tables(
        rendered["final_sql"]
    )


def test_numeric_month_continuation_stays_monthly_even_for_current_month_number() -> None:
    question = "신용카드 없이 기업 체크카드만 갖고 있고 월평균 5천만원 넘게 쓴 기업"
    selected, missing = _select_and_render(question)
    state = workflow._new_initial_state(question)
    state.update(
        {
            "matched_query_name": selected["matched_query_name"],
            "matched_query_sql": selected["matched_query_sql"],
            "matched_query_params": selected["matched_query_params"],
            "user_provided_params": {
                **missing["extracted_params"],
                "기준년월": workflow._current_ym(),
            },
        }
    )
    with patch.object(workflow, "_call_llm", return_value="{}"):
        rendered = workflow.extract_and_apply_params(state)

    assert workflow._extract_schema_tables(rendered["final_sql"]) == {"tmdaa1d12"}

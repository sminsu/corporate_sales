from __future__ import annotations

from unittest.mock import patch

from text2sql_agent import workflow
from text2sql_agent.schema import (
    SCHEMA,
    _validate_sql_against_schema,
    semantic_query_contract_candidates,
)


LIMIT_QUESTION = "2026년 6월 기준 기업별 총한도와 잔여한도, 한도소진율을 알려줘"
NAMED_LIMIT_QUESTION = "쿠팡의 2026년 6월 총한도와 잔여한도를 알려줘"
CURRENT_LIMIT_QUESTION = "현재 기업카드 한도를 알려줘"
NAMED_BARE_LIMIT_QUESTION = "쿠팡의 2026년 6월 한도를 알려줘"
AVERAGE_QUESTION = "2026년 상반기 기업별 월평균 이용금액을 알려줘"
AVERAGE_THRESHOLD_QUESTION = "최근 6개월 월평균 이용금액이 5천만원 이상인 기업 리스트"
NAMED_AVERAGE_QUESTION = "쿠팡의 2026년 상반기 월평균 이용금액을 알려줘"
LIMIT_THRESHOLD_QUESTION = "2026년 6월 총한도 2천만원 이상이고 한도소진율 50% 미만인 기업"
USAGE_DECLINE_QUESTION = (
    "현재기준 유효한 기업카드를 보유한 기업 회원중 2025년 상반기 전체 이용금액 대비 "
    "2026년 상반기 이용 금액이 하락한 기업회원의 상위 100개와 이용금액을 봅아줘"
)


def _build_verified_sql(question: str) -> dict:
    capability = workflow._select_verified_query_capability(question, {})
    assert capability is not None
    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        return workflow.extract_and_apply_params(
            {
                "question": question,
                **capability,
                "user_provided_params": {},
            }
        )


def test_limit_and_monthly_average_questions_route_to_authoritative_vqs() -> None:
    cases = [
        (
            LIMIT_QUESTION,
            "corporate_limit_status_at_month",
            "corporate_limit_status_at_month",
            "credit_risk",
        ),
        (
            NAMED_LIMIT_QUESTION,
            "corporate_limit_status_at_month",
            "corporate_limit_status_at_month",
            "credit_risk",
        ),
        (
            CURRENT_LIMIT_QUESTION,
            "corporate_limit_status_at_month",
            "corporate_limit_status_at_month",
            "credit_risk",
        ),
        (
            NAMED_BARE_LIMIT_QUESTION,
            "named_corporate_limit_status_at_month",
            "corporate_limit_status_at_month",
            "credit_risk",
        ),
        (
            AVERAGE_QUESTION,
            "corporate_monthly_average_usage_by_company",
            "corporate_monthly_average_usage",
            "corporate_sales_targeting",
        ),
        (
            AVERAGE_THRESHOLD_QUESTION,
            "corporate_monthly_average_usage_by_company",
            "corporate_monthly_average_usage",
            "corporate_sales_targeting",
        ),
        (
            NAMED_AVERAGE_QUESTION,
            "named_corporate_monthly_average_usage",
            "corporate_monthly_average_usage",
            "corporate_sales_targeting",
        ),
    ]

    for question, contract_name, vq_name, domain in cases:
        contracts = semantic_query_contract_candidates(SCHEMA, question, max_count=1)
        capability = workflow._select_verified_query_capability(question, {})

        assert workflow._rule_classify_question(question) is True
        assert contracts[0]["name"] == contract_name
        assert workflow._reference_domain_by_rule(question) == domain
        expected_table = "tbdaa1d12" if question == CURRENT_LIMIT_QUESTION else "tmdaa1d12"
        assert workflow._rule_rank_tables(question) == [expected_table]
        assert capability is not None
        assert capability["matched_query_name"] == vq_name


def test_limit_status_sql_uses_latest_snapshot_and_named_company_filter() -> None:
    result = _build_verified_sql(LIMIT_QUESTION)
    named_result = _build_verified_sql(NAMED_LIMIT_QUESTION)
    sql = result["final_sql"]
    named_sql = named_result["final_sql"]

    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {"기준년월": "202606"}
    assert 'a."기준년월" = \'202606\'' in sql
    assert "tmdaa1d12" in sql
    assert 'ROW_NUMBER() OVER (PARTITION BY a."고객식별자"' in sql
    assert 'c."기업총한도금액" - c."기업총잔여한도금액"' in sql
    assert 'AS "한도소진율_퍼센트"' in sql
    assert "LIKE '%__ALL__%'" not in sql

    assert named_result["extracted_params"] == {"기준년월": "202606", "기업명": "쿠팡"}
    assert "CONCAT('%', '쿠팡', '%')" in named_sql
    assert workflow._extract_company_name_by_rule(NAMED_LIMIT_QUESTION) == "쿠팡"
    assert workflow._extract_company_name_by_rule(NAMED_BARE_LIMIT_QUESTION) == "쿠팡"
    assert _validate_sql_against_schema(sql, ["tmdaa1d12"]) == []
    assert _validate_sql_against_schema(named_sql, ["tmdaa1d12"]) == []


def test_monthly_average_sql_uses_calendar_months_and_amount_threshold() -> None:
    result = _build_verified_sql(AVERAGE_QUESTION)
    threshold_result = _build_verified_sql(AVERAGE_THRESHOLD_QUESTION)
    named_result = _build_verified_sql(NAMED_AVERAGE_QUESTION)
    sql = result["final_sql"]
    threshold_sql = threshold_result["final_sql"]

    assert result["extracted_params"] == {"기간_시작": "202601", "기간_종료": "202606"}
    assert "DATE_DIFF('month'" in sql
    assert '+ 1 AS "평균산정월수"' in sql
    assert 'PARTITION BY a."고객식별자", a."기준년월"' in sql
    assert 'a."기준년월" BETWEEN \'202601\' AND \'202606\'' in sql
    assert '/ NULLIF(CAST(MAX(p."평균산정월수") AS DOUBLE), 0.0)' in sql
    assert "LIKE '%__ALL__%'" not in sql

    assert threshold_result["extracted_params"]["월평균금액"] == 50_000_000
    assert 'WHERE a."월평균이용금액" >= 50000000' in threshold_sql
    assert named_result["extracted_params"]["기업명"] == "쿠팡"
    assert "CONCAT('%', '쿠팡', '%')" in named_result["final_sql"]
    assert workflow._extract_company_name_by_rule(NAMED_AVERAGE_QUESTION) == "쿠팡"
    assert "tmdaa1d12" in sql
    assert _validate_sql_against_schema(sql, ["tmdaa1d12"]) == []
    assert _validate_sql_against_schema(threshold_sql, ["tmdaa1d12"]) == []


def test_limit_threshold_paraphrase_keeps_the_existing_semantic_contract() -> None:
    contracts = semantic_query_contract_candidates(SCHEMA, LIMIT_THRESHOLD_QUESTION, max_count=1)

    assert contracts[0]["name"] == "corporate_card_low_limit_utilization"
    assert contracts[0]["execution_mode"] == "semantic_generation"
    assert contracts[0]["calculation"]["limit_utilization"].startswith("1.0 -")


def test_current_valid_members_half_year_usage_decline_preserves_requested_row_limit() -> None:
    contracts = semantic_query_contract_candidates(SCHEMA, USAGE_DECLINE_QUESTION, max_count=1)
    selection = workflow.select_tool({"question": USAGE_DECLINE_QUESTION})
    result = _build_verified_sql(USAGE_DECLINE_QUESTION)
    sql = result["final_sql"]

    assert contracts[0]["name"] == "current_corporate_half_year_usage_decline"
    assert workflow._rule_rank_tables(USAGE_DECLINE_QUESTION) == [
        "tbdaa1d12",
        "tmdaa1d12",
    ]
    assert workflow._rule_match_tool(USAGE_DECLINE_QUESTION) == ""
    assert (
        workflow._rule_match_tool("최근 6개월 무실적 기업회원")
        == "corporate_card_active_no_usage_members"
    )
    assert selection["selected_capability_type"] == "verified_query"
    assert selection["matched_query_name"] == "current_corporate_half_year_usage_decline"
    assert selection["selected_tool"] == ""
    assert result["extracted_params"] == {
        "기준기간_시작": "202501",
        "기준기간_종료": "202506",
        "대상기간_시작": "202601",
        "대상기간_종료": "202606",
        "limit": 100,
    }
    assert "tbdaa1d12" in sql
    assert "tmdaa1d12" in sql
    assert 'a."기준년월"' in sql
    assert 'c."대상기간이용금액" < c."기준기간이용금액"' in sql
    assert 'ORDER BY c."하락금액" DESC' in sql
    assert sql.rstrip().endswith("LIMIT 100")
    assert _validate_sql_against_schema(sql, ["tbdaa1d12", "tmdaa1d12"]) == []

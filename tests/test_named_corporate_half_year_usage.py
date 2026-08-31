from __future__ import annotations

from unittest.mock import patch

from text2sql_agent import workflow
from text2sql_agent.schema import (
    SCHEMA,
    _validate_sql_against_schema,
    semantic_query_contract_candidates,
)


QUESTION = "쿠팡의 작년과 올해 반기별 이용금액 실적을 알려줘 "


def test_named_company_and_previous_to_current_year_period_are_extracted() -> None:
    current_ym = workflow._current_ym()
    previous_year = int(current_ym[:4]) - 1

    assert workflow._extract_company_name_by_rule(QUESTION) == "쿠팡"
    assert workflow._extract_period_by_rule(QUESTION) == (
        f"{previous_year}01",
        current_ym,
        "",
    )


def test_named_corporate_half_year_contract_selects_verified_query() -> None:
    contracts = semantic_query_contract_candidates(SCHEMA, QUESTION, max_count=1)
    capability = workflow._select_verified_query_capability(QUESTION, {})

    assert contracts[0]["name"] == "named_corporate_half_year_usage"
    assert workflow._reference_domain_by_rule(QUESTION) == "corporate_sales_targeting"
    assert capability is not None
    assert capability["matched_query_name"] == "named_corporate_half_year_usage"


def test_named_corporate_half_year_query_is_built_without_llm() -> None:
    capability = workflow._select_verified_query_capability(QUESTION, {})
    assert capability is not None

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {
                "question": QUESTION,
                **capability,
                "user_provided_params": {},
            }
        )

    current_ym = workflow._current_ym()
    previous_year = int(current_ym[:4]) - 1
    sql = result["final_sql"]

    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {
        "기업명": "쿠팡",
        "기간_시작": f"{previous_year}01",
        "기간_종료": current_ym,
    }
    assert "LIKE LOWER(CONCAT('%', '쿠팡', '%'))" in sql
    assert f"BETWEEN '{previous_year}01' AND '{current_ym}'" in sql
    assert "THEN '상반기'" in sql
    assert 'AS "총이용금액"' in sql
    assert "ROW_NUMBER() OVER" in sql
    assert _validate_sql_against_schema(sql, ["tbdaa1d12"]) == []

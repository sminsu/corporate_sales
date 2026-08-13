from __future__ import annotations

from unittest.mock import patch

from text2sql_agent import workflow
from text2sql_agent.schema import _validate_sql_against_schema


def test_brand_merchant_owner_corporate_card_count_intent_is_deterministic() -> None:
    question = "파파존스 가맹점주 중에 KB국민카드 기업카드 소지하고 있는 사람은 몇명이야?"

    assert workflow._extract_merchant_name_by_rule(question) == "파파존스"
    assert workflow._is_brand_merchant_owner_corporate_card_count_question(question) is True
    assert workflow._reference_domain_by_rule(question) == "corporate_sales_targeting"

    capability = workflow._select_verified_query_capability(question, {})
    assert capability is not None
    assert capability["matched_query_name"] == "brand_merchant_owner_corporate_card_count"


def test_brand_merchant_owner_count_sql_uses_previous_day_snapshot_and_distinct_owner() -> None:
    question = "파파존스 가맹점주 중에 KB국민카드 기업카드 소지하고 있는 사람은 몇명이야?"
    capability = workflow._select_verified_query_capability(question, {})
    assert capability is not None

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {
                "question": question,
                **capability,
                "user_provided_params": {},
            }
        )

    sql = result["final_sql"]
    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {"가맹점명": "파파존스"}
    assert 'COUNT(DISTINCT a."대표고객식별자")' in sql
    assert "DATE_ADD('DAY', -1, CURRENT_TIMESTAMP AT TIME ZONE 'ASIA/SEOUL')" in sql.upper()
    assert 'MAX(a."기준년월일")' not in sql
    assert "CONCAT('%', '파파존스', '%')" in sql
    assert 'COALESCE(a."유효기업신용카드수", 0)' in sql
    assert 'COALESCE(a."유효기업체크카드수", 0)' in sql
    assert _validate_sql_against_schema(sql, ["tbdaaus01"]) == []


def test_brand_owner_list_request_is_not_forced_to_count_query() -> None:
    question = "파파존스 가맹점주 중 기업카드 소지자 목록을 알려줘"

    assert workflow._is_brand_merchant_owner_corporate_card_count_question(question) is False

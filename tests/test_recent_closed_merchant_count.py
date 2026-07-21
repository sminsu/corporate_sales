from __future__ import annotations

from unittest.mock import patch

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import _validate_sql_against_schema


@pytest.mark.parametrize(
    ("question", "expected_name"),
    [
        ("최근 일년 이내 폐업한 교촌 치킨 가맹점 수 알려줘", "교촌 치킨"),
        ("최근 1년 이내 폐업한 교촌치킨 가맹점 개수", "교촌치킨"),
        ("최근 12개월 동안 폐업한 KFC 가맹점은 몇 개야", "KFC"),
        ("교촌 치킨 가맹점 중 최근 1년 내 해지된 곳은 몇 개야", "교촌 치킨"),
    ],
)
def test_recent_closed_named_merchant_count_is_detected(
    question: str,
    expected_name: str,
) -> None:
    assert workflow._extract_merchant_name_by_rule(question) == expected_name
    assert workflow._is_recent_closed_named_merchant_count_question(question) is True
    assert workflow._reference_domain_by_rule(question) == "merchant_sales"

    capability = workflow._select_verified_query_capability(question, {})
    assert capability is not None
    assert capability["matched_query_name"] == "recent_closed_brand_merchant_count"


@pytest.mark.parametrize(
    "question",
    [
        "최근 1년 이내 폐업한 가맹점 수 알려줘",
        "교촌 치킨 가맹점 수 알려줘",
        "최근 1년 교촌 치킨 매출액 알려줘",
    ],
)
def test_recent_closed_query_requires_name_period_closure_and_count(question: str) -> None:
    assert workflow._is_recent_closed_named_merchant_count_question(question) is False


def test_recent_closed_merchant_sql_uses_first_closed_observation_without_llm() -> None:
    question = "최근 일년 이내 폐업한 교촌 치킨 가맹점 수 알려줘"
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
    assert result["extracted_params"] == {"가맹점명": "교촌 치킨"}
    assert 'MIN(a."기준년월일") AS "최초폐업관측일"' in sql
    assert 'COALESCE(a."휴폐업여부", \'0\') <> \'0\'' in sql
    assert "DATE_ADD('YEAR', -1, CURRENT_DATE)" in sql.upper()
    assert 'COUNT(DISTINCT f."가맹점번호")' in sql
    assert "CONCAT('%', '교촌 치킨', '%')" in sql
    assert "REGEXP_REPLACE" in sql
    assert _validate_sql_against_schema(sql, ["tbdaaus01"]) == []


def test_recent_closed_merchant_exact_name_request_removes_wildcards() -> None:
    question = "최근 1년 내 폐업한 교촌 치킨 가맹점을 이름만으로 검색해서 수 알려줘"
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

    assert result["extracted_params"] == {
        "가맹점명": "교촌 치킨",
        "이름정확일치": True,
    }
    assert "CONCAT('%', '교촌 치킨', '%')" not in result["final_sql"]
    assert "LIKE LOWER('교촌 치킨')" in result["final_sql"]

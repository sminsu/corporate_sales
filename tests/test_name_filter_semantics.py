from __future__ import annotations

from unittest.mock import patch

import pytest

from text2sql_agent import workflow
from text2sql_agent.tools.bad_debt import _build_bad_debt_sql
from text2sql_agent.tools.sql_builders import _apply_params_to_vq
from text2sql_agent.tools.verified_queries import load_external_verified_queries


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("도미노 가맹점을 검색해줘", False),
        ("도미노 가맹점 이름만 알려줘", False),
        ("도미노 가맹점 이름만으로 검색해줘", True),
        ("가맹점명을 도미노로 고정해줘", True),
        ("가맹점명과 정확히 일치하는 것만 검색해줘", True),
    ],
)
def test_exact_name_intent_requires_explicit_search_wording(question: str, expected: bool) -> None:
    assert workflow._is_exact_name_match_requested(question) is expected


def test_generated_sql_defaults_to_contains_and_exact_request_removes_wildcards() -> None:
    equality_sql = "SELECT * FROM t WHERE 가맹점명 = '굽네 치킨'"
    wildcard_sql = "SELECT * FROM t WHERE 가맹점명 LIKE '%굽네 치킨%'"

    assert workflow._apply_name_filter_mode("굽네 치킨 가맹점 검색", equality_sql) == (
        "SELECT * FROM t WHERE 가맹점명 LIKE '%굽네%치킨%'"
    )
    assert workflow._apply_name_filter_mode("가맹점명을 굽네 치킨으로 고정", wildcard_sql) == (
        "SELECT * FROM t WHERE 가맹점명 LIKE '굽네 치킨'"
    )


def test_verified_query_name_placeholder_uses_contains_by_default_and_exact_on_request() -> None:
    query = {
        item["name"]: item for item in load_external_verified_queries()
    }["merchant_detail_by_name"]

    contains_sql = _apply_params_to_vq(
        query["sql"],
        {"가맹점명": "굽네 치킨"},
        query["name"],
        query["parameters"],
    )
    exact_sql = _apply_params_to_vq(
        query["sql"],
        {"가맹점명": "굽네 치킨", "이름정확일치": True},
        query["name"],
        query["parameters"],
    )

    assert "CONCAT('%', '굽네%치킨', '%')" in contains_sql
    assert "CONCAT('%', '굽네%치킨', '%')" not in exact_sql
    assert "LIKE LOWER('굽네 치킨')" in exact_sql


def test_name_token_wildcards_keep_user_wildcards_escaped() -> None:
    query = {
        item["name"]: item for item in load_external_verified_queries()
    }["merchant_detail_by_name"]

    sql = _apply_params_to_vq(
        query["sql"],
        {"가맹점명": "굽네 50% 치킨_"},
        query["name"],
        query["parameters"],
    )

    assert "CONCAT('%', '굽네%50\\%%치킨\\_', '%')" in sql


def test_llm_cannot_enable_exact_match_without_explicit_user_intent() -> None:
    query = {
        item["name"]: item for item in load_external_verified_queries()
    }["merchant_detail_by_name"]
    state = {
        "question": "도미노 가맹점 기본 정보를 알려줘",
        "matched_query_name": query["name"],
        "matched_query_sql": query["sql"],
        "matched_query_params": query["parameters"],
        "user_provided_params": {},
    }

    with patch.object(
        workflow,
        "_call_llm",
        return_value='{"가맹점명":"도미노","이름정확일치":true}',
    ):
        result = workflow.extract_and_apply_params(state)

    assert "이름정확일치" not in result["extracted_params"]
    assert "CONCAT('%', '도미노', '%')" in result["final_sql"]


def test_bad_debt_tool_respects_exact_name_mode() -> None:
    template = "SELECT * FROM t WHERE LOWER(가맹점명) LIKE LOWER('%{가맹점명}%')"

    contains_sql = _build_bad_debt_sql(template, {"가맹점명": "한빛 카드", "기준년월": "202512"})
    exact_sql = _build_bad_debt_sql(
        template,
        {"가맹점명": "한빛 카드", "기준년월": "202512", "이름정확일치": True},
    )

    assert "LIKE LOWER('%한빛%카드%')" in contains_sql
    assert "LIKE LOWER('한빛 카드')" in exact_sql

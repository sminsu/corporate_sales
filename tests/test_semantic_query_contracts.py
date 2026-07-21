from __future__ import annotations

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import (
    SCHEMA,
    build_metrics_summary,
    find_relevant_semantic_query_contracts,
    semantic_query_contract_candidates,
)


@pytest.mark.parametrize(
    ("question", "contract_name", "domain", "tables"),
    [
        (
            "2024년도 기업카드 월간 사용액과 전월비를 계산해줘",
            "monthly_corporate_card_usage_with_mom",
            "card_usage",
            ["tmdaa3e16"],
        ),
        (
            "최근 6개월간 도미노피자 매출금액 추이를 보여줘",
            "named_merchant_monthly_sales",
            "merchant_sales",
            ["tmdaa5e11"],
        ),
        (
            "파파존스 점주 가운데 법인카드를 가진 대표자 인원은?",
            "brand_owner_corporate_card_count",
            "corporate_sales_targeting",
            ["tbdaaus01"],
        ),
        (
            "지난 12개월 동안 문 닫은 교촌치킨 점포 수는?",
            "recent_closed_brand_merchant_count",
            "merchant_sales",
            ["tbdaaus01"],
        ),
    ],
)
def test_semantic_contracts_route_paraphrases_without_exact_verified_query_text(
    question: str,
    contract_name: str,
    domain: str,
    tables: list[str],
) -> None:
    candidates = semantic_query_contract_candidates(SCHEMA, question, max_count=2)

    assert candidates
    assert candidates[0]["name"] == contract_name
    assert workflow._reference_domain_by_rule(question) == domain
    assert workflow._rule_rank_tables(question) == tables

    context = find_relevant_semantic_query_contracts(SCHEMA, question, domain)
    assert contract_name in context
    assert "result_grain" in context


@pytest.mark.parametrize(
    ("question", "verified_query"),
    [
        (
            "2024년도 기업카드 월간 사용액과 전월비를 계산해줘",
            "monthly_corporate_card_usage",
        ),
        ("지난 일년 동안 스타벅스 매출금액 추이를 보여줘", "merchant_sales_comparison"),
        (
            "파파존스 점주 가운데 법인카드를 가진 대표자 인원은?",
            "brand_merchant_owner_corporate_card_count",
        ),
        (
            "지난 12개월 동안 문 닫은 교촌치킨 점포 수는?",
            "recent_closed_brand_merchant_count",
        ),
    ],
)
def test_semantic_contracts_select_bound_verified_queries(
    question: str,
    verified_query: str,
) -> None:
    capability = workflow._select_verified_query_capability(question, {})

    assert capability is not None
    assert capability["matched_query_name"] == verified_query


def test_supported_contract_verified_query_bindings_are_valid() -> None:
    active_vqs = {item["name"] for item in SCHEMA["verified_queries"]}
    supported_contracts = [
        contract
        for contract in SCHEMA["semantic_query_contracts"]
        if str(contract.get("support_status") or "supported").startswith("supported")
    ]

    assert all(contract.get("verified_query") in active_vqs for contract in supported_contracts)


def test_monthly_corporate_usage_metric_contains_window_and_range_semantics() -> None:
    question = "2025년 월별 법인카드 이용금액과 전월 대비 증감률"
    context = build_metrics_summary(SCHEMA, question, "card_usage", max_count=8)

    assert "법인카드월이용금액" in context
    assert "법인카드월전월대비증감률" in context
    assert "LAG" in context
    assert "요청 시작월 직전월" in context
    assert '개인기업구분코드\" = \'2\'' in context


def test_merchant_daily_entity_exposes_owner_card_and_closure_semantics() -> None:
    entity = next(
        item for item in SCHEMA["semantic_entities"] if item["name"] == "merchant_daily_enrichment"
    )

    assert {
        "기준년월일",
        "가맹점번호",
        "가맹점명",
        "브랜드명",
        "대표고객식별자",
        "휴폐업여부",
    } <= set(entity["canonical_dimensions"])
    assert {"유효기업신용카드수", "유효기업체크카드수"} <= set(entity["canonical_facts"])


def test_account_type_semantic_gap_blocks_hallucinated_sql_generation() -> None:
    question = "파파존스 가맹점주 중 가맹점 계좌를 당좌로 이용하는 사람의 명수와 비율 알려줘"
    candidates = semantic_query_contract_candidates(SCHEMA, question, max_count=1)

    assert candidates
    assert candidates[0]["name"] == "brand_owner_account_type_share"
    assert candidates[0]["support_status"] == "blocked_missing_source_semantics"
    assert candidates[0]["source_tables"] == []

    selection = workflow.select_tool({"question": question, "domain_context": ""})
    assert selection["selected_tool"] == ""
    assert selection["matched_query_name"] == ""
    assert "계좌 종류 컬럼" in selection["answer"]
    assert "현금카드결제기관구분코드" in selection["answer"]
    assert workflow.after_tool_selection(selection) == "__end__"


def test_global_contract_forbids_treating_payment_institution_as_account_type() -> None:
    ambiguity_rules = SCHEMA["sql_generation_contract"]["ambiguity_rules"]

    assert any(
        "현금카드결제기관구분코드" in rule and "계좌 종류" in rule
        for rule in ambiguity_rules
    )

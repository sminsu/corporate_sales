from __future__ import annotations

from unittest.mock import patch

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import (
    SCHEMA,
    build_semantic_attributes_summary,
    build_metrics_summary,
    find_relevant_semantic_query_contracts,
    resolve_semantic_attribute_value,
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


def test_supported_contract_execution_bindings_are_valid() -> None:
    active_vqs = {item["name"] for item in SCHEMA["verified_queries"]}
    supported_contracts = [
        contract
        for contract in SCHEMA["semantic_query_contracts"]
        if str(contract.get("support_status") or "supported").startswith("supported")
    ]

    assert all(
        str(contract.get("execution_mode") or "").lower() == "semantic_generation"
        or contract.get("verified_query") in active_vqs
        for contract in supported_contracts
    )


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


@pytest.mark.parametrize(
    ("question", "expected_code", "expected_tables"),
    [
        ("2026년 대기업 기업카드 이용금액 뽑아줘", "1", ["tmdaa1d01", "tmdaa3e16"]),
        (
            "2026년 현재 기준 대기업 기업카드 이용금액 뽑아줘",
            "1",
            ["tbdaaat01", "tmdaa3e16"],
        ),
        ("작년 대기업 기업카드 이용금액 뽑아줘", "1", ["tmdaa1d01", "tmdaa3e16"]),
        ("작년 중소기업 기업카드 이용금액 뽑아줘", "2", ["tmdaa1d01", "tmdaa3e16"]),
        ("지난해 공공기업 법인카드 사용액 합계", "7", ["tmdaa1d01", "tmdaa3e16"]),
        ("2025년 영세기업 기업카드 이용금액", "6", ["tmdaa1d01", "tmdaa3e16"]),
        ("2025년 외부감사대상 중소기업 법인카드 사용액", "4", ["tmdaa1d01", "tmdaa3e16"]),
    ],
)
def test_enterprise_size_usage_routes_to_safe_semantic_contract(
    question: str,
    expected_code: str,
    expected_tables: list[str],
) -> None:
    candidates = semantic_query_contract_candidates(SCHEMA, question, max_count=1)

    assert candidates
    assert candidates[0]["name"] == "enterprise_size_corporate_card_usage"
    assert candidates[0]["source_tables"] == ["tbdaaat01", "tmdaa1d01", "tmdaa3e16"]
    assert candidates[0]["support_status"] == "generative"
    assert workflow._reference_domain_by_rule(question) == "card_usage"
    assert workflow._rule_rank_tables(question) == expected_tables
    assert resolve_semantic_attribute_value(SCHEMA, "enterprise_size", question) == expected_code

    selection = workflow.select_tool({"question": question, "domain_context": ""})
    assert selection["selected_capability_type"] == "semantic_generation"
    assert selection["selected_capability_name"] == "enterprise_size_corporate_card_usage"


def test_enterprise_size_attribute_uses_verified_business_codebook() -> None:
    attribute = next(
        item for item in SCHEMA["semantic_attributes"] if item["name"] == "enterprise_size"
    )
    expected_values = {
        "0": "기업규모산정미대상",
        "1": "대기업",
        "2": "중소기업",
        "3": "중소기업간주",
        "4": "외부감사대상 중소기업",
        "5": "외부감사비대상 중소기업",
        "6": "영세기업",
        "7": "공공기업",
        "8": "가계",
        "9": "기타",
    }
    context = build_semantic_attributes_summary(
        SCHEMA,
        "작년 대기업 기업카드 이용금액",
        "card_usage",
    )

    assert attribute["codebook_status"] == "verified"
    assert attribute["value_semantics"] == expected_values
    assert all(
        resolve_semantic_attribute_value(SCHEMA, "enterprise_size", label) == code
        for code, label in expected_values.items()
    )
    assert {mapping["table"] for mapping in attribute["source_mappings"]} == {
        "tbdaaat01",
        "tmdaa1d01",
    }
    assert '"1": "대기업"' in context
    assert "현재 고객 마스터가 아니라 동일 기준년월" in context


@pytest.mark.parametrize(
    ("question", "expected_code"),
    [
        ("대기업 고객 목록 보여줘", "1"),
        ("중소기업 회사 명단 알려줘", "2"),
        ("공공기업 고객을 찾아줘", "7"),
    ],
)
def test_current_enterprise_size_list_uses_customer_master(
    question: str,
    expected_code: str,
) -> None:
    candidates = semantic_query_contract_candidates(SCHEMA, question, max_count=1)

    assert candidates[0]["name"] == "current_enterprise_size_customer_list"
    assert workflow._reference_domain_by_rule(question) == "customer_card_portfolio"
    assert workflow._rule_rank_tables(question) == ["tbdaaat01"]
    assert resolve_semantic_attribute_value(SCHEMA, "enterprise_size", question) == expected_code

    selection = workflow.select_tool({"question": question, "domain_context": ""})
    assert selection["selected_capability_type"] == "semantic_generation"
    assert selection["selected_capability_name"] == "current_enterprise_size_customer_list"


def test_explicit_current_snapshot_sql_prompt_includes_master_join_contract() -> None:
    question = "2026년 현재 기준 대기업 기업카드 이용금액 뽑아줘"
    selected_tables = workflow._rule_rank_tables(question)
    state = workflow._new_initial_state(question)
    state.update(
        {
            "selected_domain": "card_usage",
            "selected_tables": selected_tables,
            "table_details": workflow._table_details(selected_tables, question),
        }
    )
    captured: dict[str, str] = {}

    def fake_llm(prompt: str, **_: object) -> str:
        captured["prompt"] = prompt
        return "SELECT 1"

    with patch.object(workflow, "_call_llm", side_effect=fake_llm):
        workflow.generate_sql(state)

    prompt = captured["prompt"]
    assert selected_tables == ["tbdaaat01", "tmdaa3e16"]
    assert "current_customer_size CTE" in prompt
    assert "tbdaaat01.\"고객식별자\" = tmdaa3e16.\"고객식별자\"" in prompt
    assert "대기업은 기업규모구분코드 = '1'" in prompt


def test_year_period_sql_prompt_includes_monthly_snapshot_join_contract() -> None:
    question = "2026년 대기업 기업카드 이용금액 뽑아줘"
    selected_tables = workflow._rule_rank_tables(question)
    state = workflow._new_initial_state(question)
    state.update(
        {
            "selected_domain": "card_usage",
            "selected_tables": selected_tables,
            "table_details": workflow._table_details(selected_tables, question),
        }
    )
    captured: dict[str, str] = {}

    def fake_llm(prompt: str, **_: object) -> str:
        captured["prompt"] = prompt
        return "SELECT 1"

    with patch.object(workflow, "_call_llm", side_effect=fake_llm):
        workflow.generate_sql(state)

    prompt = captured["prompt"]
    assert selected_tables == ["tmdaa1d01", "tmdaa3e16"]
    assert "customer_month_size CTE" in prompt
    assert (
        'tmdaa1d01."기준년월" = tmdaa3e16."기준년월"'
        in prompt
    )
    assert (
        'tmdaa1d01."고객식별자" = tmdaa3e16."고객식별자"'
        in prompt
    )


def test_explicit_current_snapshot_sql_rejects_missing_customer_master() -> None:
    question = "2026년 현재 기준 대기업 기업카드 이용금액 뽑아줘"
    selected_tables = workflow._rule_rank_tables(question)
    state = workflow._new_initial_state(question)
    state.update(
        {
            "selected_domain": "card_usage",
            "selected_tables": selected_tables,
            "generated_sql": (
                'SELECT SUM(COALESCE(f."금월이용합계금액", 0)) AS "이용금액" '
                f'FROM {workflow.DB_SCHEMA_PREFIX}tmdaa3e16 f '
                'WHERE f."기준년월" BETWEEN \'202601\' AND \'202612\' '
                'AND f."개인기업구분코드" = \'2\''
            ),
        }
    )

    result = workflow.validate_sql(state)

    assert result["is_valid"] is False
    assert "필수 테이블 누락: tbdaaat01" in result["validation_result"]

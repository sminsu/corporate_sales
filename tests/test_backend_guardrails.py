from __future__ import annotations

import re
from unittest.mock import patch

import pytest

from text2sql_agent import schema, workflow
from text2sql_agent.tools.sql_builders import _apply_params_to_vq
from text2sql_agent.tools.verified_queries import load_external_verified_queries
import web_service


REFERENCE_VQ_NAMES = {
    "corporate_card_active_no_usage_members",
    "corporate_card_churned_after_usage_members",
    "corporate_check_card_only_high_monthly_avg",
    "merchant_corporate_sales_target_no_corporate_card",
    "corporate_limit_low_utilization_members",
    "corporate_target_industry_usage",
    "managed_company_usage_anomalies",
    "managed_company_delinquency",
    "managed_company_limit_reduction",
    "brand_active_merchant_count",
    "brand_merchants_with_corporate_card",
    "merchant_detail_by_name",
}


REFERENCE_ROUTING_CASES = [
    (
        "KB국민 기업카드 보유회원 중에 현재 시점 기준 6개월 무실적인 기업 회원 명단을 알고 싶어",
        "corporate_sales_targeting",
        {"tbdaa1d12"},
    ),
    (
        "KB국민 기업카드 보유, 이용하였으나 현재 탈회 또는 해지한 기업 회원 명단을 알고 싶어",
        "corporate_sales_targeting",
        {"tbdaa1d12"},
    ),
    (
        "KB국민 기업 체크카드만을 보유, 이용하고 있는 기업회원 중 월 평균 5천만원 이상 이용하는 기업 회원 명단을 알고 싶어",
        "corporate_sales_targeting",
        {"tbdaa1d12"},
    ),
    (
        "당사 가맹점 중 월 매출액이 1억원 이상이고, 법인사업자이며, KB국민 기업카드를 보유하고 있지 않은 기업 회원 명단을 알고 싶어",
        "corporate_sales_targeting",
        {"tmdaa5e11", "tmdaaus01"},
    ),
    (
        "KB국민 기업카드 보유회원 중에 한도가 2천만원 이상이고, 한도 소진율이 50% 미만인 기업 회원 명단을 알고 싶어",
        "corporate_sales_targeting",
        {"tbdaa1d12"},
    ),
    (
        "1~5 질문 대상 기업회원의 업종별 이용금액을 알고 싶어",
        "corporate_sales_targeting",
        {"tbdaabt30", "tbdaa1d12", "tbdaadb17"},
    ),
    (
        "내가 관리하고 있는 기업회원 중 최근 6개월 이용금액 추이를 보여주고, 특이사항 있는 업체를 알려줘",
        "relationship_sales_management",
        {"tbdaa1d12"},
    ),
    (
        "내가 관리하고 있는 기업회원 중 연체 발생한 기업회원을 알려줘",
        "relationship_sales_management",
        {"tbdaa1d12"},
    ),
    (
        "내가 관리하고 있는 기업회원 중 한도 감액이 발생한 기업회원을 알려줘",
        "relationship_sales_management",
        {"tbdaa1d12"},
    ),
    (
        "도미노 브랜드 가맹점 수 몇개야?",
        "merchant_sales",
        {"tbdaadt01"},
    ),
    (
        "꾸석지 가맹점 중에 기업카드 소지하고 있는 사람 기업고객식별자 리스트 알려줘",
        "corporate_sales_targeting",
        {"tbdaaus01"},
    ),
    (
        "마초스테이크하우스 가맹점 기본 정보 알려줘",
        "merchant_sales",
        {"tbdaadt01"},
    ),
]

REFERENCE_VQ_CASES = list(zip(
    [
        "corporate_card_active_no_usage_members",
        "corporate_card_churned_after_usage_members",
        "corporate_check_card_only_high_monthly_avg",
        "merchant_corporate_sales_target_no_corporate_card",
        "corporate_limit_low_utilization_members",
        "corporate_target_industry_usage",
        "managed_company_usage_anomalies",
        "managed_company_delinquency",
        "managed_company_limit_reduction",
        "brand_active_merchant_count",
        "brand_merchants_with_corporate_card",
        "merchant_detail_by_name",
    ],
    [question for question, _, _ in REFERENCE_ROUTING_CASES],
))


def _sample_vq_params(query: dict) -> dict:
    """Build deterministic, type-correct values for every template parameter."""
    params = {}
    for name, info in (query.get("parameters") or {}).items():
        samples = info.get("sample_values") or []
        default = info.get("default")
        if default not in (None, ""):
            params[name] = default
        elif samples:
            params[name] = samples[0]
        elif info.get("type") == "integer":
            params[name] = 10
        elif info.get("type") in {"number", "float", "decimal"}:
            params[name] = 0.5
        elif info.get("type") == "list":
            params[name] = ["7005"]
        elif info.get("type") == "like_string" or name == "가맹점명":
            params[name] = "테스트브랜드"
        elif "년월일" in name or name.endswith("일"):
            params[name] = "20260430"
        elif name == "기간_시작":
            params[name] = "202601"
        elif name == "전월기준년월":
            params[name] = "202603"
        elif "년월" in name or name == "기간_종료":
            params[name] = "202604"
        else:
            params[name] = "테스트"
    return params


def test_required_time_default_is_not_used_silently() -> None:
    definitions = {
        "기준년월": {
            "type": "string",
            "required": True,
            "default": "202604",
        }
    }

    missing = workflow._missing_vq_required_params(definitions, {})

    assert [item["name"] for item in missing] == ["기준년월"]
    assert workflow._missing_vq_required_params(definitions, {"기준년월": "202512"}) == []


def test_rule_params_extract_period_amount_ratio_and_limit() -> None:
    specs = [
        {"name": "기간_시작", "type": "string"},
        {"name": "기간_종료", "type": "string"},
        {"name": "월매출금액", "type": "integer"},
        {"name": "한도소진율", "type": "number"},
        {"name": "limit", "type": "integer"},
    ]

    params = workflow._extract_params_by_rule(
        "2025년 1월부터 2025년 3월까지 월 매출 1억원 이상, 한도소진율 50% 미만 상위 20개",
        specs,
    )

    assert params == {
        "기간_시작": "202501",
        "기간_종료": "202503",
        "월매출금액": 100_000_000,
        "한도소진율": 0.5,
        "limit": 20,
    }


def test_verified_query_numeric_placeholder_rejects_expression() -> None:
    with pytest.raises(ValueError, match="정수"):
        _apply_params_to_vq(
            "SELECT 고객식별자 FROM tbdaaat01 LIMIT {limit}",
            {"limit": "1 OR 1=1"},
            "test_query",
            {"limit": {"type": "integer", "required": True}},
        )


def test_managed_company_queries_use_required_request_scoped_values_cte() -> None:
    managed_names = {
        "managed_company_usage_anomalies",
        "managed_company_delinquency",
        "managed_company_limit_reduction",
    }
    external = {query["name"]: query for query in load_external_verified_queries()}

    for name in managed_names:
        query = external[name]
        sql = query["sql"]
        scope_param = query["parameters"]["관리기업목록"]

        assert 'managed_scope ("사업자등록번호") AS (' in sql, name
        assert "VALUES {관리기업목록}" in sql, name
        assert "list_k120879_tmp" not in sql, name
        assert scope_param["type"] == "business_number_list", name
        assert scope_param["required"] is True, name
        assert "default" not in scope_param, name

    assert all(table["name"] != "managed_scope" for table in schema.SCHEMA["tables"])
    assert "runtime_scope_inputs" not in schema.SCHEMA["semantic_layer_metadata"]


def test_verified_queries_only_reference_semantic_tables() -> None:
    known = {
        str(name).lower()
        for table in schema.SCHEMA["tables"]
        for name in (table.get("name"), str(table.get("physical_table", "")).rsplit(".", 1)[-1])
        if name
    }
    table_pattern = re.compile(
        r'\b(?:FROM|JOIN)\s+(?:(?:[A-Za-z_][A-Za-z0-9_]*|"[^"]+")\.)?"?([A-Za-z_][A-Za-z0-9_]*)"?',
        re.IGNORECASE,
    )
    for query in schema.VERIFIED_QUERIES:
        ctes = schema._extract_cte_names(query["sql"])
        used = {
            match.group(1).lower()
            for match in table_pattern.finditer(query["sql"])
            if match.group(1).lower() not in ctes
        }
        assert used <= known, (query["name"], sorted(used - known))


def test_reference_verified_queries_are_active_and_schema_valid_after_substitution() -> None:
    external = {query["name"]: query for query in load_external_verified_queries()}
    active = {query["name"]: query for query in schema.VERIFIED_QUERIES}
    disabled = {
        item["name"]: item
        for item in schema.SCHEMA.get("disabled_verified_queries", [])
    }

    assert REFERENCE_VQ_NAMES <= external.keys(), sorted(REFERENCE_VQ_NAMES - external.keys())
    assert REFERENCE_VQ_NAMES <= active.keys(), {
        name: disabled.get(name, "external VQ가 runtime schema에 병합되지 않음")
        for name in sorted(REFERENCE_VQ_NAMES - active.keys())
    }
    assert REFERENCE_VQ_NAMES.isdisjoint(disabled)

    for name in sorted(REFERENCE_VQ_NAMES):
        query = active[name]
        sql = _apply_params_to_vq(
            query["sql"],
            _sample_vq_params(query),
            name,
            query.get("parameters") or {},
        )
        assert not re.findall(r"\{[A-Za-z0-9가-힣_]+\}", sql), name
        issues = schema._validate_sql_against_schema(sql, [])
        assert not issues, f"{name}: {issues}\n{sql}"


def test_schema_guard_rejects_unknown_qualified_column() -> None:
    sql = (
        'SELECT customer."문서에없는컬럼" '
        f'FROM {schema.DB_SCHEMA_PREFIX}tbdaa1d12 customer '
        'LIMIT 1'
    )

    issues = schema._validate_sql_against_schema(sql, [])

    assert any('customer."문서에없는컬럼"' in issue for issue in issues), issues


def test_relevance_helpers_do_not_inject_unrelated_fallbacks() -> None:
    isolated_schema = {
        "query_references": [
            {
                "intent": "가맹점 월매출 순위",
                "when_user_says": ["가맹점 매출"],
                "primary_table": "tmdaa5e11",
            }
        ],
        "verified_queries": [
            {
                "name": "merchant_sales",
                "question": "가맹점 매출을 보여줘",
                "description": "가맹점 매출 조회",
                "tags": ["가맹점", "매출"],
                "sql": "SELECT 1",
            }
        ],
    }

    assert schema.find_relevant_references(isolated_schema, "양자역학 블랙홀") == (
        "(질문과 직접 관련된 reference 없음)"
    )
    assert schema.find_relevant_queries(isolated_schema, "양자역학 블랙홀") == (
        "(질문과 직접 관련된 verified query 없음)"
    )


def test_rule_params_extract_current_period_and_explicit_merchant_names() -> None:
    specs = [
        {"name": "기간_시작", "type": "string"},
        {"name": "기간_종료", "type": "string"},
        {"name": "기준년월", "type": "string"},
        {"name": "가맹점명", "type": "like_string"},
    ]
    current_ym = workflow._current_ym()

    brand = workflow._extract_params_by_rule(
        "현재 도미노피자 브랜드 가맹점 중 기업카드 보유 고객을 알려줘",
        specs,
    )
    merchant = workflow._extract_params_by_rule(
        "현재 스타벅스 가맹점 기본정보를 검색해줘",
        specs,
    )

    assert brand == {
        "기간_시작": current_ym,
        "기간_종료": current_ym,
        "기준년월": current_ym,
        "가맹점명": "도미노피자",
    }
    assert merchant["가맹점명"] == "스타벅스"
    assert merchant["기준년월"] == current_ym


@pytest.mark.parametrize(
    ("question", "expected_domain", "required_tables"),
    REFERENCE_ROUTING_CASES,
)
def test_reference_questions_route_to_expected_domain_and_required_tables(
    question: str,
    expected_domain: str,
    required_tables: set[str],
) -> None:
    assert workflow._reference_domain_by_rule(question) == expected_domain
    ranked_tables = set(workflow._rule_rank_tables(question))
    assert required_tables <= ranked_tables, {
        "question": question,
        "required": sorted(required_tables),
        "ranked": sorted(ranked_tables),
    }


@pytest.mark.parametrize(("expected_vq", "question"), REFERENCE_VQ_CASES)
def test_reference_questions_match_verified_query_without_llm(expected_vq: str, question: str) -> None:
    matched = workflow._match_vq_by_rules(question)

    assert matched is not None, question
    assert matched["matched_query_name"] == expected_vq


def test_restricted_tables_and_columns_never_enter_semantic_prompts() -> None:
    restricted_table = "tbdaaat18"
    restricted_question = "고객 연락처 이메일 전화번호 상세주소를 조회해줘"

    assert restricted_table not in workflow._rule_rank_tables(restricted_question)
    assert restricted_table not in workflow._compact_table_catalog([restricted_table])
    assert workflow._table_details([restricted_table], restricted_question, max_columns=500) == ""

    restricted_columns = {"기업회원이메일주소", "계좌번호", "대표카드번호"}
    managed_table = next(table for table in schema.SCHEMA["tables"] if table["name"] == "tsmagcca1")
    assert restricted_columns <= set(managed_table.get("restricted_columns", []))
    managed_details = workflow._table_details(
        ["tsmagcca1"],
        "기업회원이메일주소 계좌번호 대표카드번호를 포함한 기업 전담관리 정보",
        max_columns=500,
    )
    managed_catalog = workflow._compact_table_catalog(["tsmagcca1"])

    assert "tsmagcca1" in managed_details
    for column in restricted_columns:
        assert f"  - {column} [" not in managed_details
        assert column not in managed_catalog


def test_compact_table_details_are_bounded_and_keep_keys() -> None:
    table = max(
        schema.SCHEMA["tables"],
        key=lambda item: sum(len(item.get(section, [])) for section in ("dimensions", "measures", "time_dimensions")),
    )

    details = workflow._table_details([table["name"]], "지난달 기업 이용금액과 한도", max_columns=24)

    assert details.count("\n  - ") <= 24
    for key in table.get("primary_key", []):
        assert str(key) in details
    assert len(details) < 10_000


def test_generate_answer_falls_back_without_llm() -> None:
    state = workflow._new_initial_state("상위 기업을 알려줘")
    state.update(
        {
            "final_sql": "SELECT 기업명, 이용금액 FROM card_system.example",
            "query_columns": ["기업명", "이용금액"],
            "query_rows": [("한빛", 123_000_000), ("새봄", 50_000_000)],
        }
    )

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("timeout")):
        result = workflow.generate_answer(state)

    assert "조회 결과: 2건" in result["answer"]
    assert "한빛" in result["answer"]


def test_public_error_message_does_not_expose_provider_details() -> None:
    class ProviderError(RuntimeError):
        pass

    raw_detail = "POST http://internal-model:8000/v1/chat token=secret failed"

    public_detail = web_service._public_error_detail(ProviderError(raw_detail))

    assert public_detail == "모델 서비스 응답을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요."
    assert "internal-model" not in public_detail
    assert "secret" not in public_detail


def test_public_result_error_does_not_expose_sql_or_db_details() -> None:
    raw_detail = "column secret_col does not exist in SELECT * FROM internal_schema.customer"

    public_detail = web_service._public_result_error(raw_detail)

    assert public_detail == "조회 처리 중 오류가 발생했습니다. 질문 조건을 확인하거나 다시 시도해 주세요."
    assert "internal_schema" not in public_detail
    assert web_service._public_result_error("") == ""

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
        {"tbdaa1d12", "tmdaa1d12"},
    ),
    (
        "KB국민 기업카드 보유, 이용하였으나 현재 탈회 또는 해지한 기업 회원 명단을 알고 싶어",
        "corporate_sales_targeting",
        {"tbdaa1d12", "tmdaa1d12"},
    ),
    (
        "KB국민 기업 체크카드만을 보유, 이용하고 있는 기업회원 중 월 평균 5천만원 이상 이용하는 기업 회원 명단을 알고 싶어",
        "corporate_sales_targeting",
        {"tbdaa1d12", "tmdaa1d12"},
    ),
    (
        "당사 가맹점 중 월 매출액이 1억원 이상이고, 법인사업자이며, KB국민카드를 보유하고 있지 않은 기업 회원 명단을 알고 싶어",
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
        {"tbdaabt30", "tbdaa1d12", "tmdaa1d12", "tbdaadb17"},
    ),
    (
        "내가 관리하고 있는 기업회원 중 최근 6개월 이용금액 추이를 보여주고, 특이사항 있는 업체를 알려줘",
        "relationship_sales_management",
        {"tmdaa1d12"},
    ),
    (
        "내가 관리하고 있는 기업회원 중 연체 발생한 기업회원을 알려줘",
        "relationship_sales_management",
        {"tbdaa1d12"},
    ),
    (
        "내가 관리하고 있는 기업회원 중 한도 감액이 발생한 기업회원을 알려줘",
        "relationship_sales_management",
        {"tmdaa1d12"},
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
        "corporate_target_industry_usage",
        "managed_company_usage_anomalies",
        "managed_company_delinquency",
        "managed_company_limit_reduction",
        "brand_active_merchant_count",
        "brand_merchants_with_corporate_card",
        "merchant_detail_by_name",
    ],
    [question for question, _, _ in REFERENCE_ROUTING_CASES[5:]],
))

SEMANTIC_GENERATION_CASES = [
    (
        "corporate_card_churn_after_usage",
        "최근 반년 내 기업카드 결제는 있었는데 2026년 4월 현재 유효 카드가 없는 법인",
        {"tmdaa1d12"},
    ),
    (
        "corporate_card_low_limit_utilization",
        "2026년 4월 기업신용카드를 가진 고객 중 총한도 2천만원 이상, 사용률 50% 아래인 곳",
        {"tmdaa1d12"},
    ),
]


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


def test_industry_card_usage_inherits_end_year_and_uses_daily_bounds() -> None:
    question = "2026년 1월부터 6월까지 업종별 법인카드 이용금액 합계를 보여줘"

    assert workflow._extract_period_by_rule(question) == ("202601", "202606", "")
    contracts = schema.semantic_query_contract_candidates(schema.SCHEMA, question, max_count=1)
    capability = workflow._select_verified_query_capability(question, {})

    assert contracts[0]["name"] == "corporate_card_usage_by_merchant_industry"
    assert capability is not None
    assert capability["matched_query_name"] == "sales_by_industry"

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {"question": question, **capability, "user_provided_params": {}}
        )

    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {
        "기간_시작": "202601",
        "기간_종료": "202606",
    }
    assert "BETWEEN '20260101' AND '20260630'" in result["final_sql"]
    assert "SUM(a.매출금액)" in result["final_sql"]
    assert not schema._validate_sql_against_schema(
        result["final_sql"], ["tbdaabt30", "tbdaadb17"]
    )


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


def test_merchant_sales_target_tool_renders_the_verified_query() -> None:
    name = "merchant_corporate_sales_target_no_corporate_card"
    workflow._TOOL_SCHEMA_COMPATIBILITY.pop(name, None)
    query = next(item for item in schema.VERIFIED_QUERIES if item["name"] == name)
    params = {"기준년월": "202604", "월매출금액": 100_000_000, "limit": 100}

    sql = workflow.TOOL_MAP[name]["fn"](params)
    expected = _apply_params_to_vq(query["sql"], params, name, query["parameters"])

    assert sql == expected
    assert workflow._tool_schema_compatible(workflow.TOOL_MAP[name])
    assert "tmdaaus01" in sql
    assert "tmdaa5e11" in sql
    assert "tbdaaus01" not in sql
    assert "가맹점총지급금액" not in sql
    assert not schema._validate_sql_against_schema(sql, ["tmdaaus01", "tmdaa5e11"])


def test_schema_guard_rejects_unknown_qualified_column() -> None:
    sql = (
        'SELECT customer."문서에없는컬럼" '
        f'FROM {schema.DB_SCHEMA_PREFIX}tbdaa1d12 customer '
        'LIMIT 1'
    )

    issues = schema._validate_sql_against_schema(sql, [])

    assert any('customer."문서에없는컬럼"' in issue for issue in issues), issues


def test_schema_guard_allows_cte_star_only_after_explicit_projection() -> None:
    safe_sql = f'''WITH base AS (
      SELECT a."고객식별자", a."기업명"
      FROM {schema.DB_SCHEMA_PREFIX}tbdaa1d12 a
    ), ranked AS (
      SELECT * FROM base
    )
    SELECT "고객식별자", "기업명" FROM ranked'''
    physical_star = f'''WITH base AS (
      SELECT * FROM {schema.DB_SCHEMA_PREFIX}tbdaa1d12
    )
    SELECT "기업명" FROM base'''
    result_star = f'''WITH base AS (
      SELECT a."고객식별자", a."기업명"
      FROM {schema.DB_SCHEMA_PREFIX}tbdaa1d12 a
    )
    SELECT * FROM base'''

    assert not schema._validate_sql_against_schema(safe_sql, ["tbdaa1d12"])
    for unsafe_sql in (physical_star, result_star):
        issues = schema._validate_sql_against_schema(unsafe_sql, ["tbdaa1d12"])
        assert "SELECT * 대신 필요한 컬럼만 명시하세요." in issues


def test_churn_query_validation_accepts_recent_window_and_internal_cte_star() -> None:
    question = "최근 반년 내 기업카드 이용하였는데 2026년 4월 현재 유효 카드가 없는 법인"
    sql = f'''WITH base AS (
      SELECT
        a."고객식별자",
        a."사업자등록번호",
        a."기업명",
        COALESCE(a."유효기업신용카드수", 0) + COALESCE(a."유효기업체크카드수", 0) AS "현재카드수",
        ROW_NUMBER() OVER (PARTITION BY a."고객식별자" ORDER BY a."기준년월일" DESC) AS rn
      FROM {schema.DB_SCHEMA_PREFIX}tbdaa1d12 a
      WHERE a."기준년월일" BETWEEN '20251101' AND '20260430'
    ), current_snapshot AS (
      SELECT * FROM base WHERE rn = 1
    )
    SELECT "고객식별자", "사업자등록번호", "기업명"
    FROM current_snapshot
    WHERE "현재카드수" = 0'''

    with patch.object(workflow, "_call_llm", return_value="VALID") as verifier:
        result = workflow.validate_sql(
            {
                "question": question,
                "generated_sql": sql,
                "selected_tables": ["tbdaa1d12"],
                "selected_domain": "corporate_sales_targeting",
                "retry_count": 0,
            }
        )

    assert result["is_valid"] is True
    assert "202608" not in result["final_sql"]
    assert "최근 N개월/N달" in verifier.call_args.args[0]


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


def test_refine_search_query_preserves_source_and_exact_constraints() -> None:
    question = "2026년 7월 도미노피자 매출 1억원 이상을 높은 순으로 좀 찾아줘"
    state = {"question": question, "retrieval_query": ""}
    refined = "2026년 7월 도미노피자 가맹점 매출 1억원 이상 높은 순 조회"

    with patch.object(
        workflow,
        "_call_llm",
        return_value={"retrieval_query": refined},
    ) as call_llm:
        result = workflow.refine_search_query(state)

    assert result == {"retrieval_query": refined}
    assert state["question"] == question
    prompt = call_llm.call_args.args[0]
    for exact_value in ("2026년 7월", "도미노피자", "1억원", "높은 순"):
        assert exact_value in prompt
    assert workflow.route_by_question_type({"question_type": "need_sql"}) == "refine_search_query"
    graph_edges = {
        (edge.source, edge.target)
        for edge in workflow.build_graph().get_graph().edges
    }
    assert ("refine_search_query", "route_domain") in graph_edges


def test_refine_search_query_falls_back_to_source_on_model_failure() -> None:
    question = "최근 6개월 기업카드 무실적 기업을 알려줘"

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.refine_search_query({"question": question, "retrieval_query": ""})

    assert result == {"retrieval_query": question}

    with patch.object(workflow, "_call_llm") as call_llm:
        followup = workflow.refine_search_query(
            {
                "question": "그럼 전월은?",
                "retrieval_query": "",
                "previous_question": question,
            }
        )

    assert followup == {"retrieval_query": "그럼 전월은?"}
    call_llm.assert_not_called()
    assert workflow._normalize_retrieval_query(
        {"retrieval_query": "2026년 7월 매출 조회"},
        "전월 매출 조회",
    ) == "전월 매출 조회"


def test_retrieval_query_drives_capability_search_without_overwriting_source() -> None:
    question = "리포트용인데 카드 안 쓰는 기업 좀 찾아줘"
    refined = "기업카드 미이용 기업회원 명단"
    state = {
        "question": question,
        "retrieval_query": refined,
        "domain_context": "",
    }
    combined = f"{refined}\n{question}"

    with (
        patch.object(workflow, "semantic_query_contract_candidates", return_value=[]) as contracts,
        patch.object(workflow, "_select_verified_query_capability", return_value=None),
        patch.object(workflow, "_rule_match_tool", return_value=""),
        patch.object(workflow, "_tool_candidates", return_value=[]),
    ):
        workflow.select_tool(state)

    contracts.assert_called_once_with(workflow.SCHEMA, combined, max_count=1)
    assert state["question"] == question

    with (
        patch.object(workflow, "semantic_query_contract_candidates", return_value=[]),
        patch.object(workflow, "_match_vq_by_semantic_contract", return_value=None) as vq_match,
        patch.object(workflow, "_match_vq_by_embedding", return_value=None),
        patch.object(workflow, "_match_vq_by_rules", return_value=None),
    ):
        workflow._select_verified_query_capability(question, state)

    vq_match.assert_called_once_with(combined, intent_question=question)


@pytest.mark.parametrize(
    ("question", "expected_domain", "required_tables"),
    REFERENCE_ROUTING_CASES[:5],
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


@pytest.mark.parametrize(("expected_contract", "question", "required_tables"), SEMANTIC_GENERATION_CASES)
def test_reference_paraphrases_use_semantic_generation_without_verified_query(
    expected_contract: str,
    question: str,
    required_tables: set[str],
) -> None:
    contracts = schema.semantic_query_contract_candidates(schema.SCHEMA, question, max_count=1)
    selection = workflow.select_tool({"question": question, "domain_context": ""})

    assert contracts and contracts[0]["name"] == expected_contract
    assert contracts[0]["execution_mode"] == "semantic_generation"
    assert selection["selected_capability_type"] == "semantic_generation"
    assert selection["selected_capability_name"] == expected_contract
    assert selection["matched_query_name"] == ""
    assert workflow._reference_domain_by_rule(question) == "corporate_sales_targeting"
    assert required_tables <= set(workflow._rule_rank_tables(question))


@pytest.mark.parametrize(
    "question",
    [REFERENCE_ROUTING_CASES[index][0] for index in (1, 4)],
)
def test_reference_sql_is_not_runtime_verified_query(question: str) -> None:
    selection = workflow.select_tool({"question": question, "domain_context": ""})

    assert workflow._select_verified_query_capability(question, {}) is None
    assert selection["selected_capability_type"] == "semantic_generation"
    assert selection["matched_query_name"] == ""


def test_merchant_sales_target_uses_verified_query_and_requires_basis_month() -> None:
    question = (
        "당사 가맹점 중 월 매출액이 1억원 이상이고, 법인사업자이며, "
        "KB국민 기업카드를 보유하고 있지 않은 기업 회원 명단을 알고 싶어"
    )
    state = {"question": question, "domain_context": "", "user_provided_params": {}}
    state.update(workflow.select_tool(state))

    assert state["selected_capability_type"] == "verified_query"
    assert state["matched_query_name"] == "merchant_corporate_sales_target_no_corporate_card"

    with patch.object(workflow, "_call_llm", return_value="{}"):
        missing = workflow.extract_and_apply_params(state)

    assert missing["param_stage"] == "need_params"
    assert missing["extracted_params"] == {"월매출금액": 100_000_000}
    assert [item["name"] for item in missing["missing_params"]] == ["기준년월"]

    state["user_provided_params"] = {"기준년월": "202604"}
    with patch.object(workflow, "_call_llm", return_value="{}"):
        result = workflow.extract_and_apply_params(state)

    sql = result["final_sql"]
    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {
        "기준년월": "202604",
        "월매출금액": 100_000_000,
    }
    assert "tmdaaus01" in sql
    assert "tmdaa5e11" in sql
    assert "가맹점일시불매출금액" in sql
    assert "가맹점할부매출금액" in sql
    assert "tbdaaus01" not in sql
    assert "가맹점총지급금액" not in sql
    assert not re.findall(r"\{[A-Za-z0-9가-힣_]+\}", sql)
    assert not schema._validate_sql_against_schema(sql, ["tmdaaus01", "tmdaa5e11"])


def test_corporate_card_no_usage_query_uses_verified_query_with_explicit_basis_month() -> None:
    question = "KB카드 기업카드 보유회원 중에 2026년 7월 기준으로 6개월 무실적인 기업 회원 명단을 알려줘"
    state = {"question": question, "domain_context": "", "user_provided_params": {}}
    state.update(workflow.select_tool(state))

    with patch.object(workflow, "_call_llm", return_value="{}"):
        result = workflow.extract_and_apply_params(state)

    assert state["selected_capability_type"] == "verified_query"
    assert state["matched_query_name"] == "corporate_card_active_no_usage_members"
    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {"기준년월": "202607", "조회개월수": 6}
    assert "tmdaa1d12" in result["final_sql"]
    assert "tbdaa1d12" not in result["final_sql"]
    assert "tmdaa3e16" not in result["final_sql"]
    assert 'a."기준년월" = \'202607\'' in result["final_sql"]
    assert 'b."기준년월" BETWEEN' in result["final_sql"]
    assert "DATE_PARSE(CONCAT('202607', '01'), '%Y%m%d')" in result["final_sql"]
    assert 'a."기준년월일"' not in result["final_sql"]
    assert "ROW_NUMBER" not in result["final_sql"]
    assert not re.findall(r"\{[A-Za-z0-9가-힣_]+\}", result["final_sql"])
    assert not schema._validate_sql_against_schema(result["final_sql"], ["tmdaa1d12"])


def test_current_corporate_card_no_usage_splits_live_population_and_monthly_history() -> None:
    question = (
        "KB국민 기업카드 보유회원 중에 현재 시점 기준 6개월 무실적인 "
        "기업 회원 명단을 알고 싶어"
    )
    state = {"question": question, "domain_context": "", "user_provided_params": {}}
    state.update(workflow.select_tool(state))

    with patch.object(workflow, "_call_llm", return_value="{}"):
        result = workflow.extract_and_apply_params(state)

    sql = result["final_sql"]
    assert state["matched_query_name"] == "corporate_card_active_no_usage_members"
    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {"기준년월": workflow._current_ym(), "조회개월수": 6}
    assert sql.count("card_system.tbdaa1d12") == 1
    assert sql.count("card_system.tmdaa1d12") == 1
    assert "DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'ASIA/SEOUL')" in sql
    assert 'b."기준년월" BETWEEN' in sql
    assert 'COALESCE(a."금월신용카드이용금액", 0)' in sql
    assert not schema._validate_sql_against_schema(sql, ["tbdaa1d12", "tmdaa1d12"])


def test_current_no_usage_table_rank_uses_request_routed_vq_sql() -> None:
    current_question = "현재 기업카드 보유회원 중 6개월 무실적 기업 명단"
    past_question = "2026년 7월 기준 기업카드 보유회원 중 6개월 무실적 기업 명단"

    assert workflow._rule_rank_tables(current_question) == ["tbdaa1d12", "tmdaa1d12"]
    assert workflow._rule_rank_tables(past_question) == ["tmdaa1d12"]


def test_corporate_card_no_usage_query_requires_basis_month_when_omitted() -> None:
    question = "KB카드 기업카드 보유회원 중에 6개월 무실적인 기업 회원 명단을 알려줘"
    state = {"question": question, "domain_context": "", "user_provided_params": {}}
    state.update(workflow.select_tool(state))

    with patch.object(workflow, "_call_llm", return_value="{}"):
        result = workflow.extract_and_apply_params(state)

    assert state["matched_query_name"] == "corporate_card_active_no_usage_members"
    assert result["param_stage"] == "need_params"
    assert result["extracted_params"] == {"조회개월수": 6}
    assert [item["name"] for item in result["missing_params"]] == ["기준년월"]


def test_verified_query_execution_error_preserves_sql_instead_of_generating_a_replacement() -> None:
    question = "KB카드 기업카드 보유회원 중에 2026년 7월 기준으로 6개월 무실적인 기업 회원 명단을 알려줘"
    state = {"question": question, "domain_context": "", "user_provided_params": {}}
    state.update(workflow.select_tool(state))

    with patch.object(workflow, "_call_llm", return_value="{}"):
        state.update(workflow.extract_and_apply_params(state))
    verified_sql = state["final_sql"]

    with patch.object(workflow, "execute_sql", return_value=([], [], "Athena execution failed")):
        state.update(workflow.run_matched_query(state))

    assert state["matched_query_name"] == "corporate_card_active_no_usage_members"
    assert state["final_sql"] == verified_sql
    assert state["retry_count"] == 1
    assert state["query_error"] == "Athena execution failed"
    assert workflow.after_matched_query(state) == "handle_error"


def test_corporate_card_no_usage_fallback_builder_matches_simple_not_exists_vq() -> None:
    sql = workflow.TOOL_MAP["corporate_card_active_no_usage_members"]["fn"](
        {"기준년월": "202604", "조회개월수": 6, "limit": 100}
    )

    assert 'a."기준년월" = \'202604\'' in sql
    assert 'b."고객식별자" = a."고객식별자"' in sql
    assert 'b."기준년월" BETWEEN' in sql
    assert "DATE_PARSE(CONCAT('202604', '01'), '%Y%m%d')" in sql
    assert "ROW_NUMBER" not in sql
    assert "WITH params" not in sql


def test_check_card_only_query_requires_current_check_card_holding() -> None:
    query = next(
        item
        for item in schema.VERIFIED_QUERIES
        if item["name"] == "corporate_check_card_only_high_monthly_avg"
    )
    sql = query["sql"]

    assert "current_snapshot AS" in sql
    assert 'COALESCE(c."유효기업신용카드수", 0) = 0' in sql
    assert 'COALESCE(c."유효기업체크카드수", 0) > 0' in sql
    assert '"현재유효기업체크카드수"' in sql


def test_check_card_only_question_uses_verified_query_and_extracts_threshold() -> None:
    question = (
        "KB국민 기업 체크카드만을 보유, 이용하고 있는 기업회원 중 "
        "월 평균 5천만원 이상 이용하는 기업 회원 명단을 알고 싶어"
    )
    state = {"question": question, "domain_context": "", "user_provided_params": {}}
    state.update(workflow.select_tool(state))

    with patch.object(workflow, "_call_llm", return_value="{}"):
        missing = workflow.extract_and_apply_params(state)

    assert state["selected_capability_type"] == "verified_query"
    assert state["matched_query_name"] == "corporate_check_card_only_high_monthly_avg"
    assert missing["param_stage"] == "need_params"
    assert missing["extracted_params"] == {"월평균금액": 50_000_000}
    assert [item["name"] for item in missing["missing_params"]] == ["기준년월"]

    state["user_provided_params"] = {"기준년월": "202604"}
    with patch.object(workflow, "_call_llm", return_value="{}"):
        result = workflow.extract_and_apply_params(state)

    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {
        "기준년월": "202604",
        "월평균금액": 50_000_000,
    }
    assert "50000000" in result["final_sql"]
    assert "tmdaa1d12" in result["final_sql"]
    assert "tbdaa1d12" not in result["final_sql"]
    assert not re.findall(r"\{[A-Za-z0-9가-힣_]+\}", result["final_sql"])
    assert not schema._validate_sql_against_schema(result["final_sql"], ["tmdaa1d12"])


def test_athena_contract_explicitly_rejects_oracle_sas_wrappers() -> None:
    rules = " ".join(schema.SCHEMA["sql_generation_contract"]["athena_rules"])

    assert "CONNECTION TO ORACLE" in rules
    assert "NVL" in rules and "COALESCE" in rules
    assert "ROW_NUMBER()" in rules


def test_table_metadata_question_uses_semantic_layer_without_llm() -> None:
    question = "tbdaa1d12 테이블의 grain, 기본키와 기준년월일 컬럼을 설명해줘"

    assert workflow.classify_question({"question": question})["question_type"] == "direct"
    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("model unavailable")):
        answer = workflow.direct_answer({"question": question, "selected_domain": ""})["answer"]

    assert "tbdaa1d12" in answer
    assert "grain:" in answer
    assert "primary_key:" in answer
    assert "기준년월일" in answer


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

    assert public_detail == "SQL 처리에 실패했습니다. 아래 실패 원인 분석과 마지막 SQL을 확인해 주세요."
    assert "internal_schema" not in public_detail
    assert web_service._public_result_error("") == ""

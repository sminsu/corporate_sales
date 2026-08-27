from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.run_quality_eval import _actual_from_join_tables, run_contract_evaluations
from text2sql_agent import db, llm, schema, workflow


def test_offline_small_model_quality_contracts() -> None:
    results = run_contract_evaluations()
    failures = [result for result in results if not result.passed]
    details = "\n".join(
        f"- {item.category}/{item.name}: expected={item.expected!r}, actual={item.actual!r}, detail={item.detail}"
        for item in failures
    )
    assert not failures, f"offline quality contracts failed:\n{details}"


def test_llm_provider_exception_is_not_swallowed() -> None:
    class BrokenCompletions:
        @staticmethod
        def create(**_: object) -> object:
            raise RuntimeError("provider unavailable")

    client = SimpleNamespace(chat=SimpleNamespace(completions=BrokenCompletions()))
    with (
        patch.object(llm, "_get_llm_client", return_value=client),
        patch.object(llm, "_MAX_RETRIES", 0),
    ):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            llm._call_llm("테스트")


def test_llm_malformed_response_has_actionable_error() -> None:
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **_: SimpleNamespace(choices=[])),
        )
    )
    with patch.object(llm, "_get_llm_client", return_value=client):
        with pytest.raises(ValueError, match=r"choices\[0\]\.message\.content"):
            llm._call_llm("테스트")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("<reasoning>내부 판단</reasoning>\ndirect", "direct"),
        ("<think>need_sql일 수 있는지 검토</think>\ndirect", "direct"),
        ("<analysis>검증 과정</analysis>\nVALID", "VALID"),
    ],
)
def test_llm_text_normalization_removes_reasoning_wrappers(raw: str, expected: str) -> None:
    assert llm._normalize_llm_text(raw) == expected


def test_json_parser_ignores_think_block_with_example_object() -> None:
    raw = '<think>예시를 검토한다: {"tool": "WRONG"}</think>\n```json\n{"tool": "RIGHT", "params": {}}\n```'

    assert workflow._parse_llm_json(raw) == {"tool": "RIGHT", "params": {}}


def test_athena_query_error_closes_cursor() -> None:
    class FailingCursor:
        def __init__(self) -> None:
            self.closed = False
            self.description = None

        def execute(self, _: str) -> None:
            raise RuntimeError("bad query")

        def close(self) -> None:
            self.closed = True

    class FakeConnection:
        def __init__(self) -> None:
            self.cursor_value = FailingCursor()

        def cursor(self) -> FailingCursor:
            return self.cursor_value

    connection = FakeConnection()

    with patch.object(db, "_get_athena_connection", return_value=connection):
        with pytest.raises(RuntimeError, match="bad query"):
            db._execute_athena("SELECT broken_column FROM known_table")

    assert connection.cursor_value.closed, "cursor must be closed on the exception path"


def test_schema_guard_ignores_dangerous_words_inside_literals_and_identifiers() -> None:
    table = schema.SCHEMA["tables"][0]
    physical_name = str(table["physical_table"]).rsplit(".", 1)[-1]
    qualified_name = f"{schema.DB_SCHEMA_PREFIX}{physical_name}"
    sql = f"SELECT 'DROP is text' AS update_note, created_at FROM {qualified_name}"

    issues = schema._validate_sql_against_schema(sql, [str(table["name"])])

    assert not any("INSERT/UPDATE/DELETE/DDL" in issue for issue in issues), issues


def test_validation_requires_exact_valid_verdict() -> None:
    table = schema.SCHEMA["tables"][0]
    physical_name = str(table["physical_table"]).rsplit(".", 1)[-1]
    qualified_name = f"{schema.DB_SCHEMA_PREFIX}{physical_name}"
    column_name = table["dimensions"][0]["name"]
    state = workflow._new_initial_state("고객을 보여줘")
    state.update(
        {
            "generated_sql": f"SELECT {column_name} FROM {qualified_name} LIMIT 10",
            "selected_tables": [table["name"]],
        }
    )

    with patch.object(workflow, "_call_llm", return_value="VALIDATION FAILED: 질문 조건이 누락됨"):
        result = workflow.validate_sql(state)

    assert result["is_valid"] is False
    assert result["retry_count"] == 1


def test_table_selection_accepts_small_model_bullet_output() -> None:
    names = [str(item["name"]) for item in schema.SCHEMA["tables"][:2]]
    state = workflow._new_initial_state("기업 고객 기본 정보를 보여줘")

    with patch.object(workflow, "_call_llm", return_value=f"- {names[0]}\n- {names[1]}"):
        result = workflow.analyze_question(state)

    # Deterministic rule candidates may be merged with the model selection.
    # The contract is that both explicitly emitted table names survive parsing,
    # not that the model can suppress a higher-confidence safe fallback.
    assert set(names).issubset(result["selected_tables"])
    assert len(result["selected_tables"]) <= 4


def test_generate_sql_uses_small_model_sql_extractor() -> None:
    state = workflow._new_initial_state("고객 수를 보여줘")
    state["table_details"] = "name: tbdaa1d12\nphysical_table: tbdaa1d12"
    raw = (
        "<think>초안:\nSELECT * FROM hallucinated_table</think>\n"
        "SELECT COUNT(*) AS customer_count FROM tbdaa1d12"
    )

    with patch.object(workflow, "_call_llm", return_value=raw):
        result = workflow.generate_sql(state)

    assert result["generated_sql"] == "SELECT COUNT(*) AS customer_count FROM tbdaa1d12"


def test_semantic_layer_has_required_contract_sections() -> None:
    required_top_level = {
        "canonical_domains",
        "semantic_entities",
        "canonical_metrics",
        "semantic_join_graph",
        "llm_semantic_contract",
        "time_resolution_rules",
    }
    assert required_top_level.issubset(schema.SCHEMA)
    contract = schema.SCHEMA["llm_semantic_contract"]
    assert {"purpose", "sql_output_rules", "evidence_priority", "ambiguity_policy"}.issubset(contract)
    assert all(contract[key] for key in ("purpose", "sql_output_rules", "evidence_priority", "ambiguity_policy"))


@pytest.mark.parametrize(
    ("question", "expected_domain"),
    [
        ("지난달 가맹점 업종별 매출 순위", "merchant_sales"),
        ("법인카드의 월별 국내 이용금액과 거래건수", "card_usage"),
        ("기업별 연체율과 한도 사용률", "credit_risk"),
    ],
)
def test_representative_questions_have_deterministic_top_domain(question: str, expected_domain: str) -> None:
    ranked = schema._weighted_domain_scores(
        schema._keyword_rule_domain_scores(schema.SCHEMA, question),
        schema._metric_entity_domain_scores(schema.SCHEMA, question),
        {},
    )
    assert ranked[0]["domain"] == expected_domain
    assert ranked[0]["score"] > ranked[1]["score"]


def test_safe_join_paths_reference_known_entities_tables_and_columns() -> None:
    entities = {item["name"]: item for item in schema.SCHEMA["semantic_entities"]}
    tables = {
        str(item.get("physical_table") or item["name"]).rsplit(".", 1)[-1]: item
        for item in schema.SCHEMA["tables"]
    }
    paths = schema.SCHEMA["semantic_join_graph"]["safe_paths"]
    assert paths

    for path in paths:
        endpoint_tables = set()
        for endpoint in ("from_entity", "to_entity"):
            assert path[endpoint] in entities, path["name"]
            table_name = str(entities[path[endpoint]]["physical_table"]).rsplit(".", 1)[-1]
            assert table_name in tables, path["name"]
            endpoint_tables.add(table_name)

        references = re.findall(
            r'\b([a-z][a-z0-9_]*)\.\s*"([^"]+)"',
            path["sql"],
            flags=re.IGNORECASE,
        )
        assert {table_name for table_name, _ in references}.issubset(endpoint_tables), path["name"]
        for table_name, column_name in references:
            known_columns = {
                column["name"]
                for section in ("dimensions", "measures", "time_dimensions")
                for column in tables[table_name].get(section, [])
            }
            assert column_name in known_columns, f"{path['name']}: {table_name}.{column_name}"


def test_verified_query_table_extractor_excludes_cte_aliases() -> None:
    sql = """WITH base AS (
        SELECT customer_id FROM card_system.known_customer
    ), ranked AS (
        SELECT customer_id FROM base
    )
    SELECT r.customer_id
    FROM ranked r
    JOIN "card_system"."known_sales" s ON r.customer_id = s.customer_id
    """
    assert _actual_from_join_tables(sql) == {"known_customer", "known_sales"}


def test_active_verified_queries_only_reference_semantic_tables() -> None:
    known_tables = {
        str(table.get("physical_table") or table["name"]).rsplit(".", 1)[-1].lower()
        for table in schema.SCHEMA["tables"]
    }
    assert schema.VERIFIED_QUERIES

    failures = {}
    for verified_query in schema.VERIFIED_QUERIES:
        used_tables = _actual_from_join_tables(verified_query["sql"])
        unknown_tables = sorted(used_tables - known_tables)
        if unknown_tables:
            failures[verified_query["name"]] = unknown_tables

    assert failures == {}

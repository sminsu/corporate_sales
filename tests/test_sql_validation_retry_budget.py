"""재시도가 남지 않은 마지막 검증에서는 LLM 의미 검증을 하지 않는다.

세 번째 validate_sql 이 LLM 판정으로 실패하면 사용자에게는 "SQL 오류" 만 남는다.
그 판정을 반영해 SQL 을 고칠 기회가 이미 없기 때문에, 정적으로 안전한 SQL 을
실행조차 못 한 채 버리는 셈이다. 마지막 패스에서는 정적 검증까지만 하고 실행으로
넘긴다.
"""

from __future__ import annotations

from unittest.mock import patch

from text2sql_agent import schema, workflow

QUESTION = "2026년 5월 기준 가맹점 수를 가맹점업종명별로 알려줘"
CLEAN_SQL = (
    'SELECT b."가맹점업종명", COUNT(DISTINCT a."가맹점번호") AS "가맹점수" '
    f'FROM {schema.DB_SCHEMA_PREFIX}tmdaa5d01 a '
    f'JOIN {schema.DB_SCHEMA_PREFIX}tbdaadb17 b '
    'ON a."가맹점업종코드" = b."가맹점업종코드" '
    "WHERE a.\"기준년월\" = '202605' "
    'GROUP BY b."가맹점업종명"'
)


def _validate(sql: str, retry_count: int):
    return workflow.validate_sql(
        {
            "question": QUESTION,
            "generated_sql": sql,
            "selected_tables": ["tmdaa5d01", "tbdaadb17"],
            "selected_domain": "merchant_sales",
            "retry_count": retry_count,
        }
    )


def test_earlier_pass_runs_llm_semantic_check() -> None:
    with patch.object(workflow, "_call_llm", return_value="VALID") as verifier:
        result = _validate(CLEAN_SQL, retry_count=0)

    assert result["is_valid"] is True
    assert verifier.call_count == 1


def test_final_pass_skips_llm_semantic_check() -> None:
    with patch.object(workflow, "_call_llm", return_value="VALID") as verifier:
        result = _validate(CLEAN_SQL, retry_count=workflow.SQL_RETRY_LIMIT - 1)

    assert result["is_valid"] is True
    assert verifier.call_count == 0
    assert "마지막 시도" in result["validation_result"]
    assert result["final_sql"]


def test_final_pass_still_fails_on_static_issues() -> None:
    broken_sql = CLEAN_SQL.replace("tmdaa5d01", "tmd_없는테이블")
    with patch.object(workflow, "_call_llm", return_value="VALID") as verifier:
        result = _validate(broken_sql, retry_count=workflow.SQL_RETRY_LIMIT - 1)

    assert result["is_valid"] is False
    assert result["retry_count"] == workflow.SQL_RETRY_LIMIT
    assert verifier.call_count == 0


def test_retry_limit_is_shared_by_graph_edges() -> None:
    last_pass = {"retry_count": workflow.SQL_RETRY_LIMIT, "is_valid": False}
    one_before = {"retry_count": workflow.SQL_RETRY_LIMIT - 1, "is_valid": False}

    assert workflow.after_validate(last_pass) == "handle_error"
    assert workflow.after_validate(one_before) == "generate_sql"
    assert workflow.after_query({"query_error": "boom", "retry_count": workflow.SQL_RETRY_LIMIT}) == "handle_error"

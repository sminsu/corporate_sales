from __future__ import annotations

from unittest.mock import patch

import web_service
from text2sql_agent import workflow


def _known_table_sql() -> tuple[str, str]:
    table = next(item for item in workflow.SCHEMA["tables"] if item.get("dimensions"))
    column = table["dimensions"][0]["name"]
    physical_table = table.get("physical_table") or table["name"]
    if workflow.DB_SCHEMA_PREFIX and not str(physical_table).startswith(workflow.DB_SCHEMA_PREFIX):
        physical_table = f"{workflow.DB_SCHEMA_PREFIX}{physical_table}"
    return f'SELECT "{column}" FROM {physical_table} LIMIT 1', table["name"]


def test_user_authored_select_uses_direct_sql_validation_path() -> None:
    sql, table_name = _known_table_sql()
    state = workflow._new_initial_state(sql)

    classified = workflow.classify_question(state)
    state.update(classified)
    prepared = workflow.prepare_direct_sql(state)
    state.update(prepared)
    validated = workflow.validate_sql(state)

    assert classified["question_type"] == "direct_sql"
    assert state["generated_sql"] == sql
    assert table_name in state["selected_tables"]
    assert validated["is_valid"] is True
    assert "사용자 입력 SQL 정적 검증 통과" in validated["validation_result"]


def test_direct_sql_execution_failure_preserves_sql_and_stops_rewriting() -> None:
    sql, _ = _known_table_sql()
    state = workflow._new_initial_state(sql)
    state.update(workflow.classify_question(state))
    state.update(workflow.prepare_direct_sql(state))
    state.update(workflow.validate_sql(state))

    with patch.object(
        workflow,
        "execute_sql",
        return_value=([], [], "COLUMN_NOT_FOUND: line 1: Column cannot be resolved"),
    ):
        state.update(workflow.run_query(state))

    assert " ".join(state["final_sql"].split()) == " ".join(sql.split())
    assert workflow.after_query(state) == "handle_error"
    handled = workflow.handle_error(state)
    assert state["final_sql"] in handled["error_message"]
    assert "입력한 SQL을 실행하지 못했습니다." in handled["answer"]


def test_direct_sql_success_uses_deterministic_answer_without_llm() -> None:
    state = workflow._new_initial_state("SELECT 1 AS value")
    state.update(
        {
            "question_type": "direct_sql",
            "final_sql": "SELECT 1 AS value",
            "query_columns": ["value"],
            "query_rows": [(1,)],
        }
    )

    with patch.object(workflow, "_call_llm") as call_llm:
        answer = workflow.generate_answer(state)["answer"]

    call_llm.assert_not_called()
    assert "조회 결과: 1건" in answer
    assert "| 1 |" in answer


def test_failure_payload_exposes_safe_analysis_and_failed_sql() -> None:
    sql, table_name = _known_table_sql()
    result = workflow._new_initial_state(sql)
    result.update(
        {
            "question_type": "direct_sql",
            "selected_domain": "direct_sql",
            "selected_tables": [table_name],
            "generated_sql": sql,
            "final_sql": sql,
            "query_error": "COLUMN_NOT_FOUND: line 1:7: Column cannot be resolved",
            "validation_result": "DB 실행 오류: COLUMN_NOT_FOUND: line 1:7",
            "retry_count": 1,
            "error_message": "SQL 생성/실행에 실패했습니다.",
            "answer": "입력한 SQL을 실행하지 못했습니다.",
        }
    )
    session = {"id": "direct-sql-session", "user_id": "ui", "messages": []}

    with (
        patch.object(web_service._SESSION_STORE, "save_result"),
        patch.object(web_service._SESSION_STORE, "update_session_state"),
        patch.object(web_service, "_suggest_followups", return_value=[]),
    ):
        payload = web_service._result_payload(result, session, message_id=1, top_k=10)

    details = payload["failure_details"]
    assert payload["sql"] == sql
    assert payload["error"] == "SQL 처리에 실패했습니다. 아래 실패 원인 분석과 마지막 SQL을 확인해 주세요."
    assert details["stage"] == "sql_execution"
    assert details["reason"] == "SQL에 작성한 컬럼을 찾을 수 없습니다."
    assert details["selected_tables"] == [table_name]
    assert details["sql"] == sql
    assert "COLUMN_NOT_FOUND" not in details["reason"]
    assert details["validation_summary"] == ""


def test_frontend_renders_failure_analysis_and_sql() -> None:
    source = web_service.STATIC_DIR.joinpath("index.html").read_text(encoding="utf-8")
    styles = web_service.STATIC_DIR.joinpath("styles.css").read_text(encoding="utf-8")

    assert 'id="errorDiagnostics"' in source
    assert 'id="errorFailureReason"' in source
    assert 'id="failedSqlBox"' in source
    assert "renderFailureDiagnostics(settings.diagnostics);" in source
    assert '{ diagnostics: data.failure_details }' in source
    assert "function looksLikeDirectSql(value)" in source
    assert "if (!directSql && state.llmReady === false)" in source
    assert ".error-diagnostic-grid" in styles
    assert ".failed-sql pre" in styles

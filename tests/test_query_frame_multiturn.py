from __future__ import annotations

from unittest.mock import patch

import web_service
from text2sql_agent import workflow
from text2sql_agent.followup_ops import plan_followup
from text2sql_agent.query_frame import build_query_frame, build_result_scope, query_frame_prompt


COLUMNS = ["기준년월", "가맹점명", "매출금액"]
ROWS = [
    ("202501", "도미노 강남점", 100_000_000),
    ("202502", "도미노 서초점", 300_000_000),
]
SQL = """
SELECT "기준년월", "가맹점명", SUM("매출금액") AS "매출금액"
FROM card_system.tmdaa5e11
WHERE LOWER("가맹점명") LIKE LOWER('%도미노피자%')
  AND "기준년월" = '202502'
GROUP BY "기준년월", "가맹점명"
ORDER BY "매출금액" DESC
"""


def _base_result(*, complete: bool = True) -> dict:
    scope = build_result_scope(
        SQL if complete else f"{SQL}\nLIMIT 2",
        fetched_row_count=2,
        displayed_row_count=2,
    )
    return {
        "question": "2025년 2월 도미노피자 가맹점별 매출을 보여줘",
        "selected_domain": "merchant_sales",
        "selected_tables": ["tmdaa5e11"],
        "table_details": "name: tmdaa5e11",
        "final_sql": SQL if complete else f"{SQL}\nLIMIT 2",
        "query_columns": COLUMNS,
        "query_rows": ROWS,
        "result_scope": scope,
        "answer": "도미노피자 매출 결과입니다.",
        "analysis_history": [],
    }


def test_query_frame_captures_inheritable_business_shape() -> None:
    frame = build_query_frame(_base_result())

    assert frame["domain"] == "merchant_sales"
    assert frame["tables"] == ["tmdaa5e11"]
    assert frame["entities"] == [{"column": "가맹점명", "value": "도미노피자"}]
    assert "매출금액" in frame["metrics"]
    assert "기준년월" in frame["dimensions"]
    assert frame["time"]["sql_values"] == ["202502"]
    assert frame["sort"]["direction"] == "desc"
    assert frame["result_scope"]["is_complete"] is True
    prompt = query_frame_prompt(frame)
    assert "가맹점명=도미노피자" in prompt
    assert "직전 결과 전체성: 전체" in prompt


def test_result_scope_marks_display_and_sql_limits_as_incomplete() -> None:
    display_limited = build_result_scope(
        "SELECT value FROM sales",
        fetched_row_count=120,
        displayed_row_count=100,
    )
    sql_limited = build_result_scope(
        "SELECT value FROM sales LIMIT 100",
        fetched_row_count=100,
        displayed_row_count=100,
    )

    assert display_limited["is_complete"] is False
    assert display_limited["display_truncated"] is True
    assert sql_limited["is_complete"] is False
    assert sql_limited["sql_limit_reached"] is True


def test_partial_results_force_global_top_n_and_aggregation_back_to_sql() -> None:
    result = _base_result(complete=False)
    frame = build_query_frame(result)

    top_n = plan_followup(
        "매출금액 상위 1개만 보여줘",
        COLUMNS,
        ROWS,
        query_frame=frame,
        result_scope=frame["result_scope"],
    )
    grouped = plan_followup(
        "월별 매출금액 합계로 묶어줘",
        COLUMNS,
        ROWS,
        query_frame=frame,
        result_scope=frame["result_scope"],
    )
    display_only = plan_followup(
        "매출금액을 억원 단위로 바꿔줘",
        COLUMNS,
        ROWS,
        query_frame=frame,
        result_scope=frame["result_scope"],
    )

    assert top_n["mode"] == "rewrite_sql"
    assert top_n["route_reason"] == "incomplete_result_requires_sql"
    assert grouped["mode"] == "rewrite_sql"
    assert display_only["mode"] == "transform"


def test_new_metric_reroutes_domain_and_tables_but_rewrite_keeps_them() -> None:
    base = _base_result()
    frame = build_query_frame(base)
    base["query_frame"] = frame

    new_metric_plan = plan_followup(
        "그 기업의 연체 현황도 같이 보여줘",
        COLUMNS,
        ROWS,
        query_frame=frame,
        result_scope=frame["result_scope"],
    )
    new_metric_state = web_service._followup_query_state(
        base,
        "그 기업의 연체 현황도 같이 보여줘",
        new_metric_plan,
    )
    rewrite_plan = plan_followup(
        "그럼 전월 기준으로 바꿔줘",
        COLUMNS,
        ROWS,
        query_frame=frame,
        result_scope=frame["result_scope"],
    )
    rewrite_state = web_service._followup_query_state(
        base,
        "그럼 전월 기준으로 바꿔줘",
        rewrite_plan,
    )

    assert new_metric_plan["mode"] == "new_sql"
    assert new_metric_plan["route_reason"] == "new_metric_requires_reroute"
    assert new_metric_plan["new_metrics"] == ["연체"]
    assert new_metric_state["selected_domain"] == ""
    assert new_metric_state["selected_tables"] == []
    assert new_metric_state["table_details"] == ""
    assert new_metric_state["query_frame"]["requires_reroute"] is True
    assert workflow.check_sql_gen_params(new_metric_state)["param_stage"] == "done"
    context = workflow._multiturn_sql_context(new_metric_state)
    assert "구조화된 조회 상태" in context
    assert "가맹점명=도미노피자" in context
    assert "소스 재탐색 필요: 예" in context

    assert rewrite_plan["mode"] == "rewrite_sql"
    assert rewrite_state["selected_domain"] == "merchant_sales"
    assert rewrite_state["selected_tables"] == ["tmdaa5e11"]
    assert rewrite_state["query_frame"]["time"]["expressions"] == ["전월"]


def test_query_execution_records_raw_and_displayed_result_scope() -> None:
    rows = [(index,) for index in range(120)]
    state = workflow._new_initial_state("값을 보여줘")
    state["generated_sql"] = "SELECT value FROM sample"
    state["final_sql"] = "SELECT value FROM sample"

    with patch.object(workflow, "execute_sql", return_value=(["value"], rows, None)):
        result = workflow.run_query(state)

    assert len(result["query_rows"]) == 100
    assert result["result_scope"]["fetched_row_count"] == 120
    assert result["result_scope"]["displayed_row_count"] == 100
    assert result["result_scope"]["is_complete"] is False
    meta = web_service._result_meta({**state, **result})
    assert meta["result_complete"] is False
    assert meta["rows_may_be_limited"] is True

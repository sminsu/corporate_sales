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


def _bad_debt_result() -> dict:
    sql = """
SELECT '202604' AS "기준년월", "구분", "대손비용률_퍼센트", "보정계수"
FROM card_system.tmdaa5d01
WHERE LOWER("가맹점명") LIKE LOWER('%꾸석지%')
"""
    result = {
        "question": "2026년 4월 꾸석지의 대손비용률 분석해줘",
        "selected_domain": "credit_risk",
        "selected_tables": ["tmdaa5d01", "tbmewcm94", "tbmaisd06"],
        "table_details": "name: tmdaa5d01",
        "selected_tool": "대손비용률_분석",
        "selected_capability_type": "tool",
        "selected_capability_name": "대손비용률_분석",
        "tool_params": {
            "가맹점명": "꾸석지",
            "기준년월": "202604",
            "LS": 0.8,
            "IS": 1.2,
        },
        "tool_completed": True,
        "final_sql": sql,
        "query_columns": ["기준년월", "구분", "대손비용률_퍼센트", "보정계수"],
        "query_rows": [("202604", "1", 1.25, 0.5)],
        "result_scope": build_result_scope(sql, fetched_row_count=1, displayed_row_count=1),
        "answer": "꾸석지 대손비용률 결과입니다.",
        "analysis_history": [],
    }
    result["query_frame"] = build_query_frame(result)
    return result


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


def test_popeyes_five_year_count_trend_reroutes_to_new_sql_and_keeps_brand_context() -> None:
    question = "지난 5년간 파파이스 가맹점 수 변동 추이를 알려주세요"
    result = {
        "question": "파파이스 가맹점 수 알려줘",
        "selected_domain": "merchant_sales",
        "selected_tables": ["tbdaadt01"],
        "table_details": "name: tbdaadt01",
        "final_sql": """
            SELECT COUNT(DISTINCT m."가맹점번호") AS "가맹점수"
            FROM card_system.tbdaadt01 m
            WHERE LOWER(m."가맹점명") LIKE LOWER('%파파이스%')
        """,
        "query_columns": ["가맹점수"],
        "query_rows": [(42,)],
        "answer": "파파이스 가맹점은 42개입니다.",
        "analysis_history": [],
    }
    frame = build_query_frame(result)
    resolution = {
        "relation": "refine_query",
        "source_strategy": "rediscover",
        "resolved_question": question,
        "reason": "현재 단일 집계 결과에는 5년 시계열이 없음",
        "used_llm": True,
    }
    plan = plan_followup(
        question,
        result["query_columns"],
        result["query_rows"],
        query_frame=frame,
        result_scope=frame["result_scope"],
        context_resolution=resolution,
    )
    state = web_service._followup_query_state(result, question, plan)

    assert frame["entities"] == [{"column": "가맹점명", "value": "파파이스"}]
    assert plan["mode"] == "new_sql_visualization"
    assert plan["requires_sql"] is True
    assert plan["route_reason"] == "new_time_series_requires_reroute"
    assert plan["context_relation"] == "refine_query"
    assert state["selected_domain"] == ""
    assert state["selected_tables"] == []
    assert state["query_frame"]["time"]["expressions"] == ["지난 5년간"]
    context = workflow._multiturn_sql_context(state)
    assert "가맹점명=파파이스" in context
    assert "지난 5년간" in context
    assert question in context


def test_llm_context_resolution_reconstructs_elliptical_followup_for_sql() -> None:
    result = _base_result()
    result["query_frame"] = build_query_frame(result)
    result["analysis_history"] = [
        {
            "question": "상위 가맹점만 보여줘",
            "answer": "상위 가맹점 결과입니다.",
            "mode": "transform",
        }
    ]
    question = "버거킹 쪽은 어떻게 될까?"
    preliminary = plan_followup(
        question,
        COLUMNS,
        ROWS,
        query_frame=result["query_frame"],
        result_scope=result["result_scope"],
    )
    llm_result = """{
      "relation": "refine_query",
      "source_strategy": "same_source",
      "resolved_question": "2025년 2월 버거킹 가맹점별 매출을 보여줘",
      "reason": "기존 기간과 지표를 유지하고 브랜드만 교체"
    }"""

    with patch.object(web_service.agent, "_call_llm", return_value=llm_result) as call:
        resolution = web_service._resolve_followup_context(result, question, preliminary)

    plan = plan_followup(
        question,
        COLUMNS,
        ROWS,
        query_frame=result["query_frame"],
        result_scope=result["result_scope"],
        context_resolution=resolution,
    )
    state = web_service._followup_query_state(result, question, plan)
    prompt = call.call_args.args[0]

    assert "가맹점명=도미노피자" in prompt
    assert "상위 가맹점만 보여줘" in prompt
    assert resolution["used_llm"] is True
    assert plan["mode"] == "rewrite_sql"
    assert plan["route_reason"] == "context_change_requires_sql"
    assert state["question"] == "2025년 2월 버거킹 가맹점별 매출을 보여줘"
    assert state["followup_question"] == question
    assert state["selected_tables"] == ["tmdaa5e11"]


def test_bad_debt_entity_followup_reuses_tool_and_replaces_only_entity() -> None:
    result = _bad_debt_result()
    question = "그럼 도미노 피자는?"
    resolved_question = "2026년 4월 도미노 피자의 대손비용률 분석해줘"
    resolution = {
        "relation": "refine_query",
        "source_strategy": "same_source",
        "resolved_question": resolved_question,
        "reason": "deterministic_entity_replacement",
        "used_llm": False,
    }
    plan = plan_followup(
        question,
        result["query_columns"],
        result["query_rows"],
        query_frame=result["query_frame"],
        result_scope=result["result_scope"],
        context_resolution=resolution,
    )
    state = web_service._followup_query_state(result, question, plan)

    with patch.object(workflow, "_call_llm", side_effect=AssertionError("inherited tool must not call LLM")):
        selected = workflow.select_tool(state)

    assert state["question"] == resolved_question
    assert plan["requires_sql"] is True
    assert selected["selected_tool"] == "대손비용률_분석"
    assert selected["tool_params"] == {
        "가맹점명": "도미노 피자",
        "기준년월": "202604",
        "LS": 0.8,
        "IS": 1.2,
    }
    assert state["query_frame"]["entities"] == [{"column": "가맹점명", "value": "도미노 피자"}]
    assert workflow.check_tool_params({**state, **selected})["param_stage"] == "done"


def test_entity_only_followup_uses_deterministic_context_without_router() -> None:
    result = _bad_debt_result()
    question = "그럼 도미노 피자는?"
    preliminary = plan_followup(
        question,
        result["query_columns"],
        result["query_rows"],
        query_frame=result["query_frame"],
        result_scope=result["result_scope"],
    )

    with patch.object(web_service.agent, "_call_llm", side_effect=AssertionError("router must not run")):
        resolution = web_service._resolve_followup_context(result, question, preliminary)

    plan = plan_followup(
        question,
        result["query_columns"],
        result["query_rows"],
        query_frame=result["query_frame"],
        result_scope=result["result_scope"],
        context_resolution=resolution,
    )
    compact = resolution["resolved_question"].replace(" ", "")

    assert resolution["used_llm"] is False
    assert resolution["relation"] == "refine_query"
    assert resolution["source_strategy"] == "same_source"
    assert "2026년4월" in compact
    assert "도미노피자" in compact
    assert "대손비용률" in compact
    assert "꾸석지" not in compact
    assert plan["requires_sql"] is True


def test_entity_only_fallback_rejects_operations_and_non_entity_subjects() -> None:
    result = _bad_debt_result()

    for question in (
        "그럼 월별로 보여줘",
        "그럼 법인카드는?",
        "그럼 상위 1개는?",
        "그럼 억원 단위는?",
        "그럼 요약은?",
        "그럼 가장 높은 곳은?",
        "그럼 LS는 0.9?",
        "그럼 삼성전자 주가는?",
        "그럼 전체는?",
        "그럼 결측값은?",
        "그럼 표는?",
        "그럼 중앙값은?",
        "그럼 표준편차는?",
        "그럼 3줄 정리는?",
        "그럼 점유율은?",
        "그럼 성장률은?",
        "그럼 전년 대비는?",
        "그럼 상세는?",
        "그럼 원본은?",
    ):
        assert web_service._entity_only_followup_change(result, question) == {}, question


def test_llm_context_resolution_separates_independent_new_question() -> None:
    result = _base_result()
    frame = build_query_frame(result)
    resolution = {
        "relation": "new_query",
        "source_strategy": "rediscover",
        "resolved_question": "법인카드 연체 고객 현황을 알려줘",
        "reason": "기존 가맹점 매출과 독립된 질문",
        "used_llm": True,
    }
    plan = plan_followup(
        "법인카드 연체 고객 현황을 알려줘",
        COLUMNS,
        ROWS,
        query_frame=frame,
        result_scope=frame["result_scope"],
        context_resolution=resolution,
    )
    state = web_service._followup_query_state(
        result,
        "법인카드 연체 고객 현황을 알려줘",
        plan,
    )

    assert plan["mode"] == "new_sql"
    assert plan["route_reason"] == "independent_question_requires_new_query"
    assert plan["next_query_frame"].get("entities") is None
    assert state["selected_domain"] == ""
    assert state["selected_tables"] == []
    assert state["previous_question"] == ""
    assert state["previous_sql"] == ""
    assert state["skip_tool_selection"] is False
    assert state["skip_verified_query_matching"] is False
    assert state["question"] == "법인카드 연체 고객 현황을 알려줘"


def test_invalid_context_router_output_falls_back_to_deterministic_plan() -> None:
    result = _base_result()
    preliminary = plan_followup(
        "그럼 전월은?",
        COLUMNS,
        ROWS,
        query_frame=build_query_frame(result),
    )

    with patch.object(web_service.agent, "_call_llm", return_value="잘 모르겠습니다"):
        resolution = web_service._resolve_followup_context(
            result,
            "그럼 전월은?",
            preliminary,
        )

    assert resolution["used_llm"] is False
    assert resolution["relation"] == "refine_query"
    assert resolution["source_strategy"] == "same_source"


def test_completed_followup_becomes_context_for_the_next_turn() -> None:
    result = _base_result()
    frame = build_query_frame(result)
    first_question = "그럼 버거킹은?"
    first_resolution = {
        "relation": "refine_query",
        "source_strategy": "same_source",
        "resolved_question": "2025년 2월 버거킹 가맹점별 매출을 보여줘",
        "reason": "브랜드 변경",
        "used_llm": True,
    }
    first_plan = plan_followup(
        first_question,
        COLUMNS,
        ROWS,
        query_frame=frame,
        result_scope=frame["result_scope"],
        context_resolution=first_resolution,
    )
    burger_result = {
        **result,
        "final_sql": SQL.replace("도미노피자", "버거킹"),
        "answer": "버거킹 매출 결과입니다.",
    }
    completed = web_service._finalize_followup_result(
        result,
        first_question,
        burger_result,
        burger_result["answer"],
        first_plan["mode"],
        "후속 SQL",
        first_plan,
    )
    next_question = "그중 가장 높은 곳은?"
    preliminary = plan_followup(
        next_question,
        COLUMNS,
        ROWS,
        query_frame=completed["query_frame"],
        result_scope=completed["result_scope"],
    )
    llm_result = """{
      "relation": "existing_result",
      "source_strategy": "current_result",
      "resolved_question": "2025년 2월 버거킹 가맹점별 매출 결과에서 매출이 가장 높은 가맹점을 알려줘",
      "reason": "현재 결과 정렬로 답변 가능"
    }"""

    with patch.object(web_service.agent, "_call_llm", return_value=llm_result) as call:
        resolution = web_service._resolve_followup_context(
            completed,
            next_question,
            preliminary,
        )

    prompt = call.call_args.args[0]
    assert completed["question"] == first_resolution["resolved_question"]
    assert completed["query_frame"]["entities"] == [{"column": "가맹점명", "value": "버거킹"}]
    assert "가맹점명=버거킹" in prompt
    assert first_question in prompt
    assert resolution["relation"] == "existing_result"


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

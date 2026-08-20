from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import patch

import web_service
from text2sql_agent.followup_ops import apply_local_transform, build_chart_spec, plan_followup


COLUMNS = ["기준년월", "가맹점명", "매출금액"]
ROWS = [
    ("202501", "강남점", 100_000_000),
    ("202502", "서초점", 300_000_000),
    ("202503", "송파점", 200_000_000),
]


def test_followup_router_separates_database_and_local_work() -> None:
    assert plan_followup("그 중 상위 2개만 보여줘", COLUMNS, ROWS)["mode"] == "transform"
    assert plan_followup("위 결과를 막대 차트로 보여줘", COLUMNS, ROWS)["mode"] == "visualization"
    assert plan_followup("그럼 전월은?", COLUMNS, ROWS)["mode"] == "rewrite_sql"
    assert plan_followup("그 회사의 연체 현황도 같이 보여줘", COLUMNS, ROWS)["mode"] == "new_sql"
    assert plan_followup("2024년으로 바꿔서 라인 차트로 보여줘", COLUMNS, ROWS)["mode"] == "rewrite_sql_visualization"


def test_sort_and_limit_use_existing_rows_without_mutating_input() -> None:
    transformed = apply_local_transform("매출금액 기준 상위 2개만 보여줘", COLUMNS, ROWS)

    assert transformed["applied"] is True
    assert transformed["rows"] == [
        ["202502", "서초점", 300_000_000],
        ["202503", "송파점", 200_000_000],
    ]
    assert ROWS[0] == ("202501", "강남점", 100_000_000)
    assert "매출금액 내림차순 정렬" in transformed["operations"]


def test_top_and_bottom_comparison_extracts_both_ends_but_generic_comparison_stays_analysis() -> None:
    transformed = apply_local_transform("매출금액 상위 1개와 하위 1개를 보여줘", COLUMNS, ROWS)
    assert [row[1] for row in transformed["rows"]] == ["서초점", "강남점"]
    assert transformed["operations"] == ["매출금액 상위 1건·하위 1건 추출"]

    planned = plan_followup("상위/하위 항목의 차이를 비교해줘", COLUMNS, ROWS)
    assert planned["mode"] == "analysis"


def test_numeric_filter_does_not_mistake_row_only_wording_for_column_selection() -> None:
    transformed = apply_local_transform("매출금액 2억 이상만 보여줘", COLUMNS, ROWS)

    assert transformed["columns"] == COLUMNS
    assert [row[1] for row in transformed["rows"]] == ["서초점", "송파점"]
    assert transformed["operations"] == ["매출금액 2억 이상 필터"]


def test_preprocessing_supports_missing_duplicates_units_and_column_selection() -> None:
    rows = [
        ("202501", "강남점", 100_000_000),
        ("202501", "강남점", 100_000_000),
        ("202502", "서초점", None),
    ]
    deduplicated = apply_local_transform("중복을 제거해줘", COLUMNS, rows)
    assert len(deduplicated["rows"]) == 2

    without_missing = apply_local_transform("매출금액 결측값을 제외해줘", COLUMNS, rows)
    assert len(without_missing["rows"]) == 2

    converted = apply_local_transform("매출금액을 억원 단위로 바꿔줘", COLUMNS, ROWS)
    assert converted["columns"][-1] == "매출금액(억원)"
    assert converted["rows"][0][-1] == 1

    selected = apply_local_transform("가맹점명과 매출금액 컬럼만 보여줘", COLUMNS, ROWS)
    assert selected["columns"] == ["가맹점명", "매출금액"]


def test_local_grouping_share_and_growth_are_computed_from_supplied_rows() -> None:
    detail_rows = [
        ("202501", "강남점", 100),
        ("202501", "서초점", 300),
        ("202502", "송파점", 600),
    ]
    grouped = apply_local_transform("월별 매출금액 합계로 묶어줘", COLUMNS, detail_rows)
    assert grouped["columns"] == ["기준년월", "매출금액 합계"]
    assert grouped["rows"] == [["202501", 400.0], ["202502", 600.0]]

    shared = apply_local_transform("매출금액 비중을 계산해줘", COLUMNS, ROWS)
    assert shared["columns"][-1] == "매출금액 비중(%)"
    assert round(sum(row[-1] for row in shared["rows"]), 8) == 100

    growth = apply_local_transform("전월 대비 매출금액 증감률을 추가해줘", COLUMNS, ROWS)
    assert growth["columns"][-1] == "매출금액 증감률(%)"
    assert growth["rows"][0][-1] is None
    assert growth["rows"][1][-1] == 200


def test_chart_contract_uses_time_as_x_axis_and_numeric_metric_as_series() -> None:
    chart = build_chart_spec("위 결과를 라인 차트로 보여줘", COLUMNS, ROWS)

    assert chart is not None
    assert chart["type"] == "line"
    assert chart["x_label"] == "기준년월"
    assert chart["labels"] == ["202501", "202502", "202503"]
    assert chart["datasets"] == [{"label": "매출금액", "data": [100_000_000.0, 300_000_000.0, 200_000_000.0]}]


MEMBER_LIST_COLUMNS = ["기업고객식별자", "가맹점명", "사업자등록번호", "한글시도명", "월매출액", "유효기업신용카드수"]
MEMBER_LIST_ROWS = [
    ("C001", "강남상사", "1010112345", "서울특별시", 152_000_000, 0),
    ("C002", "서초물산", "1010167890", "서울특별시", 210_000_000, 0),
    ("C003", "부산유통", "6010112345", "부산광역시", 130_000_000, 0),
    ("C004", "수원기업", "1350112345", "경기도", 175_000_000, 0),
    ("C004", "수원기업 2호점", "1350112345", "경기도", 90_000_000, 0),
]


def test_region_distribution_counts_companies_instead_of_charting_the_raw_list() -> None:
    question = "대상 기업들의 지역별 분포 현황과 지역별 기업 수를 알려줘"
    planned = plan_followup(question, MEMBER_LIST_COLUMNS, MEMBER_LIST_ROWS)

    assert planned["mode"] == "transform_visualization"
    transform = planned["transform"]
    assert transform["columns"] == ["한글시도명", "기업 수"]
    # 한 기업이 두 가맹점으로 나뉘어 있어도 기업은 하나로 센다.
    assert transform["rows"] == [["서울특별시", 2], ["부산광역시", 1], ["경기도", 1]]
    assert transform["operations"] == ["한글시도명별 기업 수 집계 (기업고객식별자 고유값 기준)"]

    chart = build_chart_spec(question, transform["columns"], transform["rows"])
    assert chart["x_label"] == "한글시도명"
    assert chart["y_label"] == "기업 수"
    assert chart["labels"] == ["서울특별시", "부산광역시", "경기도"]
    assert chart["datasets"] == [{"label": "기업 수", "data": [2.0, 1.0, 1.0]}]


def test_counting_an_entity_the_result_cannot_identify_goes_back_to_sql() -> None:
    # 회원 수는 회원일련번호로만 셀 수 있다. 결과에 없으면 기업 수로 대신 세지 않는다.
    planned = plan_followup("지역별 회원 수 알려줘", MEMBER_LIST_COLUMNS, MEMBER_LIST_ROWS)
    assert planned["transform"]["applied"] is False
    assert planned["requires_sql"] is True


def test_a_partial_result_is_not_counted_locally() -> None:
    planned = plan_followup(
        "지역별 기업 수를 차트로 보여줘",
        MEMBER_LIST_COLUMNS,
        MEMBER_LIST_ROWS,
        result_scope={"is_complete": False},
    )

    assert planned["mode"] == "rewrite_sql_visualization"
    assert planned["route_reason"] == "incomplete_result_requires_sql"


def test_chart_axis_skips_a_constant_column_and_keeps_time_order() -> None:
    single_month = [
        ("202506", "음식점", 900),
        ("202506", "주유소", 700),
        ("202506", "병원", 500),
    ]
    by_industry = build_chart_spec("위 결과를 막대그래프로 보여줘", ["기준년월", "업종", "매출금액"], single_month)
    assert by_industry["x_label"] == "업종"
    assert by_industry["labels"] == ["음식점", "주유소", "병원"]

    monthly = [("202501", 100), ("202502", 300), ("202503", 200), ("202504", 250)]
    over_time = build_chart_spec("월별 매출 막대그래프로 그려줘", ["기준년월", "매출금액"], monthly)
    assert over_time["labels"] == ["202501", "202502", "202503", "202504"]

    long_history = [(f"{year}{month:02d}", month) for year in (2024, 2025, 2026) for month in range(1, 13)]
    truncated = build_chart_spec("월별 매출 추이 라인차트", ["기준년월", "매출금액"], long_history)
    assert truncated["labels"][-1] == "202612"
    assert "최근 구간만 표시했습니다." in truncated["note"]


def test_chart_series_exclude_identifier_and_time_columns() -> None:
    chart = build_chart_spec(
        "차트로 보여줘",
        ["가맹점명", "가맹점번호", "기준년월", "매출금액"],
        [("강남점", 10023456, "202501", 900), ("서초점", 10023457, "202502", 700)],
    )

    assert [dataset["label"] for dataset in chart["datasets"]] == ["매출금액"]


def test_chart_moves_a_mismatched_series_to_the_right_axis_or_drops_it() -> None:
    two_series = build_chart_spec(
        "업종별 매출과 가맹점수 막대그래프",
        ["업종", "매출금액", "가맹점수"],
        [("음식점", 900_000_000, 120), ("주유소", 700_000_000, 80)],
    )
    assert [(dataset["label"], dataset.get("axis")) for dataset in two_series["datasets"]] == [
        ("매출금액", None),
        ("가맹점수", "right"),
    ]
    assert "오른쪽 축" in two_series["note"]

    three_series = build_chart_spec(
        "업종별 매출 건수 가맹점수 막대그래프",
        ["업종", "매출금액", "거래건수", "가맹점수"],
        [("음식점", 900_000_000, 3200, 120), ("주유소", 700_000_000, 2800, 80)],
    )
    assert [dataset["label"] for dataset in three_series["datasets"]] == ["매출금액"]
    assert "축을 공유할 수 없어 제외했습니다" in three_series["note"]


def test_chart_merges_duplicate_labels_only_when_the_metric_is_additive() -> None:
    columns = ["기준년월", "가맹점명", "매출금액"]
    rows = [("202501", "강남점", 100), ("202502", "강남점", 120), ("202501", "서초점", 90)]
    merged = build_chart_spec("가맹점별 매출 막대그래프", columns, rows)
    assert merged["labels"] == ["강남점", "서초점"]
    assert merged["datasets"][0]["data"] == [220.0, 90.0]
    assert "합계로 묶었습니다" in merged["note"]

    ratio_columns = ["기준년월", "가맹점명", "승인율"]
    ratio_rows = [("202501", "강남점", 96.0), ("202502", "강남점", 97.0), ("202501", "서초점", 90.0)]
    kept = build_chart_spec("가맹점별 승인율 막대그래프", ratio_columns, ratio_rows)
    assert kept["labels"] == ["강남점", "강남점", "서초점"]
    assert kept["datasets"][0]["data"] == [97.0, 96.0, 90.0]
    assert "합산하지 않고" in kept["note"]


def test_chart_type_follows_the_named_shape_and_pie_rejects_negatives() -> None:
    columns = ["기준년월", "매출금액"]
    rows = [("202501", 100), ("202502", 300)]
    assert build_chart_spec("매출 추이를 막대그래프로 보여줘", columns, rows)["type"] == "bar"
    assert build_chart_spec("추이 그래프로 보여줘", columns, rows)["type"] == "line"

    negative = build_chart_spec("파이차트로 보여줘", ["업종", "증감액"], [("음식점", 900), ("주유소", -300)])
    assert negative["type"] == "bar"
    assert "음수가 섞여" in negative["note"]


def test_followup_stream_runs_local_transform_without_graph_or_llm() -> None:
    base_result = {
        "question": "월별 가맹점 매출을 보여줘",
        "final_sql": "SELECT 기준년월, 가맹점명, 매출금액 FROM sales",
        "answer": "조회 결과입니다.",
        "query_columns": COLUMNS,
        "query_rows": ROWS,
        "analysis_history": [],
    }
    captured: dict = {}

    def capture_payload(result, *_args, **_kwargs):
        captured.update(result)
        return {
            "answer": result["answer"],
            "columns": result["query_columns"],
            "rows": result["query_rows"],
            "chart": result.get("chart"),
        }

    request = web_service.FollowupRequest(result_id="previous", question="매출금액 상위 2개를 막대 차트로 보여줘")
    session = {"id": "session-1", "user_id": "user-1", "messages": []}
    with (
        patch.object(web_service._SESSION_STORE, "get_result", return_value=base_result),
        patch.object(
            web_service,
            "_resolve_followup_context",
            return_value={
                "relation": "existing_result",
                "source_strategy": "current_result",
                "resolved_question": request.question,
                "reason": "test",
                "used_llm": True,
            },
        ),
        patch.object(web_service, "_stream_graph", side_effect=AssertionError("SQL graph must not run")),
        patch.object(web_service, "_followup_analysis", side_effect=AssertionError("LLM analysis must not run")),
        patch.object(web_service, "_result_payload", side_effect=capture_payload),
        patch.object(web_service, "_finalize_assistant_message", side_effect=lambda _session, data, _message_id: data),
        patch.object(web_service.agent, "create_trace_context", return_value={}),
        patch.object(web_service.agent, "observability_context", return_value=nullcontext()),
        patch.object(web_service.agent, "emit_execution_log"),
    ):
        chunks = list(web_service._stream_followup(request, session, 1))

    assert any(chunk.startswith("event: result") for chunk in chunks)
    assert captured["followup_mode"] == "transform_visualization"
    assert [row[1] for row in captured["query_rows"]] == ["서초점", "송파점"]
    assert captured["chart"]["type"] == "bar"
    assert captured["parent_result_id"] == "previous"


def test_empty_tool_result_can_rerun_sql_for_an_entity_followup() -> None:
    sql = "SELECT * FROM sales WHERE 가맹점명 LIKE '%꾸석지%' AND 기준년월 = '202604'"
    base_result = {
        "question": "2026년 4월 꾸석지의 대손비용률 분석해줘",
        "final_sql": sql,
        "answer": "조회 결과가 없습니다.",
        "selected_tool": "대손비용률_분석",
        "selected_capability_name": "대손비용률_분석",
        "tool_params": {"가맹점명": "꾸석지", "기준년월": "202604", "LS": 0.8, "IS": 1.2},
        "query_columns": [],
        "query_rows": [],
        "analysis_history": [],
    }
    captured: dict = {}

    def fake_stream_graph(_request, state, **_kwargs):
        captured["state"] = state
        yield "generate_answer", {
            **state,
            "answer": "파파이스 대손비용률 결과입니다.",
            "final_sql": sql.replace("꾸석지", "파파이스"),
            "query_columns": ["기준년월", "대손비용률_퍼센트"],
            "query_rows": [("202604", 1.25)],
        }

    def capture_payload(result, *_args, **_kwargs):
        captured["final_result"] = result
        return {
            "answer": result["answer"],
            "columns": result["query_columns"],
            "rows": result["query_rows"],
        }

    request = web_service.FollowupRequest(result_id="empty-result", question="파파이스는?")
    session = {"id": "session-1", "user_id": "user-1", "messages": []}
    with (
        patch.object(web_service._SESSION_STORE, "get_result", return_value=base_result),
        patch.object(web_service.agent, "_call_llm", side_effect=RuntimeError("router unavailable")),
        patch.object(web_service, "_stream_graph", side_effect=fake_stream_graph),
        patch.object(web_service, "_result_payload", side_effect=capture_payload),
        patch.object(web_service, "_finalize_assistant_message", side_effect=lambda _session, data, _message_id: data),
        patch.object(web_service.agent, "create_trace_context", return_value={}),
        patch.object(web_service.agent, "observability_context", return_value=nullcontext()),
        patch.object(web_service.agent, "emit_execution_log"),
    ):
        chunks = list(web_service._stream_followup(request, session, 1))

    assert any(chunk.startswith("event: result") for chunk in chunks)
    assert captured["state"]["selected_tool"] == "대손비용률_분석"
    assert captured["state"]["question"] == "2026년 4월 파파이스의 대손비용률 분석해줘"
    assert captured["state"]["tool_params"] == {
        "가맹점명": "파파이스",
        "기준년월": "202604",
        "LS": 0.8,
        "IS": 1.2,
    }
    assert captured["final_result"]["answer"] == "파파이스 대손비용률 결과입니다."
    assert captured["final_result"]["original_question"] == base_result["question"]


def test_frontend_contains_dependency_free_chart_renderer() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="chartBox"' in source
    assert "function renderChart(chart)" in source
    assert "function drawChart(chart)" in source
    assert "renderChart(data.chart);" in source
    assert 'const rightDatasets = datasets.filter((dataset) => dataset.axis === "right");' in source
    assert "const rotateLabels =" in source
    assert 'const zeroBased = chart.type !== "line";' in source
    assert "const niceStep = (span) =>" in source


def test_frontend_history_keeps_the_original_question() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "web" / "static" / "index.html").read_text(encoding="utf-8")

    assert source.count("const originalQuestion = data.original_question || data.question") == 2


def _failed_base_result() -> dict:
    return {
        "question": "2026년 4월 A기업 카드 이용금액 알려줘",
        "final_sql": "SELECT 이용금액 FROM 기업카드이용 WHERE 기준년월 = '202604'",
        "selected_domain": "법인카드",
        "selected_tables": ["기업카드이용"],
        "answer": "",
        "error_message": "SQL 실행에 실패했습니다.",
        "query_columns": [],
        "query_rows": [],
        "analysis_history": [],
    }


def _run_followup_stream(base_result: dict, question: str, stream_graph):
    captured: dict = {}

    def capture_payload(result, *_args, **_kwargs):
        captured["final_result"] = result
        return {"answer": result.get("answer", ""), "columns": [], "rows": []}

    request = web_service.FollowupRequest(result_id="previous", question=question)
    session = {"id": "session-1", "user_id": "user-1", "messages": []}
    with (
        patch.object(web_service._SESSION_STORE, "get_result", return_value=base_result),
        patch.object(
            web_service,
            "_resolve_followup_context",
            return_value={
                "relation": "existing_result",
                "source_strategy": "current_result",
                "resolved_question": question,
                "reason": "test",
                "used_llm": False,
            },
        ),
        patch.object(web_service, "_stream_graph", side_effect=stream_graph),
        patch.object(web_service, "_result_payload", side_effect=capture_payload),
        patch.object(web_service, "_finalize_assistant_message", side_effect=lambda _session, data, _message_id: data),
        patch.object(web_service.agent, "create_trace_context", return_value={}),
        patch.object(web_service.agent, "observability_context", return_value=nullcontext()),
        patch.object(web_service.agent, "emit_execution_log"),
    ):
        return list(web_service._stream_followup(request, session, 1)), captured


def test_followup_after_a_failed_turn_reruns_sql_with_the_inherited_scope() -> None:
    base_result = _failed_base_result()

    def fake_stream_graph(_request, state, **_kwargs):
        yield "generate_answer", {
            **state,
            "answer": "2026년 4월 A기업 이용금액입니다.",
            "final_sql": "SELECT 이용금액 FROM 기업카드이용 WHERE 기준년월 = '202604'",
            "query_columns": ["이용금액"],
            "query_rows": [(1_000,)],
        }

    chunks, captured = _run_followup_stream(base_result, "이 결과 3줄로 정리해줘", fake_stream_graph)

    assert not any(chunk.startswith("event: error") for chunk in chunks)
    assert any("직전 조회가 실패해" in chunk for chunk in chunks)
    final_result = captured["final_result"]
    assert final_result["followup_mode"] == "rewrite_sql"
    assert final_result["followup_route"]["reason"] == "previous_turn_failed_requires_sql"
    assert final_result["selected_tables"] == ["기업카드이용"]
    assert final_result["selected_domain"] == "법인카드"


def test_followup_on_an_empty_but_successful_result_still_refuses_local_analysis() -> None:
    base_result = {**_failed_base_result(), "error_message": "", "answer": "조회 결과가 없습니다."}

    def unreachable_graph(*_args, **_kwargs):
        raise AssertionError("SQL graph must not run")
        yield

    chunks, captured = _run_followup_stream(base_result, "이 결과 3줄로 정리해줘", unreachable_graph)

    assert any("후속 분석에 사용할 조회 데이터가 없습니다" in chunk for chunk in chunks)
    assert "final_result" not in captured


def _session_with_dropped_result() -> dict:
    return {
        "id": "session-1",
        "user_id": "user-1",
        "messages": [
            {"role": "user", "text": "2026년 4월 A기업 카드 이용금액 알려줘"},
            {
                "role": "assistant",
                "text": "2026년 4월 A기업 이용금액은 1,000원입니다.",
                "result_id": "dropped",
                "sql": "SELECT 이용금액 FROM 기업카드이용 WHERE 기준년월 = '202604'",
                "columns": ["기준년월", "이용금액"],
                "rows": [["202604", 1_000]],
                "row_count": 1,
                "query_frame": {"version": 1, "entities": [{"column": "기업명", "value": "A기업"}]},
                "result_meta": {
                    "selected_domain": "법인카드",
                    "planned_data_sources": ["기업카드이용"],
                    "selected_tool": "",
                },
            },
        ],
    }


def test_dropped_result_is_rebuilt_from_the_stored_conversation() -> None:
    base_result = web_service._result_from_session_message(_session_with_dropped_result(), "dropped")

    assert base_result is not None
    assert base_result["question"] == "2026년 4월 A기업 카드 이용금액 알려줘"
    assert base_result["final_sql"].startswith("SELECT 이용금액")
    assert base_result["query_columns"] == ["기준년월", "이용금액"]
    assert base_result["query_rows"] == [["202604", 1_000]]
    assert base_result["selected_tables"] == ["기업카드이용"]
    assert base_result["selected_domain"] == "법인카드"
    assert base_result["query_frame"]["entities"] == [{"column": "기업명", "value": "A기업"}]
    assert [turn["question"] for turn in base_result["analysis_history"]] == [
        "2026년 4월 A기업 카드 이용금액 알려줘"
    ]
    assert web_service._result_from_session_message(_session_with_dropped_result(), "other") is None


def test_followup_continues_after_the_result_cache_evicted_the_base_result() -> None:
    session = _session_with_dropped_result()
    captured: dict = {}

    def capture_payload(result, *_args, **_kwargs):
        captured["final_result"] = result
        return {"answer": result.get("answer", ""), "columns": [], "rows": []}

    request = web_service.FollowupRequest(result_id="dropped", question="이 결과 3줄로 정리해줘")
    with (
        patch.object(web_service._SESSION_STORE, "get_result", return_value=None),
        patch.object(
            web_service,
            "_resolve_followup_context",
            return_value={
                "relation": "existing_result",
                "source_strategy": "current_result",
                "resolved_question": request.question,
                "reason": "test",
                "used_llm": False,
            },
        ),
        patch.object(web_service, "_stream_graph", side_effect=AssertionError("SQL graph must not run")),
        patch.object(web_service, "_followup_analysis", return_value="핵심만 정리했습니다."),
        patch.object(web_service, "_result_payload", side_effect=capture_payload),
        patch.object(web_service, "_finalize_assistant_message", side_effect=lambda _session, data, _message_id: data),
        patch.object(web_service.agent, "create_trace_context", return_value={}),
        patch.object(web_service.agent, "observability_context", return_value=nullcontext()),
        patch.object(web_service.agent, "emit_execution_log"),
    ):
        chunks = list(web_service._stream_followup(request, session, 1))

    assert not any(chunk.startswith("event: error") for chunk in chunks)
    assert any("보관 기간을 지나" in chunk for chunk in chunks)
    assert captured["final_result"]["answer"] == "핵심만 정리했습니다."

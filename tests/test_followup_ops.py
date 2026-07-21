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


def test_frontend_contains_dependency_free_chart_renderer() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "web" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="chartBox"' in source
    assert "function renderChart(chart)" in source
    assert "function drawChart(chart)" in source
    assert "renderChart(data.chart);" in source

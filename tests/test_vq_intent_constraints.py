from __future__ import annotations

from unittest.mock import patch

import pytest

from text2sql_agent import workflow
from text2sql_agent.followup_ops import apply_local_transform
from text2sql_agent.query_frame import build_result_scope, evolve_query_frame
from text2sql_agent.row_constraints import RowRequest, outer_limit, parse_row_request
from text2sql_agent.tools.sql_builders import _apply_params_to_vq


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("20개만 뽑아줘", RowRequest(20, "head")),
        ("상위 20개만 뽑아줘", RowRequest(20, "top")),
        ("하위 20개만 뽑아줘", RowRequest(20, "bottom")),
        ("뒤에서 20개만 뽑아줘", RowRequest(20, "tail")),
        ("마지막 20건만 보여줘", RowRequest(20, "tail")),
        ("최근 20건만 보여줘", RowRequest(20, "latest")),
        ("한도 사용률 낮은 순으로 기업 20개만", RowRequest(20, "bottom")),
        ("매출 내림차순으로 20개만", RowRequest(20, "top")),
        ("최신순으로 20개만", RowRequest(20, "latest")),
        ("최근 20개월 매출을 보여줘", RowRequest(None, "")),
        ("최근 20일 매출을 보여줘", RowRequest(None, "")),
        ("최근 20시간 승인을 보여줘", RowRequest(None, "")),
        ("매출 상위 20억원 기업", RowRequest(None, "top")),
        ("회원이 20명만 있는 기업", RowRequest(None, "")),
        ("거래가 20건만 발생한 가맹점", RowRequest(None, "")),
    ],
)
def test_row_request_parser_distinguishes_rows_from_periods(
    question: str,
    expected: RowRequest,
) -> None:
    assert parse_row_request(question) == expected


@pytest.mark.parametrize(
    "question",
    [
        "가맹점별 연체원금 상위 20개 알려줘",
        "현재 기업고객별 회원 수를 알려줘",
        "2026년 상반기 회원별 해외 이용금액 알려줘",
        "2026년 6월 기업별 한도 사용률 하위 20개만 뽑아줘",
        "2026년 6월 기업별 한도 사용률 상위 20개만 뽑아줘",
        "2026년 6월 기업별 잔여한도 상위 20개만 뽑아줘",
        "2026년 상반기 가맹점별 매출건수 상위 20개만 뽑아줘",
        "2026년 상반기 가맹점별 평균 수수료율 상위 20개만 뽑아줘",
        "2026년 일별 매출을 높은 순으로 20개만 보여줘",
        "2026년 일별 매출금액을 내림차순으로 20개만 보여줘",
        "2026년 일별 매출 마지막 20개만 뽑아줘",
        "2026년 해외업종에서 가장 많이 사용한 달과 이용금액을 20개만 뽑아줘",
        "업무 보고용으로 확인해줘. 2026년 상반기 월별 국내 카드매출금액과 거래건수 알려줘",
        "도미노 브랜드 가맹점 20개만 뽑아줘",
        "2026년 6월 기업회원 유실적 업체수를 알려줘 20개만",
        "2026년 6월 기업카드 무실적 회원 20개만 뽑아줘",
    ],
)
def test_vq_is_rejected_when_metric_grain_direction_or_cardinality_conflicts(
    question: str,
) -> None:
    assert workflow._select_verified_query_capability(question, {}) is None


def test_presentation_and_grain_words_are_not_merchant_names() -> None:
    assert workflow._extract_merchant_name_by_rule("일별 매출 최근 20건만 보여줘") == ""
    assert workflow._extract_merchant_name_by_rule(
        "업무 보고용으로 확인해줘. 2026년 상반기 월별 국내 카드매출금액과 거래건수 알려줘"
    ) == ""
    for grain in ("연도별", "분기별", "주별", "날짜별", "기업별"):
        assert workflow._extract_merchant_name_by_rule(f"{grain} 매출 최근 20건만 보여줘") == ""


def test_explicit_limit_is_applied_to_vq_without_limit_metadata() -> None:
    question = (
        "현재기준 유효한 기업카드를 보유한 기업 회원중 2025년 상반기 전체 이용금액 대비 "
        "2026년 상반기 이용 금액이 하락한 기업회원의 상위 100개와 이용금액을 뽑아줘"
    )
    capability = workflow._select_verified_query_capability(question, {})
    assert capability is not None

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {"question": question, **capability, "user_provided_params": {}}
        )

    assert result["extracted_params"]["limit"] == 100
    assert result["final_sql"].rstrip().endswith("LIMIT 100")


def test_only_final_limit_is_replaced() -> None:
    sql = "WITH sample AS (SELECT id FROM source LIMIT 1) SELECT id FROM sample LIMIT 10"
    rendered = _apply_params_to_vq(
        sql,
        {"limit": 20},
        "sample",
        {"limit": {"type": "integer"}},
    )

    assert "source LIMIT 1" in rendered
    assert rendered.endswith("LIMIT 20")


def test_outer_limit_is_appended_when_only_cte_has_a_limit() -> None:
    sql = "WITH sample AS (SELECT id FROM source LIMIT 1) SELECT id FROM sample"
    rendered = _apply_params_to_vq(
        sql,
        {"limit": 20},
        "sample",
        {"limit": {"type": "integer"}},
    )

    assert "source LIMIT 1" in rendered
    assert outer_limit(rendered) == 20


def test_inner_limit_and_order_are_not_recorded_as_final_result_constraints() -> None:
    sql = (
        "WITH sample AS (SELECT id FROM source ORDER BY created_at DESC LIMIT 1) "
        "SELECT id FROM sample"
    )
    scope = build_result_scope(sql, fetched_row_count=1, displayed_row_count=1)

    assert scope["sql_limit"] is None
    assert scope["ordered"] is False


def test_followup_plain_and_tail_limits_preserve_requested_side() -> None:
    columns = ["순번", "값"]
    rows = [(1, 10), (2, 20), (3, 30), (4, 40)]

    head = apply_local_transform("2개만 보여줘", columns, rows)
    tail = apply_local_transform("뒤에서 2개만 보여줘", columns, rows)

    assert [row[0] for row in head["rows"]] == [1, 2]
    assert [row[0] for row in tail["rows"]] == [3, 4]

    frame = evolve_query_frame({}, "마지막 2건만", mode="rewrite_sql")
    assert frame["limit"] == 2
    assert frame["sort"]["direction"] == "tail"


def test_followup_latest_limit_sorts_by_time_before_slicing() -> None:
    result = apply_local_transform(
        "최근 2건만 보여줘",
        ["가맹점명", "기준일자", "매출금액"],
        [
            ("A", "2026-01-01", 100),
            ("B", "2026-03-01", 300),
            ("C", "2026-02-01", 200),
        ],
    )

    assert [row[1] for row in result["rows"]] == ["2026-03-01", "2026-02-01"]


def test_followup_ranking_uses_numeric_metric_not_named_dimension() -> None:
    result = apply_local_transform(
        "가맹점 매출 상위 2개만 보여줘",
        ["가맹점명", "매출금액"],
        [("Z", 100), ("A", 300), ("M", 200)],
    )

    assert [row[0] for row in result["rows"]] == ["A", "M"]


def test_generated_sql_validation_enforces_requested_limit_and_direction() -> None:
    bad = workflow._validate_requested_row_constraints(
        "일별 매출 마지막 20개만 보여줘",
        "SELECT day, amount FROM sales ORDER BY day ASC LIMIT 20",
    )
    good = workflow._validate_requested_row_constraints(
        "일별 매출 마지막 20개만 보여줘",
        "SELECT day, amount FROM sales ORDER BY day DESC LIMIT 20",
    )

    assert any("DESC" in issue for issue in bad)
    assert good == []


def test_generated_sql_validation_enforces_requested_rank_metric() -> None:
    bad = workflow._validate_requested_row_constraints(
        "가맹점별 매출건수 상위 20개만 보여줘",
        "SELECT merchant, sales_amount, sales_count FROM sales ORDER BY 총매출금액 DESC LIMIT 20",
    )
    good = workflow._validate_requested_row_constraints(
        "가맹점별 매출건수 상위 20개만 보여줘",
        "SELECT merchant, sales_count FROM sales ORDER BY 총매출건수 DESC LIMIT 20",
    )

    assert any("순위 지표" in issue for issue in bad)
    assert good == []


def test_generated_sql_validation_ignores_inner_row_constraints() -> None:
    issues = workflow._validate_requested_row_constraints(
        "최근 20건만 보여줘",
        "WITH recent AS (SELECT day FROM sales ORDER BY day DESC LIMIT 20) SELECT day FROM recent",
    )
    mixed_directions = workflow._validate_requested_row_constraints(
        "최근 20건만 보여줘",
        "SELECT day, amount FROM sales ORDER BY day ASC, amount DESC LIMIT 20",
    )

    assert any("LIMIT" in issue for issue in issues)
    assert any("ORDER BY" in issue for issue in issues)
    assert any("DESC" in issue for issue in mixed_directions)


def test_generated_sql_validation_rejects_scalar_and_non_time_latest_results() -> None:
    scalar = workflow._validate_requested_row_constraints(
        "가맹점 20개만 보여줘",
        "SELECT COUNT(DISTINCT merchant_id) AS merchant_count FROM merchants LIMIT 20",
    )
    non_time = workflow._validate_requested_row_constraints(
        "최신 20건만 보여줘",
        "SELECT merchant, amount FROM sales ORDER BY amount DESC LIMIT 20",
    )

    assert any("단일 집계값" in issue for issue in scalar)
    assert any("시간 컬럼" in issue for issue in non_time)


def test_scalar_guard_ignores_correlated_scalar_subqueries() -> None:
    sql = (
        "SELECT merchant_id, (SELECT COUNT(*) FROM sales s WHERE s.merchant_id = m.id) AS cnt "
        "FROM merchants m ORDER BY cnt DESC LIMIT 20"
    )

    assert workflow.is_scalar_aggregate_query(sql) is False


def test_scalar_guard_detects_aggregate_on_one_fixed_group() -> None:
    sql = (
        "SELECT SUBSTRING(joined_at, 1, 6) AS month, COUNT(DISTINCT customer_id) AS customer_count "
        "FROM customers WHERE SUBSTRING(joined_at, 1, 6) = '202612' "
        "GROUP BY SUBSTRING(joined_at, 1, 6)"
    )

    assert workflow.is_scalar_aggregate_query(sql) is True


def test_tool_selection_rejects_unsupported_tail_and_scalar_count_shapes() -> None:
    tool = workflow.TOOL_MAP["corporate_card_active_no_usage_members"]

    assert workflow._tool_request_is_supported(
        "2026년 6월 기업카드 무실적 회원 20개만 뽑아줘", tool
    )
    assert not workflow._tool_request_is_supported(
        "2026년 6월 기업카드 무실적 회원 마지막 20개만 뽑아줘", tool
    )
    assert not workflow._tool_request_is_supported(
        "2026년 6월 기업카드 무실적 회원 수를 알려줘", tool
    )
    merchant_tool = workflow.TOOL_MAP["merchant_corporate_sales_target_no_corporate_card"]
    assert workflow._tool_request_is_supported(
        "2026년 6월 매출 1억원 이상 법인 가맹점 중 기업카드 미보유 20개만 뽑아줘",
        merchant_tool,
    )


def test_tool_execution_rechecks_rendered_row_constraints() -> None:
    result = workflow.execute_tool(
        {
            "question": "2026년 6월 기업카드 무실적 회원 마지막 20개만 뽑아줘",
            "selected_tool": "corporate_card_active_no_usage_members",
            "tool_params": {"기준년월": "202606", "조회개월수": 6, "limit": 20},
        }
    )

    assert result["selected_tool"] == ""
    assert "결과 제약" in result["query_error"]

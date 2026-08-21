"""가맹점 신용판매 매출금액은 이번 달과 지난 달의 원천이 다르다.

금년·전년가맹점신용판매매출금액은 가맹점기본(tbdaadt01, 일 적재)·가맹점월실적
(tmdaa5e11)·가맹점월스냅샷(tmdaa5d01) 세 곳에 같은 이름으로 있다. 월 실적은 달이
닫힌 뒤 적재되므로 이번 달 값은 일 적재 마스터에만 있고, 지난 달 이전은 월 실적이
원천이다(사용자 제공).

v1 에는 이 갈림길이 없었다. 컬럼명이 "금년…" 으로 시작해 "가맹점 신용판매 매출금액"
표면형과 맞지 않아 세 테이블 모두 단서를 못 받고, 매출금액 컬럼을 가진 전표
테이블(tbdaabt30·tbdaabt08)이 1순위로 올라왔다. tbdaadt01 을 골라도 기간이 붙으면
적재 정책이 tmdaa5d01(월말 스냅샷)로 돌려 버렸다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from text2sql_agent import schema, workflow

ROOT = Path(__file__).resolve().parents[1]
RAW_SEMANTIC = yaml.safe_load((ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))
ATTRIBUTE = next(
    item
    for item in RAW_SEMANTIC["semantic_attributes"]
    if item["name"] == "merchant_credit_sales"
)
# 이번 달은 실행일에 따라 바뀌므로 질문을 날짜에서 만든다.
CURRENT_YM = workflow._current_ym()
CURRENT_MONTH_PHRASE = f"{CURRENT_YM[:4]}년 {int(CURRENT_YM[4:]):d}월"


def _selected_tables(question: str) -> list[str]:
    for attribute in schema.semantic_attribute_candidates(
        schema.SCHEMA, question, max_count=6
    ):
        if attribute["name"] == ATTRIBUTE["name"]:
            return [
                str(mapping["table"])
                for mapping in workflow._attribute_source_mappings(question, attribute)
            ]
    raise AssertionError(f"속성이 질문에 잡히지 않았다: {question}")


def test_attribute_declares_both_sources_with_the_open_month_rule() -> None:
    mappings = {mapping["role"]: mapping for mapping in ATTRIBUTE["source_mappings"]}

    assert mappings["current_merchant_credit_sales"]["table"] == "tbdaadt01"
    assert mappings["monthly_merchant_credit_sales"]["table"] == "tmdaa5e11"
    for mapping in mappings.values():
        assert mapping["columns"] == [
            "금년가맹점신용판매매출금액",
            "전년가맹점신용판매매출금액",
        ]
    assert ATTRIBUTE["source_selection"]["open_month_uses_current"] is True
    assert any("tmdaa5e11" in caution for caution in ATTRIBUTE["semantic_cautions"])


@pytest.mark.parametrize(
    "question",
    [
        "이번 달 가맹점 신용판매 매출금액 알려줘",
        "현재 가맹점 신용판매 매출금액 상위 10곳 알려줘",
        "가맹점 신용판매 매출금액 알려줘",
        f"{CURRENT_MONTH_PHRASE} 가맹점 신용판매 매출금액 알려줘",
    ],
)
def test_open_month_reads_the_daily_master(question: str) -> None:
    assert _selected_tables(question) == ["tbdaadt01"]
    assert workflow._rule_rank_tables(question)[0] == "tbdaadt01"


@pytest.mark.parametrize(
    "question",
    [
        "2026년 5월 가맹점 신용판매 매출금액 알려줘",
        "2025년 가맹점 신용판매 매출금액 상위 10곳 알려줘",
        "2026년 1월부터 5월까지 가맹점 신용판매 매출금액 추이를 보여줘",
    ],
)
def test_closed_months_read_the_monthly_performance_table(question: str) -> None:
    assert _selected_tables(question) == ["tmdaa5e11"]
    assert workflow._rule_rank_tables(question)[0] == "tmdaa5e11"
    # 마스터는 후보에서 빠져야 적재 정책이 tmdaa5d01 로 돌리지 못한다.
    assert "tbdaadt01" in workflow._attribute_snapshot_exclusions(question)


def test_open_month_is_not_swapped_to_the_month_end_snapshot() -> None:
    """기간이 붙으면 적재 정책이 tbdaadt01 을 tmdaa5d01 로 돌려 버렸다."""
    question = "이번 달 가맹점 신용판매 매출금액 상위 10곳 알려줘"

    assert workflow._recent_merchant_time_route(question)[0] == "master"
    assert workflow._route_accumulation_table_names(question, ["tbdaadt01"]) == [
        "tbdaadt01"
    ]


def test_closed_month_question_keeps_the_monthly_performance_table() -> None:
    question = "2026년 5월 가맹점 신용판매 매출금액 상위 10곳 알려줘"

    assert workflow._route_accumulation_table_names(question, ["tmdaa5e11"]) == [
        "tmdaa5e11"
    ]


def test_prompt_names_the_two_sources_for_the_measure() -> None:
    summary = schema.build_semantic_attributes_summary(
        schema.SCHEMA, "이번 달 가맹점 신용판매 매출금액 알려줘", "merchant_sales"
    )

    assert "merchant_credit_sales" in summary
    assert "금년가맹점신용판매매출금액" in summary


def test_open_month_monthly_sql_is_rerouted_to_the_daily_source() -> None:
    """월 실적은 달이 닫힌 뒤 적재된다. 두 테이블이 함께 가진 컬럼이면 돌린다."""
    sql = (
        'SELECT a."가맹점번호", SUM(COALESCE(a."금년가맹점신용판매매출금액", 0)) AS "신용판매매출금액"\n'
        "FROM card_system.tmdaa5e11 a\n"
        f'WHERE a."기준년월" = \'{CURRENT_YM}\'\n'
        'GROUP BY a."가맹점번호"'
    )

    routed = workflow._apply_current_month_live_sources(sql, {"tmdaa5e11"})

    assert "card_system.tbdaadt01" in routed
    assert '"실적기준년월일" = ' in routed
    # 감싸는 SELECT 가 기준년월을 파생해 호출부가 쓰던 축은 그대로 남는다.
    assert 'SUBSTR(tbdaadt01."실적기준년월일", 1, 6) AS "기준년월"' in routed
    assert not workflow._open_month_live_source_notes(sql)


def test_closed_month_monthly_sql_stays_on_the_monthly_table() -> None:
    sql = (
        'SELECT a."가맹점번호", a."금년가맹점신용판매매출금액"\n'
        "FROM card_system.tmdaa5e11 a\n"
        "WHERE a.\"기준년월\" = '202605'"
    )

    assert workflow._apply_current_month_live_sources(sql, {"tmdaa5e11"}) == sql


def test_column_the_daily_source_lacks_reports_no_data_instead_of_rerouting() -> None:
    """일시불·할부 매출금액은 tbdaadt01 에 없다. 돌리면 빈 결과가 컬럼 오류가 된다."""
    sql = (
        'SELECT a."가맹점번호", SUM(COALESCE(a."가맹점일시불매출금액", 0)) AS "월매출"\n'
        "FROM card_system.tmdaa5e11 a\n"
        f'WHERE a."기준년월" = \'{CURRENT_YM}\'\n'
        'GROUP BY a."가맹점번호"'
    )

    assert workflow._apply_current_month_live_sources(sql, {"tmdaa5e11"}) == sql
    notes = workflow._open_month_live_source_notes(sql)
    assert notes and "가맹점일시불매출금액" in notes[0]
    assert "tbdaadt01" in notes[0]

    answer = workflow.generate_answer(
        {"question": "이번 달 가맹점 일시불 매출금액 알려줘", "final_sql": sql, "query_columns": [], "query_rows": []}
    )["answer"]

    assert answer.startswith("해당 데이터가 없습니다.")
    assert "가맹점일시불매출금액" in answer

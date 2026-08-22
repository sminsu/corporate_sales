"""마스터에만 있는 속성을 물었는데 SQL 이 같은 엔티티의 스냅샷으로 생성되던 자리.

"한신포차 가맹점주 가맹점 도로명 주소 2026년 1월 기준으로 알려줘" 는 주소가
가맹점기본(tbdaadt01)에만 있어 마스터 질문으로 판정되고, 테이블 선택도 tbdaadt01
하나로 좁혀진다. 그런데 함께 걸린 '가맹점 주대표자' 속성이 tbdaaus01·tmdaaus01·
tmdaa5d01 을 같은 대표고객식별자의 원천으로 선언해 두어서, 그 목록을 그대로 받은 SQL
생성이 마스터 대신 가맹점일별요약을 베껴 왔다. 주소 컬럼이 없는 테이블이라 답이 될 수
없는데도 적재 라우팅은 tbdaaus01 을 과거 월 짝(tmdaaus01)으로 옮겨 주기만 했다.

이미 tmdaa5d01(선언된 historical_source)에는 같은 되돌리기가 있었다. 같은 엔티티의
다른 스냅샷도 같은 규칙으로 되돌린다. 단, 마스터에 없는 컬럼을 읽고 있으면 되돌릴 곳이
없으므로 그대로 둔다.
"""

from __future__ import annotations

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import (
    SCHEMA,
    _validate_sql_against_schema,
    build_metrics_summary,
    build_semantic_attributes_summary,
    find_relevant_queries,
    find_relevant_references,
)

QUESTION = "한신포차 가맹점주 가맹점 도로명 주소 2026년 1월 기준으로 알려줘"
UNDATED_QUESTION = "한신포차 가맹점주 가맹점 도로명 주소 알려줘"


def _snapshot_sql(table: str, column: str, value: str) -> str:
    return (
        'SELECT s."가맹점명", s."대표고객식별자"\n'
        f"FROM card_system.{table} s\n"
        f"WHERE s.\"가맹점명\" LIKE '%한신포차%'\n"
        f"  AND s.\"{column}\" = '{value}'"
    )


def test_the_question_declares_the_snapshot_siblings() -> None:
    assert workflow._master_attribute_alternate_sources(QUESTION) == [
        "tbdaaus01",
        "tmdaa5d01",
        "tmdaaus01",
    ]


@pytest.mark.parametrize(
    ("table", "column", "value"),
    [
        ("tbdaaus01", "기준년월일", "20260131"),
        ("tmdaaus01", "기준년월", "202601"),
        ("tmdaa5d01", "기준년월", "202601"),
    ],
)
def test_a_dated_snapshot_answer_is_restored_to_the_master(
    table: str, column: str, value: str
) -> None:
    routed = workflow._apply_accumulation_historical_sources(
        QUESTION, _snapshot_sql(table, column, value)
    )

    assert "tbdaadt01" in routed
    assert table not in routed
    # 요청한 달은 마스터에 없다. 최신 적재일 1건으로 좁히고 그 사실은 안내가 밝힌다.
    assert f'"{workflow._TBDAADT01_TIME_COLUMN}" = ' in routed
    assert value not in routed
    assert _validate_sql_against_schema(routed, ["tbdaadt01"]) == []


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT s."가맹점명", s."대표고객식별자" FROM card_system.tbdaaus01 s',
        'SELECT s."가맹점명", s."대표고객식별자" FROM card_system.tbdaaus01 s '
        'WHERE s."기준년월일" = (SELECT MAX(x."기준년월일") FROM card_system.tbdaaus01 x)',
    ],
)
def test_an_undated_snapshot_answer_is_restored_to_the_master(sql: str) -> None:
    routed = workflow._apply_accumulation_historical_sources(UNDATED_QUESTION, sql)

    assert "tbdaadt01" in routed
    assert "tbdaaus01" not in routed
    assert _validate_sql_against_schema(routed, ["tbdaadt01"]) == []


def test_the_restored_answer_names_the_load_it_was_read_from() -> None:
    routed = workflow._apply_accumulation_historical_sources(
        QUESTION, _snapshot_sql("tbdaaus01", "기준년월일", "20260131")
    )
    note = workflow._implicit_time_basis_note(QUESTION, routed)

    assert "202601" in note
    assert workflow._latest_available_day("tbdaadt01") in note


def test_a_snapshot_only_column_stays_on_the_snapshot() -> None:
    """되돌리면 마스터에 없는 컬럼이 된다. 빈 결과를 컬럼 오류로 바꾸지 않는다."""
    sql = (
        'SELECT s."가맹점명", s."유효기업신용카드수"\n'
        "FROM card_system.tbdaaus01 s\n"
        "WHERE s.\"기준년월일\" = '20260131'"
    )

    routed = workflow._apply_accumulation_historical_sources(QUESTION, sql)

    assert "tbdaadt01" not in routed


@pytest.mark.parametrize(
    ("question", "sql"),
    [
        (
            "2026년 1월 기준 가맹점 수 알려줘",
            'SELECT COUNT(DISTINCT s."가맹점번호") FROM card_system.tmdaaus01 s '
            "WHERE s.\"기준년월\" = '202601'",
        ),
        (
            "2026년 1월 기준 가맹점별 유효기업신용카드수 알려줘",
            'SELECT s."가맹점번호", s."유효기업신용카드수" FROM card_system.tmdaaus01 s '
            "WHERE s.\"기준년월\" = '202601'",
        ),
    ],
)
def test_a_snapshot_question_keeps_its_snapshot(question: str, sql: str) -> None:
    assert workflow._apply_accumulation_historical_sources(question, sql) == sql


def test_table_selection_already_pinned_the_master() -> None:
    """선택은 마스터였다. 새던 곳은 SQL 생성이다."""
    assert workflow._rule_rank_tables(QUESTION) == ["tbdaadt01"]
    assert workflow._route_accumulation_table_names(QUESTION, ["tbdaaus01"]) == [
        "tbdaadt01"
    ]


def test_the_sql_prompt_only_offers_the_selected_table_as_a_source() -> None:
    """되돌리기는 그물이고, 애초에 다른 원천을 적어 주지 않는 것이 먼저다."""
    summary = build_semantic_attributes_summary(
        SCHEMA, QUESTION, "merchant_sales", table_names=["tbdaadt01"]
    )

    assert "가맹점상세주소" in summary
    assert "    - tbdaadt01: 대표고객식별자" in summary
    for snapshot in ("tbdaaus01", "tmdaaus01", "tmdaa5d01"):
        assert f"    - {snapshot}:" not in summary


def test_table_selection_still_sees_every_source() -> None:
    """테이블을 고르는 단계는 후보가 다 보여야 한다."""
    summary = build_semantic_attributes_summary(SCHEMA, QUESTION, "merchant_sales")

    assert "    - tbdaaus01: 대표고객식별자" in summary


# 프롬프트 근거는 고른 테이블과 그 적재 짝으로 좁힌다.
PROMPT_SOURCES = ["tbdaadt01", "tmdaa5d01"]


def test_the_load_pair_travels_with_the_selected_table() -> None:
    assert workflow._prompt_source_tables(["tbdaadt01"]) == PROMPT_SOURCES


def test_the_reference_recipe_comes_from_the_selected_table() -> None:
    """폐업 가맹점 수(tbdaaus01) reference 가 "가맹점주"·브랜드명만 겹쳐 이겼다."""
    scoped = find_relevant_references(
        SCHEMA, QUESTION, domain_name="merchant_sales", table_names=PROMPT_SOURCES
    )

    assert "primary_table: tbdaadt01" in scoped
    assert "tbdaaus01" not in scoped
    assert "tmdaaus01" not in scoped


def test_a_metric_the_selected_table_cannot_compute_is_not_offered() -> None:
    scoped = build_metrics_summary(
        SCHEMA, QUESTION, "merchant_sales", table_names=PROMPT_SOURCES
    )

    assert "브랜드가맹점주수" not in scoped
    assert "tbdaaus01" not in scoped


def test_a_verified_query_example_reads_the_selected_table() -> None:
    """참고 SQL 은 거의 그대로 복사된다. 고른 테이블을 안 읽는 예시는 붙이지 않는다."""
    scoped = find_relevant_queries(
        SCHEMA, QUESTION, domain_name="merchant_sales", table_names=PROMPT_SOURCES
    )

    assert "tmdaaus01" not in scoped
    assert "tbdaaus01" not in scoped


def test_table_selection_still_sees_every_recipe() -> None:
    """테이블을 고르는 단계는 후보가 다 보여야 한다."""
    unscoped = find_relevant_references(SCHEMA, QUESTION, domain_name="merchant_sales")

    assert "tbdaaus01" in unscoped

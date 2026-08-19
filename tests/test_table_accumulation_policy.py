from __future__ import annotations

import contextlib
from copy import deepcopy
from datetime import date
from unittest.mock import patch

import pytest

from scripts.v2_build.build_semantic_layer_v2 import (
    KST_PREVIOUS_DAY_SQL,
    apply_accumulation_policies,
    apply_previous_day_semantic_overrides,
)
from text2sql_agent import db, schema, time_policy, workflow
from text2sql_agent.tools.sql_builders import _vq_sql_가맹점카드소지현황
from text2sql_agent.time_policy import (
    TABLE_ACCUMULATION_POLICIES,
    previous_day_ymd,
    recent_window_ymd,
)
import web_service


# 전일(D-1) 계약을 검증하는 테스트는 오늘 날짜에 의존한다. 날짜를 고정하지 않으면
# 자정을 넘길 때마다 기대값이 어긋난다(실제로 8/12 → 8/13 에 3건이 깨졌다).
#
# 시계가 한 곳이 아닌 게 함정이다. previous_day_ymd() 는 time_policy.kst_today 를
# 부르고, workflow·schema 는 각자 import 한 이름을 부른다. 한쪽만 patch 하면 나머지가
# 실제 날짜로 새어 들어와 테스트가 "왜 통과하는지 모르게" 통과하거나 깨진다.
FROZEN_TODAY = date(2026, 8, 12)
FROZEN_PREVIOUS_DAY = "20260811"      # FROZEN_TODAY 의 D-1
FROZEN_TODAY_YMD = "20260812"


@contextlib.contextmanager
def frozen_clock(today: date = FROZEN_TODAY):
    """모든 kst_today 바인딩을 한 날짜로 묶는다."""
    with (
        patch.object(time_policy, "kst_today", return_value=today),
        patch.object(workflow, "kst_today", return_value=today),
        patch.object(schema, "kst_today", return_value=today),
    ):
        yield today


@contextlib.contextmanager
def no_period_range():
    """적재 범위를 못 읽는 상태. 최신 가용일이 KST 전일로 되돌아가는지 볼 때 쓴다."""
    with patch.object(workflow, "loaded_period_range", return_value=None):
        yield


@contextlib.contextmanager
def latest_loaded_day(day: str):
    """적재된 최신 일자를 고정한다. 시계의 D-1 과 다른 값을 줘야 의미가 있다."""
    with patch.object(workflow, "loaded_period_range", return_value=("20240101", day)):
        yield


def test_frozen_clock_covers_every_kst_today_binding() -> None:
    """헬퍼가 시계를 하나라도 놓치면 아래 테스트들이 조용히 날짜에 의존하게 된다."""
    with frozen_clock():
        assert time_policy.kst_today() == FROZEN_TODAY
        assert workflow.kst_today() == FROZEN_TODAY
        assert schema.kst_today() == FROZEN_TODAY
        assert previous_day_ymd() == FROZEN_PREVIOUS_DAY


def _assert_previous_day_semantics(document: dict) -> None:
    metrics = {item["name"]: item for item in document["canonical_metrics"]}
    contracts = {item["name"]: item for item in document["semantic_query_contracts"]}
    references = {item["intent"]: item for item in document["query_references"]}

    for name in ("브랜드가맹점주수", "기업카드소지가맹점주수"):
        metric = metrics[name]
        assert KST_PREVIOUS_DAY_SQL in metric["required_filters"][0]
        assert "MAX(기준년월일)" not in str(metric)

    closed_metric = metrics["최근기간폐업가맹점수"]
    assert KST_PREVIOUS_DAY_SQL in closed_metric["expression"]
    assert closed_metric["time_policy"]["range_end"] == KST_PREVIOUS_DAY_SQL
    assert "CURRENT_DATE" not in str(closed_metric)

    owner_contract = contracts["brand_owner_corporate_card_count"]
    assert owner_contract["time_policy"]["snapshot"] == KST_PREVIOUS_DAY_SQL
    assert "MAX(기준년월일)" not in str(owner_contract)

    closed_contract = contracts["recent_closed_brand_merchant_count"]
    assert closed_contract["time_policy"]["range_end"] == KST_PREVIOUS_DAY_SQL
    assert "CURRENT_DATE" not in str(closed_contract)

    assert "CURRENT_DATE" not in str(references["최근기간_브랜드가맹점_폐업수"])
    assert "최신 기준년월일" not in str(references["브랜드가맹점주_기업카드보유인원"])


def test_all_user_provided_table_accumulation_policies_are_loaded() -> None:
    expected_counts = {"daily": 7, "monthly": 11, "yearly": 1, "previous_day": 2}
    assert len(TABLE_ACCUMULATION_POLICIES) == 21
    assert {
        cadence: sum(policy["cadence"] == cadence for policy in TABLE_ACCUMULATION_POLICIES.values())
        for cadence in expected_counts
    } == expected_counts

    loaded = {
        table["name"]: table.get("accumulation_policy")
        for table in schema.SCHEMA["tables"]
        if table["name"] in TABLE_ACCUMULATION_POLICIES
    }
    assert loaded == TABLE_ACCUMULATION_POLICIES


def test_corporate_customer_daily_and_monthly_sources_have_distinct_policies() -> None:
    daily = TABLE_ACCUMULATION_POLICIES["tbdaa1d12"]
    monthly = TABLE_ACCUMULATION_POLICIES["tmdaa1d12"]

    assert daily == {
        "cadence": "previous_day",
        "query_time_dimension": "기준년월일",
        "format": "YYYYMMDD",
        "lag_days": 1,
        "has_reference_month": False,
        "historical_source": {
            "table": "tmdaa1d12",
            "query_time_dimension": "기준년월",
            "format": "YYYYMM",
        },
    }
    assert monthly == {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    }


@pytest.mark.parametrize(
    "question",
    [
        "2026년 8월 11일 기업별 한도 현황",
        "2026-08-11 기업별 한도 현황",
        "2026.08.11 기업별 한도 현황",
        "2026/08/11 기업별 한도 현황",
        "20260811 기업별 한도 현황",
    ],
)
def test_explicit_kst_previous_day_without_current_word_uses_daily_snapshot(
    question: str,
) -> None:
    contract = next(
        item
        for item in schema.SCHEMA["semantic_query_contracts"]
        if item["name"] == "corporate_limit_status_at_month"
    )

    with patch.object(schema, "kst_today", return_value=date(2026, 8, 12)):
        assert not schema._has_historical_period_expression(question)
        assert schema.source_tables_for_question(contract, question) == ["tbdaa1d12"]
        assert workflow._rule_rank_tables(question) == ["tbdaa1d12"]
        assert workflow._previous_day_archive_route(question) == ("", "")


@pytest.mark.parametrize(
    "question",
    [
        "2026년 8월 10일 기업별 한도 현황",
        "2026-08-10 기업별 한도 현황",
        "2026/08/10 기업별 한도 현황",
        "20260810 기업별 한도 현황",
    ],
)
def test_explicit_date_before_kst_previous_day_uses_monthly_snapshot(
    question: str,
) -> None:
    contract = next(
        item
        for item in schema.SCHEMA["semantic_query_contracts"]
        if item["name"] == "corporate_limit_status_at_month"
    )

    with patch.object(schema, "kst_today", return_value=date(2026, 8, 12)):
        assert schema._has_historical_period_expression(question)
        assert schema.source_tables_for_question(contract, question) == ["tmdaa1d12"]
        assert workflow._rule_rank_tables(question) == ["tmdaa1d12"]


def test_explicit_previous_day_does_not_hide_an_additional_history_window() -> None:
    question = "2026-08-11 기업고객의 최근 6개월 이용 추이"

    with patch.object(schema, "kst_today", return_value=date(2026, 8, 12)):
        assert schema._has_historical_period_expression(question)


def test_current_corporate_customer_snapshot_stays_on_previous_day_source() -> None:
    sql = """
    SELECT a."고객식별자"
    FROM card_system.tbdaa1d12 a
    WHERE a."기준년월일" = '20260811'
    """

    routed = workflow._apply_accumulation_historical_sources("현재 기업고객 현황", sql)

    assert "tbdaa1d12" in routed
    assert "tmdaa1d12" not in routed
    assert 'a."기준년월일" = \'20260811\'' in routed


def _merchant_month_sql(reference_month: str) -> str:
    """기업카드 미보유 영업대상 VQ 의 tmdaaus01 축소 CTE 를 그대로 옮긴 것."""
    return f"""
    WITH merchant_ranked AS (
      SELECT a."기준년월", a."기준년월일", a."가맹점번호", a."가맹점사업주체구분코드",
             a."유효기업신용카드수", a."유효기업체크카드수",
             ROW_NUMBER() OVER (
               PARTITION BY a."기준년월", a."가맹점번호"
               ORDER BY a."기준년월일" DESC
             ) AS rn
      FROM card_system.tmdaaus01 a
      WHERE a."기준년월" = '{reference_month}'
    )
    SELECT r."가맹점번호" FROM merchant_ranked r WHERE r.rn = 1
    """


def test_current_month_monthly_snapshot_reads_its_daily_twin() -> None:
    """월말요약은 달이 닫혀야 적재된다. 이번 달은 일별 짝의 최신 가용일이 유일한 원천이다."""
    sql = _merchant_month_sql("202608")

    with frozen_clock(), latest_loaded_day("20260810"):
        routed = workflow._apply_accumulation_historical_sources(
            "월 매출액 1억원 이상 법인사업자 중 기업카드 미보유 명단",
            sql,
        )
        # 적재 계약 검증도 통과해야 실행까지 간다.
        assert workflow._availability_policy_issues("기업카드 미보유 명단", routed, []) == []

    assert "card_system.tmdaaus01" not in routed
    assert "FROM card_system.tbdaaus01 tbdaaus01" in routed
    # 시계의 D-1(20260811)이 아니라 실제 적재된 최신일로 고정한다.
    assert 'tbdaaus01."기준년월일" = \'20260810\'' in routed
    # 감싸는 방식이라 호출부가 쓰던 축(SELECT·PARTITION BY·WHERE)은 그대로 남는다.
    assert 'SUBSTR(tbdaaus01."기준년월일", 1, 6) AS "기준년월"' in routed
    assert 'a."기준년월" = \'202608\'' in routed
    assert 'PARTITION BY a."기준년월", a."가맹점번호"' in routed


@pytest.mark.parametrize(
    ("label", "reference_month"),
    [("직전월", "202607"), ("작년 같은 달", "202508")],
)
def test_closed_month_keeps_the_monthly_snapshot(label: str, reference_month: str) -> None:
    sql = _merchant_month_sql(reference_month)

    with frozen_clock(), latest_loaded_day(FROZEN_PREVIOUS_DAY):
        routed = workflow._apply_accumulation_historical_sources("가맹점 명단", sql)

    assert routed == sql, label


def test_month_range_spanning_closed_months_keeps_the_monthly_snapshot() -> None:
    """이번 달이 섞였다고 범위 전체를 하루짜리 스냅샷으로 바꾸면 안 된다."""
    sql = """
    SELECT a."가맹점번호"
    FROM card_system.tmdaaus01 a
    WHERE a."기준년월" BETWEEN '202601' AND '202608'
    """

    with frozen_clock(), latest_loaded_day(FROZEN_PREVIOUS_DAY):
        assert workflow._apply_accumulation_historical_sources("가맹점 명단", sql) == sql


def test_daily_twin_without_the_open_month_keeps_the_monthly_snapshot() -> None:
    """월초나 적재 지연으로 짝의 최신일이 아직 지난 달이면 돌릴 곳이 없다."""
    sql = _merchant_month_sql("202608")

    with frozen_clock(date(2026, 8, 1)), latest_loaded_day("20260731"):
        assert workflow._apply_accumulation_historical_sources("가맹점 명단", sql) == sql

    # 적재 범위를 못 읽으면 시계의 D-1 로 되돌아간다. 8/12 의 D-1 은 이번 달이다.
    with frozen_clock(), no_period_range():
        assert "card_system.tbdaaus01" in workflow._apply_accumulation_historical_sources(
            "가맹점 명단", sql
        )


def test_column_the_daily_twin_lacks_keeps_the_monthly_snapshot() -> None:
    """빈 결과를 컬럼 오류로 바꾸면 안 된다. 짝이 못 가진 컬럼을 읽으면 돌리지 않는다."""
    sql = """
    SELECT c."고객식별자", c."소호로지스틱BSS등급구분코드"
    FROM card_system.tmdaa1d12 c
    WHERE c."기준년월" = '202608'
    """

    with frozen_clock(), latest_loaded_day(FROZEN_PREVIOUS_DAY):
        assert workflow._apply_accumulation_historical_sources("기업고객 현황", sql) == sql
        # 같은 질문이라도 짝이 가진 컬럼만 읽으면 이번 달은 일별로 간다.
        assert "card_system.tbdaa1d12" in workflow._apply_accumulation_historical_sources(
            "기업고객 현황",
            sql.replace(', c."소호로지스틱BSS등급구분코드"', ""),
        )


def test_live_source_pairs_come_from_the_declared_historical_source() -> None:
    assert time_policy.live_source_for("tmdaaus01") == {
        "table": "tbdaaus01",
        "query_time_dimension": "기준년월일",
        "format": "YYYYMMDD",
    }
    assert time_policy.live_source_for("tmdaa1d12")["table"] == "tbdaa1d12"
    # 짝이 아닌 월별 테이블은 돌릴 곳이 없다. tbdaadt01 의 과거 원천이지만
    # 이름 접미사가 달라 같은 grain 의 일별 짝이 아니다.
    assert time_policy.live_source_for("tmdaa5d01") is None
    assert time_policy.live_source_for("tmdaa5e11") is None
    assert time_policy.live_source_for("tbdaaus01") is None


def test_archive_pairs_are_derived_from_one_policy_declaration() -> None:
    assert workflow._PREVIOUS_DAY_ARCHIVE_TABLES == {
        "tbdaaus01": "tmdaaus01",
        "tbdaa1d12": "tmdaa1d12",
    }
    assert {
        time_policy.live_source_for(monthly)["table"]
        for monthly in workflow._PREVIOUS_DAY_ARCHIVE_TABLES.values()
    } == set(workflow._PREVIOUS_DAY_ARCHIVE_TABLES)


def test_pure_historical_corporate_customer_sql_routes_to_monthly_source() -> None:
    sql = """
    SELECT a."고객식별자", a."기업명"
    FROM card_system.tbdaa1d12 a
    WHERE a."기준년월일" BETWEEN '20251201' AND '20251231'
    """

    routed = workflow._apply_accumulation_historical_sources(
        "2025년 12월 기업고객 현황",
        sql,
    )

    assert "FROM card_system.tmdaa1d12 a" in routed
    assert "FROM card_system.tbdaa1d12 a" not in routed
    assert 'a."기준년월" = \'202512\'' in routed


def test_mixed_current_and_history_routes_only_the_historical_occurrence() -> None:
    # '20260811' 이 "현재 스냅샷"으로 인식되려면 그 날이 D-1 이어야 한다.
    sql = f"""
    WITH current_snapshot AS (
      SELECT c."고객식별자"
      FROM card_system.tbdaa1d12 c
      WHERE c."기준년월일" = '{FROZEN_PREVIOUS_DAY}'
    ),
    historical_usage AS (
      SELECT h."고객식별자", h."금월신용카드이용금액"
      FROM card_system.tbdaa1d12 h
      WHERE SUBSTR(h."기준년월일", 1, 6) BETWEEN '202501' AND '202506'
    )
    SELECT c."고객식별자", SUM(h."금월신용카드이용금액")
    FROM current_snapshot c
    JOIN historical_usage h ON h."고객식별자" = c."고객식별자"
    GROUP BY c."고객식별자"
    """

    with frozen_clock():
        routed = workflow._apply_accumulation_historical_sources(
            "현재 기업고객의 2025년 상반기 이용금액",
            sql,
        )

    assert "FROM card_system.tbdaa1d12 c" in routed
    assert f'c."기준년월일" = \'{FROZEN_PREVIOUS_DAY}\'' in routed
    assert "FROM card_system.tmdaa1d12 h" in routed
    assert 'h."기준년월" BETWEEN \'202501\' AND \'202506\'' in routed
    assert 'SUBSTR(h."기준년월일"' not in routed


def test_kst_previous_day_and_recent_ten_day_bounds_are_inclusive() -> None:
    today = date(2026, 8, 12)
    assert previous_day_ymd(today) == "20260811"
    assert recent_window_ymd(today=today) == ("20260803", "20260812")


def test_previous_day_tables_expose_d_minus_one_without_reference_month() -> None:
    """적재 범위를 못 읽으면 최신 가용일은 KST 전일로 돌아간다."""
    with frozen_clock(), no_period_range():
        instruction = workflow._accumulation_policy_instruction(["tbdaa1d12"])

    assert f"최신 가용일은 `{FROZEN_PREVIOUS_DAY}`" in instruction
    assert "물리 `기준년월` 컬럼은 없습니다" in instruction


def test_table_details_exposes_accumulation_policy_to_sql_generation() -> None:
    details = workflow._table_details(["tbdaadt01"], "최근 가맹점 현황")
    assert "accumulation_policy: 매일" in details
    assert "실적기준년월일" in details
    assert "tmdaa5d01.기준년월(YYYYMM)" in details
    # 적재 주기를 조회 가능 범위로 바꿔 말하지 않는다. 마스터에는 시간 grain 이 없다.
    assert "최근 10일만 조회 가능" not in details


def test_semantic_build_preserves_recent_daily_archive_metadata() -> None:
    rebuilt = deepcopy(schema.SCHEMA)
    assert apply_accumulation_policies(rebuilt) == 21

    table = next(item for item in rebuilt["tables"] if item["name"] == "tbdaadt01")
    assert table["accumulation_policy"]["historical_source"] == {
        "table": "tmdaa5d01",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    }
    table["accumulation_policy"]["historical_source"]["table"] = "changed"
    assert TABLE_ACCUMULATION_POLICIES["tbdaadt01"]["historical_source"]["table"] == "tmdaa5d01"


@pytest.mark.parametrize(
    "vq_name",
    [
        "brand_active_merchant_count",
        "merchant_detail_by_name",
        "merchant_payment_institution_list",
        "merchant_count_by_payment_institution",
    ],
)
def test_historical_month_routes_recent_merchant_vqs_to_monthly_archive(vq_name: str) -> None:
    query = next(item for item in schema.VERIFIED_QUERIES if item["name"] == vq_name)

    with patch.object(workflow, "kst_today", return_value=date(2026, 8, 12)):
        sql = workflow._apply_tbdaadt01_historical_source(
            "2025년 12월 도미노 가맹점 조회",
            query["sql"],
        )

    assert "tmdaa5d01" in sql
    assert "tbdaadt01" not in sql
    assert 'm."기준년월" = \'202512\'' in sql
    assert "실적기준년월일" not in sql
    assert "CURRENT_TIMESTAMP" not in sql
    assert not schema._validate_sql_against_schema(
        sql,
        sorted(workflow._extract_schema_tables(sql)),
    )


def test_explicit_daily_and_month_range_choose_the_declared_merchant_source() -> None:
    query = next(
        item for item in schema.VERIFIED_QUERIES if item["name"] == "brand_active_merchant_count"
    )
    with patch.object(workflow, "kst_today", return_value=date(2026, 8, 12)):
        recent_day = workflow._apply_tbdaadt01_historical_source(
            "2026년 8월 5일 도미노 가맹점 수",
            query["sql"],
        )
        old_day = workflow._apply_tbdaadt01_historical_source(
            "2026년 7월 1일 도미노 가맹점 수",
            query["sql"],
        )
        month_range = workflow._apply_tbdaadt01_historical_source(
            "2025년 10월부터 12월까지 도미노 가맹점 수",
            query["sql"],
        )

    assert "tbdaadt01" in recent_day
    assert 'm."실적기준년월일" = \'20260805\'' in recent_day
    assert "tmdaa5d01" in old_day
    assert 'm."기준년월" = \'202607\'' in old_day
    assert "tmdaa5d01" in month_range
    assert 'm."기준년월" BETWEEN \'202510\' AND \'202512\'' in month_range


MASTER_ADDRESS_QUESTION = "한신포차 가맹점주 가맹점 도로명 주소 2026년 5월 기준으로 알려줘"

# 주소 본문(가맹점상세주소)은 민감 컬럼이라 조회 대상이 아니다. 답할 수 있는 것은
# 표기 구분과 우편번호·도로명 번호 체계까지다.
MASTER_ADDRESS_SQL = """
SELECT m."가맹점번호", m."가맹점명", m."주소표시구분코드", m."우편번호", m."도로명건물본번호"
FROM card_system.tbdaadt01 m
WHERE LOWER(m."가맹점명") LIKE LOWER('%한신포차%')
"""


@contextlib.contextmanager
def loaded_window(start: str, end: str):
    """적재된 구간 전체를 고정한다. 요청한 달이 그 밖인지 판단할 때 쓴다."""
    with patch.object(workflow, "loaded_period_range", return_value=(start, end)):
        yield


def test_generated_snapshot_sql_is_restored_to_the_master_with_its_own_time_column() -> None:
    """선택은 tbdaadt01 인데 SQL 은 tmdaa5d01 로 나오면 검증에서 끊긴다.

    스냅샷에는 "기준년월일" 이 없어서 스키마 검증이 "없는 컬럼" 으로 막았다.
    되돌릴 때 시간축도 마스터의 실적기준년월일로 바꿔야 한다.
    """
    sql = (
        'SELECT m."가맹점번호", m."가맹점명", m."주소표시구분코드"\n'
        "FROM card_system.tmdaa5d01 m\n"
        "WHERE m.\"기준년월일\" = '20260531' "
        "AND LOWER(m.\"가맹점명\") LIKE LOWER('%한신포차%')"
    )

    with frozen_clock(), latest_loaded_day("20260819"):
        routed = workflow._apply_tbdaadt01_historical_source(MASTER_ADDRESS_QUESTION, sql)

    assert "tmdaa5d01" not in routed
    assert "card_system.tbdaadt01" in routed
    assert "기준년월일\" = '20260531'" not in routed
    assert '"실적기준년월일" = \'20260819\'' in routed
    assert not schema._validate_sql_against_schema(routed, ["tbdaadt01"])


def test_snapshot_sql_joined_to_a_monthly_fact_is_left_alone() -> None:
    """마스터에는 기준년월이 없다. 조인을 끊으면서까지 되돌리지 않는다."""
    sql = (
        'SELECT m."가맹점번호"\n'
        "FROM card_system.tmdaa5d01 m "
        'JOIN card_system.tmdaa5e11 f ON m."가맹점번호" = f."가맹점번호" '
        'AND m."기준년월" = f."기준년월"\n'
        "WHERE m.\"기준년월\" = '202605'"
    )

    with frozen_clock(), latest_loaded_day("20260819"):
        routed = workflow._apply_tbdaadt01_historical_source(MASTER_ADDRESS_QUESTION, sql)

    assert routed == sql


def test_llm_chosen_month_snapshot_is_routed_back_to_the_merchant_master(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """규칙 랭킹이 마스터를 1순위로 올려도 analyze_question 은 LLM 목록을 그대로 쓴다.

    "2026년 5월" 이 붙으면 LLM 은 월 스냅샷을 고르고, 치환이 마스터→스냅샷
    한 방향뿐이던 동안에는 그 선택을 되돌릴 곳이 없었다.
    """
    monkeypatch.setattr(workflow, "_call_llm", lambda *_args, **_kwargs: "tmdaa5d01, tmdaaus01")

    with frozen_clock():
        result = workflow.analyze_question(
            {
                "question": MASTER_ADDRESS_QUESTION,
                "selected_domain": "merchant_sales",
                "domain_context": "테스트 도메인",
                "domain_routing_trace": "테스트 라우팅",
            }
        )

    assert result["selected_tables"][0] == "tbdaadt01"
    assert "tmdaa5d01" not in result["selected_tables"]


def test_month_metric_question_still_routes_the_master_to_its_snapshot() -> None:
    """반대 방향 치환이 생겨도 월 지표 질문의 기존 라우팅은 그대로다."""
    with frozen_clock():
        assert workflow._route_accumulation_table_names(
            "2026년 5월 가맹점 수를 알려줘", ["tbdaadt01"]
        ) == ["tmdaa5d01"]


def test_master_attribute_question_keeps_the_merchant_master_for_a_past_month() -> None:
    """주소는 마스터 한 곳만 가리키는 속성이다. 월 스냅샷으로 돌리면 답할 곳이 없다.

    질문이 남긴 단서가 "가맹점주"(merchant_owner) 뿐이던 동안에는 마스터·일별·월별
    스냅샷 4개가 동점이었고, 월 라우팅이 그중 마스터만 걷어내 tmdaa5d01 이 1순위였다.
    """
    with frozen_clock():
        selected = workflow._rule_rank_tables(MASTER_ADDRESS_QUESTION)

    assert selected[0] == "tbdaadt01"


def test_master_attribute_sql_is_pinned_to_the_latest_loaded_day() -> None:
    """마스터는 보관일수만큼 가맹점당 행이 늘어난다. 최신 가용일 1건으로 좁힌다."""
    with frozen_clock(), loaded_window("20260803", "20260812"):
        routed = workflow._apply_tbdaadt01_historical_source(
            MASTER_ADDRESS_QUESTION, MASTER_ADDRESS_SQL
        )

    assert "tmdaa5d01" not in routed
    assert 'm."실적기준년월일" = \'20260812\'' in routed
    assert not schema._validate_sql_against_schema(
        routed, sorted(workflow._extract_schema_tables(routed))
    )


def test_master_attribute_answer_says_the_requested_month_is_missing() -> None:
    with frozen_clock(), loaded_window("20260803", "20260812"):
        note = workflow._implicit_time_basis_note(
            MASTER_ADDRESS_QUESTION, MASTER_ADDRESS_SQL
        )

    assert "202605 데이터는 없습니다" in note
    assert "20260803 ~ 20260812" in note
    assert "20260812 기준으로 조회했습니다" in note


def test_master_attribute_answer_does_not_claim_a_gap_it_did_not_read() -> None:
    """적재 범위를 못 읽었으면 "없다" 고 단정하지 않는다."""
    with frozen_clock(), no_period_range():
        note = workflow._implicit_time_basis_note(
            MASTER_ADDRESS_QUESTION, MASTER_ADDRESS_SQL
        )

    assert "없을 수 있습니다" in note
    assert "데이터는 없습니다" not in note


def test_counting_the_same_past_month_still_routes_to_the_monthly_snapshot() -> None:
    """마스터 예외는 속성 조회에만 준다. 그 달의 모수를 세는 질문은 스냅샷이 답한다."""
    with frozen_clock():
        selected = workflow._rule_rank_tables("2026년 5월 한신포차 가맹점 수")

    assert "tmdaa5d01" in selected
    assert "tbdaadt01" not in selected


def test_historical_vq_capability_is_routed_before_parameter_application() -> None:
    question = "2025년 12월 도미노 브랜드 가맹점 수 몇 개야"
    with patch.object(workflow, "kst_today", return_value=date(2026, 8, 12)):
        selected = workflow._select_verified_query_capability(
            question,
            {"question": question, "selected_domain": "merchant_sales"},
        )

    assert selected["matched_query_name"] == "brand_active_merchant_count"
    assert "tmdaa5d01" in selected["matched_query_sql"]
    assert '"기준년월" = \'202512\'' in selected["matched_query_sql"]
    assert "CURRENT_TIMESTAMP" not in selected["matched_query_sql"]


@pytest.mark.parametrize(
    "question",
    [
        "현재 도미노 브랜드 가맹점 수 몇 개야",
        "도미노 브랜드 가맹점 수 몇 개야",
    ],
)
def test_rule_rank_keeps_recent_merchant_source_without_historical_period(
    question: str,
) -> None:
    selected = workflow._rule_rank_tables(question)

    assert "tbdaadt01" in selected


def test_rule_rank_routes_authoritative_historical_merchant_vq_to_archive() -> None:
    selected = workflow._rule_rank_tables("2025년 12월 도미노 브랜드 가맹점 수 몇 개야")

    assert "tmdaa5d01" in selected
    assert "tbdaadt01" not in selected


def test_rule_rank_routes_historical_semantic_composition_to_archive() -> None:
    selected = workflow._rule_rank_tables("2025년 12월 가맹점상태구분코드별 가맹점 구성")

    assert "tmdaa5d01" in selected
    assert "tbdaadt01" not in selected


@pytest.mark.parametrize(
    "runner",
    [workflow.run_matched_query, workflow.run_tool_query, workflow.run_query],
)
def test_all_sql_execution_paths_route_historical_merchant_source(runner) -> None:
    query = next(
        item for item in schema.VERIFIED_QUERIES if item["name"] == "brand_active_merchant_count"
    )
    state = {
        "question": "2025년 12월 도미노 브랜드 가맹점 수",
        "final_sql": query["sql"],
    }
    with patch.object(workflow, "execute_sql", return_value=(["가맹점수"], [(1,)], None)) as execute:
        result = runner(state)

    executed_sql = execute.call_args.args[0]
    assert not result.get("query_error")
    assert "tmdaa5d01" in executed_sql
    assert '"기준년월" = \'202512\'' in executed_sql
    assert "tbdaadt01" not in executed_sql


def test_generated_sql_is_routed_before_availability_validation() -> None:
    sql = """
    SELECT COUNT(DISTINCT m."가맹점번호") AS "가맹점수"
    FROM card_system.tbdaadt01 m
    WHERE SUBSTR(m."실적기준년월일", 1, 6) = '202512'
      AND LOWER(m."가맹점명") LIKE LOWER('%도미노%')
    """
    state = {
        "question": "2025년 12월 도미노 브랜드 가맹점 수",
        "generated_sql": sql,
        "selected_tables": ["tbdaadt01"],
        "retry_count": 0,
    }
    with patch.object(workflow, "_call_llm", return_value="VALID"):
        result = workflow.validate_sql(state)

    assert result["is_valid"] is True
    assert "tmdaa5d01" in result["final_sql"]
    assert '"기준년월" = \'202512\'' in result["final_sql"]
    assert "tbdaadt01" not in result["final_sql"]


def test_merchant_card_possession_builder_uses_monthly_archive_for_historical_month() -> None:
    sql = _vq_sql_가맹점카드소지현황(
        {
            "기준년월": "202512",
            "카드만료기준일": "20251231",
            "가맹점명": "도미노",
        }
    )

    routed = workflow._apply_tbdaadt01_historical_source(
        "2025년 12월 도미노 가맹점 카드 소지 현황",
        sql,
    )

    assert "FROM tmdaa5d01 a" in routed
    assert 'a."기준년월" = \'202512\'' in routed
    assert "FROM tbdaadt01 a" not in routed


def test_historical_merchant_join_routes_with_matching_archive_month() -> None:
    sql = """
    SELECT m."가맹점번호", SUM(f."가맹점일시불매출금액")
    FROM card_system.tbdaadt01 m
    JOIN card_system.tmdaa5e11 f
      ON f."가맹점번호" = m."가맹점번호"
    WHERE m."실적기준년월일" BETWEEN '20251001' AND '20251231'
      AND f."기준년월" BETWEEN '202510' AND '202512'
    GROUP BY m."가맹점번호"
    """

    routed = workflow._apply_tbdaadt01_historical_source(
        "2025년 10월부터 12월까지 가맹점 매출",
        sql,
    )

    compact = "".join(routed.replace('"', "").split())
    assert "tmdaa5d01" in routed
    assert (
        "f.기준년월=m.기준년월" in compact
        or "m.기준년월=f.기준년월" in compact
    )


@pytest.mark.parametrize("include_month_join", [False, True])
def test_multi_month_monthly_join_requires_matching_load_month(
    include_month_join: bool,
) -> None:
    month_join = 'AND f."기준년월" = m."기준년월"' if include_month_join else ""
    sql = f"""
    SELECT m."가맹점번호", SUM(f."가맹점일시불매출금액")
    FROM card_system.tmdaa5d01 m
    JOIN card_system.tmdaa5e11 f
      ON f."가맹점번호" = m."가맹점번호"
      {month_join}
    WHERE m."기준년월" BETWEEN '202510' AND '202512'
      AND f."기준년월" BETWEEN '202510' AND '202512'
    GROUP BY m."가맹점번호"
    """

    issues = workflow._availability_policy_issues(
        "2025년 10월부터 12월까지 가맹점 매출",
        sql,
    )

    if include_month_join:
        assert not issues
    else:
        assert any("기준년월" in issue for issue in issues)


def test_policy_validator_blocks_a_previous_day_table_queried_by_month() -> None:
    bad_month_sql = 'SELECT a."기준년월" FROM card_system.tbdaa1d12 a'

    month_issues = workflow._availability_policy_issues("현재 기업 현황", bad_month_sql)

    assert any('물리 "기준년월" 컬럼이 없습니다' in issue for issue in month_issues)


def test_merchant_master_does_not_require_a_load_window_filter() -> None:
    """적재가 최근 N일 단위인 것과 조회에 그 창을 걸어야 하는 것은 다른 이야기다.

    tbdaadt01 에 available_days 가 있던 동안에는 "도미노피자 가맹점 기본 정보"
    같은 마스터 조회까지 실적기준년월일 기간 조건을 강요당했다.
    """
    master_sql = (
        'SELECT COUNT(*) FROM card_system.tbdaadt01 m '
        'WHERE m."가맹점상태구분코드" = \'1\''
    )

    assert workflow._availability_policy_issues("현재 가맹점 수", master_sql) == []
    assert not TABLE_ACCUMULATION_POLICIES["tbdaadt01"].get("available_days")


def test_verified_query_execution_checks_accumulation_policy_before_database() -> None:
    """적재 축 위반은 DB 를 때리기 전에 잡는다.

    예시는 팩트 테이블로 든다. 마스터(tbdaadt01)는 시간 grain 이 없어 기간 조건을
    요구하지 않으므로 이 검사의 예시가 될 수 없다.
    """
    state = {
        "question": "2026년 7월 가맹점 매출",
        "final_sql": 'SELECT SUM(a."가맹점일시불매출금액") FROM card_system.tmdaa5e11 a',
    }
    with patch.object(workflow, "execute_sql") as execute:
        result = workflow.run_matched_query(state)

    assert "적재 주기 위반" in result["query_error"]
    execute.assert_not_called()


def _current_snapshot_sql(day: str) -> str:
    return (
        'SELECT a."고객식별자" FROM card_system.tbdaa1d12 a '
        f"WHERE a.\"기준년월일\" = '{day}'"
    )


def test_blocking_follows_the_loaded_max_not_the_clock() -> None:
    """적재가 밀리면 KST 전일에는 데이터가 없다. 그날로 고정한 SQL을 통과시키면 안 된다."""
    stale_max = "20260810"

    with frozen_clock(), latest_loaded_day(stale_max):
        # 시계의 D-1 은 적재된 최신일보다 뒤라 막힌다.
        clock_issues = workflow._availability_policy_issues(
            "현재 기업고객 현황", _current_snapshot_sql(FROZEN_PREVIOUS_DAY)
        )
        # 실제 적재된 최신일은 통과한다.
        loaded_issues = workflow._availability_policy_issues(
            "현재 기업고객 현황", _current_snapshot_sql(stale_max)
        )

    assert any(f"최신 가용일은 {stale_max}" in issue for issue in clock_issues)
    assert loaded_issues == []


def test_blocking_falls_back_to_the_clock_when_the_range_is_unreadable() -> None:
    with frozen_clock(), no_period_range():
        assert (
            workflow._availability_policy_issues(
                "현재 기업고객 현황", _current_snapshot_sql(FROZEN_PREVIOUS_DAY)
            )
            == []
        )


def test_generation_and_params_target_the_same_day_the_guard_accepts() -> None:
    """프롬프트·파라미터가 시계를 보고 가드가 데이터를 보면 통과할 수 없는 SQL만 나온다."""
    stale_max = "20260810"
    specs = [{"name": "기준년월일", "type": "string"}]

    with frozen_clock(), latest_loaded_day(stale_max):
        instruction = workflow._accumulation_policy_instruction(["tbdaa1d12"])
        params = workflow._extract_params_by_rule("현재 현황", specs, ["tbdaa1d12"])
        basis = workflow._implicit_time_basis_note("현재 현황", _current_snapshot_sql(stale_max))

    assert f"최신 가용일은 `{stale_max}`" in instruction
    assert params == {"기준년월일": stale_max}
    assert stale_max in basis


@contextlib.contextmanager
def stub_period_range(min_value: str, max_value: str):
    """적재 범위 조회만 가로챈다. 캐시는 테스트끼리 새지 않게 비우고 시작한다."""
    issued: list[str] = []

    def execute(sql: str, **_kwargs: object):
        issued.append(" ".join(sql.split()))
        return ["min", "max"], [(min_value, max_value)], None

    db._PERIOD_RANGE_CACHE.clear()
    with patch.object(db, "execute_sql", side_effect=execute):
        yield issued
    db._PERIOD_RANGE_CACHE.clear()


def test_no_data_answer_reports_the_range_read_from_the_table() -> None:
    """"왜 없는지"는 적재 정책표가 아니라 테이블의 MIN/MAX 가 답한다."""
    with stub_period_range("20240101", "20260810") as issued:
        answer = workflow.generate_answer(
            {
                "question": "2026년 8월 기업카드 이용금액 알려줘",
                "final_sql": 'SELECT 1 FROM card_system.tmdaa5e11 a',
                "query_columns": ["x"],
                "query_rows": [],
            }
        )

    assert issued == ['SELECT MIN("기준년월"), MAX("기준년월") FROM card_system.tmdaa5e11']
    assert answer["answer"].startswith("해당 데이터가 없습니다.")
    assert 'tmdaa5e11 조회 가능 기간: "기준년월" 20240101 ~ 20260810' in answer["answer"]


def test_loaded_period_range_is_read_once_per_table() -> None:
    """MIN/MAX 는 해당 컬럼을 훑는다. 0건 답변마다 다시 때리면 안 된다."""
    with stub_period_range("202401", "202607") as issued:
        assert db.loaded_period_range("tmdaa5e11") == ("202401", "202607")
        assert db.loaded_period_range("tmdaa5e11") == ("202401", "202607")

    assert len(issued) == 1


def test_period_range_is_not_probed_for_ungoverned_tables() -> None:
    with stub_period_range("202401", "202607") as issued:
        assert db.loaded_period_range("tbdaaac67") is None

    assert issued == []


def test_out_of_range_period_answers_no_data_with_the_loaded_range() -> None:
    with stub_period_range("202401", "202607") as issued:
        handled = workflow.handle_error(
            {
                "validation_result": (
                    "SQL 적재 주기 위반: tmdaa5e11의 최신 가용일은 KST 전일 20260813입니다."
                ),
                "query_error": "테이블 적재 주기 위반: 최신 가용일은 KST 전일 20260813입니다.",
                "final_sql": "SELECT 1 FROM card_system.tmdaa5e11 a",
                "retry_count": 3,
            }
        )

    assert issued
    assert 'tmdaa5e11 조회 가능 기간: "기준년월" 202401 ~ 202607' in handled["answer"]
    assert "20260813" not in handled["answer"]
    assert handled["error_message"] == ""
    assert handled["query_error"] == ""


def test_out_of_range_period_answers_no_data_instead_of_sql_failure() -> None:
    """적재 범위 밖 기간은 SQL 실패가 아니라 "데이터가 없다"로 알린다.

    SQL 을 못 만든 것과 SQL 은 맞는데 그 기간이 적재돼 있지 않은 것은 사용자가
    해야 할 일이 다르다. 앞의 것은 질문을 고쳐야 하고, 뒤의 것은 고쳐도 안 나온다.

    DB 를 못 읽으면(여기서는 관리 테이블이 없는 SQL) 검증 메시지로 되돌아간다.
    """
    handled = workflow.handle_error(
        {
            "validation_result": (
                "SQL 적재 주기 위반: tbdaabt30의 조회 가능 범위는 KST 20260807~20260813입니다. "
                "범위 밖 일자는 조회할 수 없습니다."
            ),
            "query_error": "테이블 적재 주기 위반: 범위 밖 일자는 조회할 수 없습니다.",
            "final_sql": "SELECT 1",
            "retry_count": 3,
        }
    )

    assert handled["answer"].startswith("해당 데이터가 없습니다.")
    assert "조회 가능 범위는 KST 20260807~20260813입니다" in handled["answer"]
    # error 가 비어야 UI 가 "SQL 처리 실패" 대신 답변으로 렌더한다.
    assert handled["error_message"] == ""
    assert handled["query_error"] == ""
    assert web_service._public_result_error(handled["error_message"]) == ""


def test_unfixable_sql_still_reports_a_sql_failure() -> None:
    handled = workflow.handle_error(
        {
            "validation_result": "COLUMN_NOT_FOUND: 스키마에 없는 컬럼입니다.",
            "query_error": "COLUMN_NOT_FOUND",
            "final_sql": "SELECT 없는컬럼 FROM card_system.tmdaa5e11",
            "retry_count": 3,
        }
    )

    assert handled["error_message"].startswith("SQL 생성/실행에 실패했습니다")
    assert "해당 데이터가 없습니다" not in handled["answer"]


def test_continuation_parser_understands_previous_day() -> None:
    parsed = web_service._natural_params_by_rule(
        "전일",
        [{"name": "기준년월일", "type": "string"}],
    )
    assert parsed == {"기준년월일": previous_day_ymd()}


def test_current_date_parameter_uses_previous_day_for_previous_day_sources() -> None:
    specs = [{"name": "기준년월일", "type": "string"}]

    with frozen_clock():
        # 전일 적재 테이블은 D-1, 그 외는 당일.
        assert workflow._extract_params_by_rule("현재 현황", specs, ["tbdaaus01"]) == {
            "기준년월일": FROZEN_PREVIOUS_DAY
        }
        assert workflow._extract_params_by_rule("현재 현황", specs, ["tddaa3d01"]) == {
            "기준년월일": FROZEN_TODAY_YMD
        }


def test_continuation_current_date_uses_selected_table_cadence() -> None:
    missing = [{"name": "기준년월일", "type": "string"}]

    assert web_service._natural_params_by_rule("현재", missing, ["tbdaa1d12"]) == {
        "기준년월일": previous_day_ymd()
    }
    assert web_service._natural_params_by_rule("현재", missing, ["tddaa3d01"]) == {
        "기준년월일": workflow.kst_today().strftime("%Y%m%d")
    }


def test_cross_cycle_fallback_is_allowed_for_history_but_not_current_snapshot() -> None:
    sql = 'SELECT a."기준년월일" FROM card_system.tbdaaus01 a'

    assert workflow._allow_cross_cycle_fallback("2025년 12월 가맹점 현황", sql) is True
    assert workflow._allow_cross_cycle_fallback("최근 6개월 폐업 가맹점", sql) is True
    assert workflow._allow_cross_cycle_fallback("현재 가맹점 현황", sql) is False
    assert workflow._allow_cross_cycle_fallback("전일 가맹점 현황", sql) is False
    assert workflow._allow_cross_cycle_fallback("어제 가맹점 현황", sql) is False


def test_previous_month_metric_name_does_not_turn_d_minus_one_vq_into_history() -> None:
    exact_snapshot_sql = """
    SELECT SUM(a."전월일시불매입금액")
    FROM card_system.tbdaaus01 a
    WHERE a."기준년월일" = DATE_FORMAT(
      DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'),
      '%Y%m%d'
    )
    """
    historical_range_sql = """
    SELECT COUNT(*)
    FROM card_system.tbdaaus01 a
    WHERE a."기준년월일" BETWEEN '20250101' AND '20251231'
      AND a."기준년월일" <= DATE_FORMAT(
        DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'),
        '%Y%m%d'
      )
    """

    assert workflow._allow_cross_cycle_fallback(
        "지역별 기업영업 가맹점 전월 매입 상위 지역", exact_snapshot_sql
    ) is False
    assert workflow._allow_cross_cycle_fallback(
        "2025년 가맹점 전월 매입 추이", historical_range_sql
    ) is True


def test_semantic_metrics_contracts_and_references_use_kst_previous_day() -> None:
    _assert_previous_day_semantics(schema.SCHEMA)


def test_semantic_build_reapplies_previous_day_overrides() -> None:
    rebuilt = deepcopy(schema.SCHEMA)
    metrics = {item["name"]: item for item in rebuilt["canonical_metrics"]}
    contracts = {item["name"]: item for item in rebuilt["semantic_query_contracts"]}
    references = {item["intent"]: item for item in rebuilt["query_references"]}

    metrics["브랜드가맹점주수"]["required_filters"][0] = "MAX(기준년월일)"
    metrics["기업카드소지가맹점주수"]["required_filters"][0] = "MAX(기준년월일)"
    metrics["최근기간폐업가맹점수"]["expression"] = "CURRENT_DATE"
    contracts["brand_owner_corporate_card_count"]["time_policy"]["snapshot"] = "MAX(기준년월일)"
    contracts["recent_closed_brand_merchant_count"]["time_policy"]["range"] = "CURRENT_DATE"
    references["최근기간_브랜드가맹점_폐업수"]["rules"][0] = "CURRENT_DATE"
    references["브랜드가맹점주_기업카드보유인원"]["rules"][2] = "최신 기준년월일"

    assert apply_previous_day_semantic_overrides(rebuilt) == 7
    _assert_previous_day_semantics(rebuilt)


@pytest.mark.parametrize(
    ("sql", "should_pass"),
    [
        (
            """
            SELECT '202601' AS "요청기준년월", COUNT(*)
            FROM card_system.tmdaa3e16 m
            WHERE m."최종심사년월일" BETWEEN '20260101' AND '20260131'
            """,
            False,
        ),
        (
            """
            SELECT COUNT(*)
            FROM card_system.tmdaa3e16 m
            WHERE m."기준년월" = '202601'
            """,
            True,
        ),
    ],
)
def test_monthly_explicit_period_must_filter_declared_load_month(
    sql: str,
    should_pass: bool,
) -> None:
    issues = workflow._availability_policy_issues("2026년 1월 심사 현황", sql)

    if should_pass:
        assert not issues
    else:
        assert any("기준년월" in issue for issue in issues)


@pytest.mark.parametrize(
    ("sql", "should_pass"),
    [
        (
            """
            SELECT '2025' AS "요청기준년", COUNT(*)
            FROM card_system.tbmaisd06 d
            WHERE d."특수채권편입기준년월일" BETWEEN '20250101' AND '20251231'
            """,
            False,
        ),
        (
            """
            SELECT COUNT(*)
            FROM card_system.tbmaisd06 d
            WHERE d."기준년" = '2025'
            """,
            True,
        ),
    ],
)
def test_yearly_explicit_period_must_filter_declared_load_year(
    sql: str,
    should_pass: bool,
) -> None:
    issues = workflow._availability_policy_issues("2025년 특수채권 현황", sql)

    if should_pass:
        assert not issues
    else:
        assert any("기준년" in issue for issue in issues)


@pytest.mark.parametrize(
    ("sql", "should_pass"),
    [
        (
            """
            SELECT COUNT(*)
            FROM card_system.tbdaaat03 s
            WHERE s."전표매출년월일" = '20260805'
            """,
            False,
        ),
        (
            """
            SELECT COUNT(*)
            FROM card_system.tbdaaat03 s
            WHERE s."실적기준년월일" = '20260805'
            """,
            True,
        ),
    ],
)
def test_daily_explicit_day_must_filter_declared_load_day(
    sql: str,
    should_pass: bool,
) -> None:
    issues = workflow._availability_policy_issues("2026년 8월 5일 매출 현황", sql)

    if should_pass:
        assert not issues
    else:
        assert any("실적기준년월일" in issue for issue in issues)


def test_previous_day_default_question_requires_exact_kst_d_minus_one() -> None:
    sql = 'SELECT COUNT(*) FROM card_system.tbdaaus01 a'

    with patch.object(workflow, "previous_day_ymd", return_value="20260811"):
        issues = workflow._availability_policy_issues("가맹점 수 알려줘", sql)

    assert any("20260811" in issue for issue in issues)


def test_previous_day_rejects_explicit_today_even_as_a_dated_request() -> None:
    sql = """
    SELECT COUNT(*)
    FROM card_system.tbdaaus01 a
    WHERE a."기준년월일" = '20260812'
    """

    with patch.object(workflow, "previous_day_ymd", return_value="20260811"):
        issues = workflow._availability_policy_issues("2026년 8월 12일 가맹점 수", sql)

    assert any("20260811" in issue for issue in issues)


def test_recent_ten_day_ignores_day_literals_outside_source_axis_predicate() -> None:
    sql = """
    SELECT '20200101' AS "비교값", COUNT(*)
    FROM card_system.tbdaadt01 m
    WHERE m."실적기준년월일" BETWEEN '20260803' AND '20260812'
      AND m."가맹점번호" <> '20200101'
    """

    with patch.object(workflow, "recent_window_ymd", return_value=("20260803", "20260812")):
        issues = workflow._availability_policy_issues("현재 가맹점 수", sql)

    assert not issues

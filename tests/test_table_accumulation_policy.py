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
from text2sql_agent import schema, time_policy, workflow
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
    with frozen_clock():
        instruction = workflow._accumulation_policy_instruction(["tbdaa1d12"])

    assert f"KST 전일 `{FROZEN_PREVIOUS_DAY}`" in instruction
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

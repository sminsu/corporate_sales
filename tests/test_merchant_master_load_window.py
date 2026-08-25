"""가맹점 마스터는 질문이 날짜를 말할 때만 실적기준년월일로 자른다.

tbdaadt01 은 가맹점번호 1건이 grain 인 마스터다. 적재가 매일 돈다는 것과 조회에
그 적재 창을 걸어야 한다는 것은 다른 이야기인데, v2 초기 빌드가 마스터를 읽는 참고
SQL 네 건에 "최근 10일 실적기준년월일" 창을 얹어 두었다. 질문이 기간을 말하지
않았는데 조회 범위가 좁혀지면 사용자가 묻지 않은 조건이 답에 섞인다(사용자 제공).

반대로 "이번주 실적 기준" · "이번달 실적 기준" 은 적재 축의 일자를 좁혀 달라는 말이다.
주는 월 스냅샷에 없는 단위이고 이번 달은 달이 닫혀야 적재되므로, 둘 다 월 스냅샷으로
돌리지 않고 마스터에서 일자로 자른다.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import VERIFIED_QUERIES
from text2sql_agent.time_policy import TABLE_ACCUMULATION_POLICIES, load_axis_window_ymd

# 2026-08-21 은 금요일이다. 그 주 월요일은 8/17.
TODAY = date(2026, 8, 21)

MASTER_SQL = (
    'SELECT COUNT(DISTINCT m."가맹점번호") AS "가맹점수"\n'
    "FROM tbdaadt01 m\n"
    "WHERE m.\"가맹점상태구분코드\" = '1'"
)

# 마스터를 읽는 참고 SQL. 여기에 적재 창이 다시 들어오면 안 된다.
MASTER_QUERIES = (
    "brand_active_merchant_count",
    "merchant_detail_by_name",
    "merchant_payment_institution_list",
    "merchant_count_by_payment_institution",
)


@pytest.mark.parametrize("name", MASTER_QUERIES)
def test_master_templates_carry_no_load_window(name: str) -> None:
    query = next(item for item in VERIFIED_QUERIES if item["name"] == name)

    assert "tbdaadt01" in query["sql"]
    assert "실적기준년월일" not in query["sql"]


def test_question_without_a_date_gets_no_period_predicate() -> None:
    with patch.object(workflow, "kst_today", return_value=TODAY):
        routed = workflow._apply_accumulation_historical_sources(
            "가맹점 수 알려줘", MASTER_SQL
        )

    assert routed == MASTER_SQL
    assert workflow._recent_merchant_time_route("가맹점 수 알려줘") == ("", "", "")


@pytest.mark.parametrize(
    ("question", "start", "end"),
    [
        ("이번주 실적 기준 가맹점 수 알려줘", "20260817", "20260821"),
        ("이번 주 실적 기준으로 가맹점 수 알려줘", "20260817", "20260821"),
        ("이번달 실적 기준 가맹점 수 알려줘", "20260801", "20260821"),
        ("이번 달 실적 기준으로 가맹점 수 알려줘", "20260801", "20260821"),
    ],
)
def test_load_axis_request_bounds_the_days_on_the_master(
    question: str, start: str, end: str
) -> None:
    with patch.object(workflow, "kst_today", return_value=TODAY):
        assert workflow._recent_merchant_time_route(question) == ("daily", start, end)
        routed = workflow._apply_accumulation_historical_sources(question, MASTER_SQL)

    assert "tbdaadt01" in routed
    assert "tmdaa5d01" not in routed
    assert f"m.\"실적기준년월일\" BETWEEN '{start}' AND '{end}'" in routed


def test_this_month_alone_keeps_the_master_snapshot_rule() -> None:
    """"이번 달" 은 그 달의 상태를 묻는 말로도 쓰인다. 적재 축을 말해야 일자로 자른다."""
    question = "이번 달 가맹점 신용판매 매출금액 상위 10곳 알려줘"

    with patch.object(workflow, "kst_today", return_value=TODAY):
        assert workflow._recent_merchant_time_route(question)[0] == "master"


def test_named_day_and_past_month_keep_their_declared_sources() -> None:
    with patch.object(workflow, "kst_today", return_value=TODAY):
        same_day = workflow._apply_accumulation_historical_sources(
            "2026년 8월 19일 가맹점 수 알려줘", MASTER_SQL
        )
        past_month = workflow._apply_accumulation_historical_sources(
            "2025년 12월 가맹점 수 알려줘", MASTER_SQL
        )

    assert "m.\"실적기준년월일\" = '20260819'" in same_day
    assert "tmdaa5d01" in past_month
    assert "m.\"기준년월\" = '202512'" in past_month


def test_load_window_helper_bounds_week_and_month() -> None:
    assert load_axis_window_ymd("week", today=TODAY) == ("20260817", "20260821")
    assert load_axis_window_ymd("month", today=TODAY) == ("20260801", "20260821")
    with pytest.raises(ValueError):
        load_axis_window_ymd("quarter", today=TODAY)


def test_policy_tells_the_prompt_when_to_bound_the_master() -> None:
    """모델이 스스로 창을 만들지 않도록 적재 정책이 규칙을 적어 둔다."""
    assert TABLE_ACCUMULATION_POLICIES["tbdaadt01"]["filter_when_dated_only"] is True

    details = workflow._table_details(["tbdaadt01"], "가맹점 수 알려줘")

    assert "질문이 날짜를 말할 때만 실적기준년월일 조건" in details


# 질문이 날짜를 찍지 않으면 답의 단위도 달이다. 마스터의 시간축은 일자뿐이라
# 앞 6자리를 잘라 달로 쓰고, 요청한 달이 적재 범위 안이면 그 달을 그대로 읽는다.
MASTER_MONTH_QUESTION = "한신포차 가맹점주 가맹점 기본정보 2026년 8월 기준으로 알려줘"


@pytest.mark.parametrize(
    ("bounds", "requested", "expected"),
    [
        # 요청한 달이 적재 범위 안이면 최신 달로 갈아타지 않는다.
        (("20260801", "20260823"), ("202608", "202608"), ("202608", "202608")),
        # 범위 밖이면 최신 적재 달로 물러난다.
        (("20260801", "20260823"), ("202601", "202601"), ("202608", "202608")),
        # 걸친 요청은 겹치는 구간만 읽는다.
        (("20260715", "20260823"), ("202606", "202608"), ("202607", "202608")),
        # 적재 범위를 못 읽으면 최신 가용일의 달로 답한다.
        (None, ("202601", "202601"), ("202608", "202608")),
    ],
)
def test_master_month_window_keeps_a_loaded_request(
    bounds: tuple[str, str] | None,
    requested: tuple[str, str],
    expected: tuple[str, str],
) -> None:
    with (
        patch.object(workflow, "loaded_period_range", return_value=bounds),
        patch.object(workflow, "_latest_available_day", return_value="20260823"),
    ):
        assert workflow._master_month_window(*requested) == expected


def test_a_month_question_is_cut_by_month_not_by_the_latest_day() -> None:
    """"8월 기준" 을 8월 23일 하루로 좁히면 묻지 않은 조건이 답에 섞인다."""
    with (
        patch.object(workflow, "loaded_period_range", return_value=("20260801", "20260823")),
        patch.object(workflow, "_latest_available_day", return_value="20260823"),
    ):
        routed = workflow._apply_accumulation_historical_sources(
            MASTER_MONTH_QUESTION, MASTER_SQL
        )
        note = workflow._implicit_time_basis_note(MASTER_MONTH_QUESTION, routed)

    assert f'SUBSTR(m."{workflow._TBDAADT01_TIME_COLUMN}", 1, 6) = \'202608\'' in routed
    assert "20260823" not in routed
    assert f"{workflow._TBDAADT01_MONTH_LABEL} 202608" in note

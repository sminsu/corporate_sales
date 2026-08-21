from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

import web_service
from text2sql_agent import workflow
from text2sql_agent.tools.sql_builders import VQ_PARAM_SPECS


SEPTEMBER = date(2026, 9, 15)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("2026년 3분기 업종별 기업카드 이용금액", ("202607", "202609")),
        ("3분기 업종별 기업카드 이용금액", ("202607", "202609")),
        ("작년 4분기 이용금액", ("202510", "202512")),
        ("2025년 3분기 대비 2026년 3분기 이용금액", ("202507", "202609")),
        ("이번 분기 기업카드 이용금액", ("202607", "202609")),
        ("지난 분기 기업카드 이용금액", ("202604", "202606")),
    ],
)
def test_quarter_resolves_to_its_three_months(question: str, expected: tuple[str, str]) -> None:
    with patch.object(workflow, "kst_today", return_value=SEPTEMBER):
        assert workflow._extract_period_by_rule(question)[:2] == expected


def test_first_quarter_lookback_crosses_the_year() -> None:
    with patch.object(workflow, "kst_today", return_value=date(2026, 2, 10)):
        assert workflow._extract_period_by_rule("지난 분기 이용금액")[:2] == ("202510", "202512")


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("작년 9월 이용금액", ("202509", "202509")),
        ("전년 9월 이용금액", ("202509", "202509")),
        ("작년 9월 대비 올해 9월 이용금액", ("202509", "202609")),
        ("올해 9월 이용금액", ("202609", "202609")),
    ],
)
def test_relative_year_with_month_keeps_the_month(question: str, expected: tuple[str, str]) -> None:
    with patch.object(workflow, "kst_today", return_value=SEPTEMBER):
        assert workflow._extract_period_by_rule(question)[:2] == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("9월 기업카드 이용금액", ("202609", "202609")),
        ("12월 신규 고객 알려줘", ("202512", "202512")),
        ("9월부터 12월까지 이용금액", ("202509", "202512")),
        ("1월부터 6월까지 이용금액", ("202601", "202606")),
    ],
)
def test_year_less_month_reads_as_the_most_recent_one(
    question: str,
    expected: tuple[str, str],
) -> None:
    with patch.object(workflow, "kst_today", return_value=SEPTEMBER):
        assert workflow._extract_period_by_rule(question)[:2] == expected


def test_comma_listed_months_stay_with_the_issue_cancel_rule() -> None:
    """발급/해지 월 목록은 기간이 아니라 _extract_card_issue_cancel_periods_by_rule 소관이다."""
    question = (
        "기업카드 발급을 26년 1,2,3월에 하고 4,5,6월 안에 해지한 경우의 "
        "카드좌수와 업체수를 알려줘"
    )

    with patch.object(workflow, "kst_today", return_value=SEPTEMBER):
        assert workflow._extract_period_by_rule(question) == ("", "", "")


def test_quarter_period_reaches_verified_query_parameters() -> None:
    with patch.object(workflow, "kst_today", return_value=SEPTEMBER):
        params = workflow._extract_params_by_rule(
            "3분기 월별 법인카드 이용금액 합계를 보여줘",
            VQ_PARAM_SPECS["monthly_corporate_card_usage"],
        )

    assert params == {"기간_시작": "202607", "기간_종료": "202609"}


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("최근 반년 기업카드 이용금액", ("202604", "202609")),
        ("지난 반년 이용금액", ("202604", "202609")),
        ("최근 6개월 기업카드 이용금액", ("202604", "202609")),
        ("최근 1년 기업카드 이용금액", ("202510", "202609")),
        ("최근 일년 기업카드 이용금액", ("202510", "202609")),
    ],
)
def test_half_year_is_a_six_month_span(question: str, expected: tuple[str, str]) -> None:
    """"최근 반년"이 현재월 1개월로 좁혀지던 회귀를 막는다."""
    with patch.object(workflow, "kst_today", return_value=SEPTEMBER):
        assert workflow._extract_period_by_rule(question)[:2] == expected


def test_half_year_answer_fills_period_parameters() -> None:
    missing = [{"name": "기간_시작"}, {"name": "기간_종료"}]

    with patch.object(web_service, "kst_today", return_value=SEPTEMBER):
        assert web_service._natural_params_by_rule("최근 반년", missing) == {
            "기간_시작": "202604",
            "기간_종료": "202609",
        }


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("26년 9월 기업카드 이용금액", ("202609", "202609", "")),
        ("25년 9월 기업카드 이용금액", ("202509", "202509", "")),
        ("24년 12월 기업카드 이용금액", ("202412", "202412", "")),
        ("26년 7월 9일 하루 기준 기업신용매출건수", ("202607", "202607", "20260709")),
        ("25년 상반기 기업카드 이용금액", ("202501", "202506", "")),
        ("25년 3분기 기업카드 이용금액", ("202507", "202509", "")),
        ("25년 1월부터 3월까지 기업카드 이용금액", ("202501", "202503", "")),
    ],
)
def test_two_digit_year_is_read_as_the_year_it_names(
    question: str, expected: tuple[str, str, str]
) -> None:
    """두 자리 연도가 버려져 "25년 9월"이 202609가 되던 회귀를 막는다.

    기간 정규식이 네 자리 연도만 받아서 "26년 7월 9일"의 일자도 함께 사라졌다.
    """
    with patch.object(workflow, "kst_today", return_value=SEPTEMBER):
        assert workflow._extract_period_by_rule(question) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("최근 10년 기업카드 이용금액", ("201610", "202609")),
        ("최근 5년 기업카드 이용금액", ("202110", "202609")),
    ],
)
def test_relative_year_length_is_not_a_two_digit_year(
    question: str, expected: tuple[str, str]
) -> None:
    """"최근 10년"의 10은 연도가 아니라 기간 길이다."""
    with patch.object(workflow, "kst_today", return_value=SEPTEMBER):
        assert workflow._extract_period_by_rule(question)[:2] == expected

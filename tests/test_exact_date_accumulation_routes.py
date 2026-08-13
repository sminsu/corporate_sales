from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from text2sql_agent import workflow


@pytest.mark.parametrize(
    "written_date",
    ["2026-08-10", "2026.08.10", "2026/08/10", "20260810"],
)
def test_exact_date_formats_resolve_to_one_month_and_day(written_date: str) -> None:
    with patch.object(workflow, "kst_today", return_value=date(2026, 8, 12)):
        assert workflow._extract_period_by_rule(
            f"{written_date} 기업고객 현황"
        ) == ("202608", "202608", "20260810")


@pytest.mark.parametrize(
    "written_date",
    ["2026-08-10", "2026.08.10", "2026/08/10", "20260810"],
)
def test_past_exact_date_routes_previous_day_source_to_monthly_archive(
    written_date: str,
) -> None:
    sql = '''
    SELECT a."고객식별자"
    FROM card_system.tbdaa1d12 a
    WHERE a."기준년월일" = '20260810'
    '''
    with (
        patch.object(workflow, "kst_today", return_value=date(2026, 8, 12)),
        patch.object(workflow, "previous_day_ymd", return_value="20260811"),
    ):
        routed = workflow._apply_accumulation_historical_sources(
            f"{written_date} 기업고객 현황", sql
        )

    assert "card_system.tmdaa1d12" in routed
    assert "card_system.tbdaa1d12" not in routed
    assert 'a."기준년월" = \'202608\'' in routed


@pytest.mark.parametrize(
    "written_date",
    ["2026-08-11", "2026.08.11", "2026/08/11", "20260811"],
)
def test_exact_d_minus_one_date_stays_on_live_previous_day_source(
    written_date: str,
) -> None:
    sql = '''
    SELECT a."고객식별자"
    FROM card_system.tbdaa1d12 a
    WHERE a."기준년월일" = '20260811'
    '''
    with (
        patch.object(workflow, "kst_today", return_value=date(2026, 8, 12)),
        patch.object(workflow, "previous_day_ymd", return_value="20260811"),
    ):
        routed = workflow._apply_accumulation_historical_sources(
            f"{written_date} 기업고객 현황", sql
        )

    assert "card_system.tbdaa1d12" in routed
    assert "card_system.tmdaa1d12" not in routed
    assert 'a."기준년월일" = \'20260811\'' in routed


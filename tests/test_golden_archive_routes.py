from __future__ import annotations

from scripts.generate_semantic_golden_set import (
    _composition_cases,
    _composition_seed_specs,
    _core_cases,
)
from scripts.generate_workbook_goldenset import (
    CASES,
    Q02_NAMED_HALF_YEAR_USAGE,
    Q04_TOP10_QUARTERLY,
    Q05_TOP10_QUARTERLY_EXCLUDED,
    Q08_LOSS_COST_RATE,
    Q10_NEW_CHECK_CARD_ISSUANCE,
    Q15_CORPORATE_MEMBER_WITH_USAGE,
    Q17_PRODUCT_TOP10_COMPANIES,
    Q23_MONTHLY_AVERAGE_COMPARE,
    half_year_decline_sql,
)


def test_historical_composition_seeds_use_month_safe_merchant_paths() -> None:
    specs = {str(item["id"]): item for item in _composition_seed_specs()}

    assert specs["merchant_sales_by_industry"]["paths"] == [
        "merchant_monthly_performance_to_industry"
    ]
    assert specs["merchant_fee_by_industry"]["paths"] == [
        "merchant_monthly_performance_to_industry"
    ]
    assert specs["domestic_sales_by_merchant"]["paths"] == [
        "domestic_sales_to_merchant"
    ]


def test_core_semantic_specs_follow_the_routed_corporate_archive() -> None:
    cases = _core_cases()
    policy_cases = {
        "corporate_limit_status_at_month",
        "named_corporate_limit_status_at_month",
        "corporate_card_low_limit_utilization",
    }

    for case in cases:
        if case["source"]["id"] in policy_cases:
            assert case["expected_tables"] == ["tmdaa1d12"]

    for case in cases:
        answer = case["expected_sql"]
        if not isinstance(answer, dict) or answer.get("kind") == "verified_query":
            continue
        rendered = str(answer)
        tables = set(case["expected_tables"])
        if "tmdaa1d12" in tables and "tbdaa1d12" not in tables:
            assert "tbdaa1d12" not in rendered, case["question_ko"]
        if "tbdaa1d12" in tables and "tmdaa1d12" not in tables:
            assert "tmdaa1d12" not in rendered, case["question_ko"]


def test_credit_limit_compositions_use_monthly_path_tables_and_formulas() -> None:
    names = {
        "total_credit_limit",
        "remaining_credit_limit",
        "used_credit_limit",
        "limit_utilization",
    }

    for case in _composition_cases():
        if case["source"]["id"] not in names:
            continue
        assert case["source"]["join_paths"] == ["customer_to_corporate_monthly"]
        assert case["expected_tables"] == ["tbdaaat01", "tmdaa1d12"]
        answer = case["expected_sql"]
        assert "tbdaa1d12" not in str(answer["metric_expressions"])
        assert "tbdaa1d12" not in str(answer["required_filters"])
        assert "tmdaa1d12" in str(answer["metric_expressions"])


def test_historical_workbook_sql_uses_monthly_corporate_source_and_axis() -> None:
    sqls = (
        Q02_NAMED_HALF_YEAR_USAGE,
        Q04_TOP10_QUARTERLY,
        Q05_TOP10_QUARTERLY_EXCLUDED,
        Q08_LOSS_COST_RATE,
        Q15_CORPORATE_MEMBER_WITH_USAGE,
        Q17_PRODUCT_TOP10_COMPANIES,
        Q23_MONTHLY_AVERAGE_COMPARE,
    )

    for sql in sqls:
        assert "FROM tmdaa1d12 a" in sql
        assert "FROM tbdaa1d12 a" not in sql
        assert 'a."기준년월"' in sql


def test_half_year_decline_splits_previous_day_population_and_monthly_history() -> None:
    sql = half_year_decline_sql(None)

    assert sql.count("FROM tbdaa1d12 a") == 1
    assert 'a."기준년월일" = \'20260809\'' in sql
    assert sql.count("FROM tmdaa1d12 a") == 1
    assert 'a."기준년월" BETWEEN \'202501\' AND \'202506\'' in sql
    assert 'a."기준년월" BETWEEN \'202601\' AND \'202606\'' in sql


def test_current_new_check_card_profile_uses_previous_day_source() -> None:
    assert "FROM tbdaa1d12 a" in Q10_NEW_CHECK_CARD_ISSUANCE
    assert 'a."기준년월일" = \'20260809\'' in Q10_NEW_CHECK_CARD_ISSUANCE
    assert "tmdaa1d12" not in Q10_NEW_CHECK_CARD_ISSUANCE


def test_workbook_expected_tables_match_corporate_archive_split() -> None:
    by_row = {int(case["row"]): set(case["expected_tables"]) for case in CASES}

    for row in (3, 4, 5, 6, 9, 16, 18, 19, 24):
        assert "tmdaa1d12" in by_row[row]
        assert "tbdaa1d12" not in by_row[row]
    for row in (7, 8):
        assert {"tbdaa1d12", "tmdaa1d12"}.issubset(by_row[row])
    assert "tbdaa1d12" in by_row[11]
    assert "tmdaa1d12" not in by_row[11]

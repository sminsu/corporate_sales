from unittest.mock import patch

from text2sql_agent.tools.bad_debt import (
    BAD_DEBT_BORROWER_CODE,
    BAD_DEBT_QUERIES,
    _build_bad_debt_sql,
    _tool_fn_대손비용률,
)


PARAMS = {
    "가맹점명": "한빛",
    "기준년월": "202512",
    "LS": 1.0,
    "IS": 2.0,
}


def test_bad_debt_queries_use_one_monthly_corporate_customer_cohort() -> None:
    assert BAD_DEBT_BORROWER_CODE == "02"
    queries = {
        name: _build_bad_debt_sql(template, PARAMS)
        for name, template in BAD_DEBT_QUERIES.items()
    }

    for sql in queries.values():
        cohort_sql = sql.split("),", 1)[0]
        assert "tmdaa5d01" in cohort_sql
        assert "기준년월 = '202512'" in cohort_sql
        assert '"기업고객식별자" IS NOT NULL' in cohort_sql
        assert "tbdaadt01" not in sql

    for name in ("월초충당금", "월말충당금", "대손비용률종합"):
        assert "충당금차주구분코드 = '02'" in queries[name]
        assert "충당금차주구분코드 = '2'" not in queries[name]

    assert "개인기업구분코드 = '2'" in queries["상각내역"]

    assert "LEFT JOIN cor_table B" in queries["상각내역"]
    assert "LEFT JOIN cor_table B" in queries["대손비용률종합"]


def test_bad_debt_queries_coalesce_nullable_amounts() -> None:
    combined = "\n".join(BAD_DEBT_QUERIES.values())

    assert "SUM(COALESCE(기대대손충당금, 0))" in combined
    assert "SUM(COALESCE(원화대출잔액, 0))" in combined
    assert (
        "SUM(COALESCE(특수채권편입원금, 0) + "
        "COALESCE(특수채권편입가지급금, 0))"
    ) in combined


def test_bad_debt_partial_query_failure_does_not_create_report() -> None:
    query_results = [
        (["구분"], [("1",)], None),
        ([], [], "Athena failure"),
        (["구분"], [("1",)], None),
        (["구분"], [("1",)], None),
    ]

    with (
        patch("text2sql_agent.tools.bad_debt.execute_sql", side_effect=query_results) as execute_sql,
        patch("text2sql_agent.tools.bad_debt._generate_bad_debt_excel") as generate_excel,
    ):
        result = _tool_fn_대손비용률(PARAMS)

    assert result["is_complete"] is True
    assert result["rows"] == []
    assert result["excel_path"] == ""
    assert "분석을 중단했습니다" in result["answer"]
    assert "[월말충당금] Athena failure" in result["answer"]
    executed_sql = [call.args[0] for call in execute_sql.call_args_list]
    for sql in (sql for sql in executed_sql if "tbmewcm94" in sql):
        assert "충당금차주구분코드 = '02'" in sql
        assert "충당금차주구분코드 = '2'" not in sql
    generate_excel.assert_not_called()

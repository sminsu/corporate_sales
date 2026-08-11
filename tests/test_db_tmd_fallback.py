from unittest.mock import patch

import pytest

from text2sql_agent import db, workflow


def test_empty_tbd_query_retries_registered_tmd_table() -> None:
    sql = """
    SELECT 'tbdaa1d12' AS source_name, tbdaa1d12.customer_id AS tbdaa1d12
    FROM card_system.tbdaa1d12
    WHERE note <> 'FROM tbdaa1d12'
    """
    events: list[str] = []
    backend_results = iter(
        [
            (["customer_id"], []),
            (["customer_id"], []),
            (["customer_id"], []),
            (["customer_id"], [("C001",)]),
        ]
    )

    def execute_query(executed_sql: str) -> tuple[list[str], list[tuple]]:
        events.append("tmd" if "card_system.tmdaa1d12" in executed_sql else "tbd")
        return next(backend_results)

    def registered_tables() -> frozenset[str]:
        events.append("check_tmd")
        return frozenset({"tmdaa1d12"})

    with (
        patch.object(db, "_registered_physical_table_names", side_effect=registered_tables),
        patch.object(db, "_execute_backend", side_effect=execute_query) as execute,
        patch.object(db, "_log_db_query"),
    ):
        result = db.execute_sql(sql)

    assert result == (["customer_id"], [("C001",)], None)
    assert events == ["tbd", "tbd", "tbd", "check_tmd", "tmd"]
    assert execute.call_count == 4
    assert all("card_system.tbdaa1d12" in call.args[0] for call in execute.call_args_list[:3])
    fallback_sql = execute.call_args_list[3].args[0]
    assert "FROM card_system.tmdaa1d12" in fallback_sql
    assert "'tbdaa1d12' AS source_name" in fallback_sql
    assert "tmdaa1d12.customer_id AS tbdaa1d12" in fallback_sql
    assert "'FROM tbdaa1d12'" in fallback_sql


def test_verified_query_execution_also_retries_the_registered_tmd_table() -> None:
    sql = "SELECT COUNT(*) AS customer_count FROM card_system.tbdaa1d12"
    zero = (["customer_count"], [(0,)])

    with (
        patch.object(db, "_registered_physical_table_names", return_value=frozenset({"tmdaa1d12"})),
        patch.object(db, "_execute_backend", side_effect=[zero, zero, zero, (["customer_count"], [(2,)])]) as execute,
        patch.object(db, "_log_db_query"),
    ):
        result = workflow.run_matched_query({"final_sql": sql})

    assert result["query_rows"] == [(2,)]
    assert ["tmdaa1d12" in call.args[0] for call in execute.call_args_list] == [False, False, False, True]


@pytest.mark.parametrize(
    ("sql", "registered_tables", "first_rows", "expected_calls"),
    [
        ("SELECT customer_id FROM card_system.tbdaa1d12", frozenset(), [], 3),
        ("SELECT customer_id FROM card_system.tmdaa1d12", frozenset(), [], 1),
        (
            "SELECT customer_id FROM card_system.tbdaa1d12",
            frozenset({"tmdaa1d12"}),
            [("C001",)],
            1,
        ),
        ("SELECT 0 FROM card_system.tbdaa1d12", frozenset({"tmdaa1d12"}), [(0,)], 1),
        (
            "SELECT COUNT(*) AS total_count, COUNT(customer_id) AS matched_count "
            "FROM card_system.tbdaa1d12",
            frozenset({"tmdaa1d12"}),
            [(10, 0)],
            1,
        ),
    ],
)
def test_tmd_retry_requires_a_registered_candidate_and_zero_rows(
    sql: str,
    registered_tables: frozenset[str],
    first_rows: list[tuple],
    expected_calls: int,
) -> None:
    with (
        patch.object(db, "_registered_physical_table_names", return_value=registered_tables),
        patch.object(db, "_execute_backend", return_value=(["customer_id"], first_rows)) as execute,
        patch.object(db, "_log_db_query"),
    ):
        result = db.execute_sql(sql)

    assert result == (["customer_id"], first_rows, None)
    assert execute.call_count == expected_calls


@pytest.mark.parametrize(
    "fallback_result",
    [(["customer_id"], []), RuntimeError("TABLE_NOT_FOUND: tmdaa1d12 does not exist")],
)
def test_empty_or_missing_tmd_table_keeps_the_original_zero_result(fallback_result: object) -> None:
    effects = [(["customer_id"], [])] * 3
    effects.append(fallback_result)
    with (
        patch.object(db, "_registered_physical_table_names", return_value=frozenset({"tmdaa1d12"})),
        patch.object(db, "_execute_backend", side_effect=effects) as execute,
        patch.object(db, "_log_db_query"),
    ):
        result = db.execute_sql("SELECT customer_id FROM card_system.tbdaa1d12")

    assert result == (["customer_id"], [], None)
    assert execute.call_count == 4


@pytest.mark.parametrize(
    ("sql", "registered_table", "first_result", "fallback_result"),
    [
        (
            "SELECT COUNT(*) AS customer_count FROM card_system.tbdaa1d12",
            "tmdaa1d12",
            (["customer_count"], [(0,)]),
            (["customer_count"], [(3,)]),
        ),
        (
            "SELECT MAX(snapshot_date), COUNT(*) FROM card_system.tbdaaus01",
            "tmdaaus01",
            (["snapshot_date", "owner_count"], [("20260731", 0)]),
            (["snapshot_date", "owner_count"], [("20260731", 2)]),
        ),
    ],
)
def test_zero_count_result_retries_registered_tmd_table(
    sql: str,
    registered_table: str,
    first_result: tuple[list[str], list[tuple]],
    fallback_result: tuple[list[str], list[tuple]],
) -> None:
    with (
        patch.object(db, "_registered_physical_table_names", return_value=frozenset({registered_table})),
        patch.object(
            db,
            "_execute_backend",
            side_effect=[first_result, first_result, first_result, fallback_result],
        ) as execute,
        patch.object(db, "_log_db_query"),
    ):
        result = db.execute_sql(sql)

    assert result == (*fallback_result, None)
    assert execute.call_count == 4


def test_real_closed_merchant_zero_count_retries_monthly_table() -> None:
    question = "최근 6개월 이내 문닫은 교촌 치킨 가맹점 개수"
    capability = workflow._select_verified_query_capability(question, {})
    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        rendered = workflow.extract_and_apply_params(
            {"question": question, **capability, "user_provided_params": {}}
        )

    columns = ["조회시작일", "조회종료일", "폐업가맹점수"]
    zero = (columns, [("20260207", "20260807", 0)])
    monthly = (columns, [("20260207", "20260807", 2)])
    with (
        patch.object(db, "_registered_physical_table_names", return_value=frozenset({"tmdaaus01"})),
        patch.object(db, "_execute_backend", side_effect=[zero, zero, zero, monthly]) as execute,
        patch.object(db, "_log_db_query"),
    ):
        result = db.execute_sql(rendered["final_sql"])

    assert result == (*monthly, None)
    assert db._top_level_count_output_indexes(rendered["final_sql"]) == [2]
    assert "tbdaaus01" in execute.call_args_list[0].args[0]
    assert "tmdaaus01" in execute.call_args_list[-1].args[0]


@pytest.mark.parametrize(
    "results",
    [
        [(["customer_id"], []), (["customer_id"], [("C001",)])],
        [
            (["customer_id"], []),
            (["customer_id"], []),
            (["customer_id"], [("C001",)]),
        ],
    ],
)
def test_tbd_retry_stops_as_soon_as_rows_are_found(results: list[tuple[list[str], list[tuple]]]) -> None:
    with (
        patch.object(db, "_registered_physical_table_names", return_value=frozenset({"tmdaa1d12"})),
        patch.object(db, "_execute_backend", side_effect=results) as execute,
        patch.object(db, "_log_db_query"),
    ):
        result = db.execute_sql("SELECT customer_id FROM card_system.tbdaa1d12")

    assert result == (["customer_id"], [("C001",)], None)
    assert execute.call_count == len(results)
    assert all("card_system.tbdaa1d12" in call.args[0] for call in execute.call_args_list)


def test_tmd_fallback_error_keeps_the_original_zero_result() -> None:
    with (
        patch.object(db, "_registered_physical_table_names", return_value=frozenset({"tmdaa1d12"})),
        patch.object(
            db,
            "_execute_backend",
            side_effect=[
                (["customer_id"], []),
                (["customer_id"], []),
                (["customer_id"], []),
                RuntimeError("query timed out"),
            ],
        ) as execute,
        patch.object(db, "_log_db_query"),
    ):
        state = workflow.run_matched_query(
            {"final_sql": "SELECT customer_id FROM card_system.tbdaa1d12"}
        )

    assert state["query_columns"] == ["customer_id"]
    assert state["query_rows"] == []
    assert state["query_error"] == ""
    assert workflow.after_matched_query(state) == "generate_answer"
    answer = workflow.generate_answer({"question": "기업회원을 알려줘", **state})
    assert answer["answer"].startswith("조회 결과가 0건입니다.")
    assert execute.call_count == 4


def test_tbd_retry_error_is_reported_before_tmd_fallback() -> None:
    with (
        patch.object(db, "_registered_physical_table_names") as registered_tables,
        patch.object(
            db,
            "_execute_backend",
            side_effect=[(["customer_id"], []), RuntimeError("query timed out")],
        ) as execute,
        patch.object(db, "_log_db_query"),
    ):
        result = db.execute_sql("SELECT customer_id FROM card_system.tbdaa1d12")

    assert result == ([], [], "query timed out")
    assert execute.call_count == 2
    registered_tables.assert_not_called()


def test_tbd_named_cte_is_not_treated_as_a_physical_table() -> None:
    sql = "WITH tbdaa1d12 AS (SELECT 1 AS id) SELECT id FROM tbdaa1d12"

    with patch.object(db, "_registered_physical_table_names", return_value=frozenset({"tmdaa1d12"})):
        assert db._tmd_fallback_sql(sql) is None


def test_explicit_fetch_limit_bounds_backend_sql_and_caps_at_one_million() -> None:
    with (
        patch.object(db, "_execute_backend", return_value=(["value"], [(1,)])) as execute,
        patch.object(db, "_log_db_query"),
    ):
        result = db.execute_sql("SELECT value FROM sample", max_rows=2_000_000)

    assert result == (["value"], [(1,)], None)
    bounded_sql, max_rows, timeout_ms = execute.call_args.args
    assert "SELECT value FROM sample" in bounded_sql
    assert bounded_sql.endswith("LIMIT 1000000")
    assert max_rows == 1_000_000
    assert timeout_ms == 30_000

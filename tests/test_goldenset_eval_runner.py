from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import run_goldenset_answers as golden
from scripts.run_goldenset_eval import (
    V_AGENT_ERROR,
    V_EXACT,
    V_MATCH,
    V_MISMATCH,
    V_NEAR,
    V_NO_GOLD,
    V_NO_SQL,
    V_SQL_ERROR,
    V_VALUE_MATCH,
    _bucket,
    build_report,
    gold_answer,
    load_gold_cases,
    main,
    render_markdown,
    scalar_close,
    score,
    self_test,
    sql_tables,
    table_metrics,
    value_hash,
)

COLUMNS = ["구분", "금액"]
ROWS = [("A", 10), ("B", 20)]


def _answer(columns: list[str], rows: list[tuple], float_digits: int = 6) -> dict:
    hashes = golden.result_hashes(columns, rows, float_digits)
    return {
        "columns": columns,
        "row_count": len(rows),
        "row_count_truncated": False,
        "rows": [list(row) for row in rows],
        "rows_stored": len(rows),
        "scalar": golden.scalar_answer(columns, rows),
        "result_hash": hashes["result_hash"],
        "ordered_hash": hashes["ordered_hash"],
        "value_hash": value_hash(columns, rows, float_digits),
        "float_digits": float_digits,
    }


def _case(**overrides) -> dict:
    case = {
        "id": "cs-golden-v2-0001",
        "question": "유실적 업체수를 알려줘",
        "sql": "SELECT count(*) FROM tbdaa1d12",
        "domain": "corporate_sales_targeting",
        "query_shape": "business_case",
        "difficulty": "easy",
        "expected_tables": ["tbdaa1d12"],
    }
    case.update(overrides)
    return case


def test_self_test_passes() -> None:
    assert self_test() == 0


def test_identical_result_is_exact() -> None:
    gold = _answer(COLUMNS, ROWS)
    outcome = score(_case(), gold, _answer(COLUMNS, ROWS), "SELECT 1 FROM tbdaa1d12")
    assert outcome["verdict"] == V_EXACT
    assert outcome["correct"] is True


def test_row_order_difference_still_counts_as_correct() -> None:
    gold = _answer(COLUMNS, ROWS)
    shuffled = _answer(COLUMNS, list(reversed(ROWS)))
    outcome = score(_case(), gold, shuffled, "SELECT 1 FROM tbdaa1d12")
    assert outcome["verdict"] == V_MATCH
    assert outcome["correct"] is True
    assert outcome["detail"]["result_match"] is True
    assert outcome["detail"]["ordered_match"] is False


def test_column_order_difference_is_value_match() -> None:
    gold = _answer(COLUMNS, ROWS)
    swapped = _answer(["금액", "구분"], [(10, "A"), (20, "B")])
    outcome = score(_case(), gold, swapped, "SELECT 1 FROM tbdaa1d12")
    assert outcome["verdict"] == V_VALUE_MATCH
    assert outcome["correct"] is True


def test_different_values_are_not_correct() -> None:
    gold = _answer(COLUMNS, ROWS)
    other = _answer(COLUMNS, [("A", 11), ("B", 20)])
    outcome = score(_case(), gold, other, "SELECT 1 FROM tbdaa1d12")
    assert outcome["verdict"] in {V_NEAR, V_MISMATCH}
    assert outcome["correct"] is False


def test_scalar_tolerance_and_row_count_drive_near() -> None:
    gold = _answer(["cnt"], [(1000,)])
    drifted = _answer(["건수"], [(1000.0000001,)])
    outcome = score(_case(sql="SELECT count(*) FROM tbdaa1d12"), gold, drifted,
                    "SELECT count(*) FROM tbdaa1d12", rel_tol=1e-6)
    assert outcome["detail"]["scalar_match"] is True
    assert outcome["verdict"] in {V_VALUE_MATCH, V_NEAR}

    far = _answer(["건수"], [(2000,)])
    missed = score(_case(), gold, far, "SELECT count(*) FROM tbdaa1d12", rel_tol=1e-6)
    assert missed["correct"] is False


@pytest.mark.parametrize(
    "kwargs, sql, expected",
    [
        ({}, "", V_NO_SQL),
        ({"sql_error": "SYNTAX_ERROR"}, "SELECT 1", V_SQL_ERROR),
        ({"agent_error": "TimeoutError"}, "SELECT 1", V_AGENT_ERROR),
    ],
)
def test_failure_paths(kwargs, sql, expected) -> None:
    outcome = score(_case(), _answer(COLUMNS, ROWS), None, sql, **kwargs)
    assert outcome["verdict"] == expected
    assert outcome["correct"] is False


def test_missing_gold_is_excluded_from_scoring() -> None:
    outcome = score(_case(), None, None, "SELECT 1 FROM tbdaa1d12")
    assert outcome["verdict"] == V_NO_GOLD
    assert _bucket([{"verdict": V_NO_GOLD, "correct": False, "detail": {}}])["scored"] == 0


def test_gold_answer_only_accepts_executed_answers() -> None:
    assert gold_answer({"answer_status": "ok", "expected_answer": {"result_hash": "h"}})
    assert gold_answer({"answer_status": "empty", "expected_answer": {"result_hash": "h"}})
    assert gold_answer({"answer_status": "error", "expected_answer": {"result_hash": "h"}}) is None
    assert gold_answer({"answer_status": "not_executed"}) is None
    assert gold_answer({"expected_answer": {"row_count": 3}}) is None


def test_scalar_close_handles_numbers_and_text() -> None:
    assert scalar_close(1000, 1000.0000001, 1e-6)
    assert not scalar_close(1000, 1100, 1e-6)
    assert scalar_close("1,234", 1234, 1e-6)
    assert scalar_close("삼성전자", "삼성전자", 1e-6)
    assert not scalar_close(None, 1, 1e-6)


def test_table_metrics_ignore_cte_names_and_schema_prefix() -> None:
    found = sql_tables(
        "WITH base AS (SELECT 1) SELECT * FROM base b JOIN tbdaa1d12 a ON 1=1",
        golden.FALLBACK_TABLES,
    )
    assert found == {"tbdaa1d12"}

    metrics = table_metrics(["card_system.tbdaa1d12", "tmdaa5e11"], {"tbdaa1d12", "tbdaadb17"})
    assert metrics["recall"] == 0.5
    assert metrics["missing"] == ["tmdaa5e11"]
    assert metrics["extra"] == ["tbdaadb17"]
    assert metrics["exact"] is False


def test_load_gold_cases_merges_answers_from_history(tmp_path: Path) -> None:
    cases_path = tmp_path / "cases.jsonl"
    answers_path = tmp_path / "answers.jsonl"
    cases_path.write_text(json.dumps(_case(), ensure_ascii=False) + "\n", encoding="utf-8")
    answers_path.write_text(
        json.dumps(
            {"id": "cs-golden-v2-0001", "status": "ok", "answer": _answer(COLUMNS, ROWS)},
            ensure_ascii=False,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )
    merged = load_gold_cases(cases_path, answers_path)
    assert merged[0]["answer_status"] == "ok"
    assert gold_answer(merged[0]) is not None


def _write_goldenset(path: Path, count: int) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for index in range(1, count + 1):
            case = _case(
                id="cs-golden-v2-%04d" % index,
                sql="SELECT count(*) AS c FROM tbdaa1d12 WHERE 기준년월 = '2026%02d'" % index,
                answer_status="ok",
                expected_answer=_answer(["c"], [(index * 10,)]),
            )
            fh.write(json.dumps(case, ensure_ascii=False) + "\n")


def test_end_to_end_scores_and_resumes(tmp_path: Path, monkeypatch, capsys) -> None:
    cases_path = tmp_path / "answered.jsonl"
    results_path = tmp_path / "eval.jsonl"
    _write_goldenset(cases_path, 6)

    argv = [
        "run_goldenset_eval.py",
        "--predictor", "stub",
        "--cases", str(cases_path),
        "--results", str(results_path),
        "--summary", str(tmp_path / "summary.md"),
        "--report-json", str(tmp_path / "report.json"),
        "--review-csv", str(tmp_path / "review.csv"),
    ]
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0

    records = [json.loads(line) for line in results_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 6
    assert {r["verdict"] for r in records} <= set(
        [V_EXACT, V_MATCH, V_VALUE_MATCH, V_NEAR, V_MISMATCH, V_SQL_ERROR, V_NO_SQL, V_AGENT_ERROR]
    )
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["overall"]["scored"] == 6
    assert 0.0 <= report["overall"]["execution_accuracy"] <= 1.0
    assert report["predictor"] == "stub"
    assert (tmp_path / "review.csv").read_text(encoding="utf-8-sig").splitlines()[0].startswith("id,")
    assert "# 골든셋 채점 요약" in (tmp_path / "summary.md").read_text(encoding="utf-8")

    # 다시 실행하면 저장된 케이스를 건너뛴다.
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    assert len(results_path.read_text(encoding="utf-8").splitlines()) == 6
    assert "채점할 케이스가 없다" in capsys.readouterr().out


def test_fail_under_returns_nonzero_on_report_only(tmp_path: Path, monkeypatch) -> None:
    cases_path = tmp_path / "answered.jsonl"
    results_path = tmp_path / "eval.jsonl"
    _write_goldenset(cases_path, 4)
    results_path.write_text(
        "\n".join(
            json.dumps(record, ensure_ascii=False)
            for record in (
                {"id": "cs-golden-v2-0001", "verdict": V_EXACT, "correct": True,
                 "agent": {"sql": "SELECT 1"}, "candidate": {}, "detail": {}},
                {"id": "cs-golden-v2-0002", "verdict": V_MISMATCH, "correct": False,
                 "agent": {"sql": "SELECT 1"}, "candidate": {}, "detail": {}},
            )
        )
        + "\n",
        encoding="utf-8",
    )
    base = [
        "run_goldenset_eval.py", "--report-only",
        "--cases", str(cases_path), "--results", str(results_path),
        "--summary", str(tmp_path / "summary.md"),
        "--report-json", str(tmp_path / "report.json"),
        "--review-csv", str(tmp_path / "review.csv"),
    ]
    monkeypatch.setattr("sys.argv", base + ["--fail-under", "0.9"])
    assert main() == 1
    monkeypatch.setattr("sys.argv", base + ["--fail-under", "0.4"])
    assert main() == 0


def test_render_markdown_covers_every_breakdown(tmp_path: Path) -> None:
    class Args:
        cases = tmp_path / "cases.jsonl"
        results = tmp_path / "results.jsonl"
        predictions = None
        predictor = "agent"

    cases = [_case(id="a"), _case(id="b", domain="merchant_sales", difficulty="hard")]
    saved = {
        "a": {"id": "a", "question": "q1", "domain": "corporate_sales_targeting",
              "query_shape": "business_case", "difficulty": "easy", "predictor": "agent",
              "verdict": V_EXACT, "correct": True, "candidate": {"row_count": 1},
              "agent": {"sql": "SELECT 1"}, "detail": {"tables": {"recall": 1.0}}},
        "b": {"id": "b", "question": "q2", "domain": "merchant_sales",
              "query_shape": "business_case", "difficulty": "hard", "predictor": "agent",
              "verdict": V_MISMATCH, "correct": False, "candidate": {"row_count": 5},
              "expected_row_count": 3, "agent": {"sql": "SELECT 2"},
              "detail": {"tables": {"recall": 0.0, "missing": ["tbdaa1d12"]}}},
    }
    report = build_report(cases, saved, Args())
    assert report["overall"]["execution_accuracy"] == 0.5
    assert report["by_domain"]["merchant_sales"]["scored"] == 1
    assert report["by_difficulty"]["hard"]["execution_accuracy"] == 0.0
    assert len(report["failures"]) == 1

    markdown = render_markdown(report)
    assert "실행 정확도" in markdown
    assert "## 도메인별" in markdown
    assert "## 오답" in markdown
    assert "`b`" in markdown

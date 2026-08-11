from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts.report_goldenset_failures import (
    analyze_mismatch,
    build_summary,
    classify_error,
    collect,
    dedupe_latest,
    diff_rows,
    error_signature,
    group_by_difficulty,
    main,
    render_markdown,
    self_test,
)

MONTHLY = ["기준년월", "월이용금액"]
GOLD_ROWS = [["202601", 1000], ["202602", 2000], ["202603", 3000]]


def _gold(columns=None, rows=None) -> dict:
    columns = MONTHLY if columns is None else columns
    rows = GOLD_ROWS if rows is None else rows
    return {
        "columns": columns,
        "row_count": len(rows),
        "row_count_truncated": False,
        "rows": [list(row) for row in rows],
        "rows_stored": len(rows),
        "scalar": rows[0][0] if len(rows) == 1 and len(columns) == 1 else None,
        "float_digits": 6,
    }


GOLD_SQL = "SELECT 기준년월, sum(이용금액) AS 월이용금액 FROM tbdaa1d12 GROUP BY 1"


def _case(case_id: str, **overrides) -> dict:
    """골든셋 케이스 한 건. collect() 는 정답 SQL도 함께 실어야 해서 케이스를 통째로 받는다."""
    case = {
        "id": case_id,
        "question": "월별 이용금액",
        "sql": GOLD_SQL,
        "domain": "merchant_sales",
        "difficulty": "hard",
        "query_shape": "business_case",
        "expected_tables": ["tbdaa1d12"],
        "answer_status": "ok",
        "expected_answer": dict(_gold(), result_hash="h", ordered_hash="o"),
    }
    case.update(overrides)
    return case


def _record(**overrides) -> dict:
    record = {
        "id": "cs-golden-v2-0001",
        "question": "3개월 월별 이용금액 추이",
        "domain": "merchant_sales",
        "query_shape": "business_case",
        "difficulty": "hard",
        "verdict": "mismatch",
        "correct": False,
        "sql_error": "",
        "agent": {"sql": "SELECT 1 FROM tbdaa1d12", "error": "", "meta": {}},
        "candidate": _gold(),
        "detail": {
            "expected_columns": MONTHLY,
            "actual_columns": MONTHLY,
            "expected_row_count": 3,
            "actual_row_count": 3,
            "tables": {"expected": ["tbdaa1d12"], "actual": ["tbdaa1d12"],
                       "missing": [], "extra": []},
            "notes": [],
        },
    }
    for key, value in overrides.items():
        if key in ("detail", "candidate", "agent") and isinstance(value, dict):
            merged = dict(record[key])
            merged.update(value)
            record[key] = merged
        else:
            record[key] = value
    return record


def test_self_test_passes() -> None:
    assert self_test() == 0


@pytest.mark.parametrize(
    "message, kind, subject",
    [
        ("SYNTAX_ERROR: line 5:12: Column '가맹점명' cannot be resolved",
         "column_not_found", "가맹점명"),
        ("TABLE_NOT_FOUND: line 1:15: Table 'card_system.tb_x' does not exist",
         "table_not_found", "card_system.tb_x"),
        ("SYNTAX_ERROR: line 1:1: mismatched input 'FROM'", "syntax", ""),
        ("TYPE_MISMATCH: Cannot apply operator: varchar = bigint",
         "type_mismatch", "varchar = bigint"),
        ("FUNCTION_NOT_FOUND: line 1:8: Function 'date_diff2' not registered",
         "function_not_found", "date_diff2"),
        ("ValueError: 읽기 전용이 아닌 키워드 포함", "guard", ""),
        ("AccessDeniedException: not authorized to perform", "permission", ""),
        ("Query exhausted resources at this scale factor", "resource", ""),
        ("무언가 알 수 없는 실패", "other", ""),
        ("", "other", ""),
    ],
)
def test_error_classification(message, kind, subject) -> None:
    result = classify_error(message)
    assert result["kind"] == kind
    assert result["subject"] == subject


def test_error_signature_groups_same_cause() -> None:
    left = error_signature("SYNTAX_ERROR: line 5:12: Column 'a' cannot be resolved")
    right = error_signature("SYNTAX_ERROR: line 90:3: Column 'zzz' cannot be resolved")
    assert left == right
    other = error_signature("TABLE_NOT_FOUND: line 1:1: Table 'x' does not exist")
    assert other != left


def test_value_difference_points_at_the_column() -> None:
    candidate = _gold(rows=[["202601", 1000], ["202602", 2000], ["202603", 2500]])
    analysis = analyze_mismatch(_record(candidate=candidate), _gold(), 5)
    assert analysis["primary_reason"] == "row_values"
    diffs = analysis["findings"]["rows"]["column_diffs"]
    assert [d["column"] for d in diffs] == ["월이용금액"]
    assert diffs[0]["differing_rows"] == 1
    assert diffs[0]["compared_rows"] == 3
    sample = diffs[0]["samples"][0]
    assert sample["key"] == "202603"
    assert sample["expected"] == 3000 and sample["actual"] == 2500
    assert sample["delta"]["ratio_pct"] == pytest.approx(-16.67, abs=0.01)


def test_row_count_difference_reports_direction_and_missing_rows() -> None:
    candidate = _gold(rows=GOLD_ROWS[:1])
    record = _record(candidate=candidate,
                     detail={"actual_row_count": 1,
                             "tables": {"expected": ["tbdaa1d12", "tmdaa5e11"],
                                        "actual": ["tbdaa1d12"],
                                        "missing": ["tmdaa5e11"], "extra": []}})
    analysis = analyze_mismatch(record, _gold(), 5)
    assert analysis["primary_reason"] == "row_count"
    assert analysis["findings"]["row_count"]["direction"] == "과소"
    assert analysis["findings"]["row_count"]["diff"] == -2
    assert analysis["findings"]["rows"]["only_in_gold_count"] == 2
    assert "tables" in analysis["reasons"]
    assert analysis["findings"]["tables"]["missing"] == ["tmdaa5e11"]


def test_scalar_difference_outranks_column_naming() -> None:
    record = _record(
        candidate={"columns": ["업체수"], "row_count": 1, "rows": [[1250]],
                   "scalar": 1250, "float_digits": 6},
        detail={"expected_columns": ["유실적업체수"], "actual_columns": ["업체수"],
                "expected_row_count": 1, "actual_row_count": 1,
                "expected_scalar": 1000, "actual_scalar": 1250, "scalar_match": False},
    )
    analysis = analyze_mismatch(
        record, {"columns": ["유실적업체수"], "row_count": 1, "rows": [[1000]],
                 "scalar": 1000, "float_digits": 6}, 5)
    assert analysis["primary_reason"] == "scalar_value"
    assert "column_names" in analysis["reasons"]
    assert analysis["findings"]["scalar"]["delta"]["ratio_pct"] == 25.0


def test_column_count_difference_does_not_emit_row_noise() -> None:
    candidate = _gold(columns=MONTHLY + ["건수"],
                      rows=[row + [index] for index, row in enumerate(GOLD_ROWS, 1)])
    record = _record(candidate=candidate,
                     detail={"actual_columns": MONTHLY + ["건수"]})
    analysis = analyze_mismatch(record, _gold(), 5)
    assert analysis["primary_reason"] == "column_count"
    assert analysis["findings"]["columns"]["only_in_actual"] == ["건수"]
    rows = analysis["findings"]["rows"]
    assert rows["aligned_columns"] is False
    assert rows["only_in_gold_count"] == 0
    assert rows["column_diffs"] == []


def test_column_order_difference_still_compares_values() -> None:
    candidate = {"columns": ["월이용금액", "기준년월"], "row_count": 3,
                 "rows": [[1000, "202601"], [2000, "202602"], [2500, "202603"]],
                 "float_digits": 6}
    record = _record(candidate=candidate,
                     detail={"actual_columns": ["월이용금액", "기준년월"]})
    analysis = analyze_mismatch(record, _gold(), 5)
    assert analysis["findings"]["columns"]["order_only"] is True
    assert [d["column"] for d in analysis["findings"]["rows"]["column_diffs"]] == ["월이용금액"]


def test_truncated_results_skip_value_comparison() -> None:
    candidate = _gold()
    candidate.update({"row_count": 5000, "row_count_truncated": True})
    record = _record(candidate=candidate, detail={"actual_row_count": 5000})
    analysis = analyze_mismatch(record, _gold(), 5)
    assert "skipped" in analysis["findings"]["rows"]
    assert analysis["primary_reason"] == "row_count"


def test_diff_rows_falls_back_to_positional_comparison() -> None:
    columns = ["구분", "값"]
    gold_rows = [["A", 1], ["A", 2]]          # 키가 유일하지 않다
    cand_rows = [["A", 1], ["A", 3]]
    diff = diff_rows(columns, gold_rows, columns, cand_rows, 6, 5)
    assert diff["key_column"] is None
    assert diff["matched_rows"] == 2
    assert [d["column"] for d in diff["column_diffs"]] == ["값"]


def test_dedupe_keeps_last_attempt_per_id() -> None:
    deduped = dedupe_latest([
        {"id": "a", "verdict": "sql_error"},
        {"id": "b", "verdict": "mismatch"},
        {"id": "a", "verdict": "match"},
    ])
    assert [r["verdict"] for r in deduped] == ["match", "mismatch"]


def test_group_by_difficulty_orders_easy_medium_hard() -> None:
    grouped = group_by_difficulty(
        [{"difficulty": "hard"}, {"difficulty": "easy"},
         {"difficulty": "medium"}, {"difficulty": "unknown"}]
    )
    assert list(grouped) == ["easy", "medium", "hard", "unknown"]


class _Args:
    results = Path("results.jsonl")
    cases = Path("cases.jsonl")
    verdicts = {"sql_error", "mismatch"}
    domain = ""
    difficulty = ""
    shape = ""
    ids: list[str] = []
    max_samples = 5
    max_cases = 60
    sql_chars = 1500


def test_collect_and_summary_split_errors_from_mismatches() -> None:
    records = [
        _record(id="e1", verdict="sql_error", difficulty="hard",
                sql_error="SYNTAX_ERROR: line 1:1: Column '가맹점명' cannot be resolved",
                candidate=None),
        _record(id="m1", candidate=_gold(rows=[["202601", 1000], ["202602", 2000],
                                               ["202603", 2500]])),
        _record(id="ok1", verdict="exact", correct=True),
    ]
    collected = collect(records, {"m1": _case("m1"), "e1": _case("e1")}, _Args())
    assert [item["id"] for item in collected["errors"]] == ["e1"]
    assert [item["id"] for item in collected["mismatches"]] == ["m1"]
    assert collected["errors"][0]["kind"] == "column_not_found"
    assert collected["mismatches"][0]["primary_reason"] == "row_values"
    # 정답 SQL이 오류·불일치 양쪽에 함께 실려야 한다.
    assert collected["mismatches"][0]["expected_sql"] == GOLD_SQL
    assert collected["errors"][0]["expected_sql"] == GOLD_SQL

    summary = build_summary(collected, records, _Args())
    assert summary["sql_error"]["total"] == 1
    assert summary["sql_error"]["by_difficulty"] == {"hard": 1}
    assert summary["sql_error"]["by_kind"] == {"column_not_found": 1}
    assert summary["mismatch"]["by_primary_reason"] == {"row_values": 1}
    assert summary["mismatch"]["top_diff_columns"] == {"월이용금액": 1}
    assert summary["verdict_counts"]["exact"] == 1

    markdown = render_markdown(summary, _Args())
    assert "## 1. SQL 오류 (난이도별)" in markdown
    assert "### 난이도 `hard`" in markdown
    assert "컬럼 없음" in markdown
    assert "## 2. mismatch" in markdown
    assert "월이용금액" in markdown
    assert "<summary>정답 SQL</summary>" in markdown
    assert "<summary>후보 SQL</summary>" in markdown
    assert GOLD_SQL in markdown


def test_filters_narrow_the_selection() -> None:
    records = [
        _record(id="a", difficulty="hard", domain="merchant_sales"),
        _record(id="b", difficulty="easy", domain="corporate_sales_targeting"),
    ]

    class Filtered(_Args):
        difficulty = "easy"

    collected = collect(records, {}, Filtered())
    assert [item["id"] for item in collected["mismatches"]] == ["b"]
    # 골든셋을 못 찾아도 나머지 정리는 계속된다(정답 SQL만 빈다).
    assert collected["mismatches"][0]["expected_sql"] == ""


def _write_fixture(tmp_path: Path) -> tuple[Path, Path]:
    cases_path = tmp_path / "cases.jsonl"
    results_path = tmp_path / "results.jsonl"
    case = _case("cs-golden-v2-0001")
    error_case = _case("cs-golden-v2-0002", difficulty="easy")
    cases_path.write_text(
        "\n".join(json.dumps(c, ensure_ascii=False) for c in (case, error_case)) + "\n",
        encoding="utf-8",
    )
    mismatch = _record(id="cs-golden-v2-0001", question="월별 이용금액",
                       candidate=_gold(rows=[["202601", 1000], ["202602", 2000],
                                             ["202603", 2500]]))
    sql_error = _record(id="cs-golden-v2-0002", question="월별 이용금액",
                        difficulty="easy", verdict="sql_error", candidate=None,
                        sql_error="TABLE_NOT_FOUND: line 1:15: Table 'card_system.tb_x' "
                                  "does not exist")
    results_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in (mismatch, sql_error, mismatch))
        + "\n",
        encoding="utf-8",
    )
    return cases_path, results_path


def test_end_to_end_writes_every_output(tmp_path: Path, monkeypatch) -> None:
    cases_path, results_path = _write_fixture(tmp_path)
    monkeypatch.setattr("sys.argv", [
        "report_goldenset_failures.py",
        "--results", str(results_path),
        "--cases", str(cases_path),
        "--out-md", str(tmp_path / "failures.md"),
        "--out-json", str(tmp_path / "failures.json"),
        "--sql-error-csv", str(tmp_path / "errors.csv"),
        "--mismatch-csv", str(tmp_path / "mismatches.csv"),
    ])
    assert main() == 0

    summary = json.loads((tmp_path / "failures.json").read_text(encoding="utf-8"))
    assert summary["records_total"] == 2          # 중복 id는 마지막 것만
    assert summary["sql_error"]["total"] == 1
    assert summary["mismatch"]["total"] == 1
    assert summary["sql_error"]["by_difficulty"] == {"easy": 1}

    markdown = (tmp_path / "failures.md").read_text(encoding="utf-8")
    assert "테이블/스키마 없음" in markdown
    assert "행 값이 다름" in markdown
    assert GOLD_SQL in markdown          # mismatch 상세에 정답 SQL이 함께 실린다

    errors = list(csv.DictReader((tmp_path / "errors.csv").read_text(encoding="utf-8-sig")
                                .splitlines()))
    assert errors[0]["difficulty"] == "easy"
    assert errors[0]["error_kind"] == "table_not_found"
    assert errors[0]["question"] == "월별 이용금액"
    assert errors[0]["domain"] == "merchant_sales"
    assert errors[0]["expected_sql"] == GOLD_SQL

    mismatches = list(csv.DictReader((tmp_path / "mismatches.csv").read_text(encoding="utf-8-sig")
                                     .splitlines()))
    assert mismatches[0]["primary_reason"] == "row_values"
    assert "월이용금액" in mismatches[0]["diff_columns"]
    assert "202603" in mismatches[0]["diff_sample"]
    # 원인이 아니어도 검수에 필요한 모양 정보는 항상 채워져 있어야 한다.
    assert mismatches[0]["expected_columns"] == "기준년월, 월이용금액"
    assert mismatches[0]["actual_columns"] == "기준년월, 월이용금액"
    assert mismatches[0]["expected_row_count"] == "3"
    assert mismatches[0]["actual_row_count"] == "3"
    assert mismatches[0]["expected_sql"] == GOLD_SQL
    assert mismatches[0]["agent_sql"] == "SELECT 1 FROM tbdaa1d12"


def test_missing_results_file_is_reported(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", [
        "report_goldenset_failures.py", "--results", str(tmp_path / "nope.jsonl"),
    ])
    assert main() == 1
    assert "채점 결과 파일이 없다" in capsys.readouterr().out

#!/usr/bin/env python3
"""Run every question in a CSV through the agent and save the generated SQL.

입력 CSV에 question(또는 질문) 컬럼을 채워두면 한 건씩 순차로 에이전트를 실행하고,
입력 컬럼 + 결과 SQL을 출력 CSV에 한 줄씩 바로 기록한다. CLI와 달리 대화형 파라미터
입력 루프를 타지 않으므로, 파라미터가 부족한 질문은 error 컬럼에 사유만 남는다.

    python scripts/run_questions_csv.py questions.csv
    python scripts/run_questions_csv.py questions.csv -o result.csv --limit 10
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

QUESTION_COLUMNS = ("question", "질문", "question_ko", "질의")
RESULT_COLUMNS = ("sql", "question_type", "route", "tables", "row_count", "error")


def _read_text(path: Path) -> str:
    content = path.read_bytes()
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"CSV는 UTF-8 또는 CP949 인코딩이어야 합니다: {path}")


def read_rows(path: Path, column: str | None) -> tuple[list[dict[str, Any]], list[str], str]:
    """Return the input rows, their column order, and the question column name."""
    reader = csv.DictReader(io.StringIO(_read_text(path)))
    rows = list(reader)
    fields = [name for name in (reader.fieldnames or []) if name]
    if not fields:
        raise SystemExit(f"CSV 첫 줄에 헤더가 필요합니다: {path}")
    if column:
        if column not in fields:
            raise SystemExit(f"'{column}' 컬럼이 없습니다. 사용 가능: {', '.join(fields)}")
        return rows, fields, column
    for name in QUESTION_COLUMNS:
        if name in fields:
            return rows, fields, name
    return rows, fields, fields[0]


def run_question(question: str) -> dict[str, Any]:
    """Invoke the same compiled graph the service uses, without an input loop."""
    from text2sql_agent.workflow import _get_app, _new_initial_state

    result = _get_app().invoke(_new_initial_state(question))

    error = result.get("error_message") or result.get("query_error") or ""
    if not error and result.get("param_stage") == "need_params":
        missing = result.get("missing_params") or []
        labels = [str(p.get("label") or p.get("name")) for p in missing if isinstance(p, dict)]
        error = "파라미터 부족: " + (", ".join(labels) or "확인 필요")

    if result.get("selected_tool"):
        route = result["selected_tool"]
    elif result.get("matched_query_name"):
        route = result["matched_query_name"]
    else:
        route = "sql_generation" if result.get("final_sql") else ""

    rows = result.get("query_rows") or []
    return {
        "sql": result.get("final_sql") or result.get("generated_sql") or "",
        "question_type": result.get("question_type") or "",
        "route": route,
        "tables": ", ".join(result.get("selected_tables") or []),
        "row_count": len(rows),
        "error": error,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="CSV의 question을 에이전트로 실행해 SQL을 저장합니다.")
    parser.add_argument("input", type=Path, help="question 컬럼이 있는 입력 CSV")
    parser.add_argument("-o", "--output", type=Path, default=None, help="결과 CSV (기본: <입력>_result.csv)")
    parser.add_argument("--question-column", default=None, help="질문 컬럼명 (기본: 자동 인식)")
    parser.add_argument("--limit", type=int, default=0, help="앞에서부터 N건만 실행")
    args = parser.parse_args()

    input_path = args.input.expanduser()
    if not input_path.exists():
        raise SystemExit(f"입력 파일이 없습니다: {input_path}")
    output_path = (args.output or input_path.with_name(f"{input_path.stem}_result.csv")).expanduser()

    rows, fields, column = read_rows(input_path, args.question_column)
    if args.limit > 0:
        rows = rows[: args.limit]
    print(f"질문 컬럼: {column} | 대상 {len(rows)}건 | 결과: {output_path}")

    out_fields = fields + [name for name in RESULT_COLUMNS if name not in fields]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=out_fields, extrasaction="ignore")
        writer.writeheader()
        for index, row in enumerate(rows, 1):
            question = str(row.get(column) or "").strip()
            if not question:
                continue
            started = time.perf_counter()
            try:
                row.update(run_question(question))
            except Exception as exc:  # noqa: BLE001 - 한 건 실패가 전체 실행을 막지 않는다.
                row.update(dict.fromkeys(RESULT_COLUMNS, ""))
                row["error"] = f"{type(exc).__name__}: {exc}"
            writer.writerow(row)
            handle.flush()
            status = "OK" if row["sql"] else (row["error"] or "SQL 없음")[:60]
            print(f"  [{index}/{len(rows)}] {question[:40]} -> {status} ({time.perf_counter() - started:.1f}s)")

    print(f"완료: {output_path}")


if __name__ == "__main__":
    main()

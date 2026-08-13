#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""골든셋 정답(SQL·결과)과 에이전트가 만든 답(SQL·결과)을 비교해 채점한다.

`run_goldenset_answers.py` 가 채워 넣은 정답 결과(`expected_answer`)를 기준으로,
같은 질의를 이 프로젝트의 에이전트에 태워 나온 SQL을 실행하고 **값 단위로** 비교한다.
정답 SQL과 문자열이 달라도(WHERE 조건이나 참조 테이블이 달라도) 결과가 같으면 정답으로 친다
(execution accuracy). SELECT 목록이 달라도 질의가 요구한 컬럼의 값만 맞으면 정답으로 친다
(후보가 컬럼을 더 냈으면 `subset_match`, 정답에만 있던 부가 컬럼이 빠졌으면 `required_match`).

    # 채점 로직만 점검 (에이전트·Athena 불필요)
    python scripts/run_goldenset_eval.py --self-test

    # 앞의 20건만 실제 에이전트로 채점
    python scripts/run_goldenset_eval.py --limit 20

    # 저장된 결과는 건너뛰고 나머지 전체 채점 (중단해도 이어서 실행)
    python scripts/run_goldenset_eval.py

    # 다른 곳에서 이미 뽑아둔 에이전트 SQL을 채점만 하기
    python scripts/run_goldenset_eval.py --predictions reports/agent-sql.jsonl

    # 실행 없이 리포트만 다시 생성
    python scripts/run_goldenset_eval.py --report-only

정답 결과의 해시·정규화 규칙은 `run_goldenset_answers.py` 의 것을 그대로 재사용하므로
정답과 후보가 완전히 같은 기준으로 비교된다.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for _path in (str(ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import run_goldenset_answers as golden  # noqa: E402  정규화·해시·실행기 재사용

DEFAULT_CASES_ANSWERED = ROOT / "tests" / "fixtures" / "corporate_sales_text2sql_goldenset_v2_answered.jsonl"
DEFAULT_CASES_BASE = ROOT / "tests" / "fixtures" / "corporate_sales_text2sql_goldenset_v2.jsonl"
DEFAULT_ANSWERS = ROOT / "reports" / "goldenset-v2-answers.jsonl"
DEFAULT_RESULTS = ROOT / "reports" / "goldenset-v2-eval-results.jsonl"
DEFAULT_SUMMARY = ROOT / "reports" / "goldenset-v2-eval-summary.md"
DEFAULT_REPORT_JSON = ROOT / "reports" / "goldenset-v2-eval.json"
DEFAULT_REVIEW_CSV = ROOT / "reports" / "goldenset-v2-eval-review.csv"

# 채점 결과(verdict). 위에서부터 좋은 순서다.
V_EXACT = "exact"                # 행 순서까지 일치
V_MATCH = "match"                # 행 집합 일치 (기본 정답 기준)
V_VALUE_MATCH = "value_match"    # 컬럼 순서·이름만 다르고 값 집합은 일치
V_SUBSET_MATCH = "subset_match"  # 정답 컬럼을 모두 담고 있고 후보가 컬럼을 더 냈을 뿐
V_REQUIRED_MATCH = "required_match"  # 질의가 요구한 컬럼은 모두 일치(정답의 부가 컬럼만 빠짐)
V_SCALAR_NEAR = "scalar_near"    # 단일 수치가 허용오차 안에서 같음(반올림 차이)
V_ROLLUP_MATCH = "rollup_match"  # 측정값 총합은 같은데 집계 축(행 단위)이 다름
V_SHAPE_ONLY = "shape_only"      # 행수·컬럼수만 같고 값은 다름
V_MISMATCH = "mismatch"          # 실행은 됐지만 결과가 다름
V_SQL_ERROR = "sql_error"        # 에이전트 SQL 실행 실패
V_NO_SQL = "no_sql"              # 에이전트가 SQL을 만들지 못함
V_AGENT_ERROR = "agent_error"    # 에이전트 호출 자체가 실패
V_NO_GOLD = "no_gold"            # 정답 결과가 없어 채점 불가(집계 제외)

CORRECT_VERDICTS = frozenset({V_EXACT, V_MATCH, V_VALUE_MATCH, V_SUBSET_MATCH, V_REQUIRED_MATCH,
                              V_SCALAR_NEAR})
SCORED_VERDICTS = frozenset({V_EXACT, V_MATCH, V_VALUE_MATCH, V_SUBSET_MATCH, V_REQUIRED_MATCH,
                             V_SCALAR_NEAR, V_ROLLUP_MATCH, V_SHAPE_ONLY, V_MISMATCH,
                             V_SQL_ERROR, V_NO_SQL, V_AGENT_ERROR})
VERDICT_ORDER = [V_EXACT, V_MATCH, V_VALUE_MATCH, V_SUBSET_MATCH, V_REQUIRED_MATCH,
                 V_SCALAR_NEAR, V_ROLLUP_MATCH, V_SHAPE_ONLY, V_MISMATCH, V_SQL_ERROR,
                 V_NO_SQL, V_AGENT_ERROR, V_NO_GOLD]

WS_RE = re.compile(r"\s+")
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[가-힣]+|\d+")
NON_WORD_RE = re.compile(r"[^0-9A-Za-z가-힣]+")
# '2026-07-01', '2026-07-01 00:00:00', '2026-07' 처럼 구분자만 다른 날짜 표기
DATE_LIKE_RE = re.compile(r"^(\d{4})[-/.](\d{2})(?:[-/.](\d{2}))?(?:[ T]00:00(?::00(?:\.0+)?)?)?$")
GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b([^)]*?)(?=\bHAVING\b|\bORDER\b|\bLIMIT\b|\bUNION\b|$)",
                         re.IGNORECASE | re.DOTALL)
# 2026 / 202607 / 20260701 처럼 더해봐야 의미 없는 연·월·일 표기
PERIOD_NUMBER_RE = re.compile(r"^(?:19|20)\d{2}(?:\d{2}){0,2}$")
SUBSET_MAX_COMBOS = 2000         # 컬럼 대응 탐색 상한(넓은 결과에서 조합 폭발 방지)
COLUMN_NGRAM = 3                 # 한글 컬럼명을 질의 어휘와 대조할 때 쓰는 n-gram 길이


# ---------------------------------------------------------------------------
# 정답셋 적재
# ---------------------------------------------------------------------------
def load_gold_cases(cases_path: Path, answers_path: Path) -> list[dict]:
    """골든셋을 읽고, `expected_answer` 가 비어 있으면 실행 이력에서 채운다."""
    cases = golden.read_jsonl(cases_path)
    if not cases:
        return []
    if all("expected_answer" in case for case in cases):
        return cases
    saved = golden.load_saved(answers_path) if answers_path.exists() else {}
    if not saved:
        return cases
    merged = []
    for case in cases:
        record = saved.get(case.get("id"))
        if record is not None and "expected_answer" not in case:
            case = dict(case)
            case["answer_status"] = record.get("status")
            case["expected_answer"] = record.get("answer")
            case["answer_error"] = record.get("error")
            case["answer_executed_at"] = record.get("executed_at")
        merged.append(case)
    return merged


def gold_answer(case: dict) -> dict | None:
    """채점에 쓸 수 있는 정답 결과만 돌려준다(실패·미실행은 None)."""
    answer = case.get("expected_answer")
    if not isinstance(answer, dict) or not answer.get("result_hash"):
        return None
    status = case.get("answer_status")
    if status not in (None, golden.STATUS_OK, golden.STATUS_EMPTY):
        return None
    return answer


# ---------------------------------------------------------------------------
# 후보 SQL 실행
# ---------------------------------------------------------------------------
def value_hash(columns: list[str], rows: list[tuple], float_digits: int) -> str:
    """컬럼 순서·이름을 무시하고 값 집합만 비교하기 위한 해시.

    같은 답을 컬럼 순서만 바꿔 낸 SQL을 '틀림'으로 깎지 않으려고 쓴다.
    """
    import hashlib

    normalized = [
        "\x1f".join(sorted(golden.normalize_value(v, float_digits) for v in row))
        for row in rows
    ]
    return hashlib.sha256("\x1e".join(sorted(normalized)).encode("utf-8")).hexdigest()


def execute_candidate(sql: str, executor, args, float_digits: int) -> dict:
    """후보 SQL을 정답과 동일한 조건으로 실행하고 요약 결과를 만든다."""
    prepared = golden.apply_schema(sql, args.schema, getattr(args, "table_names", golden.FALLBACK_TABLES))
    golden.assert_read_only(prepared)
    if args.row_limit:
        prepared = golden.wrap_row_limit(prepared, args.row_limit)

    started = time.monotonic()
    result = executor.run(prepared, args.fetch_rows)
    columns, rows = list(result["columns"]), list(result["rows"])
    hashes = golden.result_hashes(columns, rows, float_digits)
    return {
        "columns": columns,
        "row_count": len(rows),
        "row_count_truncated": len(rows) >= args.fetch_rows,
        "rows": [[golden.jsonable(v) for v in row] for row in rows[: args.answer_rows]],
        "rows_stored": min(len(rows), args.answer_rows),
        "scalar": golden.jsonable(golden.scalar_answer(columns, rows)),
        "result_hash": hashes["result_hash"],
        "ordered_hash": hashes["ordered_hash"],
        "value_hash": value_hash(columns, rows, float_digits),
        "float_digits": float_digits,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "athena": {k: v for k, v in (result.get("stats") or {}).items() if v is not None},
    }


def rebuild_answer_from_rows(columns: list, rows: list, args, float_digits: int) -> dict:
    """에이전트가 이미 들고 있는 결과를 그대로 채점 대상으로 만든다(--reuse-agent-rows)."""
    columns = [str(c) for c in columns or []]
    tuples = [tuple(r) for r in rows or []]
    hashes = golden.result_hashes(columns, tuples, float_digits)
    return {
        "columns": columns,
        "row_count": len(tuples),
        "row_count_truncated": False,
        "rows": [[golden.jsonable(v) for v in row] for row in tuples[: args.answer_rows]],
        "rows_stored": min(len(tuples), args.answer_rows),
        "scalar": golden.jsonable(golden.scalar_answer(columns, tuples)),
        "result_hash": hashes["result_hash"],
        "ordered_hash": hashes["ordered_hash"],
        "value_hash": value_hash(columns, tuples, float_digits),
        "float_digits": float_digits,
        "elapsed_ms": 0,
        "athena": {},
    }


# ---------------------------------------------------------------------------
# 채점
# ---------------------------------------------------------------------------
def numeric(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def scalar_close(expected: Any, actual: Any, rel_tol: float) -> bool:
    """단일 수치 정답 비교. 실수 오차와 반올림 차이를 허용한다."""
    if expected is None or actual is None:
        return False
    left, right = numeric(expected), numeric(actual)
    if left is None or right is None:
        return str(expected).strip() == str(actual).strip()
    if left == right:
        return True
    scale = max(abs(left), abs(right))
    return abs(left - right) <= max(rel_tol * scale, 1e-9)


def stored_rows(answer: dict) -> list[list] | None:
    """해시가 아니라 값으로 직접 대조할 수 있을 만큼 행이 온전히 남아 있을 때만 돌려준다."""
    if not isinstance(answer, dict) or answer.get("row_count_truncated"):
        return None
    rows = answer.get("rows")
    if not isinstance(rows, list) or len(rows) != (answer.get("row_count") or 0):
        return None                      # 저장 상한(--answer-rows)에 걸려 일부만 남은 결과
    return [list(row) for row in rows]


def loose_value(value: Any, float_digits: int) -> str:
    """관대한 비교(컬럼 대조) 전용 정규화. 날짜 표기 차이를 흡수한다.

    같은 날짜를 '20260701' 로 내는 SQL과 '2026-07-01' 로 내는 SQL을 다른 값으로 보지 않는다.
    해시 비교는 그대로 엄격하게 두고, 여기서만 느슨하게 본다.
    """
    text = golden.normalize_value(value, float_digits)
    match = DATE_LIKE_RE.match(text)
    return "".join(part for part in match.groups() if part) if match else text


def squash(text: str) -> str:
    """이름·질의를 붙여 쓴 소문자 문자열로 만든다(공백·기호·따옴표 제거)."""
    return NON_WORD_RE.sub("", text or "").lower()


def column_mentioned(name: str, haystack: str) -> bool:
    """컬럼 이름이 질의 어휘에 실제로 등장하는지 본다.

    한글 컬럼명은 '일매출금액' 처럼 붙여 쓰므로 공백 토큰이 아니라 n-gram 겹침으로 본다.
    """
    if not name or not haystack:
        return False
    if len(name) <= COLUMN_NGRAM:
        return name in haystack
    return any(name[i:i + COLUMN_NGRAM] in haystack
               for i in range(len(name) - COLUMN_NGRAM + 1))


def required_indexes(case: dict, gold_columns: list[str]) -> tuple[list[int], bool]:
    """정답 컬럼 중 질의가 실제로 요구한 것의 위치와, 질의 어휘에 걸린 게 있었는지를 돌려준다.

    골든셋 SQL은 질문에 없는 보조 컬럼을 함께 내는 경우가 있다(예: "일별 매출금액" 케이스의
    `매출건수`). 후보가 그 보조 컬럼까지 내지 않았다고 오답으로 깎지 않으려고 쓴다.
    어휘는 질문에 `category`·`metrics` 를 더해서 본다. 정답 SQL의 `GROUP BY` 키는 답의 축이라
    질문에 이름이 안 나와도 필수로 보되, 그것만으로는 관대한 판정을 열어주지 않는다
    (질의 어휘에 걸린 컬럼이 하나도 없으면 측정값을 통째로 빠뜨린 후보가 통과할 수 있다).
    """
    terms = [str(case.get("question") or ""), str(case.get("category") or "")]
    terms += [str(metric) for metric in (case.get("metrics") or [])]
    haystack = squash(" ".join(terms))
    keys = squash(" ".join(GROUP_BY_RE.findall(str(case.get("sql") or ""))))
    mentioned = {i for i, column in enumerate(gold_columns)
                 if column_mentioned(squash(column), haystack)}
    grouped = {i for i, column in enumerate(gold_columns)
               if squash(column) and squash(column) in keys}
    return sorted(mentioned | grouped), bool(mentioned)


def column_subset(sub_columns: list[str], sub_rows: list[list],
                  sup_columns: list[str], sup_rows: list[list],
                  float_digits: int) -> list[str] | None:
    """`sub` 의 모든 컬럼이 `sup` 안에 그대로 들어 있으면 대응된 `sup` 컬럼명을 돌려준다.

    질의에 필요한 값은 다 냈는데 설명용 컬럼을 더 붙인 SQL을 오답으로 깎지 않으려고 쓴다.
    행 순서는 무시하되 행 단위 조합은 유지하므로, 컬럼별 값 집합만 우연히 같고
    조합이 뒤바뀐 결과(A-20, B-10)는 걸러진다.
    """
    if not sub_rows or len(sub_rows) != len(sup_rows):
        return None
    if not sub_columns or len(sub_columns) > len(sup_columns):
        return None

    def column(rows: list[list], index: int) -> list[str]:
        return [loose_value(row[index], float_digits) for row in rows]

    sub_values = [column(sub_rows, i) for i in range(len(sub_columns))]
    sup_values = [column(sup_rows, j) for j in range(len(sup_columns))]
    # 값 집합이 같은 컬럼만 후보로 남겨 조합 탐색을 줄인다(대개 하나로 좁혀진다).
    options = [[j for j, values in enumerate(sup_values) if sorted(values) == sorted(wanted)]
               for wanted in sub_values]
    if any(not option for option in options):
        return None

    target = sorted("\x1f".join(row) for row in zip(*sub_values))
    for combo in itertools.islice(itertools.product(*options), SUBSET_MAX_COMBOS):
        if len(set(combo)) != len(combo):
            continue                     # 한 컬럼을 두 번 쓰는 대응은 인정하지 않는다
        projected = sorted(
            "\x1f".join(sup_values[j][r] for j in combo) for r in range(len(sup_rows))
        )
        if projected == target:
            return [sup_columns[j] for j in combo]
    return None


def measure_sums(columns: list[str], rows: list[list], float_digits: int) -> list[float]:
    """더해서 의미가 있는 수치 컬럼(측정값)의 합계만 뽑는다.

    연월·일자처럼 축 역할을 하는 숫자 컬럼은 뺀다. 집계 축이 달라 행 수는 다르지만
    총합은 같은 결과를 알아보기 위한 것이다.
    """
    totals = []
    for index in range(len(columns)):
        values = [loose_value(row[index], float_digits) for row in rows]
        if not values or all(PERIOD_NUMBER_RE.match(value) for value in values):
            continue
        numbers = [numeric(value) for value in values]
        if any(number is None for number in numbers):
            continue
        totals.append(sum(numbers))
    return totals


def rollup_totals_match(wanted: list[float], got: list[float], rel_tol: float) -> bool:
    """정답의 측정값 총합이 후보 쪽에도 하나씩 대응되는지 본다."""
    if not wanted or not got:
        return False
    remaining = list(got)
    for total in wanted:
        hit = next((i for i, other in enumerate(remaining) if scalar_close(total, other, rel_tol)),
                   None)
        if hit is None:
            return False
        remaining.pop(hit)
    return True


def sql_tables(sql: str, known: frozenset[str]) -> set[str]:
    """SQL에서 물리 테이블 참조만 뽑는다(CTE 별칭은 제외)."""
    found = set()
    for match in golden.TABLE_REF_RE.finditer(sql or ""):
        table = match.group(3).lower()
        if table in known:
            found.add(table)
    return found


def table_metrics(expected: list, actual: set[str]) -> dict:
    want = {str(t).split(".")[-1].strip().lower() for t in (expected or []) if str(t).strip()}
    if not want:
        return {"expected": [], "actual": sorted(actual), "recall": None,
                "precision": None, "exact": None, "missing": [], "extra": []}
    hit = want & actual
    return {
        "expected": sorted(want),
        "actual": sorted(actual),
        "recall": len(hit) / len(want),
        "precision": (len(hit) / len(actual)) if actual else 0.0,
        "exact": want == actual,
        "missing": sorted(want - actual),
        "extra": sorted(actual - want),
    }


def normalize_sql_text(sql: str) -> str:
    return WS_RE.sub(" ", (sql or "").strip().rstrip(";")).lower()


def sql_token_overlap(left: str, right: str) -> float:
    a = set(TOKEN_RE.findall((left or "").lower()))
    b = set(TOKEN_RE.findall((right or "").lower()))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def score(case: dict, gold: dict | None, candidate: dict | None, candidate_sql: str,
          *, agent_error: str = "", sql_error: str = "", rel_tol: float = 1e-6,
          known_tables: frozenset[str] = golden.FALLBACK_TABLES) -> dict:
    """정답 한 건과 후보 한 건을 비교해 verdict와 세부 지표를 만든다."""
    tables = table_metrics(case.get("expected_tables") or [], sql_tables(candidate_sql, known_tables))
    detail: dict[str, Any] = {
        "tables": tables,
        "sql_text_equal": bool(candidate_sql) and
        normalize_sql_text(candidate_sql) == normalize_sql_text(case.get("sql", "")),
        "sql_token_overlap": round(sql_token_overlap(candidate_sql, case.get("sql", "")), 4),
    }

    if agent_error:
        return {"verdict": V_AGENT_ERROR, "correct": False, "detail": detail}
    if not (candidate_sql or "").strip():
        return {"verdict": V_NO_SQL, "correct": False, "detail": detail}
    if sql_error:
        return {"verdict": V_SQL_ERROR, "correct": False, "detail": detail}
    if gold is None:
        return {"verdict": V_NO_GOLD, "correct": False, "detail": detail}
    if candidate is None:
        return {"verdict": V_SQL_ERROR, "correct": False, "detail": detail}

    gold_cols = [str(c) for c in (gold.get("columns") or [])]
    cand_cols = [str(c) for c in (candidate.get("columns") or [])]
    result_match = gold.get("result_hash") == candidate.get("result_hash")
    ordered_match = gold.get("ordered_hash") == candidate.get("ordered_hash")
    gold_value_hash = gold.get("value_hash")
    value_match = bool(gold_value_hash) and gold_value_hash == candidate.get("value_hash")
    row_count_match = gold.get("row_count") == candidate.get("row_count")
    scalar_match = scalar_close(gold.get("scalar"), candidate.get("scalar"), rel_tol)

    # SELECT 목록이 달라도 질의에 필요한 값이 다 나왔는지 본다(컬럼 부분집합).
    float_digits = int(gold.get("float_digits") or candidate.get("float_digits") or 6)
    gold_rows, cand_rows = stored_rows(gold), stored_rows(candidate)
    subset, subset_columns, required_cols = "", [], []
    subset_skipped = False
    if not (result_match or value_match):
        if gold_rows is None or cand_rows is None:
            subset_skipped = True
        else:
            matched = column_subset(gold_cols, gold_rows, cand_cols, cand_rows, float_digits)
            if matched is not None:
                subset, subset_columns = "candidate_contains_gold", matched
            else:
                # 정답이 질문에 없는 보조 컬럼까지 낸 경우, 요구된 컬럼만 뽑아 다시 대조한다.
                keep, mentioned = required_indexes(case, gold_cols)
                required_cols = [gold_cols[i] for i in keep]
                if mentioned and keep and len(keep) < len(gold_cols):
                    matched = column_subset(
                        required_cols, [[row[i] for i in keep] for row in gold_rows],
                        cand_cols, cand_rows, float_digits,
                    )
                    if matched is not None:
                        subset, subset_columns = "required_columns_match", matched
            if not subset and column_subset(
                cand_cols, cand_rows, gold_cols, gold_rows, float_digits
            ) is not None:
                subset = "gold_contains_candidate"

    # 행 수가 다른데 측정값 총합이 같으면 "필터는 맞고 집계 축만 다르다"로 갈라 둔다.
    gold_totals = measure_sums(gold_cols, gold_rows, float_digits) if gold_rows else []
    cand_totals = measure_sums(cand_cols, cand_rows, float_digits) if cand_rows else []
    rollup = (not subset and not row_count_match
              and rollup_totals_match(gold_totals, cand_totals, rel_tol))

    detail.update({
        "result_match": result_match,
        "ordered_match": ordered_match,
        "value_match": value_match,
        "column_subset": subset or None,
        "subset_columns": subset_columns,
        "required_columns": required_cols,
        "rollup_match": rollup,
        "expected_totals": [round(t, 4) for t in gold_totals],
        "actual_totals": [round(t, 4) for t in cand_totals],
        "row_count_match": row_count_match,
        "column_count_match": len(gold_cols) == len(cand_cols),
        "column_name_overlap": round(
            len(set(gold_cols) & set(cand_cols)) / len(set(gold_cols) | set(cand_cols)), 4
        ) if (gold_cols or cand_cols) else None,
        "scalar_match": scalar_match,
        "expected_row_count": gold.get("row_count"),
        "actual_row_count": candidate.get("row_count"),
        "expected_scalar": gold.get("scalar"),
        "actual_scalar": candidate.get("scalar"),
        "expected_columns": gold_cols,
        "actual_columns": cand_cols,
    })
    notes = []
    if gold.get("row_count_truncated") or candidate.get("row_count_truncated"):
        # 양쪽 다 상한에 걸리면 해시가 상한 이후 행을 반영하지 못한다.
        notes.append("row_limit_truncated")
    if gold.get("float_digits") != candidate.get("float_digits"):
        notes.append("float_digits_differ")
    if gold_value_hash is None:
        notes.append("gold_value_hash_missing")
    if subset_skipped:
        # 저장된 행이 잘려 있어 컬럼 부분집합까지는 확인하지 못했다는 뜻이다.
        notes.append("subset_check_skipped")
    if subset == "gold_contains_candidate":
        notes.append("candidate_columns_missing")
    detail["notes"] = notes

    if result_match and ordered_match:
        verdict = V_EXACT
    elif result_match:
        verdict = V_MATCH
    elif value_match:
        verdict = V_VALUE_MATCH
    elif subset == "candidate_contains_gold":
        verdict = V_SUBSET_MATCH
    elif subset == "required_columns_match":
        verdict = V_REQUIRED_MATCH
    elif scalar_match:
        # 1행 1열끼리 허용오차 안에서 같다 = 반올림 차이일 뿐 같은 답이다.
        verdict = V_SCALAR_NEAR
    elif rollup:
        verdict = V_ROLLUP_MATCH
    elif row_count_match and len(gold_cols) == len(cand_cols):
        verdict = V_SHAPE_ONLY
    else:
        verdict = V_MISMATCH
    return {"verdict": verdict, "correct": verdict in CORRECT_VERDICTS, "detail": detail}


# ---------------------------------------------------------------------------
# 후보 생성기 (에이전트 / 사전 예측 / 스텁)
# ---------------------------------------------------------------------------
class AgentPredictor:
    """서비스와 같은 LangGraph 그래프를 태워 SQL을 얻는다.

    파라미터가 비면 `run_semantic_golden_live.py` 의 기본값 채우기를 재사용해
    같은 케이스를 이어서 진행한다(사람 입력 없이 끝까지 도는 것이 목적이다).
    """

    name = "agent"

    def __init__(self, max_param_rounds: int = 3):
        import run_semantic_golden_live as live  # noqa: WPS433  지연 import

        self._live = live
        self._max_param_rounds = max_param_rounds

    def _param_case(self, case: dict) -> dict:
        return {"parameters": {"reference_date": case.get("reference_date") or ""}}

    def predict(self, case: dict) -> dict:
        question = str(case.get("question") or "")
        result = self._live.invoke_real_agent(question)
        applied: dict[str, Any] = {}
        rounds = 0
        for _ in range(self._max_param_rounds):
            if not isinstance(result, dict) or result.get("param_stage") != "need_params":
                break
            missing = result.get("missing_params") or []
            proposed = self._live._default_params(self._param_case(case), missing)
            fresh = {k: v for k, v in proposed.items() if k not in applied}
            if not fresh:
                break
            applied.update(fresh)
            rounds += 1
            result = self._live.invoke_real_agent(
                question, continuation=result, params=dict(applied)
            )
        if not isinstance(result, dict):
            raise TypeError("에이전트가 dict가 아닌 %s 를 돌려줬다" % type(result).__name__)
        return {
            "sql": result.get("final_sql") or result.get("generated_sql") or "",
            "columns": result.get("query_columns") or [],
            "rows": result.get("query_rows") or [],
            "meta": {
                "question_type": result.get("question_type"),
                "selected_domain": result.get("selected_domain"),
                "selected_capability_type": result.get("selected_capability_type"),
                "selected_capability_name": result.get("selected_capability_name"),
                "selected_tool": result.get("selected_tool"),
                "matched_query_name": result.get("matched_query_name"),
                "param_stage": result.get("param_stage"),
                "missing_params": result.get("missing_params") or [],
                "default_params_applied": applied,
                "default_param_rounds": rounds,
                "is_valid": result.get("is_valid"),
                "retry_count": result.get("retry_count"),
                "answer": (result.get("answer") or "")[:2000]
                if isinstance(result.get("answer"), str) else result.get("answer"),
                "query_error": result.get("query_error"),
                "error_message": result.get("error_message"),
            },
        }


class PredictionFilePredictor:
    """이미 뽑아둔 에이전트 SQL(JSONL)을 읽어 채점만 한다."""

    name = "predictions"

    def __init__(self, path: Path):
        self._by_id: dict[str, dict] = {}
        for row in golden.read_jsonl(path):
            case_id = str(row.get("id") or "")
            if case_id:
                self._by_id[case_id] = row
        if not self._by_id:
            raise ValueError("예측 파일이 비어 있다: %s" % path)

    def predict(self, case: dict) -> dict:
        row = self._by_id.get(case["id"])
        if row is None:
            return {"sql": "", "columns": [], "rows": [], "meta": {"missing_prediction": True}}
        sql = row.get("sql") or row.get("final_sql") or row.get("generated_sql") or ""
        return {
            "sql": sql,
            "columns": row.get("columns") or row.get("query_columns") or [],
            "rows": row.get("rows") or row.get("query_rows") or [],
            "meta": {k: v for k, v in row.items()
                     if k not in {"sql", "final_sql", "generated_sql", "columns",
                                  "query_columns", "rows", "query_rows"}},
        }


class StubPredictor:
    """--self-test 전용. 케이스 순서에 따라 정답/오답/무응답을 섞어 낸다."""

    name = "stub"

    def predict(self, case: dict) -> dict:
        digest = sum(ord(c) for c in str(case.get("id", "")))
        if digest % 7 == 0:
            return {"sql": "", "columns": [], "rows": [], "meta": {"stub": "no_sql"}}
        if digest % 5 == 0:
            return {"sql": "SELECT 1 AS x FROM tbdaa1d12", "columns": [], "rows": [],
                    "meta": {"stub": "wrong"}}
        return {"sql": case.get("sql", ""), "columns": [], "rows": [], "meta": {"stub": "gold"}}


def build_predictor(args) -> Any:
    if args.predictions:
        return PredictionFilePredictor(args.predictions)
    if args.predictor == "stub":
        return StubPredictor()
    return AgentPredictor(args.max_param_rounds)


# ---------------------------------------------------------------------------
# 한 건 채점
# ---------------------------------------------------------------------------
def evaluate_case(case: dict, predictor, executor, args) -> dict:
    gold = gold_answer(case)
    float_digits = int((gold or {}).get("float_digits") or args.float_digits)
    started = time.monotonic()
    record: dict[str, Any] = {
        "id": case["id"],
        "question": case.get("question", ""),
        "domain": case.get("domain", ""),
        "query_shape": case.get("query_shape", ""),
        "difficulty": case.get("difficulty", ""),
        "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "predictor": predictor.name,
        "gold_status": case.get("answer_status") or ("ok" if gold else "not_executed"),
    }

    agent_error = ""
    prediction: dict[str, Any] = {"sql": "", "columns": [], "rows": [], "meta": {}}
    try:
        prediction = predictor.predict(case)
    except Exception as exc:  # noqa: BLE001 - 한 건 실패가 전체를 멈추면 안 된다
        agent_error = "%s: %s" % (type(exc).__name__, str(exc)[:1500])

    candidate_sql = str(prediction.get("sql") or "").strip()
    record["agent"] = {
        "sql": candidate_sql,
        "error": agent_error,
        "meta": prediction.get("meta") or {},
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }

    candidate = None
    sql_error = ""
    if candidate_sql and not agent_error:
        if args.reuse_agent_rows and prediction.get("columns"):
            candidate = rebuild_answer_from_rows(
                prediction.get("columns"), prediction.get("rows"), args, float_digits
            )
        elif gold is not None or args.execute_without_gold:
            try:
                candidate = execute_candidate(candidate_sql, executor, args, float_digits)
            except Exception as exc:  # noqa: BLE001
                sql_error = "%s: %s" % (type(exc).__name__, str(exc)[:1500])

    if gold is not None and "value_hash" not in gold:
        # 정답 파일에는 value_hash가 없다. 저장된 정답 행으로 다시 계산할 수 있을 때만 채운다.
        stored = gold.get("rows") or []
        if stored and not gold.get("row_count_truncated") and len(stored) == gold.get("row_count"):
            gold = dict(gold)
            gold["value_hash"] = value_hash(
                [str(c) for c in gold.get("columns") or []],
                [tuple(r) for r in stored],
                float_digits,
            )

    outcome = score(
        case, gold, candidate, candidate_sql,
        agent_error=agent_error, sql_error=sql_error,
        rel_tol=args.rel_tol, known_tables=getattr(args, "table_names", golden.FALLBACK_TABLES),
    )
    record.update({
        "verdict": outcome["verdict"],
        "correct": outcome["correct"],
        "detail": outcome["detail"],
        "candidate": candidate,
        "sql_error": sql_error,
        "expected_scalar": (gold or {}).get("scalar"),
        "expected_row_count": (gold or {}).get("row_count"),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    })
    return record


def select_cases(cases: list[dict], saved: dict[str, dict], args) -> list[dict]:
    pending = []
    for case in cases:
        if args.ids and case["id"] not in args.ids:
            continue
        if args.domain and case.get("domain") != args.domain:
            continue
        if args.shape and case.get("query_shape") != args.shape:
            continue
        if args.difficulty and case.get("difficulty") != args.difficulty:
            continue
        if args.only_with_gold and gold_answer(case) is None:
            continue
        prev = saved.get(case["id"])
        if args.rerun_all:
            pending.append(case)
        elif args.retry_errors:
            if prev is not None and prev.get("verdict") in (V_AGENT_ERROR, V_SQL_ERROR, V_NO_SQL):
                pending.append(case)
        elif prev is None:
            pending.append(case)
    if args.limit:
        pending = pending[: args.limit]
    return pending


# ---------------------------------------------------------------------------
# 집계·리포트
# ---------------------------------------------------------------------------
def _ratio(hit: int, total: int) -> float | None:
    return round(hit / total, 4) if total else None


def _bucket(records: list[dict]) -> dict:
    scored = [r for r in records if r.get("verdict") in SCORED_VERDICTS]
    correct = [r for r in scored if r.get("correct")]
    exact = [r for r in scored if r.get("verdict") == V_EXACT]
    ran = [r for r in scored if r.get("candidate")]
    produced = [r for r in scored if (r.get("agent") or {}).get("sql")]
    recalls = [(r.get("detail") or {}).get("tables", {}).get("recall")
               for r in records]
    recalls = [v for v in recalls if isinstance(v, (int, float))]
    return {
        "total": len(records),
        "scored": len(scored),
        "no_gold": len([r for r in records if r.get("verdict") == V_NO_GOLD]),
        "execution_accuracy": _ratio(len(correct), len(scored)),
        "strict_accuracy": _ratio(len(exact), len(scored)),
        "sql_coverage": _ratio(len(produced), len(scored)),
        "sql_runnable": _ratio(len(ran), len(produced)) if produced else None,
        "table_recall_avg": round(sum(recalls) / len(recalls), 4) if recalls else None,
        "verdicts": {v: len([r for r in records if r.get("verdict") == v])
                     for v in VERDICT_ORDER
                     if any(r.get("verdict") == v for r in records)},
    }


def build_report(cases: list[dict], saved: dict[str, dict], args) -> dict:
    records = [saved[c["id"]] for c in cases if c["id"] in saved]
    by: dict[str, dict[str, list[dict]]] = {"domain": {}, "query_shape": {}, "difficulty": {}}
    for record in records:
        for key in by:
            by[key].setdefault(record.get(key) or "(없음)", []).append(record)

    total_ms = sum(r.get("elapsed_ms") or 0 for r in records)
    scanned = sum(((r.get("candidate") or {}).get("athena") or {}).get("data_scanned_bytes") or 0
                  for r in records)
    # 후보를 무엇으로 만들었는지는 저장된 결과에 남는다(리포트만 다시 그릴 때도 정확하다).
    used = sorted({str(r.get("predictor") or "") for r in records if r.get("predictor")})
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "cases_file": str(args.cases),
        "results_file": str(args.results),
        "predictor": ", ".join(used) if used else
        ("predictions" if args.predictions else args.predictor),
        "case_total": len(cases),
        "evaluated": len(records),
        "not_evaluated": len(cases) - len(records),
        "overall": _bucket(records),
        "by_domain": {k: _bucket(v) for k, v in sorted(by["domain"].items())},
        "by_query_shape": {k: _bucket(v) for k, v in sorted(by["query_shape"].items())},
        "by_difficulty": {k: _bucket(v) for k, v in sorted(by["difficulty"].items())},
        "elapsed_seconds": round(total_ms / 1000, 1),
        "data_scanned_gb": round(scanned / 1024 ** 3, 3) if scanned else 0.0,
        "failures": [
            {
                "id": r["id"],
                "question": r.get("question", ""),
                "verdict": r.get("verdict"),
                "expected_scalar": r.get("expected_scalar"),
                "actual_scalar": (r.get("candidate") or {}).get("scalar"),
                "expected_row_count": r.get("expected_row_count"),
                "actual_row_count": (r.get("candidate") or {}).get("row_count"),
                "missing_tables": (r.get("detail") or {}).get("tables", {}).get("missing"),
                "error": (r.get("sql_error") or (r.get("agent") or {}).get("error") or "")[:300],
            }
            for r in records
            if r.get("verdict") in SCORED_VERDICTS and not r.get("correct")
        ],
    }


def _pct(value: Any) -> str:
    return "-" if value is None else "%.1f%%" % (float(value) * 100)


def render_markdown(report: dict) -> str:
    overall = report["overall"]
    lines = ["# 골든셋 채점 요약", ""]
    lines.append("- 생성 시각: %s" % report["generated_at"])
    lines.append("- 골든셋: `%s`" % report["cases_file"])
    lines.append("- 후보 생성: `%s`" % report["predictor"])
    lines.append("- 전체 %d건 / 채점 %d건 / 미채점 %d건 / 정답없음 %d건"
                 % (report["case_total"], report["evaluated"],
                    report["not_evaluated"], overall.get("no_gold", 0)))
    lines.append("")
    lines.append("| 지표 | 값 |")
    lines.append("|---|---:|")
    lines.append("| 실행 정확도 (결과 일치) | %s |" % _pct(overall["execution_accuracy"]))
    lines.append("| 엄격 정확도 (행 순서까지) | %s |" % _pct(overall["strict_accuracy"]))
    lines.append("| SQL 생성률 | %s |" % _pct(overall["sql_coverage"]))
    lines.append("| 생성 SQL 실행 성공률 | %s |" % _pct(overall["sql_runnable"]))
    lines.append("| 테이블 재현율 평균 | %s |" % _pct(overall["table_recall_avg"]))
    if report.get("data_scanned_gb"):
        lines.append("| 후보 SQL 누적 스캔량 | %.2f GB |" % report["data_scanned_gb"])
    lines.append("")
    lines.append("## 판정 분포")
    lines.append("")
    lines.append("| 판정 | 건수 |")
    lines.append("|---|---:|")
    for verdict in VERDICT_ORDER:
        count = overall["verdicts"].get(verdict)
        if count:
            lines.append("| %s | %d |" % (verdict, count))
    for title, key in (("도메인별", "by_domain"), ("질의형태별", "by_query_shape"),
                       ("난이도별", "by_difficulty")):
        buckets = report.get(key) or {}
        if not buckets:
            continue
        lines.extend(["", "## %s" % title, "",
                      "| 구분 | 채점 | 실행 정확도 | 엄격 정확도 | SQL 생성률 |",
                      "|---|---:|---:|---:|---:|"])
        for name, bucket in buckets.items():
            lines.append("| %s | %d | %s | %s | %s |" % (
                name, bucket["scored"], _pct(bucket["execution_accuracy"]),
                _pct(bucket["strict_accuracy"]), _pct(bucket["sql_coverage"])))
    failures = report.get("failures") or []
    if failures:
        lines.extend(["", "## 오답 (최대 40건)", "",
                      "| id | 판정 | 기대 | 실제 | 비고 |", "|---|---|---|---|---|"])
        for item in failures[:40]:
            expected = item.get("expected_scalar")
            actual = item.get("actual_scalar")
            expected = expected if expected is not None else "%s행" % item.get("expected_row_count")
            actual = actual if actual is not None else "%s행" % item.get("actual_row_count")
            note = item.get("error") or ("누락테이블 %s" % ", ".join(item.get("missing_tables") or [])
                                         if item.get("missing_tables") else "")
            lines.append("| `%s` | %s | %s | %s | %s |" % (
                item["id"], item["verdict"], expected, actual,
                str(note).replace("|", "/").replace("\n", " ")[:160]))
        if len(failures) > 40:
            lines.append("")
            lines.append("외 %d건은 결과 JSONL을 참고한다." % (len(failures) - 40))
    return "\n".join(lines) + "\n"


def write_review_csv(path: Path, cases: list[dict], saved: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "id", "domain", "query_shape", "difficulty", "question", "verdict", "correct",
            "expected_scalar", "actual_scalar", "expected_row_count", "actual_row_count",
            "table_recall", "missing_tables", "sql_token_overlap", "error", "agent_sql",
        ])
        for case in cases:
            record = saved.get(case["id"])
            if record is None:
                writer.writerow([case["id"], case.get("domain", ""), case.get("query_shape", ""),
                                 case.get("difficulty", ""), case.get("question", ""),
                                 "not_evaluated", "", "", "", "", "", "", "", "", "", ""])
                continue
            detail = record.get("detail") or {}
            tables = detail.get("tables") or {}
            candidate = record.get("candidate") or {}
            writer.writerow([
                record["id"], record.get("domain", ""), record.get("query_shape", ""),
                record.get("difficulty", ""), record.get("question", ""),
                record.get("verdict"), "Y" if record.get("correct") else "N",
                record.get("expected_scalar", ""), candidate.get("scalar", ""),
                record.get("expected_row_count", ""), candidate.get("row_count", ""),
                tables.get("recall", ""), ", ".join(tables.get("missing") or []),
                detail.get("sql_token_overlap", ""),
                (record.get("sql_error") or (record.get("agent") or {}).get("error") or "")[:300],
                ((record.get("agent") or {}).get("sql") or "")[:2000],
            ])


def threshold_exit(report: dict, args) -> int:
    """--fail-under 기준 판정. 채점을 새로 돌렸든 리포트만 그렸든 같은 기준을 적용한다."""
    if not args.fail_under:
        return 0
    accuracy = report["overall"]["execution_accuracy"]
    if accuracy is None or accuracy < args.fail_under:
        print("실행 정확도(%s)가 기준(%.1f%%)에 미달한다."
              % (_pct(accuracy), args.fail_under * 100))
        return 1
    return 0


def write_reports(cases: list[dict], saved: dict[str, dict], args) -> dict:
    report = build_report(cases, saved, args)
    args.report_json.parent.mkdir(parents=True, exist_ok=True)
    args.report_json.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(render_markdown(report), encoding="utf-8")
    write_review_csv(args.review_csv, cases, saved)
    return report


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------
def self_test() -> int:
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        print(("  PASS " if cond else "  FAIL ") + name)
        ok = ok and cond

    print("self-test")
    columns = ["구분", "금액"]
    rows_a = [("A", 10), ("B", 20)]
    rows_b = [("B", 20), ("A", 10)]
    gold_hashes = golden.result_hashes(columns, rows_a, 6)
    cand_hashes = golden.result_hashes(columns, rows_b, 6)

    def answer(cols, rws, hashes, **extra):
        base = {
            "columns": cols, "row_count": len(rws), "row_count_truncated": False,
            "rows": [list(r) for r in rws], "rows_stored": len(rws),
            "scalar": golden.scalar_answer(cols, rws),
            "result_hash": hashes["result_hash"], "ordered_hash": hashes["ordered_hash"],
            "value_hash": value_hash(cols, rws, 6), "float_digits": 6,
        }
        base.update(extra)
        return base

    case = {"id": "t1", "sql": "SELECT 1 FROM tbdaa1d12", "expected_tables": ["tbdaa1d12"]}
    gold = answer(columns, rows_a, gold_hashes)

    same = score(case, gold, answer(columns, rows_a, gold_hashes), "SELECT 1 FROM tbdaa1d12")
    check("동일 결과 → exact", same["verdict"] == V_EXACT and same["correct"])

    reordered = score(case, gold, answer(columns, rows_b, cand_hashes), "SELECT 1 FROM tbdaa1d12")
    check("행 순서만 다름 → match", reordered["verdict"] == V_MATCH and reordered["correct"])

    swapped_cols = ["금액", "구분"]
    swapped_rows = [(10, "A"), (20, "B")]
    swapped = score(case, gold,
                    answer(swapped_cols, swapped_rows,
                           golden.result_hashes(swapped_cols, swapped_rows, 6)),
                    "SELECT 1 FROM tbdaa1d12")
    check("컬럼 순서만 다름 → value_match",
          swapped["verdict"] == V_VALUE_MATCH and swapped["correct"])

    wide_cols = ["구분", "금액", "비중"]
    wide_rows = [("A", 10, 0.33), ("B", 20, 0.67)]
    wider = score(case, gold,
                  answer(wide_cols, wide_rows, golden.result_hashes(wide_cols, wide_rows, 6)),
                  "SELECT 1 FROM tbdaa1d12")
    check("컬럼을 더 냈지만 정답 값 포함 → subset_match",
          wider["verdict"] == V_SUBSET_MATCH and wider["correct"])
    check("대응된 컬럼 기록", wider["detail"]["subset_columns"] == ["구분", "금액"])

    narrow_cols = ["금액"]
    narrow_rows = [(10,), (20,)]
    narrower = score(case, gold,
                     answer(narrow_cols, narrow_rows,
                            golden.result_hashes(narrow_cols, narrow_rows, 6)),
                     "SELECT 1 FROM tbdaa1d12")
    check("정답 컬럼을 덜 냄 → 정답 아님",
          not narrower["correct"] and
          narrower["detail"]["column_subset"] == "gold_contains_candidate")

    # 실제 케이스: 정답이 질문에 없는 '매출건수'까지 내고, 후보는 날짜 표기가 다르다.
    daily_case = {
        "id": "t3", "question": "2026년 7월 법인카드 일별 매출금액을 보여줘",
        "category": "일별 법인카드 매출", "metrics": ["카드매출금액"],
        "sql": "SELECT 1 FROM tbdaabt30", "expected_tables": ["tbdaabt30"],
    }
    daily_cols = ["전표매출년월일", "일매출금액", "매출건수"]
    daily_rows = [("20260701", 100, 3), ("20260702", 200, 5)]
    daily_gold = answer(daily_cols, daily_rows, golden.result_hashes(daily_cols, daily_rows, 6))
    agent_cols = ["기준년월일", "일별매출금액"]
    agent_rows = [("2026-07-01", 100), ("2026-07-02", 200)]
    daily = score(daily_case, daily_gold,
                  answer(agent_cols, agent_rows, golden.result_hashes(agent_cols, agent_rows, 6)),
                  "SELECT 1 FROM tbdaabt30")
    check("정답의 부가 컬럼(매출건수)은 빼도 됨 → required_match",
          daily["verdict"] == V_REQUIRED_MATCH and daily["correct"])
    check("요구 컬럼 기록", daily["detail"]["required_columns"] == ["일매출금액"])

    date_only_cols = ["기준년월일"]
    date_only_rows = [("20260701",), ("20260702",)]
    date_only = score(daily_case, daily_gold,
                      answer(date_only_cols, date_only_rows,
                             golden.result_hashes(date_only_cols, date_only_rows, 6)),
                      "SELECT 1 FROM tbdaabt30")
    check("요구 컬럼(매출금액)을 빼면 정답 아님",
          not date_only["correct"] and
          date_only["detail"]["column_subset"] == "gold_contains_candidate")

    total_case = {"id": "t5", "question": "2026년 7월 총매출을 알려줘", "category": "월 매출",
                  "sql": "SELECT 1 FROM tbdaabt30", "expected_tables": ["tbdaabt30"]}
    total_cols, total_rows = ["총매출"], [(300,)]
    split_cols = ["전표매출년월일", "일매출금액"]
    split_rows = [("20260701", 100), ("20260702", 200)]
    rolled = score(total_case,
                   answer(total_cols, total_rows,
                          golden.result_hashes(total_cols, total_rows, 6)),
                   answer(split_cols, split_rows,
                          golden.result_hashes(split_cols, split_rows, 6)),
                   "SELECT 1 FROM tbdaabt30")
    check("총합은 같고 집계 축만 다름 → rollup_match(정답 아님)",
          rolled["verdict"] == V_ROLLUP_MATCH and not rolled["correct"])
    check("총합 기록", rolled["detail"]["expected_totals"] == [300]
          and rolled["detail"]["actual_totals"] == [300])

    off_rows = [("20260701", 100), ("20260702", 250)]
    off = score(total_case,
                answer(total_cols, total_rows, golden.result_hashes(total_cols, total_rows, 6)),
                answer(split_cols, off_rows, golden.result_hashes(split_cols, off_rows, 6)),
                "SELECT 1 FROM tbdaabt30")
    check("총합이 다르면 rollup 아님", off["verdict"] != V_ROLLUP_MATCH and not off["correct"])

    iso_case = {"id": "t4", "question": "일자별 금액을 보여줘", "category": "일자별 금액",
                "sql": "SELECT 1 FROM tbdaabt30", "expected_tables": ["tbdaabt30"]}
    iso_cols = ["일자", "금액"]
    iso_gold_rows = [("2026-07-01", 5)]
    iso_cand_rows = [("20260701", 5)]
    iso = score(iso_case,
                answer(iso_cols, iso_gold_rows, golden.result_hashes(iso_cols, iso_gold_rows, 6)),
                answer(iso_cols, iso_cand_rows, golden.result_hashes(iso_cols, iso_cand_rows, 6)),
                "SELECT 1 FROM tbdaabt30")
    check("날짜 표기만 다름 → 정답", iso["correct"] and iso["verdict"] == V_SUBSET_MATCH)

    scrambled_rows = [("A", 20), ("B", 10)]
    scrambled = score(case, gold,
                      answer(columns, scrambled_rows,
                             golden.result_hashes(columns, scrambled_rows, 6)),
                      "SELECT 1 FROM tbdaa1d12")
    check("컬럼별 값만 같고 행 조합이 다름 → 정답 아님",
          not scrambled["correct"] and scrambled["detail"]["column_subset"] is None)

    truncated = answer(wide_cols, wide_rows, golden.result_hashes(wide_cols, wide_rows, 6),
                       row_count=999, row_count_truncated=True)
    cut = score(case, gold, truncated, "SELECT 1 FROM tbdaa1d12")
    check("행이 잘린 결과는 부분집합 판정 보류",
          "subset_check_skipped" in cut["detail"]["notes"] and not cut["correct"])

    other_rows = [("A", 11), ("B", 20)]
    wrong = score(case, gold,
                  answer(columns, other_rows, golden.result_hashes(columns, other_rows, 6)),
                  "SELECT 1 FROM tbdaa1d12")
    check("값이 다름 → shape_only/mismatch(정답 아님)",
          not wrong["correct"] and wrong["verdict"] in (V_SHAPE_ONLY, V_MISMATCH))

    scalar_case = {"id": "t2", "sql": "SELECT count(*) FROM tbdaa1d12", "expected_tables": ["tbdaa1d12"]}
    scalar_gold = answer(["cnt"], [(1000,)], golden.result_hashes(["cnt"], [(1000,)], 6))
    scalar_cand_rows = [(1000.0000001,)]
    scalar_near = score(scalar_case, scalar_gold,
                        answer(["건수"], scalar_cand_rows,
                               golden.result_hashes(["건수"], scalar_cand_rows, 6)),
                        "SELECT count(*) FROM tbdaa1d12", rel_tol=1e-6)
    check("스칼라 허용오차 → 정답", scalar_near["correct"])

    drift_cols, drift_gold_rows = ["평균단가"], [(1234567.891234,)]
    drift_cand_rows = [(1234567.891235,)]
    drift = score(scalar_case,
                  answer(drift_cols, drift_gold_rows,
                         golden.result_hashes(drift_cols, drift_gold_rows, 6)),
                  answer(drift_cols, drift_cand_rows,
                         golden.result_hashes(drift_cols, drift_cand_rows, 6)),
                  "SELECT 1 FROM tbdaa1d12", rel_tol=1e-6)
    check("반올림 자릿수 밖 미세 차이 → scalar_near(정답)",
          drift["verdict"] == V_SCALAR_NEAR and drift["correct"])

    off_scalar_rows = [(1010,)]
    off_scalar = score(scalar_case, scalar_gold,
                       answer(["건수"], off_scalar_rows,
                              golden.result_hashes(["건수"], off_scalar_rows, 6)),
                       "SELECT count(*) FROM tbdaa1d12", rel_tol=1e-6)
    check("스칼라가 1% 다름 → shape_only(정답 아님)",
          off_scalar["verdict"] == V_SHAPE_ONLY and not off_scalar["correct"])
    check("스칼라 비교", scalar_close(1000, 1000.0000001, 1e-6) and not scalar_close(1000, 1100, 1e-6))
    check("문자 스칼라 비교", scalar_close("삼성전자", "삼성전자", 1e-6))

    check("SQL 없음 → no_sql", score(case, gold, None, "")["verdict"] == V_NO_SQL)
    check("SQL 실행 실패 → sql_error",
          score(case, gold, None, "SELECT 1", sql_error="boom")["verdict"] == V_SQL_ERROR)
    check("에이전트 실패 → agent_error",
          score(case, gold, None, "SELECT 1", agent_error="boom")["verdict"] == V_AGENT_ERROR)
    check("정답 없음 → no_gold",
          score(case, None, None, "SELECT 1 FROM tbdaa1d12")["verdict"] == V_NO_GOLD)

    tables = table_metrics(["card_system.tbdaa1d12", "tmdaa5e11"], {"tbdaa1d12", "tbdaadb17"})
    check("테이블 재현율", abs((tables["recall"] or 0) - 0.5) < 1e-9)
    check("테이블 누락 목록", tables["missing"] == ["tmdaa5e11"])
    check("CTE는 테이블로 세지 않음",
          sql_tables("WITH base AS (SELECT 1) SELECT * FROM base JOIN tbdaa1d12 a ON 1=1",
                     golden.FALLBACK_TABLES) == {"tbdaa1d12"})

    check("정답 선별 - ok", gold_answer({"answer_status": "ok",
                                        "expected_answer": {"result_hash": "h"}}) is not None)
    check("정답 선별 - error 제외", gold_answer({"answer_status": "error",
                                             "expected_answer": {"result_hash": "h"}}) is None)
    check("정답 선별 - 미실행 제외", gold_answer({"answer_status": "not_executed"}) is None)

    records = [
        {"verdict": V_EXACT, "correct": True, "candidate": {}, "agent": {"sql": "x"}, "detail": {}},
        {"verdict": V_MATCH, "correct": True, "candidate": {}, "agent": {"sql": "x"}, "detail": {}},
        {"verdict": V_MISMATCH, "correct": False, "candidate": {}, "agent": {"sql": "x"}, "detail": {}},
        {"verdict": V_NO_GOLD, "correct": False, "candidate": None, "agent": {"sql": "x"}, "detail": {}},
    ]
    bucket = _bucket(records)
    check("집계 - 채점 대상만 계산", bucket["scored"] == 3)
    check("집계 - 실행 정확도", bucket["execution_accuracy"] == round(2 / 3, 4))
    check("집계 - 엄격 정확도", bucket["strict_accuracy"] == round(1 / 3, 4))
    check("집계 - 정답없음 분리", bucket["no_gold"] == 1)
    check("SQL 텍스트 정규화",
          normalize_sql_text("SELECT  1\nFROM t;") == normalize_sql_text("select 1 from t"))
    check("토큰 겹침 1.0", abs(sql_token_overlap("SELECT a FROM t", "select A from T") - 1.0) < 1e-9)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=None,
                        help="정답이 병합된 골든셋 JSONL (기본: *_answered.jsonl, 없으면 원본)")
    parser.add_argument("--answers", type=Path, default=DEFAULT_ANSWERS,
                        help="골든셋에 정답이 없을 때 참조할 정답 실행 이력 JSONL")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS, help="케이스별 채점 결과 JSONL")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY, help="요약 마크다운 경로")
    parser.add_argument("--report-json", type=Path, default=DEFAULT_REPORT_JSON, help="집계 JSON 경로")
    parser.add_argument("--review-csv", type=Path, default=DEFAULT_REVIEW_CSV, help="검수용 CSV 경로")

    parser.add_argument("--predictor", choices=["agent", "stub"], default="agent",
                        help="후보 SQL 생성 방식 (기본: 실제 에이전트)")
    parser.add_argument("--predictions", type=Path, default=None,
                        help="이미 뽑아둔 에이전트 SQL JSONL. 주면 에이전트를 돌리지 않는다")
    parser.add_argument("--max-param-rounds", type=int, default=3,
                        help="파라미터 누락 시 기본값으로 이어서 진행할 최대 횟수")
    parser.add_argument("--reuse-agent-rows", action="store_true",
                        help="에이전트가 이미 받아온 결과를 그대로 채점(재실행 안 함). 행수 상한에 주의")
    parser.add_argument("--execute-without-gold", action="store_true",
                        help="정답 결과가 없는 케이스도 후보 SQL을 실행해 본다")

    parser.add_argument("--executor", choices=["auto", "pyathena", "agent", "stub"], default="auto")
    parser.add_argument("--schema", default=None, help="테이블 참조에 붙일 스키마 접두사('none'이면 제거)")
    parser.add_argument("--fetch-rows", type=int, default=5000, help="후보 SQL에서 가져올 최대 행 수")
    parser.add_argument("--answer-rows", type=int, default=50, help="결과 파일에 저장할 최대 행 수")
    parser.add_argument("--row-limit", type=int, default=0, help="후보 SQL을 감싸 반환 행을 강제 제한")
    parser.add_argument("--timeout-ms", type=int, default=180_000, help="문장 타임아웃(ms)")
    parser.add_argument("--float-digits", type=int, default=6,
                        help="실수 반올림 자릿수(정답에 기록된 값이 있으면 그쪽을 따른다)")
    parser.add_argument("--rel-tol", type=float, default=1e-6, help="스칼라 비교 상대 허용오차")

    parser.add_argument("--limit", type=int, default=0, help="이번 실행에서 채점할 최대 건수")
    parser.add_argument("--ids", nargs="*", default=[], help="특정 케이스 id만")
    parser.add_argument("--domain", default="", help="특정 도메인만")
    parser.add_argument("--shape", default="", help="특정 query_shape만")
    parser.add_argument("--difficulty", default="", help="특정 난이도만")
    parser.add_argument("--only-with-gold", action="store_true", default=True,
                        help="정답 결과가 있는 케이스만 채점 (기본 켜짐)")
    parser.add_argument("--include-no-gold", dest="only_with_gold", action="store_false",
                        help="정답 결과가 없는 케이스도 대상에 포함")
    parser.add_argument("--retry-errors", action="store_true", help="실패 케이스만 다시 채점")
    parser.add_argument("--rerun-all", action="store_true", help="저장 여부와 무관하게 전부 다시 채점")
    parser.add_argument("--restart", action="store_true", help="저장된 채점 결과를 지우고 처음부터")
    parser.add_argument("--sleep", type=float, default=0.0, help="케이스 사이 대기(초)")
    parser.add_argument("--max-scanned-gb", type=float, default=0.0,
                        help="후보 SQL 누적 스캔량이 이 값을 넘으면 중단")
    parser.add_argument("--stop-after-errors", type=int, default=0, help="연속 실패 n회면 중단")

    parser.add_argument("--dry-run", action="store_true", help="채점 없이 대상만 출력")
    parser.add_argument("--report-only", action="store_true", help="채점 없이 리포트만 다시 생성")
    parser.add_argument("--fail-under", type=float, default=0.0,
                        help="실행 정확도가 이 값(0~1) 미만이면 종료 코드 1")
    parser.add_argument("--self-test", action="store_true", help="에이전트·DB 없이 채점 로직 점검")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    if args.cases is None:
        args.cases = DEFAULT_CASES_ANSWERED if DEFAULT_CASES_ANSWERED.exists() else DEFAULT_CASES_BASE
    cases = load_gold_cases(args.cases, args.answers)
    if not cases:
        print("골든셋을 찾을 수 없다: %s" % args.cases)
        return 1
    args.table_names = golden.collect_table_names(cases)

    with_gold = sum(1 for c in cases if gold_answer(c) is not None)
    if with_gold == 0:
        print("정답 결과가 하나도 없다. 먼저 scripts/run_goldenset_answers.py 로 정답을 채워야 한다.")
        if args.only_with_gold:
            return 1

    if args.restart and args.results.exists():
        args.results.unlink()
    saved = {rec["id"]: rec for rec in golden.read_jsonl(args.results)}

    if args.report_only:
        report = write_reports(cases, saved, args)
        print("리포트 갱신: %s" % args.summary)
        return threshold_exit(report, args)

    pending = select_cases(cases, saved, args)
    print("전체 %d건 / 정답 보유 %d건 / 채점됨 %d건 / 이번 대상 %d건"
          % (len(cases), with_gold, len(saved), len(pending)))
    if args.dry_run:
        for case in pending[:20]:
            print("  %s %s" % (case["id"], case.get("question", "")))
        if len(pending) > 20:
            print("  ... 외 %d건" % (len(pending) - 20))
        return 0
    if not pending:
        report = write_reports(cases, saved, args)
        print("채점할 케이스가 없다. 리포트만 갱신했다: %s" % args.summary)
        return threshold_exit(report, args)

    predictor = build_predictor(args)
    executor = golden.build_executor(
        "stub" if args.predictor == "stub" and not args.predictions else args.executor,
        args.timeout_ms,
    )

    scanned_total = 0
    consecutive_errors = 0
    for index, case in enumerate(pending, 1):
        record = evaluate_case(case, predictor, executor, args)
        saved[case["id"]] = record
        golden.append_record(args.results, record)

        verdict = record.get("verdict")
        if verdict in (V_AGENT_ERROR, V_SQL_ERROR):
            consecutive_errors += 1
            detail = (record.get("sql_error") or (record.get("agent") or {}).get("error") or "")
            detail = detail.splitlines()[0][:120] if detail else ""
        else:
            consecutive_errors = 0
            candidate = record.get("candidate") or {}
            detail = "기대 %s행 / 실제 %s행" % (record.get("expected_row_count"),
                                          candidate.get("row_count"))
        print("[%d/%d] %s %s %s (%.1fs)" % (
            index, len(pending), case["id"], verdict, detail,
            record.get("elapsed_ms", 0) / 1000))

        scanned_total += ((record.get("candidate") or {}).get("athena") or {}).get("data_scanned_bytes") or 0
        if args.max_scanned_gb and scanned_total / 1024 ** 3 >= args.max_scanned_gb:
            print("누적 스캔량 %.2f GB 도달 → 중단" % (scanned_total / 1024 ** 3))
            break
        if args.stop_after_errors and consecutive_errors >= args.stop_after_errors:
            print("연속 실패 %d회 → 중단" % consecutive_errors)
            break
        if args.sleep:
            time.sleep(args.sleep)

    report = write_reports(cases, saved, args)
    overall = report["overall"]
    print("")
    print("실행 정확도 %s / 엄격 정확도 %s (채점 %d건)"
          % (_pct(overall["execution_accuracy"]), _pct(overall["strict_accuracy"]),
             overall["scored"]))
    print("요약 리포트: %s" % args.summary)
    print("집계 JSON: %s" % args.report_json)
    print("검수용 CSV: %s" % args.review_csv)

    return threshold_exit(report, args)


if __name__ == "__main__":
    raise SystemExit(main())

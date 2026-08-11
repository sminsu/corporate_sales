#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""채점 결과(JSONL)에서 `sql_error` 와 `mismatch` 케이스만 뽑아 원인별로 정리한다.

`run_goldenset_eval.py` 가 남긴 `reports/goldenset-v2-eval-results.jsonl` 을 읽어
두 갈래로 리포트를 만든다.

  - sql_error : 난이도별로 묶고 질의·도메인·오류 유형·오류 원문을 정리한다.
                오류 문구에서 못 찾은 컬럼/테이블 이름을 뽑아 자주 걸리는 것부터 보여준다.
  - mismatch  : 무엇이 어긋났는지(컬럼 구성 / 행 수 / 스칼라 값 / 행 값 / 참조 테이블)를
                판정하고, 값이 다르면 어느 컬럼의 어떤 값이 다른지까지 짚는다.

    # 기본 경로로 정리
    python scripts/report_goldenset_failures.py

    # 특정 난이도만, 샘플을 더 많이
    python scripts/report_goldenset_failures.py --difficulty hard --max-samples 10

    # near 케이스까지 포함
    python scripts/report_goldenset_failures.py --verdicts sql_error mismatch near

    # 파일 없이 내부 로직만 점검
    python scripts/report_goldenset_failures.py --self-test

행 값 비교는 골든셋의 정답 행이 필요하므로 `--cases` 를 함께 읽는다. 정답과 후보 어느 쪽이든
저장 상한(`--answer-rows`)에 걸려 일부만 남은 케이스는 값 비교를 건너뛰고 그 사실을 표시한다.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for _path in (str(ROOT), str(SCRIPTS)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import run_goldenset_answers as golden  # noqa: E402
import run_goldenset_eval as evalrun  # noqa: E402

DEFAULT_RESULTS = ROOT / "reports" / "goldenset-v2-eval-results.jsonl"
DEFAULT_OUT_MD = ROOT / "reports" / "goldenset-v2-failures.md"
DEFAULT_OUT_JSON = ROOT / "reports" / "goldenset-v2-failures.json"
DEFAULT_SQL_ERROR_CSV = ROOT / "reports" / "goldenset-v2-sql-errors.csv"
DEFAULT_MISMATCH_CSV = ROOT / "reports" / "goldenset-v2-mismatches.csv"

DIFFICULTY_ORDER = ["easy", "medium", "hard"]

# 오류 분류. 위에서부터 먼저 맞는 것을 쓴다(구체적인 것이 앞).
ERROR_RULES: list[tuple[str, str, str, str]] = [
    # (kind, 설명, 본문 패턴, 대상 이름 패턴)
    ("guard", "읽기 전용 가드 차단", r"읽기 전용이 아닌|다중 문장|SELECT/WITH로 시작하지", ""),
    ("column_not_found", "컬럼 없음",
     r"COLUMN_NOT_FOUND|Column\s+'[^']+'\s+cannot be resolved|Column\s+[^\s]+\s+cannot be resolved",
     r"Column\s+'([^']+)'"),
    ("table_not_found", "테이블/스키마 없음",
     r"TABLE_NOT_FOUND|SCHEMA_NOT_FOUND|Table\s+'[^']+'\s+does not exist|does not exist",
     r"Table\s+'([^']+)'"),
    ("function_not_found", "함수 없음",
     r"FUNCTION_NOT_FOUND|Function\s+'?[^\s']+'?\s+not registered|Unexpected parameters",
     r"Function\s+'?([A-Za-z0-9_]+)'?"),
    ("ambiguous", "이름 모호", r"AMBIGUOUS_(NAME|ATTRIBUTE)|is ambiguous", r"'([^']+)'\s+is ambiguous"),
    ("type_mismatch", "타입 불일치",
     r"TYPE_MISMATCH|INVALID_CAST_ARGUMENT|Cannot apply operator|cannot be applied to|Cannot cast",
     r"(?:Cannot apply operator:|Cannot cast)\s+([^\n]+)"),
    ("group_by", "집계/GROUP BY 오류",
     r"must be an aggregate expression or appear in GROUP BY|EXPRESSION_NOT_AGGREGATE", ""),
    ("division_by_zero", "0으로 나눔", r"DIVISION_BY_ZERO|Division by zero", ""),
    ("syntax", "구문 오류",
     r"SYNTAX_ERROR|mismatched input|extraneous input|ParsingException|line \d+:\d+:", ""),
    ("permission", "권한 거부",
     r"AccessDenied|not authorized|UnauthorizedException|InsufficientPrivileges", ""),
    ("throttle", "쓰로틀링", r"TooManyRequests|SlowDown|ThrottlingException|Rate exceeded", ""),
    ("timeout", "타임아웃", r"TIMED?_?OUT|timeout|Query exceeded maximum time", ""),
    ("resource", "리소스 초과",
     r"EXCEEDED_(MEMORY|GLOBAL|LOCAL)|exhausted resources|Query exceeded .* limit", ""),
    ("hive_data", "원천 데이터/파티션 오류", r"HIVE_[A-Z_]+", r"(HIVE_[A-Z_]+)"),
    ("network", "접속/네트워크", r"EndpointConnectionError|ConnectTimeout|ConnectionError|NoCredentials", ""),
]
ERROR_LABEL = {kind: label for kind, label, _pattern, _subject in ERROR_RULES}
ERROR_LABEL["other"] = "기타"

# mismatch 원인. 위에서부터 먼저 맞는 것이 대표 원인이 된다.
REASON_LABEL = {
    "column_count": "컬럼 수가 다름",
    "column_names": "컬럼 이름이 다름",
    "row_count": "행 수가 다름",
    "scalar_value": "단일 수치 값이 다름",
    "row_values": "행 값이 다름",
    "row_membership": "포함된 행 자체가 다름",
    "tables": "참조 테이블이 다름",
    "unknown": "원인 미상",
}
# 대표 원인 우선순위. 결과의 '모양'이 다른 것이 먼저이고, 컬럼 이름 차이는 표기 문제라 뒤로 뺀다.
REASON_ORDER = ["column_count", "scalar_value", "row_count", "row_values",
                "row_membership", "column_names", "tables", "unknown"]

LINE_POS_RE = re.compile(r"line \d+:\d+:?\s*")
QUOTED_RE = re.compile(r"'[^']*'")
NUMBER_RE = re.compile(r"\b\d+\b")
QUERY_ID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


# ---------------------------------------------------------------------------
# 오류 분류
# ---------------------------------------------------------------------------
def classify_error(message: str) -> dict:
    """오류 원문에서 유형·대상 이름·시그니처를 뽑는다."""
    text = (message or "").strip()
    if not text:
        return {"kind": "other", "label": ERROR_LABEL["other"], "subject": "",
                "signature": "", "first_line": ""}
    for kind, label, pattern, subject_pattern in ERROR_RULES:
        if not re.search(pattern, text, re.IGNORECASE):
            continue
        subject = ""
        if subject_pattern:
            found = re.search(subject_pattern, text)
            if found:
                subject = found.group(1).strip()
        return {"kind": kind, "label": label, "subject": subject,
                "signature": error_signature(text), "first_line": first_line(text)}
    return {"kind": "other", "label": ERROR_LABEL["other"], "subject": "",
            "signature": error_signature(text), "first_line": first_line(text)}


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def error_signature(text: str) -> str:
    """같은 원인을 한 줄로 묶기 위해 위치·리터럴·숫자를 지운 형태."""
    signature = first_line(text)
    signature = QUERY_ID_RE.sub("<id>", signature)
    signature = LINE_POS_RE.sub("", signature)
    signature = QUOTED_RE.sub("'?'", signature)
    signature = NUMBER_RE.sub("N", signature)
    return signature[:200]


# ---------------------------------------------------------------------------
# mismatch 원인 분석
# ---------------------------------------------------------------------------
def _norm(value: Any, digits: int) -> str:
    return golden.normalize_value(value, digits)


def align_rows(gold_columns: list[str], gold_rows: list[list],
               cand_columns: list[str], cand_rows: list[list]) -> tuple[list[list], bool]:
    """컬럼 이름이 같고 순서만 다르면 후보 행을 정답 컬럼 순서로 재배열한다."""
    if gold_columns == cand_columns:
        return cand_rows, True
    if sorted(gold_columns) != sorted(cand_columns) or len(set(cand_columns)) != len(cand_columns):
        return cand_rows, False
    index = {name: position for position, name in enumerate(cand_columns)}
    reordered = [[row[index[name]] for name in gold_columns] for row in cand_rows]
    return reordered, True


def _row_key(row: list, positions: list[int], digits: int) -> str:
    return "\x1f".join(_norm(row[p], digits) for p in positions)


def _key_positions(columns: list[str], gold_rows: list[list], cand_rows: list[list],
                   digits: int) -> list[int] | None:
    """행을 맞출 키 컬럼을 고른다.

    집계 결과는 앞쪽이 분류축, 뒤쪽이 측정값이므로 왼쪽부터 한 칸씩 넓혀가며
    양쪽에서 유일해지는 최소 조합을 찾는다. 마지막 컬럼까지 다 써야 유일해진다면
    그건 측정값을 키로 삼는 셈이라 키가 없는 것으로 본다(그 경우 자리순으로 비교).
    """
    for width in range(1, len(columns)):
        positions = list(range(width))
        if any(len(row) < width for row in gold_rows + cand_rows):
            continue
        gold_keys = [_row_key(row, positions, digits) for row in gold_rows]
        cand_keys = [_row_key(row, positions, digits) for row in cand_rows]
        if len(set(gold_keys)) == len(gold_keys) and len(set(cand_keys)) == len(cand_keys):
            return positions
    return None


def diff_rows(gold_columns: list[str], gold_rows: list[list],
              cand_columns: list[str], cand_rows: list[list],
              digits: int, max_samples: int) -> dict:
    """행 값 차이를 컬럼 단위로 짚는다.

    키가 되는 컬럼으로 행을 맞춰 비교하고, 키를 못 찾으면 행 집합 차이만 낸다.
    """
    aligned, comparable = align_rows(gold_columns, gold_rows, cand_columns, cand_rows)
    out: dict[str, Any] = {
        "aligned_columns": comparable,
        "column_diffs": [],
        "only_in_gold": [],
        "only_in_candidate": [],
        "matched_rows": 0,
        "key_column": None,
    }

    out["only_in_gold_count"] = 0
    out["only_in_candidate_count"] = 0
    if not comparable:
        # 컬럼 구성이 다르면 행끼리 견줄 기준이 없다. 컬럼 문제부터 풀어야 한다.
        return out

    gold_norm = ["\x1f".join(_norm(v, digits) for v in row) for row in gold_rows]
    cand_norm = ["\x1f".join(_norm(v, digits) for v in row) for row in aligned]
    gold_only = Counter(gold_norm) - Counter(cand_norm)
    cand_only = Counter(cand_norm) - Counter(gold_norm)
    out["only_in_gold"] = [text.split("\x1f") for text in list(gold_only.elements())[:max_samples]]
    out["only_in_candidate"] = [text.split("\x1f") for text in list(cand_only.elements())[:max_samples]]
    out["only_in_gold_count"] = sum(gold_only.values())
    out["only_in_candidate_count"] = sum(cand_only.values())

    if not gold_rows or not aligned:
        return out

    positions = _key_positions(gold_columns, gold_rows, aligned, digits)
    if positions is None:
        if len(gold_rows) != len(aligned):
            return out
        pairs = list(zip(gold_rows, aligned))     # 키가 없으면 같은 자리끼리 본다
        key_label = "(행 순서)"
    else:
        key_label = " + ".join(gold_columns[p] for p in positions)
        out["key_column"] = key_label
        cand_by_key = {_row_key(row, positions, digits): row for row in aligned}
        pairs = [(row, cand_by_key[_row_key(row, positions, digits)])
                 for row in gold_rows if _row_key(row, positions, digits) in cand_by_key]
    out["matched_rows"] = len(pairs)

    for column_index, column in enumerate(gold_columns):
        samples = []
        differing = 0
        for gold_row, cand_row in pairs:
            if column_index >= len(gold_row) or column_index >= len(cand_row):
                continue
            expected, actual = gold_row[column_index], cand_row[column_index]
            if _norm(expected, digits) == _norm(actual, digits):
                continue
            differing += 1
            if len(samples) < max_samples:
                key_value = (" | ".join(str(gold_row[p]) for p in positions)
                             if positions else "")
                samples.append({
                    "key": key_value,
                    "expected": golden.jsonable(expected),
                    "actual": golden.jsonable(actual),
                    "delta": _delta(expected, actual),
                })
        if differing:
            out["column_diffs"].append({
                "column": column,
                "key_column": key_label,
                "differing_rows": differing,
                "compared_rows": len(pairs),
                "samples": samples,
            })
    out["column_diffs"].sort(key=lambda item: item["differing_rows"], reverse=True)
    return out


def _delta(expected: Any, actual: Any) -> dict | None:
    """수치면 차이와 비율을 함께 남긴다(필터 차이인지 집계 차이인지 감을 잡는 용도)."""
    left, right = evalrun.numeric(expected), evalrun.numeric(actual)
    if left is None or right is None:
        return None
    diff = right - left
    ratio = (diff / left) if left else None
    return {"diff": diff, "ratio_pct": round(ratio * 100, 2) if ratio is not None else None}


def analyze_mismatch(record: dict, gold: dict | None, max_samples: int) -> dict:
    """mismatch 한 건이 '어디서' 어긋났는지 정리한다."""
    detail = record.get("detail") or {}
    candidate = record.get("candidate") or {}
    tables = detail.get("tables") or {}
    gold = gold or {}
    digits = int(candidate.get("float_digits") or gold.get("float_digits") or 6)

    gold_columns = [str(c) for c in (detail.get("expected_columns")
                                     or gold.get("columns") or [])]
    cand_columns = [str(c) for c in (detail.get("actual_columns")
                                     or candidate.get("columns") or [])]
    expected_rows = detail.get("expected_row_count")
    actual_rows = detail.get("actual_row_count")
    if expected_rows is None:
        expected_rows = gold.get("row_count")
    if actual_rows is None:
        actual_rows = candidate.get("row_count")

    reasons: list[str] = []
    findings: dict[str, Any] = {}

    if len(gold_columns) != len(cand_columns):
        reasons.append("column_count")
        findings["columns"] = {
            "expected": gold_columns, "actual": cand_columns,
            "expected_count": len(gold_columns), "actual_count": len(cand_columns),
            "only_in_expected": [c for c in gold_columns if c not in cand_columns],
            "only_in_actual": [c for c in cand_columns if c not in gold_columns],
        }
    elif gold_columns != cand_columns:
        reasons.append("column_names")
        findings["columns"] = {
            "expected": gold_columns, "actual": cand_columns,
            "expected_count": len(gold_columns), "actual_count": len(cand_columns),
            "only_in_expected": [c for c in gold_columns if c not in cand_columns],
            "only_in_actual": [c for c in cand_columns if c not in gold_columns],
            "order_only": sorted(gold_columns) == sorted(cand_columns),
        }

    if expected_rows != actual_rows:
        reasons.append("row_count")
        findings["row_count"] = {
            "expected": expected_rows, "actual": actual_rows,
            "diff": (actual_rows - expected_rows)
            if isinstance(expected_rows, int) and isinstance(actual_rows, int) else None,
            "direction": _direction(expected_rows, actual_rows),
        }

    expected_scalar = detail.get("expected_scalar", gold.get("scalar"))
    actual_scalar = detail.get("actual_scalar", candidate.get("scalar"))
    if expected_scalar is not None and actual_scalar is not None and not detail.get("scalar_match"):
        reasons.append("scalar_value")
        findings["scalar"] = {
            "expected": expected_scalar, "actual": actual_scalar,
            "delta": _delta(expected_scalar, actual_scalar),
        }

    gold_rows = gold.get("rows") or []
    cand_rows = candidate.get("rows") or []
    gold_full = bool(gold_rows) and gold.get("row_count") == len(gold_rows) \
        and not gold.get("row_count_truncated")
    cand_full = bool(cand_rows) and candidate.get("row_count") == len(cand_rows) \
        and not candidate.get("row_count_truncated")
    # 1행 1열이면 '값이 다름'은 이미 스칼라 쪽에서 말한 것이라 행 단위로 또 세지 않는다.
    scalar_covers_rows = ("scalar_value" in reasons
                          and expected_rows == 1 and actual_rows == 1
                          and len(gold_columns) == 1)
    if gold_rows and cand_rows and gold_full and cand_full:
        diff = diff_rows(gold_columns, gold_rows, cand_columns, cand_rows, digits, max_samples)
        findings["rows"] = diff
        if scalar_covers_rows:
            pass
        elif diff["column_diffs"]:
            reasons.append("row_values")
        elif diff["only_in_gold_count"] or diff["only_in_candidate_count"]:
            reasons.append("row_membership")
    elif gold_rows or cand_rows:
        findings["rows"] = {
            "skipped": "저장된 행이 전체가 아니라 값 비교를 생략했다",
            "gold_rows_stored": len(gold_rows), "gold_row_count": gold.get("row_count"),
            "candidate_rows_stored": len(cand_rows),
            "candidate_row_count": candidate.get("row_count"),
        }

    if tables.get("missing") or tables.get("extra"):
        reasons.append("tables")
        findings["tables"] = {
            "expected": tables.get("expected") or [],
            "actual": tables.get("actual") or [],
            "missing": tables.get("missing") or [],
            "extra": tables.get("extra") or [],
        }

    if not reasons:
        reasons.append("unknown")
    primary = sorted(reasons, key=lambda name: REASON_ORDER.index(name))[0]
    return {
        "reasons": reasons,
        "primary_reason": primary,
        "primary_label": REASON_LABEL[primary],
        # 원인으로 잡히지 않았어도 검수할 때 필요한 값이라 항상 남긴다.
        "shape": {
            "expected_columns": gold_columns,
            "actual_columns": cand_columns,
            "expected_row_count": expected_rows,
            "actual_row_count": actual_rows,
            "expected_scalar": expected_scalar,
            "actual_scalar": actual_scalar,
        },
        "findings": findings,
        "notes": detail.get("notes") or [],
    }


def _direction(expected: Any, actual: Any) -> str:
    if not isinstance(expected, int) or not isinstance(actual, int):
        return ""
    if actual > expected:
        return "과다"
    if actual < expected:
        return "과소"
    return ""


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------
def dedupe_latest(records: list[dict]) -> list[dict]:
    """같은 id가 여러 번 있으면 마지막 채점만 남긴다(재개하면 같은 id가 다시 쌓인다)."""
    latest: dict[str, dict] = {}
    order: list[str] = []
    for record in records:
        case_id = str(record.get("id") or "")
        if not case_id:
            continue
        if case_id not in latest:
            order.append(case_id)
        latest[case_id] = record
    return [latest[case_id] for case_id in order]


def latest_records(path: Path) -> list[dict]:
    return dedupe_latest(golden.read_jsonl(path))


def keep(record: dict, args) -> bool:
    if record.get("verdict") not in args.verdicts:
        return False
    if args.ids and record.get("id") not in args.ids:
        return False
    if args.domain and record.get("domain") != args.domain:
        return False
    if args.difficulty and record.get("difficulty") != args.difficulty:
        return False
    if args.shape and record.get("query_shape") != args.shape:
        return False
    return True


def collect(records: list[dict], cases_by_id: dict[str, dict], args) -> dict:
    """채점 레코드를 골든셋 케이스와 짝지어 오류/불일치로 나눈다.

    골든셋 케이스를 통째로 받는 이유는 정답 결과뿐 아니라 **정답 SQL** 도 함께 실어
    후보 SQL과 나란히 볼 수 있게 하기 위해서다.
    """
    errors: list[dict] = []
    mismatches: list[dict] = []
    for record in records:
        if not keep(record, args):
            continue
        case = cases_by_id.get(record.get("id")) or {}
        base = {
            "id": record.get("id"),
            "question": record.get("question", "") or case.get("question", ""),
            "domain": record.get("domain", ""),
            "query_shape": record.get("query_shape", ""),
            "difficulty": record.get("difficulty") or "(없음)",
            "verdict": record.get("verdict"),
            "sql": ((record.get("agent") or {}).get("sql") or ""),
            "expected_sql": case.get("sql", ""),
        }
        if record.get("verdict") in ("sql_error", "agent_error", "no_sql"):
            message = (record.get("sql_error")
                       or (record.get("agent") or {}).get("error") or "")
            if not message and record.get("verdict") == "no_sql":
                meta = (record.get("agent") or {}).get("meta") or {}
                message = str(meta.get("query_error") or meta.get("error_message")
                              or meta.get("param_stage") or "SQL 미생성")
            item = dict(base)
            item["error"] = message
            item.update(classify_error(message))
            errors.append(item)
        else:
            item = dict(base)
            item.update(analyze_mismatch(record, evalrun.gold_answer(case), args.max_samples))
            mismatches.append(item)
    return {"errors": errors, "mismatches": mismatches}


def group_by_difficulty(items: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item.get("difficulty") or "(없음)", []).append(item)
    known = [d for d in DIFFICULTY_ORDER if d in grouped]
    rest = sorted(name for name in grouped if name not in DIFFICULTY_ORDER)
    return {name: grouped[name] for name in known + rest}


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------
def _cell(value: Any, limit: int = 120) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "/").replace("\n", " ").replace("\r", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _sql_block(title: str, sql: str | None, limit: int) -> list[str]:
    """SQL을 접이식 블록으로 싣는다. 정답 SQL은 길어서 본문에 그대로 펴면 리포트가 안 읽힌다."""
    text = (sql or "").strip()
    if not text:
        return []
    truncated = limit and len(text) > limit
    if truncated:
        text = text[:limit].rstrip() + "\n-- ... 이하 생략 (--sql-chars 0 이면 전체)"
    return ["", "<details><summary>%s%s</summary>" % (
        title, " (앞 %d자)" % limit if truncated else ""), "",
        "```sql", text, "```", "", "</details>"]


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return ("%.6f" % value).rstrip("0").rstrip(".")
    return "" if value is None else str(value)


def build_summary(collected: dict, records: list[dict], args) -> dict:
    errors, mismatches = collected["errors"], collected["mismatches"]
    error_groups = group_by_difficulty(errors)
    mismatch_groups = group_by_difficulty(mismatches)
    reason_counter: Counter = Counter()
    for item in mismatches:
        reason_counter.update(item["reasons"])
    column_counter: Counter = Counter()
    for item in mismatches:
        for diff in ((item.get("findings") or {}).get("rows") or {}).get("column_diffs", []) or []:
            column_counter[diff["column"]] += diff["differing_rows"]
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "results_file": str(args.results),
        "cases_file": str(args.cases) if args.cases else "",
        "records_total": len(records),
        "verdicts_selected": sorted(args.verdicts),
        "verdict_counts": dict(Counter(r.get("verdict") for r in records)),
        "sql_error": {
            "total": len(errors),
            "by_difficulty": {name: len(items) for name, items in error_groups.items()},
            "by_kind": dict(Counter(item["kind"] for item in errors).most_common()),
            "by_domain": dict(Counter(item["domain"] for item in errors).most_common()),
            "top_subjects": dict(Counter(
                item["subject"] for item in errors if item.get("subject")).most_common(20)),
            "top_signatures": dict(Counter(
                item["signature"] for item in errors if item.get("signature")).most_common(20)),
            "cases": errors,
        },
        "mismatch": {
            "total": len(mismatches),
            "by_difficulty": {name: len(items) for name, items in mismatch_groups.items()},
            "by_primary_reason": dict(Counter(
                item["primary_reason"] for item in mismatches).most_common()),
            "reason_occurrences": dict(reason_counter.most_common()),
            "by_domain": dict(Counter(item["domain"] for item in mismatches).most_common()),
            "top_diff_columns": dict(column_counter.most_common(20)),
            "cases": mismatches,
        },
    }


def render_markdown(summary: dict, args) -> str:
    errors = summary["sql_error"]
    mismatches = summary["mismatch"]
    lines = ["# 골든셋 오답 정리 (sql_error / mismatch)", ""]
    lines.append("- 생성 시각: %s" % summary["generated_at"])
    lines.append("- 채점 결과: `%s`" % summary["results_file"])
    lines.append("- 채점 레코드 %d건 중 SQL 오류 %d건 / mismatch %d건"
                 % (summary["records_total"], errors["total"], mismatches["total"]))
    if summary.get("verdict_counts"):
        lines.append("- 전체 판정 분포: %s" % ", ".join(
            "%s %d" % (name, count)
            for name, count in sorted(summary["verdict_counts"].items(),
                                      key=lambda kv: -kv[1]) if name))
    lines.append("")

    # ---------------- SQL 오류 ----------------
    lines.append("## 1. SQL 오류 (난이도별)")
    lines.append("")
    if not errors["total"]:
        lines.append("해당 케이스가 없다.")
    else:
        lines.append("| 난이도 | 건수 | 주요 오류 유형 |")
        lines.append("|---|---:|---|")
        for name, items in group_by_difficulty(errors["cases"]).items():
            kinds = Counter(item["kind"] for item in items).most_common(3)
            lines.append("| %s | %d | %s |" % (
                name, len(items),
                ", ".join("%s %d건" % (ERROR_LABEL.get(k, k), c) for k, c in kinds)))
        lines.append("")
        lines.append("| 오류 유형 | 건수 |")
        lines.append("|---|---:|")
        for kind, count in errors["by_kind"].items():
            lines.append("| %s (`%s`) | %d |" % (ERROR_LABEL.get(kind, kind), kind, count))
        if errors["top_subjects"]:
            lines.append("")
            lines.append("자주 걸린 대상(컬럼·테이블·함수): %s" % ", ".join(
                "`%s` %d건" % (name, count) for name, count in errors["top_subjects"].items()))

        for name, items in group_by_difficulty(errors["cases"]).items():
            lines.extend(["", "### 난이도 `%s` — %d건" % (name, len(items)), "",
                          "| id | 도메인 | 질의 | 오류 유형 | 오류 내용 |",
                          "|---|---|---|---|---|"])
            for item in items[: args.max_cases]:
                lines.append("| `%s` | %s | %s | %s | %s |" % (
                    item["id"], _cell(item["domain"], 30), _cell(item["question"], 70),
                    ERROR_LABEL.get(item["kind"], item["kind"]),
                    _cell(item.get("first_line") or item.get("error"), 160)))
            if len(items) > args.max_cases:
                lines.append("")
                lines.append("외 %d건은 CSV·JSON을 참고한다." % (len(items) - args.max_cases))

    # ---------------- mismatch ----------------
    lines.extend(["", "## 2. mismatch (무엇이 어긋났나)", ""])
    if not mismatches["total"]:
        lines.append("해당 케이스가 없다.")
    else:
        lines.append("| 대표 원인 | 건수 |")
        lines.append("|---|---:|")
        for reason, count in mismatches["by_primary_reason"].items():
            lines.append("| %s (`%s`) | %d |" % (REASON_LABEL.get(reason, reason), reason, count))
        lines.append("")
        lines.append("원인은 한 케이스에 여러 개가 겹칠 수 있다. 중복 포함 발생 횟수는 다음과 같다.")
        lines.append("")
        lines.append("| 원인 | 발생 |")
        lines.append("|---|---:|")
        for reason, count in mismatches["reason_occurrences"].items():
            lines.append("| %s | %d |" % (REASON_LABEL.get(reason, reason), count))
        if mismatches["top_diff_columns"]:
            lines.extend(["", "값이 자주 어긋난 컬럼", "", "| 컬럼 | 불일치 행수(누적) |", "|---|---:|"])
            for column, count in mismatches["top_diff_columns"].items():
                lines.append("| `%s` | %d |" % (column, count))
        lines.extend(["", "| 난이도 | 건수 |", "|---|---:|"])
        for name, count in mismatches["by_difficulty"].items():
            lines.append("| %s | %d |" % (name, count))

        lines.extend(["", "### 케이스별 상세", ""])
        for item in mismatches["cases"][: args.max_cases]:
            lines.append("#### `%s` — %s" % (item["id"], item["primary_label"]))
            lines.append("")
            lines.append("- 질의: %s" % item["question"])
            lines.append("- 도메인/난이도: %s / %s" % (item["domain"], item["difficulty"]))
            lines.append("- 원인: %s" % ", ".join(REASON_LABEL.get(r, r) for r in item["reasons"]))
            findings = item.get("findings") or {}
            columns = findings.get("columns")
            if columns:
                lines.append("- 컬럼: 기대 %d개 %s / 실제 %d개 %s"
                             % (columns["expected_count"], columns["expected"],
                                columns["actual_count"], columns["actual"]))
                if columns.get("only_in_expected"):
                    lines.append("  - 후보에 없는 컬럼: %s" % ", ".join(columns["only_in_expected"]))
                if columns.get("only_in_actual"):
                    lines.append("  - 후보에만 있는 컬럼: %s" % ", ".join(columns["only_in_actual"]))
            row_count = findings.get("row_count")
            if row_count:
                lines.append("- 행 수: 기대 %s / 실제 %s (%s%s)"
                             % (row_count["expected"], row_count["actual"],
                                row_count["direction"],
                                "" if row_count["diff"] is None else " %+d" % row_count["diff"]))
            scalar = findings.get("scalar")
            if scalar:
                delta = scalar.get("delta") or {}
                suffix = ""
                if delta.get("ratio_pct") is not None:
                    suffix = " (%+.2f%%)" % delta["ratio_pct"]
                lines.append("- 값: 기대 %s / 실제 %s%s"
                             % (_fmt(scalar["expected"]), _fmt(scalar["actual"]), suffix))
            tables = findings.get("tables")
            if tables:
                if tables.get("missing"):
                    lines.append("- 안 쓴 정답 테이블: %s" % ", ".join(tables["missing"]))
                if tables.get("extra"):
                    lines.append("- 정답에 없는 테이블: %s" % ", ".join(tables["extra"]))
            rows = findings.get("rows") or {}
            if rows.get("skipped"):
                lines.append("- 행 값 비교: %s (정답 %s행 중 %s행 저장 / 후보 %s행 중 %s행 저장)"
                             % (rows["skipped"], rows.get("gold_row_count"),
                                rows.get("gold_rows_stored"), rows.get("candidate_row_count"),
                                rows.get("candidate_rows_stored")))
            column_diffs = (rows.get("column_diffs", [])
                            if "row_values" in item["reasons"] else [])
            for diff in column_diffs[: args.max_samples]:
                lines.append("- 컬럼 `%s`: %d/%d행 불일치 (키 `%s`)"
                             % (diff["column"], diff["differing_rows"],
                                diff["compared_rows"], diff["key_column"]))
                for sample in diff["samples"][: args.max_samples]:
                    delta = sample.get("delta") or {}
                    suffix = ""
                    if delta.get("ratio_pct") is not None:
                        suffix = " (%+.2f%%)" % delta["ratio_pct"]
                    lines.append("  - %s: 기대 `%s` / 실제 `%s`%s"
                                 % (_cell(sample["key"], 40), _fmt(sample["expected"]),
                                    _fmt(sample["actual"]), suffix))
            # 컬럼별 차이로 이미 설명된 행을 다시 나열하면 같은 말을 두 번 하는 셈이다.
            if not column_diffs and "row_membership" in item["reasons"]:
                if rows.get("only_in_gold"):
                    lines.append("- 정답에만 있는 행 %d건 (예: %s)"
                                 % (rows.get("only_in_gold_count", 0),
                                    _cell(rows["only_in_gold"][0], 100)))
                if rows.get("only_in_candidate"):
                    lines.append("- 후보에만 있는 행 %d건 (예: %s)"
                                 % (rows.get("only_in_candidate_count", 0),
                                    _cell(rows["only_in_candidate"][0], 100)))
            if item.get("notes"):
                lines.append("- 참고: %s" % ", ".join(item["notes"]))
            lines.extend(_sql_block("정답 SQL", item.get("expected_sql"), args.sql_chars))
            lines.extend(_sql_block("후보 SQL", item.get("sql"), args.sql_chars))
            lines.append("")
        if len(mismatches["cases"]) > args.max_cases:
            lines.append("외 %d건은 CSV·JSON을 참고한다."
                         % (len(mismatches["cases"]) - args.max_cases))
    return "\n".join(lines) + "\n"


def write_sql_error_csv(path: Path, errors: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["difficulty", "id", "domain", "query_shape", "question",
                         "error_kind", "error_label", "subject", "error_first_line",
                         "error_full", "signature", "expected_sql", "agent_sql"])
        for name, items in group_by_difficulty(errors).items():
            for item in items:
                writer.writerow([
                    name, item["id"], item["domain"], item["query_shape"], item["question"],
                    item["kind"], ERROR_LABEL.get(item["kind"], item["kind"]),
                    item.get("subject", ""), item.get("first_line", ""),
                    (item.get("error") or "")[:4000], item.get("signature", ""),
                    (item.get("expected_sql") or "")[:4000],
                    (item.get("sql") or "")[:4000],
                ])


def write_mismatch_csv(path: Path, mismatches: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "difficulty", "domain", "query_shape", "question",
                         "primary_reason", "primary_label", "all_reasons",
                         "expected_columns", "actual_columns",
                         "expected_row_count", "actual_row_count", "row_count_diff",
                         "expected_scalar", "actual_scalar", "scalar_diff_pct",
                         "missing_tables", "extra_tables",
                         "diff_columns", "diff_sample", "notes",
                         "expected_sql", "agent_sql"])
        for item in mismatches:
            findings = item.get("findings") or {}
            shape = item.get("shape") or {}
            columns = findings.get("columns") or {}
            row_count = findings.get("row_count") or {}
            scalar = findings.get("scalar") or {}
            tables = findings.get("tables") or {}
            rows = findings.get("rows") or {}
            diffs = rows.get("column_diffs") or []
            sample = ""
            if diffs and diffs[0].get("samples"):
                first = diffs[0]["samples"][0]
                sample = "%s: %s → %s" % (first.get("key", ""),
                                          _fmt(first.get("expected")), _fmt(first.get("actual")))
            writer.writerow([
                item["id"], item["difficulty"], item["domain"], item["query_shape"],
                item["question"], item["primary_reason"], item["primary_label"],
                ", ".join(item["reasons"]),
                ", ".join(columns.get("expected") or shape.get("expected_columns") or []),
                ", ".join(columns.get("actual") or shape.get("actual_columns") or []),
                _fmt(shape.get("expected_row_count")), _fmt(shape.get("actual_row_count")),
                row_count.get("diff", ""),
                _fmt(shape.get("expected_scalar")), _fmt(shape.get("actual_scalar")),
                (scalar.get("delta") or {}).get("ratio_pct", ""),
                ", ".join(tables.get("missing") or []),
                ", ".join(tables.get("extra") or []),
                ", ".join("%s(%d행)" % (d["column"], d["differing_rows"]) for d in diffs),
                sample, ", ".join(item.get("notes") or []),
                (item.get("expected_sql") or "")[:4000],
                (item.get("sql") or "")[:4000],
            ])


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
    check("컬럼 없음 분류",
          classify_error("OperationalError: SYNTAX_ERROR: line 5:12: Column '가맹점명' "
                         "cannot be resolved")["kind"] == "column_not_found")
    check("컬럼 이름 추출",
          classify_error("Column '가맹점명' cannot be resolved")["subject"] == "가맹점명")
    check("테이블 없음 분류",
          classify_error("TABLE_NOT_FOUND: line 1:15: Table 'awsdatacatalog.card_system.tb_x' "
                         "does not exist")["kind"] == "table_not_found")
    check("구문 오류 분류",
          classify_error("SYNTAX_ERROR: line 1:1: mismatched input 'FROM'")["kind"] == "syntax")
    check("타입 불일치 분류",
          classify_error("TYPE_MISMATCH: Cannot apply operator: varchar = bigint")["kind"]
          == "type_mismatch")
    check("가드 분류", classify_error("ValueError: 읽기 전용이 아닌 키워드 포함")["kind"] == "guard")
    check("권한 분류", classify_error("AccessDeniedException: not authorized")["kind"] == "permission")
    check("미분류는 other", classify_error("무언가 이상함")["kind"] == "other")
    check("빈 문자열", classify_error("")["kind"] == "other")
    signature = classify_error("SYNTAX_ERROR: line 5:12: Column 'a' cannot be resolved")["signature"]
    same = classify_error("SYNTAX_ERROR: line 9:33: Column 'b' cannot be resolved")["signature"]
    check("시그니처로 같은 원인 묶임", signature == same)

    columns = ["기준년월", "금액"]
    gold = {"columns": columns, "row_count": 2, "rows": [["202601", 100], ["202602", 200]],
            "scalar": None, "float_digits": 6}
    record = {
        "id": "c1", "verdict": "mismatch", "difficulty": "hard",
        "detail": {"expected_columns": columns, "actual_columns": columns,
                   "expected_row_count": 2, "actual_row_count": 2,
                   "tables": {"expected": ["tbdaa1d12"], "actual": ["tbdaa1d12"],
                              "missing": [], "extra": []}},
        "candidate": {"columns": columns, "row_count": 2,
                      "rows": [["202601", 100], ["202602", 250]], "float_digits": 6},
    }
    analysis = analyze_mismatch(record, gold, 5)
    check("값 차이 → row_values", analysis["primary_reason"] == "row_values")
    diffs = analysis["findings"]["rows"]["column_diffs"]
    check("어긋난 컬럼 지목", len(diffs) == 1 and diffs[0]["column"] == "금액")
    check("불일치 행수", diffs[0]["differing_rows"] == 1)
    check("키 컬럼으로 매칭", diffs[0]["samples"][0]["key"] == "202602")
    check("증감률 계산", diffs[0]["samples"][0]["delta"]["ratio_pct"] == 25.0)

    record_rows = json.loads(json.dumps(record))
    record_rows["candidate"]["rows"] = [["202601", 100]]
    record_rows["candidate"]["row_count"] = 1
    record_rows["detail"]["actual_row_count"] = 1
    row_analysis = analyze_mismatch(record_rows, gold, 5)
    check("행 수 차이 → row_count", row_analysis["primary_reason"] == "row_count")
    check("행 수 과소 표시", row_analysis["findings"]["row_count"]["direction"] == "과소")
    check("정답에만 있는 행 집계",
          row_analysis["findings"]["rows"]["only_in_gold_count"] == 1)

    record_cols = json.loads(json.dumps(record))
    record_cols["detail"]["actual_columns"] = ["기준년월", "금액", "건수"]
    record_cols["candidate"]["columns"] = ["기준년월", "금액", "건수"]
    record_cols["candidate"]["rows"] = [["202601", 100, 1], ["202602", 200, 2]]
    col_analysis = analyze_mismatch(record_cols, gold, 5)
    check("컬럼 수 차이 → column_count", col_analysis["primary_reason"] == "column_count")
    check("후보에만 있는 컬럼",
          col_analysis["findings"]["columns"]["only_in_actual"] == ["건수"])

    swapped = json.loads(json.dumps(record))
    swapped["detail"]["actual_columns"] = ["금액", "기준년월"]
    swapped["candidate"]["columns"] = ["금액", "기준년월"]
    swapped["candidate"]["rows"] = [[100, "202601"], [250, "202602"]]
    swap_analysis = analyze_mismatch(swapped, gold, 5)
    check("컬럼 순서만 다름 인식",
          swap_analysis["findings"]["columns"].get("order_only") is True)
    check("컬럼 재배열 후 값 비교",
          any(d["column"] == "금액"
              for d in swap_analysis["findings"]["rows"]["column_diffs"]))

    scalar_record = {
        "id": "c2", "verdict": "mismatch", "difficulty": "easy",
        "detail": {"expected_columns": ["cnt"], "actual_columns": ["cnt"],
                   "expected_row_count": 1, "actual_row_count": 1,
                   "expected_scalar": 1000, "actual_scalar": 1200, "scalar_match": False,
                   "tables": {"missing": ["tmdaa5e11"], "extra": []}},
        "candidate": {"columns": ["cnt"], "row_count": 1, "rows": [[1200]],
                      "scalar": 1200, "float_digits": 6},
    }
    scalar_analysis = analyze_mismatch(
        scalar_record, {"columns": ["cnt"], "row_count": 1, "rows": [[1000]], "scalar": 1000}, 5)
    check("스칼라 차이 인식", "scalar_value" in scalar_analysis["reasons"])
    check("스칼라 증감률",
          scalar_analysis["findings"]["scalar"]["delta"]["ratio_pct"] == 20.0)
    check("테이블 누락도 원인에 포함", "tables" in scalar_analysis["reasons"])

    truncated = json.loads(json.dumps(record))
    truncated["candidate"]["row_count"] = 5000
    truncated["candidate"]["row_count_truncated"] = True
    truncated["detail"]["actual_row_count"] = 5000
    trunc_analysis = analyze_mismatch(truncated, gold, 5)
    check("상한 걸린 결과는 값 비교 생략",
          "skipped" in (trunc_analysis["findings"].get("rows") or {}))

    check("난이도 정렬",
          list(group_by_difficulty([{"difficulty": "hard"}, {"difficulty": "easy"},
                                    {"difficulty": "medium"}])) == ["easy", "medium", "hard"])
    deduped = dedupe_latest([
        {"id": "a", "verdict": "sql_error"},
        {"id": "b", "verdict": "mismatch"},
        {"id": "a", "verdict": "match"},
    ])
    check("같은 id는 마지막 채점만",
          [r["verdict"] for r in deduped] == ["match", "mismatch"])

    check("빈 SQL은 블록을 만들지 않음", _sql_block("정답 SQL", "", 100) == [])
    block = _sql_block("정답 SQL", "SELECT 1", 100)
    check("SQL 블록 구성", "```sql" in block and "SELECT 1" in block
          and "<details><summary>정답 SQL</summary>" in block)
    long_block = "\n".join(_sql_block("정답 SQL", "SELECT " + "a" * 500, 100))
    check("긴 SQL은 잘라서 표시", "이하 생략" in long_block and "앞 100자" in long_block)
    check("0이면 자르지 않음",
          "이하 생략" not in "\n".join(_sql_block("정답 SQL", "SELECT " + "a" * 500, 0)))

    collected = collect(
        [{"id": "x", "verdict": "mismatch", "difficulty": "hard",
          "agent": {"sql": "SELECT 2"}, "candidate": None, "detail": {}}],
        {"x": {"id": "x", "sql": "SELECT 1"}},
        type("A", (), {"verdicts": {"mismatch"}, "ids": [], "domain": "", "difficulty": "",
                       "shape": "", "max_samples": 5})(),
    )
    check("정답 SQL을 케이스에서 가져옴",
          collected["mismatches"][0]["expected_sql"] == "SELECT 1")
    check("후보 SQL은 채점 결과에서 가져옴", collected["mismatches"][0]["sql"] == "SELECT 2")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS, help="채점 결과 JSONL")
    parser.add_argument("--cases", type=Path, default=None,
                        help="정답 행을 읽을 골든셋 (기본: *_answered.jsonl, 없으면 원본)")
    parser.add_argument("--answers", type=Path, default=evalrun.DEFAULT_ANSWERS,
                        help="골든셋에 정답이 없을 때 참조할 정답 실행 이력")
    parser.add_argument("--out-md", type=Path, default=DEFAULT_OUT_MD, help="정리 리포트(마크다운)")
    parser.add_argument("--out-json", type=Path, default=DEFAULT_OUT_JSON, help="정리 결과(JSON)")
    parser.add_argument("--sql-error-csv", type=Path, default=DEFAULT_SQL_ERROR_CSV,
                        help="SQL 오류 CSV")
    parser.add_argument("--mismatch-csv", type=Path, default=DEFAULT_MISMATCH_CSV,
                        help="mismatch CSV")

    parser.add_argument("--verdicts", nargs="*", default=["sql_error", "mismatch"],
                        help="정리할 판정 (기본: sql_error mismatch)")
    parser.add_argument("--domain", default="", help="특정 도메인만")
    parser.add_argument("--difficulty", default="", help="특정 난이도만")
    parser.add_argument("--shape", default="", help="특정 query_shape만")
    parser.add_argument("--ids", nargs="*", default=[], help="특정 케이스 id만")
    parser.add_argument("--max-cases", type=int, default=60,
                        help="마크다운에 상세로 실을 케이스 수 (CSV·JSON은 전부 담는다)")
    parser.add_argument("--max-samples", type=int, default=5, help="값 차이 샘플 수")
    parser.add_argument("--sql-chars", type=int, default=1500,
                        help="마크다운에 실을 SQL 최대 길이 (0이면 전체. CSV·JSON은 항상 전체)")
    parser.add_argument("--print", dest="print_report", action="store_true",
                        help="마크다운을 화면에도 출력")
    parser.add_argument("--self-test", action="store_true", help="파일 없이 내부 로직 점검")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    args.verdicts = set(args.verdicts)
    if not args.results.exists():
        print("채점 결과 파일이 없다: %s" % args.results)
        print("먼저 scripts/run_goldenset_eval.py 로 채점해야 한다.")
        return 1

    records = latest_records(args.results)
    if not records:
        print("채점 결과가 비어 있다: %s" % args.results)
        return 1

    if args.cases is None:
        args.cases = (evalrun.DEFAULT_CASES_ANSWERED
                      if evalrun.DEFAULT_CASES_ANSWERED.exists() else evalrun.DEFAULT_CASES_BASE)
    cases_by_id: dict[str, dict] = {}
    if args.cases.exists():
        for case in evalrun.load_gold_cases(args.cases, args.answers):
            cases_by_id[str(case.get("id"))] = case
    else:
        print("[warn] 골든셋을 찾지 못해 정답 SQL·행 값 비교는 생략한다: %s" % args.cases)

    collected = collect(records, cases_by_id, args)
    summary = build_summary(collected, records, args)

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
    )
    markdown = render_markdown(summary, args)
    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(markdown, encoding="utf-8")
    write_sql_error_csv(args.sql_error_csv, collected["errors"])
    write_mismatch_csv(args.mismatch_csv, collected["mismatches"])

    if args.print_report:
        print(markdown)

    print("SQL 오류 %d건 / mismatch %d건 (채점 레코드 %d건)"
          % (summary["sql_error"]["total"], summary["mismatch"]["total"], len(records)))
    if summary["sql_error"]["by_kind"]:
        print("  오류 유형: %s" % ", ".join(
            "%s %d" % (ERROR_LABEL.get(k, k), v) for k, v in summary["sql_error"]["by_kind"].items()))
    if summary["mismatch"]["by_primary_reason"]:
        print("  mismatch 원인: %s" % ", ".join(
            "%s %d" % (REASON_LABEL.get(k, k), v)
            for k, v in summary["mismatch"]["by_primary_reason"].items()))
    print("정리 리포트: %s" % args.out_md)
    print("정리 JSON: %s" % args.out_json)
    print("SQL 오류 CSV: %s" % args.sql_error_csv)
    print("mismatch CSV: %s" % args.mismatch_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

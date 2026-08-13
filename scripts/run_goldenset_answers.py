#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""골든셋의 정답 SQL을 Amazon Athena에서 실제로 실행해 '정답 결과'를 채워 넣는다.

골든셋 JSONL을 한 건씩 순회하며 SQL을 실행하고, 케이스마다 결과 레코드를 즉시
flush 하므로 중단 후 같은 명령을 다시 실행하면 남은 건부터 이어서 처리한다.
실행이 끝나면 원본 골든셋에 `expected_answer` 를 병합한 파일을 새로 만든다.

    # 앞의 20건만 시험 실행
    python scripts/run_goldenset_answers.py --limit 20

    # 저장된 결과를 건너뛰고 나머지 전체 실행
    python scripts/run_goldenset_answers.py

    # 실패·빈결과 케이스만 재시도
    python scripts/run_goldenset_answers.py --retry-errors

    # 실행 없이 계획만 확인
    python scripts/run_goldenset_answers.py --dry-run

    # Athena 없이 내부 로직만 점검
    python scripts/run_goldenset_answers.py --self-test

인증은 표준 AWS 자격증명 체인을 그대로 사용하며, 접속 설정은 기존 서비스와 같은
환경변수(ATHENA_REGION / ATHENA_DATABASE / ATHENA_WORKGROUP / ATHENA_S3_STAGING_DIR /
ATHENA_CATALOG / ATHENA_PROFILE / ATHENA_ENDPOINT_URL)를 읽는다.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import sqlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CASES = ROOT / "tests" / "fixtures" / "corporate_sales_text2sql_goldenset_v2.jsonl"
DEFAULT_RESULTS = ROOT / "reports" / "goldenset-v2-answers.jsonl"
DEFAULT_SUMMARY = ROOT / "reports" / "goldenset-v2-answers-summary.md"
DEFAULT_MERGED = ROOT / "tests" / "fixtures" / "corporate_sales_text2sql_goldenset_v2_answered.jsonl"
DEFAULT_PREVIEW_CSV = ROOT / "reports" / "goldenset-v2-answers-preview.csv"

WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|MERGE|TRUNCATE|GRANT|REVOKE|MSCK|UNLOAD)\b",
    re.IGNORECASE,
)
TABLE_REF_RE = re.compile(
    r"\b(FROM|JOIN)\s+(?:([A-Za-z_][A-Za-z0-9_]*)\.)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
# 골든셋에 expected_tables 가 없을 때 쓰는 물리 테이블 목록(시맨틱 레이어 31종)
FALLBACK_TABLES = frozenset({
    "tbdaa1d12", "tmdaa1d12", "tbdaaac67", "tbdaaat01", "tbdaaat03", "tbdaaat05",
    "tbdaaat18", "tbdaaat46", "tbdaaat53", "tbdaabf07", "tbdaabt08", "tbdaabt30",
    "tbdaacb02", "tbdaada72", "tbdaadb17", "tbdaadt01", "tbdaaus01", "tbdccam02",
    "tbmaisd06", "tbmewcm94", "tddaa3d01", "tddaa3e21", "tddaa3e23", "tddaa3l01",
    "tmdaa1d01", "tmdaa3e16", "tmdaa5d01", "tmdaa5e11", "tmdaaaa12", "tmdaaus01",
    "tsmagcca1",
})

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"


# ---------------------------------------------------------------------------
# 값 정규화와 결과 해시
# ---------------------------------------------------------------------------
def normalize_value(value: Any, float_digits: int = 6) -> str:
    """DB 드라이버가 돌려주는 값을 비교 가능한 문자열로 바꾼다."""
    if value is None:
        return "\\N"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float):
        if value != value:            # NaN
            return "NaN"
        if value in (float("inf"), float("-inf")):
            return str(value)
        quantized = round(value, float_digits)
        if quantized == int(quantized):
            return str(int(quantized))
        return ("%.{}f".format(float_digits) % quantized).rstrip("0").rstrip(".")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    text = str(value).strip()
    return text


def normalize_rows(rows: Iterable[Iterable[Any]], float_digits: int = 6) -> list[str]:
    return ["\x1f".join(normalize_value(v, float_digits) for v in row) for row in rows]


def result_hashes(columns: list[str], rows: list[tuple], float_digits: int = 6) -> dict:
    """행 순서에 민감한 해시와 둔감한(멀티셋) 해시를 함께 만든다.

    ORDER BY 동점 구간에서 순서가 흔들려도 정답 비교가 깨지지 않도록 기본 비교는
    멀티셋 해시(`result_hash`)를 쓰고, 정렬까지 검증하고 싶을 때 `ordered_hash`를 쓴다.
    """
    normalized = normalize_rows(rows, float_digits)
    header = "\x1f".join(str(c) for c in columns)
    ordered = hashlib.sha256(("\x1e".join([header] + normalized)).encode("utf-8")).hexdigest()
    multiset = hashlib.sha256(
        ("\x1e".join([header] + sorted(normalized))).encode("utf-8")
    ).hexdigest()
    return {"ordered_hash": ordered, "result_hash": multiset}


def scalar_answer(columns: list[str], rows: list[tuple]) -> Any:
    """1행 1열 결과면 스칼라 정답으로 뽑아 둔다(수치 정답 채점용)."""
    if len(rows) == 1 and len(columns) == 1:
        value = rows[0][0]
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (dt.datetime, dt.date, dt.time)):
            return value.isoformat()
        return value
    return None


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    return value


# ---------------------------------------------------------------------------
# SQL 준비
# ---------------------------------------------------------------------------
def collect_table_names(cases: list[dict]) -> frozenset[str]:
    """골든셋의 expected_tables 에서 물리 테이블 이름만 모은다(접두사 유무 무관)."""
    names = set()
    for case in cases:
        for table in case.get("expected_tables") or []:
            names.add(str(table).split(".")[-1].strip().lower())
    return frozenset(names) if names else FALLBACK_TABLES


def apply_schema(sql: str, schema: str | None, tables: frozenset[str] = FALLBACK_TABLES) -> str:
    """물리 테이블 참조에 스키마 접두사를 붙이거나 뗀다.

    골든셋 SQL은 접두사 없이 테이블명만 쓰므로 기본값은 그대로 두는 것이다
    (pyathena 접속의 ATHENA_DATABASE 가 기본 스키마가 된다).
    database-qualified 이름이 필요한 환경에서만 --schema 로 접두사를 부여한다.
    schema 미지정 시 DB_SCHEMA 환경변수를 따르고, 'none' 이면 기존 접두사를 제거한다.
    """
    target = schema if schema is not None else os.getenv("DB_SCHEMA", "")
    target = (target or "").strip()
    if not target:
        return sql
    prefix = "" if target.lower() == "none" else target.rstrip(".") + "."

    def replace(match: "re.Match[str]") -> str:
        keyword, _existing, table = match.group(1), match.group(2), match.group(3)
        if table.lower() not in tables:
            return match.group(0)      # CTE 이름 등은 건드리지 않는다
        return "%s %s%s" % (keyword, prefix, table)

    return TABLE_REF_RE.sub(replace, sql)


def assert_read_only(sql: str) -> None:
    """운영 가드(db._validate_read_only_sql)와 같은 기준으로 판정한다.

    주석과 문자열 리터럴을 지우지 않고 세미콜론을 세면 멀쩡한 단일 SQL이
    다중 문장으로 걸린다. goldenset v2 실행 실패 40건 중 2건이 이것이었다.

        WHERE rn = 1 /* No column for "교체 카드" exists; using ... */
        "그룹등급", -- approximated grade column; using closest semantic match

    운영에서는 실행됐을 SQL이 평가에서만 실패해 원인 분석이 어긋난다.
    """
    stripped = sqlparse.format(sql or "", strip_comments=True).strip()
    statements = [
        statement.strip()
        for statement in sqlparse.split(stripped)
        if statement.strip().rstrip(";").strip()
    ]
    if len(statements) > 1:
        raise ValueError("다중 문장 SQL은 실행하지 않는다")
    if not statements:
        raise ValueError("실행할 SQL이 없다")
    statement = statements[0]
    # 세미콜론 없이 이어 붙인 쿼리는 sqlparse 가 한 문장으로 본다. 워크북 툴이
    # 내보내는 "-- [월초충당금] ... -- [월말충당금] ..." 이 그 모양이다.
    if len(re.findall(r"(?<![0-9A-Za-z_])WITH(?![0-9A-Za-z_])\s+[A-Za-z_\"]", statement, re.IGNORECASE)) > 1:
        raise ValueError("다중 문장 SQL은 실행하지 않는다")
    if not re.match(r"^(SELECT|WITH)\b", statement, re.IGNORECASE):
        raise ValueError("SELECT/WITH로 시작하지 않는 SQL")
    if WRITE_KEYWORDS.search(statement):
        raise ValueError("읽기 전용이 아닌 키워드 포함")


def wrap_row_limit(sql: str, limit: int) -> str:
    """스캔량이 아니라 반환 행 수를 제한한다(상세 목록 케이스 보호용)."""
    body = sql.rstrip().rstrip(";")
    return "SELECT * FROM (\n%s\n) AS _bounded_result\nLIMIT %d" % (body, limit)


# ---------------------------------------------------------------------------
# 실행기
# ---------------------------------------------------------------------------
class PyAthenaExecutor:
    """pyathena DB-API로 직접 실행한다. query id·스캔 바이트 등 실행 통계를 함께 얻는다."""

    name = "pyathena"

    def __init__(self, timeout_ms: int):
        from pyathena import connect  # 지연 import

        kwargs: dict[str, Any] = {
            "region_name": os.getenv("ATHENA_REGION", "ap-northeast-2"),
            "schema_name": os.getenv("ATHENA_DATABASE", "card_system"),
            "work_group": os.getenv("ATHENA_WORKGROUP", "primary"),
            "catalog_name": os.getenv("ATHENA_CATALOG", "AwsDataCatalog"),
            "s3_staging_dir": os.getenv("ATHENA_S3_STAGING_DIR", "") or None,
        }
        if os.getenv("ATHENA_PROFILE"):
            kwargs["profile_name"] = os.environ["ATHENA_PROFILE"]
        if os.getenv("ATHENA_ENDPOINT_URL"):
            kwargs["endpoint_url"] = os.environ["ATHENA_ENDPOINT_URL"]
        self._conn = connect(**{k: v for k, v in kwargs.items() if v is not None})
        self._timeout_ms = timeout_ms

    def run(self, sql: str, fetch_rows: int) -> dict:
        cur = self._conn.cursor()
        try:
            cur.execute(sql)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = [tuple(r) for r in cur.fetchmany(fetch_rows)] if cur.description else []
            return {
                "columns": columns,
                "rows": rows,
                "stats": {
                    "query_id": getattr(cur, "query_id", None),
                    "data_scanned_bytes": getattr(cur, "data_scanned_in_bytes", None),
                    "engine_ms": getattr(cur, "engine_execution_time_in_millis", None),
                    "total_ms": getattr(cur, "total_execution_time_in_millis", None),
                    "output_location": getattr(cur, "output_location", None),
                },
            }
        finally:
            try:
                cur.close()
            except Exception:
                pass


class AgentExecutor:
    """서비스와 완전히 같은 경로(text2sql_agent.db.execute_sql)로 실행한다.

    읽기 전용 가드·Athena 한글 식별자 처리·TBD→TMD 폴백이 그대로 적용되지만
    Athena 실행 통계(query id, 스캔 바이트)는 얻지 못한다.
    """

    name = "agent"

    def __init__(self, timeout_ms: int):
        from text2sql_agent import db as agent_db  # noqa: WPS433

        self._db = agent_db
        self._timeout_ms = timeout_ms

    def run(self, sql: str, fetch_rows: int) -> dict:
        columns, rows, error = self._db.execute_sql(
            sql, max_rows=fetch_rows, statement_timeout_ms=self._timeout_ms
        )
        if error:
            raise RuntimeError(error)
        return {"columns": list(columns), "rows": [tuple(r) for r in rows], "stats": {}}


class StubExecutor:
    """--self-test 전용. Athena 없이 파이프라인만 점검한다."""

    name = "stub"

    def __init__(self, timeout_ms: int = 0):
        self._timeout_ms = timeout_ms

    def run(self, sql: str, fetch_rows: int) -> dict:
        digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        seed = int(digest[:6], 16)
        if seed % 17 == 0:
            raise RuntimeError("stub: SYNTAX_ERROR simulated")
        if seed % 11 == 0:
            return {"columns": ["더미"], "rows": [], "stats": {"query_id": digest[:8]}}
        rows = [(("g%d" % i), Decimal(seed % 1000 + i), 1.23456789 + i) for i in range(3)]
        return {
            "columns": ["구분", "금액", "비율"],
            "rows": rows[: max(fetch_rows, 1)],
            "stats": {"query_id": digest[:8], "data_scanned_bytes": seed * 1024},
        }


def build_executor(kind: str, timeout_ms: int):
    if kind == "stub":
        return StubExecutor(timeout_ms)
    if kind == "agent":
        return AgentExecutor(timeout_ms)
    if kind == "pyathena":
        return PyAthenaExecutor(timeout_ms)
    # auto: pyathena 우선(실행 통계 확보), 실패하면 서비스 경로로 폴백
    try:
        return PyAthenaExecutor(timeout_ms)
    except Exception as exc:  # noqa: BLE001
        print("[warn] pyathena 초기화 실패(%s) → agent 실행기로 폴백" % exc)
        return AgentExecutor(timeout_ms)


# ---------------------------------------------------------------------------
# 입출력
# ---------------------------------------------------------------------------
def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_saved(path: Path) -> dict[str, dict]:
    saved: dict[str, dict] = {}
    for rec in read_jsonl(path):
        saved[rec["id"]] = rec        # 같은 id가 여러 번이면 마지막 실행이 최신
    return saved


def append_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


# ---------------------------------------------------------------------------
# 한 건 실행
# ---------------------------------------------------------------------------
def run_case(case: dict, executor, args) -> dict:
    sql = apply_schema(case["sql"], args.schema, getattr(args, "table_names", FALLBACK_TABLES))
    assert_read_only(sql)
    if args.row_limit:
        sql = wrap_row_limit(sql, args.row_limit)

    started = time.monotonic()
    record: dict[str, Any] = {
        "id": case["id"],
        "question": case.get("question", ""),
        "domain": case.get("domain", ""),
        "query_shape": case.get("query_shape", ""),
        "executed_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "executor": executor.name,
    }
    try:
        result = executor.run(sql, args.fetch_rows)
    except Exception as exc:  # noqa: BLE001
        record.update({
            "status": STATUS_ERROR,
            "error": str(exc)[:2000],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        })
        return record

    columns, rows = result["columns"], result["rows"]
    hashes = result_hashes(columns, rows, args.float_digits)
    truncated = len(rows) >= args.fetch_rows
    kept = rows[: args.answer_rows]
    record.update({
        "status": STATUS_OK if rows else STATUS_EMPTY,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "answer": {
            "columns": columns,
            "row_count": len(rows),
            "row_count_truncated": truncated,
            "rows": [[jsonable(v) for v in row] for row in kept],
            "rows_stored": len(kept),
            "scalar": jsonable(scalar_answer(columns, rows)),
            "result_hash": hashes["result_hash"],
            "ordered_hash": hashes["ordered_hash"],
            "float_digits": args.float_digits,
        },
        "athena": {k: v for k, v in (result.get("stats") or {}).items() if v is not None},
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
        prev = saved.get(case["id"])
        if args.rerun_all:
            pending.append(case)
        elif args.retry_errors:
            # 재시도 모드에서는 저장된 실패·0행 케이스만 다시 돌린다.
            if prev is not None and prev.get("status") in (STATUS_ERROR, STATUS_EMPTY):
                pending.append(case)
        elif prev is None:
            pending.append(case)
    if args.limit:
        pending = pending[: args.limit]
    return pending


# ---------------------------------------------------------------------------
# 병합·리포트
# ---------------------------------------------------------------------------
def merge_answers(cases: list[dict], saved: dict[str, dict], merged_path: Path,
                  preview_path: Path) -> dict:
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {STATUS_OK: 0, STATUS_EMPTY: 0, STATUS_ERROR: 0, "missing": 0}
    with merged_path.open("w", encoding="utf-8") as out:
        for case in cases:
            rec = saved.get(case["id"])
            merged = dict(case)
            if rec is None:
                counts["missing"] += 1
                merged["answer_status"] = "not_executed"
            else:
                counts[rec.get("status", STATUS_ERROR)] = counts.get(rec.get("status", STATUS_ERROR), 0) + 1
                merged["answer_status"] = rec.get("status")
                merged["expected_answer"] = rec.get("answer")
                merged["answer_error"] = rec.get("error")
                merged["answer_executed_at"] = rec.get("executed_at")
                if rec.get("athena"):
                    merged["answer_athena"] = rec["athena"]
            out.write(json.dumps(merged, ensure_ascii=False, default=str) + "\n")

    with preview_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["id", "domain", "question", "answer_status", "row_count",
                         "scalar_answer", "first_row", "result_hash", "error"])
        for case in cases:
            rec = saved.get(case["id"])
            ans = (rec or {}).get("answer") or {}
            first_row = ans.get("rows") or []
            writer.writerow([
                case["id"], case.get("domain", ""), case.get("question", ""),
                (rec or {}).get("status", "not_executed"),
                ans.get("row_count", ""),
                ans.get("scalar", ""),
                json.dumps(first_row[0], ensure_ascii=False) if first_row else "",
                ans.get("result_hash", ""),
                ((rec or {}).get("error") or "")[:300],
            ])
    return counts


def write_summary(path: Path, cases: list[dict], saved: dict[str, dict], counts: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    executed = [r for r in saved.values() if r.get("status") != STATUS_SKIPPED]
    scanned = sum((r.get("athena") or {}).get("data_scanned_bytes") or 0 for r in executed)
    elapsed = [r.get("elapsed_ms", 0) for r in executed if r.get("elapsed_ms")]
    by_domain: dict[str, dict[str, int]] = {}
    for case in cases:
        rec = saved.get(case["id"])
        st = (rec or {}).get("status", "not_executed")
        by_domain.setdefault(case.get("domain", ""), {}).setdefault(st, 0)
        by_domain[case.get("domain", "")][st] += 1

    lines = ["# 골든셋 정답 실행 요약", ""]
    lines.append("- 전체 케이스: %d건" % len(cases))
    lines.append("- 정상(행 있음): %d건" % counts.get(STATUS_OK, 0))
    lines.append("- 정상(0행): %d건" % counts.get(STATUS_EMPTY, 0))
    lines.append("- 실패: %d건" % counts.get(STATUS_ERROR, 0))
    lines.append("- 미실행: %d건" % counts.get("missing", 0))
    if elapsed:
        lines.append("- 평균 실행시간: %.1f초 / 최대 %.1f초"
                     % (sum(elapsed) / len(elapsed) / 1000, max(elapsed) / 1000))
    if scanned:
        lines.append("- 누적 스캔량: %.2f GB" % (scanned / 1024 ** 3))
    lines.append("")
    lines.append("| 도메인 | ok | empty | error | 미실행 |")
    lines.append("|---|---:|---:|---:|---:|")
    for domain in sorted(by_domain):
        d = by_domain[domain]
        lines.append("| %s | %d | %d | %d | %d |" % (
            domain, d.get(STATUS_OK, 0), d.get(STATUS_EMPTY, 0),
            d.get(STATUS_ERROR, 0), d.get("not_executed", 0)))
    errors = [r for r in saved.values() if r.get("status") == STATUS_ERROR]
    if errors:
        lines.extend(["", "## 실패 케이스 (최대 30건)", ""])
        for rec in errors[:30]:
            lines.append("- `%s` %s → %s" % (rec["id"], rec.get("question", ""),
                                             (rec.get("error") or "").splitlines()[0][:200]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 정답 재검증 (데이터 드리프트 감지)
# ---------------------------------------------------------------------------
def verify(cases: list[dict], saved: dict[str, dict], executor, args) -> int:
    drifted = []
    checked = 0
    for case in select_cases(cases, {}, args):
        prev = saved.get(case["id"])
        if not prev or prev.get("status") not in (STATUS_OK, STATUS_EMPTY):
            continue
        fresh = run_case(case, executor, args)
        checked += 1
        old_hash = (prev.get("answer") or {}).get("result_hash")
        new_hash = (fresh.get("answer") or {}).get("result_hash")
        if fresh.get("status") == STATUS_ERROR or old_hash != new_hash:
            drifted.append((case["id"], case.get("question", ""),
                            old_hash, new_hash, fresh.get("error")))
    print("검증 %d건 / 불일치 %d건" % (checked, len(drifted)))
    for row in drifted[:30]:
        print("  DRIFT %s | %s | %s -> %s %s" % row)
    return 1 if drifted else 0


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------
def self_test() -> int:
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  PASS " if cond else "  FAIL ") + name)
        ok = ok and cond

    print("self-test")
    check("None 정규화", normalize_value(None) == "\\N")
    check("Decimal 정규화", normalize_value(Decimal("1234.5000")) == "1234.5")
    check("float 반올림", normalize_value(1.23456789) == normalize_value(Decimal("1.234568")))
    check("정수형 float", normalize_value(3.0) == "3")

    cols = ["구분", "금액"]
    rows_a = [("A", Decimal("10")), ("B", Decimal("20"))]
    rows_b = [("B", Decimal("20")), ("A", Decimal("10"))]
    ha, hb = result_hashes(cols, rows_a), result_hashes(cols, rows_b)
    check("멀티셋 해시는 순서에 둔감", ha["result_hash"] == hb["result_hash"])
    check("정렬 해시는 순서에 민감", ha["ordered_hash"] != hb["ordered_hash"])
    check("스칼라 추출", scalar_answer(["cnt"], [(7,)]) == 7)
    check("스칼라 아님", scalar_answer(cols, rows_a) is None)

    check("접두사 없음 유지", apply_schema("FROM tbdaa1d12 a", "") == "FROM tbdaa1d12 a")
    check("접두사 부여", apply_schema("FROM tbdaa1d12 a", "kbcard_db")
          == "FROM kbcard_db.tbdaa1d12 a")
    check("기존 접두사 교체", apply_schema("FROM card_system.tbdaa1d12 a", "kbcard_db")
          == "FROM kbcard_db.tbdaa1d12 a")
    check("접두사 제거", apply_schema("FROM card_system.tbdaa1d12 a", "none")
          == "FROM tbdaa1d12 a")
    check("CTE 이름은 보존", apply_schema("FROM base b\nJOIN tbdaadb17 i", "kbcard_db")
          == "FROM base b\nJOIN kbcard_db.tbdaadb17 i")
    check("테이블 목록 수집",
          collect_table_names([{"expected_tables": ["card_system.tbdaa1d12", "tmdaa5e11"]}])
          == frozenset({"tbdaa1d12", "tmdaa5e11"}))
    try:
        assert_read_only("DELETE FROM t")
        check("쓰기 SQL 차단", False)
    except ValueError:
        check("쓰기 SQL 차단", True)
    try:
        assert_read_only("SELECT 1; SELECT 2")
        check("다중 문장 차단", False)
    except ValueError:
        check("다중 문장 차단", True)
    check("행 제한 래핑", wrap_row_limit("SELECT 1", 10).endswith("LIMIT 10"))
    return 0 if ok else 1


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help="골든셋 JSONL")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS, help="케이스별 실행 결과 JSONL")
    parser.add_argument("--merged", type=Path, default=DEFAULT_MERGED, help="정답 병합본 출력 경로")
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY, help="요약 리포트 경로")
    parser.add_argument("--preview-csv", type=Path, default=DEFAULT_PREVIEW_CSV, help="검수용 CSV 경로")

    parser.add_argument("--executor", choices=["auto", "pyathena", "agent", "stub"], default="auto")
    parser.add_argument("--schema", default=None,
                        help="테이블 참조에 붙일 스키마 접두사. 'none'이면 기존 접두사 제거. "
                             "미지정 시 DB_SCHEMA 환경변수를 따르고, 비어 있으면 원본 그대로 실행")
    parser.add_argument("--fetch-rows", type=int, default=5000,
                        help="행 수·해시 계산을 위해 실제로 가져올 최대 행 수 (기본 5000)")
    parser.add_argument("--answer-rows", type=int, default=50,
                        help="정답으로 파일에 저장할 최대 행 수 (기본 50)")
    parser.add_argument("--row-limit", type=int, default=0,
                        help="원본 SQL을 감싸 반환 행을 강제 제한 (0이면 사용 안 함)")
    parser.add_argument("--timeout-ms", type=int, default=180_000, help="문장 타임아웃(ms)")
    parser.add_argument("--float-digits", type=int, default=6, help="실수 비교 반올림 자릿수")

    parser.add_argument("--limit", type=int, default=0, help="이번 실행에서 처리할 최대 건수")
    parser.add_argument("--retry-errors", action="store_true", help="실패·0행 케이스만 재시도")
    parser.add_argument("--rerun-all", action="store_true", help="저장 여부와 무관하게 전부 다시 실행")
    parser.add_argument("--restart", action="store_true", help="저장된 결과를 지우고 처음부터")
    parser.add_argument("--domain", default="", help="특정 도메인만 실행")
    parser.add_argument("--shape", default="", help="특정 query_shape만 실행")
    parser.add_argument("--ids", nargs="*", default=[], help="특정 케이스 id만 실행")
    parser.add_argument("--sleep", type=float, default=0.0, help="케이스 사이 대기(초)")
    parser.add_argument("--max-scanned-gb", type=float, default=0.0,
                        help="누적 스캔량이 이 값을 넘으면 중단 (0이면 제한 없음)")
    parser.add_argument("--stop-after-errors", type=int, default=0,
                        help="연속 실패가 이 횟수를 넘으면 중단 (0이면 제한 없음)")

    parser.add_argument("--dry-run", action="store_true", help="실행 없이 대상만 출력")
    parser.add_argument("--verify", action="store_true", help="저장된 정답과 재실행 결과를 비교")
    parser.add_argument("--merge-only", action="store_true", help="실행 없이 병합·리포트만 다시 생성")
    parser.add_argument("--self-test", action="store_true", help="Athena 없이 내부 로직 점검")
    args = parser.parse_args()

    if args.self_test:
        return self_test()

    cases = read_jsonl(args.cases)
    if not cases:
        print("골든셋을 찾을 수 없다: %s" % args.cases)
        return 1

    args.table_names = collect_table_names(cases)

    if args.restart and args.results.exists():
        args.results.unlink()
    saved = load_saved(args.results)

    if args.merge_only:
        counts = merge_answers(cases, saved, args.merged, args.preview_csv)
        write_summary(args.summary, cases, saved, counts)
        print("병합 완료: %s" % args.merged)
        return 0

    if args.verify:
        executor = build_executor(args.executor, args.timeout_ms)
        return verify(cases, saved, executor, args)

    pending = select_cases(cases, saved, args)
    print("전체 %d건 / 저장됨 %d건 / 이번 실행 대상 %d건" % (len(cases), len(saved), len(pending)))
    if args.dry_run:
        for case in pending[:20]:
            print("  %s %s" % (case["id"], case.get("question", "")))
        if len(pending) > 20:
            print("  ... 외 %d건" % (len(pending) - 20))
        return 0
    if not pending:
        counts = merge_answers(cases, saved, args.merged, args.preview_csv)
        write_summary(args.summary, cases, saved, counts)
        print("실행할 케이스가 없다. 병합본만 갱신했다: %s" % args.merged)
        return 0

    executor = build_executor(args.executor, args.timeout_ms)
    scanned_total = sum((r.get("athena") or {}).get("data_scanned_bytes") or 0
                        for r in saved.values())
    consecutive_errors = 0

    for index, case in enumerate(pending, 1):
        record = run_case(case, executor, args)
        saved[case["id"]] = record
        append_record(args.results, record)

        status = record.get("status")
        detail = ""
        if status == STATUS_ERROR:
            detail = (record.get("error") or "").splitlines()[0][:120]
            consecutive_errors += 1
        else:
            consecutive_errors = 0
            ans = record.get("answer") or {}
            detail = "%d행" % ans.get("row_count", 0)
            if ans.get("scalar") is not None:
                detail += " / %s" % ans["scalar"]
        print("[%d/%d] %s %s %s (%.1fs)" % (
            index, len(pending), case["id"], status, detail,
            record.get("elapsed_ms", 0) / 1000))

        scanned_total += (record.get("athena") or {}).get("data_scanned_bytes") or 0
        if args.max_scanned_gb and scanned_total / 1024 ** 3 >= args.max_scanned_gb:
            print("누적 스캔량 %.2f GB 도달 → 중단" % (scanned_total / 1024 ** 3))
            break
        if args.stop_after_errors and consecutive_errors >= args.stop_after_errors:
            print("연속 실패 %d회 → 중단" % consecutive_errors)
            break
        if args.sleep:
            time.sleep(args.sleep)

    counts = merge_answers(cases, saved, args.merged, args.preview_csv)
    write_summary(args.summary, cases, saved, counts)
    print("정답 병합본: %s" % args.merged)
    print("요약 리포트: %s" % args.summary)
    print("검수용 CSV: %s" % args.preview_csv)
    return 0 if counts.get(STATUS_ERROR, 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

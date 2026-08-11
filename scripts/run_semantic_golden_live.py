#!/usr/bin/env python3
"""Run golden questions through the real LangGraph agent and save every result.

The runner is sequential on purpose: one compiled graph, one LLM/DB request at
a time, and one flushed JSONL record per case. Re-running resumes completed
case IDs. Missing parameters are filled with deterministic test defaults and
the same case continues automatically. Use --retry-errors or --restart when
needed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from calendar import monthrange
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_CASES = ROOT / "tests" / "fixtures" / "semantic_layer_golden_v1.jsonl"
DEFAULT_RESULTS = ROOT / "reports" / "semantic-golden-live-results.jsonl"
DEFAULT_SUMMARY = ROOT / "reports" / "semantic-golden-live-summary.md"
MAX_DEFAULT_PARAM_ROUNDS = 3

DEFAULT_PARAM_VALUES: dict[str, Any] = {
    "가맹점명": "도미노피자",
    "가맹점번호": "595931113",
    "기업명": "삼성전자",
    "회사명": "삼성전자",
    "대상명": "삼성전자",
    "대상기업": "삼성전자",
    "상품명": "전기요금전용",
    "결제금융기관코드": "004",
    "기업규모구분코드": "1",
    "semantic_attribute:enterprise_size": "대기업",
    "semantic_attribute:merchant_payment_institution": "KB국민은행",
    "업종축": "가맹점업종",
    "대손지표": "가맹점포트폴리오대손충당금률",
    "리스크지표": "기업한도소진율",
    "집계grain": "기업별",
    "업종": "음식점",
    "월매출금액": 100_000_000,
    "월평균금액": 50_000_000,
    "한도금액": 20_000_000,
    "한도소진율": 0.5,
    "조회개월수": 6,
    "조회기간개월수": 6,
    "limit": 100,
    "관리기업목록": ["1234567890"],
    "LS": 0.8,
    "IS": 1.2,
}


def load_jsonl(
    path: Path, *, repair_trailing_result: bool = False
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if repair_trailing_result and line_number == len(lines):
                path.write_text("".join(lines[:-1]), encoding="utf-8")
                print(
                    f"warning: removed truncated final result row from {path}",
                    file=sys.stderr,
                )
                break
            raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row must be an object at {path}:{line_number}")
        rows.append(value)
    return rows


def invoke_real_agent(
    question: str,
    *,
    continuation: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke the same compiled graph used by the service, without an input loop."""
    from text2sql_agent.workflow import _get_app, _new_initial_state

    state = _new_initial_state(question)
    if continuation:
        state["question_type"] = "need_sql"
        for key in (
            "selected_domain",
            "domain_candidates",
            "domain_routing_trace",
            "domain_context",
            "selected_capability_type",
            "selected_capability_name",
            "query_frame",
        ):
            if key in continuation:
                state[key] = continuation[key]

        supplied = dict(params or {})
        if continuation.get("selected_tool"):
            merged = dict(continuation.get("tool_params") or {})
            merged.update(supplied)
            state["selected_tool"] = continuation["selected_tool"]
            state["tool_params"] = merged
        elif continuation.get("matched_query_name"):
            merged = dict(continuation.get("extracted_params") or {})
            merged.update(continuation.get("user_provided_params") or {})
            merged.update(supplied)
            state["matched_query_name"] = continuation["matched_query_name"]
            state["matched_query_sql"] = continuation.get("matched_query_sql", "")
            state["matched_query_params"] = continuation.get("matched_query_params", {})
            state["user_provided_params"] = merged
        else:
            merged = dict(continuation.get("user_provided_params") or {})
            merged.update(supplied)
            state["user_provided_params"] = merged
            state["selected_tables"] = continuation.get("selected_tables") or []
            state["table_details"] = continuation.get("table_details") or ""
    return _get_app().invoke(state)


def _shift_month(yyyymm: str, months: int) -> str:
    month_index = int(yyyymm[:4]) * 12 + int(yyyymm[4:]) - 1 + months
    return f"{month_index // 12:04d}{month_index % 12 + 1:02d}"


def _reference_datetime(case: dict[str, Any]) -> datetime:
    raw = str((case.get("parameters") or {}).get("reference_date") or "")
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return datetime.now().astimezone()


def _default_param_value(name: str, info: dict[str, Any], reference: datetime) -> Any:
    is_time_param = any(
        token in name for token in ("년월", "기간", "시작", "종료", "기준일", "기준년", "만료")
    )
    if info.get("default") not in (None, "") and not is_time_param:
        return info["default"]
    if name in DEFAULT_PARAM_VALUES:
        value = DEFAULT_PARAM_VALUES[name]
        return list(value) if isinstance(value, list) else value

    yyyymm = reference.strftime("%Y%m")
    year = reference.strftime("%Y")
    if name == "카드만료기준일":
        return f"{yyyymm}{monthrange(reference.year, reference.month)[1]:02d}"
    if name == "기준년월일" or "기준일" in name:
        return reference.strftime("%Y%m%d")
    if "시작일" in name:
        return f"{year}0101"
    if "종료일" in name:
        return reference.strftime("%Y%m%d")
    if name == "전월기준년월":
        return _shift_month(yyyymm, -1)
    if name == "기준년":
        return year
    if name.startswith("발급기간_"):
        return f"{year}{'01' if name.endswith('시작') else '03'}"
    if name.startswith("해지기간_"):
        return f"{year}{'04' if name.endswith('시작') else '06'}"
    if name.startswith("기준기간_"):
        return f"{int(year) - 1}{'01' if name.endswith('시작') else '06'}"
    if name.startswith("대상기간_"):
        return f"{year}{'01' if name.endswith('시작') else '06'}"
    if name == "최근6개월_시작":
        return _shift_month(yyyymm, -5)
    if name == "기간":
        return yyyymm
    if "기간" in name and name.endswith("시작"):
        return _shift_month(yyyymm, -5)
    if "기간" in name and name.endswith("종료"):
        return yyyymm
    if "년월" in name or name in {"기준월", "등록기준년월"}:
        return yyyymm
    if "가맹점" in name and "명" in name:
        return "도미노피자"
    if any(token in name for token in ("기업명", "회사명", "업체명", "고객명", "대상명")):
        return "삼성전자"
    if "상품" in name and "명" in name:
        return "전기요금전용"
    if "분류축" in name:
        label = str(info.get("label") or "")
        options = label.partition("(")[2].rpartition(")")[0]
        if options:
            return options.split("/", 1)[0].strip()
        return "가맹점업종" if "업종" in name else "기업별"

    param_type = str(info.get("type") or "string").lower()
    if param_type == "business_number_list":
        return ["1234567890"]
    if param_type == "boolean":
        return False
    if param_type == "integer":
        return 10
    if param_type in {"number", "float", "decimal"}:
        return 1.0
    if param_type == "list":
        return ["전체"]
    return "전체"


def _default_params(case: dict[str, Any], missing: list[Any]) -> dict[str, Any]:
    reference = _reference_datetime(case)
    defaults: dict[str, Any] = {}
    for item in missing:
        info = item if isinstance(item, dict) else {"name": str(item)}
        name = str(info.get("name") or "").strip()
        if name:
            defaults[name] = _default_param_value(name, info, reference)
    return defaults


def _answer_saved(answer: Any) -> bool:
    return bool(answer.strip()) if isinstance(answer, str) else answer not in (None, "", [], {})


def _query_output_saved(actual: dict[str, Any]) -> bool:
    return bool(
        actual.get("generated_sql")
        or actual.get("final_sql")
        or actual.get("returned_row_count")
        or actual.get("selected_tool")
        or actual.get("matched_query_name")
    )


def _case_sha256(case: dict[str, Any]) -> str:
    payload = json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _actual(result: dict[str, Any]) -> dict[str, Any]:
    rows = result.get("query_rows")
    return {
        "answer": result.get("answer"),
        "question_type": result.get("question_type"),
        "selected_domain": result.get("selected_domain"),
        "selected_capability_type": result.get("selected_capability_type"),
        "selected_capability_name": result.get("selected_capability_name"),
        "selected_tool": result.get("selected_tool"),
        "matched_query_name": result.get("matched_query_name"),
        "selected_tables": result.get("selected_tables") or [],
        "extracted_params": result.get("extracted_params") or {},
        "param_stage": result.get("param_stage"),
        "missing_params": result.get("missing_params") or [],
        "generated_sql": result.get("generated_sql"),
        "final_sql": result.get("final_sql"),
        "validation_result": result.get("validation_result"),
        "is_valid": result.get("is_valid"),
        "retry_count": result.get("retry_count"),
        "query_columns": result.get("query_columns") or [],
        "returned_row_count": len(rows) if isinstance(rows, (list, tuple)) else 0,
        "query_error": result.get("query_error"),
        "error_message": result.get("error_message"),
        "result_scope": result.get("result_scope") or {},
    }


def _record(
    case: dict[str, Any],
    attempt: int,
    invoke: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    started = time.perf_counter()
    error: dict[str, str] | None = None
    default_params: dict[str, Any] = {}
    default_param_rounds = 0
    try:
        question = str(case.get("question_ko") or "")
        result = invoke(question)
        if not isinstance(result, dict):
            raise TypeError(f"agent returned {type(result).__name__}, expected dict")
        for _ in range(MAX_DEFAULT_PARAM_ROUNDS):
            if result.get("param_stage") != "need_params":
                break
            missing = result.get("missing_params") or case.get("expected_missing_parameters") or []
            proposed = _default_params(case, missing)
            new_defaults = {
                name: value
                for name, value in proposed.items()
                if name not in default_params
            }
            if not new_defaults:
                break
            default_params.update(new_defaults)
            default_param_rounds += 1
            result = invoke(
                question,
                continuation=result,
                params=dict(default_params),
            )
            if not isinstance(result, dict):
                raise TypeError(f"agent returned {type(result).__name__}, expected dict")
        actual = _actual(result)
        answer_saved = _answer_saved(actual["answer"])
        data_answer_saved = answer_saved and _query_output_saved(actual)
        has_error = bool(actual["query_error"] or actual["error_message"])
        if actual["param_stage"] == "need_params":
            status = "requires_params"
        elif has_error:
            status = "agent_error"
        elif case.get("expected_action") == "unsupported":
            if (
                actual.get("question_type") == "reject"
                and answer_saved
                and not data_answer_saved
            ):
                status = "unsupported"
            elif not answer_saved:
                status = "missing_answer"
            else:
                status = "action_mismatch"
        elif answer_saved:
            status = "answered"
        else:
            status = "missing_answer"
    except Exception as exc:  # noqa: BLE001 - one failed case must not stop 1,000.
        actual = {}
        answer_saved = False
        data_answer_saved = False
        has_error = True
        status = "execution_error"
        error = {"type": type(exc).__name__, "message": str(exc)}

    expected_sql = case.get("expected_sql")
    source = case.get("source") if isinstance(case.get("source"), dict) else {}
    return {
        "id": case.get("id"),
        "question_ko": case.get("question_ko"),
        "semantic_layer_version": case.get("semantic_layer_version"),
        "case_sha256": _case_sha256(case),
        "attempt": attempt,
        "started_at": started_at,
        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "duration_seconds": round(time.perf_counter() - started, 3),
        "status": status,
        "record_saved": True,
        "answer_saved": answer_saved,
        "data_answer_saved": data_answer_saved,
        "unsupported_handled": status == "unsupported",
        "has_error": has_error,
        "default_params_applied": default_params,
        "default_param_rounds": default_param_rounds,
        "default_param_reference_date": (
            _reference_datetime(case).strftime("%Y-%m-%d") if default_params else None
        ),
        "expected": {
            "action": case.get("expected_action"),
            "domain": case.get("domain"),
            "source_id": source.get("id"),
            "sql_kind": expected_sql.get("kind")
            if isinstance(expected_sql, dict)
            else None,
        },
        "actual": actual,
        "error": error,
    }


def _matching_latest(
    cases: list[dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    cases_by_id = {str(case.get("id") or ""): case for case in cases}
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        case_id = str(row.get("id") or "")
        case = cases_by_id.get(case_id)
        if not case:
            continue
        row_hash = row.get("case_sha256")
        current_hash = _case_sha256(case)
        legacy_match = (
            not row_hash
            and row.get("question_ko") == case.get("question_ko")
            and row.get("semantic_layer_version") == case.get("semantic_layer_version")
        )
        if row_hash == current_hash or legacy_match:
            latest[case_id] = row
    return latest


def _failed(row: dict[str, Any]) -> bool:
    return bool(row.get("has_error")) or row.get("status") in {
        "action_mismatch",
        "missing_answer",
        "requires_params",
    }


def _validate_case_ids(cases: list[dict[str, Any]]) -> None:
    if not cases:
        raise ValueError("golden case input must contain at least one row")
    ids = [str(case.get("id") or "") for case in cases]
    duplicates = sorted(case_id for case_id, count in Counter(ids).items() if count > 1)
    if "" in ids:
        raise ValueError("every golden case must have a non-empty id")
    if duplicates:
        raise ValueError(f"duplicate golden case ids: {', '.join(duplicates[:20])}")


def run_cases(
    cases: list[dict[str, Any]],
    results_path: Path,
    *,
    limit: int = 0,
    retry_errors: bool = False,
    invoke: Callable[..., dict[str, Any]] = invoke_real_agent,
) -> list[dict[str, Any]]:
    _validate_case_ids(cases)
    existing = load_jsonl(results_path, repair_trailing_result=True)
    latest = _matching_latest(cases, existing)

    attempts = Counter(str(row.get("id") or "") for row in existing)
    pending = [
        case
        for case in cases
        if not (
            (saved := latest.get(str(case.get("id") or "")))
            and not (
                saved.get("status") == "requires_params"
                and "default_params_applied" not in saved
            )
            and not (
                case.get("expected_action") == "unsupported"
                and "unsupported_handled" not in saved
            )
            and (not retry_errors or not _failed(saved))
        )
    ]
    if limit > 0:
        pending = pending[:limit]

    results_path.parent.mkdir(parents=True, exist_ok=True)
    with results_path.open("a", encoding="utf-8") as file:
        for index, case in enumerate(pending, 1):
            case_id = str(case.get("id") or "")
            record = _record(case, attempts[case_id] + 1, invoke)
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            file.flush()
            os.fsync(file.fileno())
            attempts[case_id] += 1
            print(
                f"[{index}/{len(pending)}] {case_id}: {record['status']} "
                f"({record['duration_seconds']:.3f}s)"
            )
    return load_jsonl(results_path, repair_trailing_result=True)


def build_summary(
    cases: list[dict[str, Any]], rows: list[dict[str, Any]], cases_path: Path
) -> dict[str, Any]:
    _validate_case_ids(cases)
    latest = _matching_latest(cases, rows)
    current = [latest[str(case["id"])] for case in cases if str(case["id"]) in latest]
    status_counts = Counter(str(row.get("status") or "unknown") for row in current)
    by_action: dict[str, dict[str, int]] = {}
    for case in cases:
        action = str(case.get("expected_action") or "unknown")
        stats = by_action.setdefault(
            action,
            {
                "total": 0,
                "attempted": 0,
                "answers_saved": 0,
                "data_answers_saved": 0,
                "unsupported_handled": 0,
                "outcome_failures": 0,
                "errors": 0,
            },
        )
        stats["total"] += 1
        row = latest.get(str(case.get("id") or ""))
        if row:
            stats["attempted"] += 1
            stats["answers_saved"] += int(bool(row.get("answer_saved")))
            stats["data_answers_saved"] += int(bool(row.get("data_answer_saved")))
            stats["unsupported_handled"] += int(bool(row.get("unsupported_handled")))
            stats["outcome_failures"] += int(_failed(row))
            stats["errors"] += int(bool(row.get("has_error")))
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dataset_path": str(cases_path.resolve()),
        "dataset_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        "total_cases": len(cases),
        "attempt_records": len(rows),
        "records_saved": len(current),
        "pending": len(cases) - len(current),
        "answers_saved": sum(bool(row.get("answer_saved")) for row in current),
        "data_answers_saved": sum(bool(row.get("data_answer_saved")) for row in current),
        "unsupported_handled": status_counts.get("unsupported", 0),
        "outcome_failures": sum(_failed(row) for row in current),
        "errors": sum(bool(row.get("has_error")) for row in current),
        "defaulted_cases": sum(bool(row.get("default_params_applied")) for row in current),
        "default_param_rounds": sum(int(row.get("default_param_rounds") or 0) for row in current),
        "requires_params": status_counts.get("requires_params", 0),
        "status_counts": dict(sorted(status_counts.items())),
        "by_action": dict(sorted(by_action.items())),
        "latest": current,
    }


def _cell(value: Any, limit: int = 100) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_summary(summary: dict[str, Any], results_path: Path) -> str:
    attempted = summary["records_saved"]
    answer_rate = summary["answers_saved"] / attempted * 100 if attempted else 0.0
    lines = [
        "# 시맨틱 골든셋 실제 에이전트 실행 결과",
        "",
        "> 실제 LangGraph 에이전트의 LLM·DB 실행 결과를 케이스별로 저장한 요약입니다.",
        "",
        "## 종합",
        "",
        "| 항목 | 건수 |",
        "|---|---:|",
        f"| 전체 골든 케이스 | {summary['total_cases']:,} |",
        f"| 실행·저장 완료 | {attempted:,} |",
        f"| 미실행 | {summary['pending']:,} |",
        f"| 응답 문구 저장 | {summary['answers_saved']:,} ({answer_rate:.2f}%) |",
        f"| SQL·데이터 답변 저장 | {summary['data_answers_saved']:,} |",
        f"| 지원 불가 정상 처리 | {summary['unsupported_handled']:,} |",
        f"| 기대 결과 불일치·실패 | {summary['outcome_failures']:,} |",
        f"| 오류 포함 | {summary['errors']:,} |",
        f"| 기본 파라미터 자동 적용 | {summary['defaulted_cases']:,} |",
        f"| 기본 파라미터 적용 횟수 | {summary['default_param_rounds']:,} |",
        f"| 기본값 적용 후에도 파라미터 필요 | {summary['requires_params']:,} |",
        f"| 누적 실행 시도 | {summary['attempt_records']:,} |",
        "",
        "| 상태 | 건수 | 의미 |",
        "|---|---:|---|",
    ]
    meanings = {
        "answered": "오류 없이 실제 답변이 저장됨",
        "unsupported": "SQL·DB 결과 없이 지원 불가 안내만 저장됨",
        "action_mismatch": "지원 불가 질문에 SQL·데이터 답변을 시도했거나 reject로 종료하지 않음",
        "requires_params": "기본값 자동 적용 후에도 에이전트가 추가 입력을 요청함",
        "agent_error": "그래프가 오류 상태를 반환함",
        "execution_error": "에이전트 호출 자체가 예외로 실패함",
        "missing_answer": "오류 표시는 없지만 답변이 비어 있음",
    }
    for status, count in summary["status_counts"].items():
        lines.append(f"| `{status}` | {count:,} | {meanings.get(status, '')} |")

    lines.extend(
        [
            "",
            "## 예상 액션별 저장 결과",
            "",
            "| 예상 액션 | 전체 | 실행 | 응답 문구 | SQL·데이터 답변 | 지원 불가 정상 | 실패 | 시스템 오류 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for action, values in summary["by_action"].items():
        lines.append(
            f"| `{action}` | {values['total']:,} | {values['attempted']:,} | "
            f"{values['answers_saved']:,} | {values['data_answers_saved']:,} | "
            f"{values['unsupported_handled']:,} | {values['outcome_failures']:,} | "
            f"{values['errors']:,} |"
        )

    problem_rows = [
        row
        for row in summary["latest"]
        if row.get("has_error")
        or row.get("status") in {"missing_answer", "action_mismatch"}
    ]
    lines.extend(["", "## 오류·기대 결과 불일치·빈 답변 상세 (최대 100건)", ""])
    if problem_rows:
        lines.extend(
            [
                "| ID | 상태 | 질문 | 오류 |",
                "|---|---|---|---|",
            ]
        )
        for row in problem_rows[:100]:
            actual = row.get("actual") if isinstance(row.get("actual"), dict) else {}
            error = row.get("error") or actual.get("query_error") or actual.get("error_message")
            lines.append(
                f"| `{row.get('id')}` | `{row.get('status')}` | "
                f"{_cell(row.get('question_ko'), 80)} | {_cell(error, 120)} |"
            )
    else:
        lines.append("현재 저장된 실행 결과에는 오류나 빈 답변이 없습니다.")

    param_rows = [row for row in summary["latest"] if row.get("status") == "requires_params"]
    lines.extend(["", "## 기본값 적용 후에도 남은 파라미터 요청 (최대 100건)", ""])
    if param_rows:
        lines.extend(["| ID | 질문 | 적용한 기본값 | 남은 값 |", "|---|---|---|---|"])
        for row in param_rows[:100]:
            actual = row.get("actual") if isinstance(row.get("actual"), dict) else {}
            lines.append(
                f"| `{row.get('id')}` | {_cell(row.get('question_ko'), 80)} | "
                f"{_cell(row.get('default_params_applied'), 120)} | "
                f"{_cell(actual.get('missing_params'), 120)} |"
            )
    else:
        lines.append("기본값 적용 후 추가 파라미터를 요청한 케이스가 없습니다.")

    lines.extend(
        [
            "",
            "## 실행 정보",
            "",
            f"- 생성 시각: `{summary['generated_at']}`",
            f"- 입력 골든셋: `{summary['dataset_path']}`",
            f"- 입력 SHA-256: `{summary['dataset_sha256']}`",
            f"- 전체 결과 JSONL: `{results_path.resolve()}`",
            "- 개인정보 노출을 줄이기 위해 DB 원본 행은 저장하지 않고 답변, SQL, 행 수, 오류 메타데이터만 저장합니다.",
            "",
        ]
    )
    return "\n".join(lines)


def write_summary(
    cases: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    cases_path: Path,
    results_path: Path,
    summary_path: Path,
) -> dict[str, Any]:
    summary = build_summary(cases, rows, cases_path)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(render_summary(summary, results_path), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--limit", type=int, default=0, help="run at most N pending cases; 0 runs all")
    parser.add_argument("--retry-errors", action="store_true", help="retry latest error/missing-answer cases")
    parser.add_argument("--restart", action="store_true", help="discard saved live results and start over")
    args = parser.parse_args()

    cases = load_jsonl(args.cases)
    _validate_case_ids(cases)
    if args.restart:
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text("", encoding="utf-8")

    interrupted = False
    try:
        run_cases(
            cases,
            args.results,
            limit=max(0, args.limit),
            retry_errors=args.retry_errors,
        )
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted; saved completed cases and rebuilding the summary.", file=sys.stderr)
    finally:
        rows = load_jsonl(args.results, repair_trailing_result=True)
        summary = write_summary(cases, rows, args.cases, args.results, args.summary)
        try:
            from text2sql_agent import close_common_clients

            close_common_clients()
        except Exception:
            pass

    print(
        f"saved={summary['records_saved']}/{summary['total_cases']} "
        f"answers={summary['answers_saved']} errors={summary['errors']} "
        f"pending={summary['pending']}"
    )
    print(f"results: {args.results.resolve()}")
    print(f"summary: {args.summary.resolve()}")
    if interrupted:
        return 130
    return 1 if any(_failed(row) for row in summary["latest"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())

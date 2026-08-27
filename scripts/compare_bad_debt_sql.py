#!/usr/bin/env python3
"""Compare the final SQL produced by two bad-debt tool implementations.

The script does not execute SQL and does not modify either project.  Each
project is loaded in an isolated Python subprocess so its own ``bad_debt.py``,
configuration, and semantic schema are used.

Example:

    python scripts/compare_bad_debt_sql.py \
      --reference-project /path/to/old/common_athena \
      --merchant 꾸석지 \
      --yyyymm 202605
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
QUERY_ORDER = ("월초충당금", "월말충당금", "상각내역", "대손비용률종합")

_SNAPSHOT_CODE = r"""
import json
import os

from text2sql_agent import config
from text2sql_agent.tools.bad_debt import BAD_DEBT_QUERIES, _build_bad_debt_sql

params = json.loads(os.environ["BAD_DEBT_COMPARE_PARAMS"])
queries = {
    name: _build_bad_debt_sql(template, params)
    for name, template in BAD_DEBT_QUERIES.items()
}
print(json.dumps(
    {
        "metadata": {
            # 옛 reference 프로젝트는 DB_BACKEND 분기가 남아 있으므로 있으면 읽는다.
            "backend": getattr(config, "DB_BACKEND", "athena"),
            "db_schema": config.DB_SCHEMA,
            "semantic_schema": str(config.SCHEMA_PATH),
        },
        "queries": queries,
    },
    ensure_ascii=False,
))
"""


def _existing_project(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    expected = path / "text2sql_agent" / "tools" / "bad_debt.py"
    if not expected.is_file():
        raise argparse.ArgumentTypeError(
            f"bad_debt.py를 찾을 수 없습니다: {expected}"
        )
    return path


def _existing_file(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"파일을 찾을 수 없습니다: {path}")
    return path


def _yyyymm(value: str) -> str:
    if not re.fullmatch(r"20\d{2}(?:0[1-9]|1[0-2])", value):
        raise argparse.ArgumentTypeError("기준년월은 YYYYMM 형식이어야 합니다.")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "현재/예전 대손비용률 도구가 동일 파라미터로 생성하는 SQL을 비교합니다. "
            "Athena 쿼리는 실행하지 않습니다."
        )
    )
    parser.add_argument(
        "--candidate-project",
        type=_existing_project,
        default=PROJECT_ROOT,
        help=f"현재 비교 대상 프로젝트 경로 (기본값: {PROJECT_ROOT})",
    )
    parser.add_argument(
        "--reference-project",
        type=_existing_project,
        required=True,
        help="예전/reference 프로젝트 경로",
    )
    parser.add_argument(
        "--candidate-schema",
        type=_existing_file,
        help="현재 프로젝트에서 강제로 사용할 semantic schema 경로",
    )
    parser.add_argument(
        "--reference-schema",
        type=_existing_file,
        help="reference 프로젝트에서 강제로 사용할 semantic schema 경로",
    )
    parser.add_argument("--merchant", required=True, help="가맹점명")
    parser.add_argument("--yyyymm", required=True, type=_yyyymm, help="기준년월(YYYYMM)")
    parser.add_argument("--ls", type=float, default=0.0, help="LS 비교 파라미터")
    parser.add_argument("--is", dest="is_value", type=float, default=0.0, help="IS 비교 파라미터")
    parser.add_argument(
        "--exact-name",
        action="store_true",
        help="현재 도구의 이름정확일치 파라미터를 true로 전달",
    )
    parser.add_argument(
        "--backend",
        default="athena",
        choices=("athena",),
        help="비교 실행 백엔드 (reference 프로젝트에 DB_BACKEND로 전달)",
    )
    parser.add_argument(
        "--db-schema",
        default="none",
        help="DB_SCHEMA 값 (Athena 기본값: none)",
    )
    parser.add_argument(
        "--fail-on-diff",
        action="store_true",
        help="차이가 있으면 종료 코드 1 반환",
    )
    return parser


def _snapshot(
    project: Path,
    *,
    semantic_schema: Path | None,
    params: dict[str, Any],
    backend: str,
    db_schema: str,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["DB_BACKEND"] = backend
    env["DB_SCHEMA"] = db_schema
    env["BAD_DEBT_COMPARE_PARAMS"] = json.dumps(params, ensure_ascii=False)
    if semantic_schema:
        env["SEMANTIC_SCHEMA_PATH"] = str(semantic_schema)

    completed = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT_CODE],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        details = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{project} 비교 SQL 생성 실패:\n{details}")

    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not output_lines:
        raise RuntimeError(f"{project} 비교 SQL 생성 결과가 비어 있습니다.")
    try:
        return json.loads(output_lines[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{project} 비교 SQL 결과를 JSON으로 읽지 못했습니다:\n"
            f"{completed.stdout.strip()}"
        ) from exc


def _print_metadata(label: str, project: Path, snapshot: dict[str, Any]) -> None:
    metadata = snapshot.get("metadata", {})
    print(f"{label} project : {project}")
    print(f"{label} backend : {metadata.get('backend', '')}")
    print(f"{label} DB schema: {metadata.get('db_schema', '') or '(none)'}")
    print(f"{label} semantic : {metadata.get('semantic_schema', '')}")


def _query_diff(name: str, reference_sql: str, candidate_sql: str) -> list[str]:
    return list(
        difflib.unified_diff(
            reference_sql.splitlines(),
            candidate_sql.splitlines(),
            fromfile=f"reference/{name}.sql",
            tofile=f"candidate/{name}.sql",
            lineterm="",
        )
    )


def main() -> int:
    args = _build_parser().parse_args()
    params: dict[str, Any] = {
        "가맹점명": args.merchant,
        "기준년월": args.yyyymm,
        "LS": args.ls,
        "IS": args.is_value,
    }
    if args.exact_name:
        params["이름정확일치"] = True

    candidate = _snapshot(
        args.candidate_project,
        semantic_schema=args.candidate_schema,
        params=params,
        backend=args.backend,
        db_schema=args.db_schema,
    )
    reference = _snapshot(
        args.reference_project,
        semantic_schema=args.reference_schema,
        params=params,
        backend=args.backend,
        db_schema=args.db_schema,
    )

    print(f"비교 파라미터: {json.dumps(params, ensure_ascii=False)}")
    _print_metadata("candidate", args.candidate_project, candidate)
    _print_metadata("reference", args.reference_project, reference)

    candidate_queries = candidate.get("queries", {})
    reference_queries = reference.get("queries", {})
    different: list[str] = []
    for name in QUERY_ORDER:
        candidate_sql = str(candidate_queries.get(name, ""))
        reference_sql = str(reference_queries.get(name, ""))
        diff_lines = _query_diff(name, reference_sql, candidate_sql)
        if not diff_lines:
            print(f"\n[SAME] {name}")
            continue
        different.append(name)
        print(f"\n[DIFF] {name}")
        print("\n".join(diff_lines))

    print(
        f"\n요약: 전체 {len(QUERY_ORDER)}개 중 "
        f"{len(different)}개 차이"
        + (f" ({', '.join(different)})" if different else "")
    )
    if args.fail_on_diff and different:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

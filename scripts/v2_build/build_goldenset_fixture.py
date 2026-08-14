"""goldenset-v2-sql-errors.csv → tests/fixtures/goldenset_v2_sql_errors.json.

원본 CSV(700KB)는 저장소에 넣지 않고, 회귀 테스트가 실제로 쓰는 부분만 추출한다.

  column_gaps       expected_sql 이 쓴 (테이블, 컬럼)과 그 질문. v1 스키마·v1 매처로는
                    질문에서 컬럼을 못 찾던 조합이다. 동의어 개선의 회귀 기준.
  dialect_failures  syntax / EXPRESSION_NOT_* 실패의 agent SQL 원문. 방언 가드 기준.
  prose_failures    SQL 대신 자연어로 되물은 응답 표본. 프로즈 가드 기준.

usage:
    python corporate_sales_fable_v2/scripts/build_goldenset_fixture.py \
        --csv ~/Downloads/goldenset_error/goldenset-v2-sql-errors.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # v2 적용본 루트
V1_ROOT = PROJECT_ROOT.parent / "corporate_sales_fable"   # 변환 원본(v1 저장소)
sys.path.insert(0, str(PROJECT_ROOT))

_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_]+")
_ALIAS_RE = re.compile(r"\b(?:FROM|JOIN)\s+([a-z][a-z0-9_]+)\s+(?:AS\s+)?([a-z][a-z0-9_]*)?", re.IGNORECASE)
_REF_RE = re.compile(r'\b([a-z][a-z0-9_]*)\."([^"]+)"')
_NON_ALIAS = {"where", "group", "order", "on", "left", "inner", "join", "select", "using", "cross"}
_PROSE_SAMPLE_LIMIT = 24
_PROSE_TEXT_LIMIT = 600


def v1_phrase_in_text(question: str, phrase: object) -> bool:
    """v1 text2sql_agent/schema.py::_phrase_in_text 와 동일한 매칭 규칙."""
    chunks = _TOKEN_RE.findall(str(phrase or ""))
    if not chunks:
        return False
    body = r"[^0-9A-Za-z가-힣_]*".join(re.escape(chunk) for chunk in chunks)
    particles = r"(?:으로|에서|에게|의|은|는|이|가|을|를|에|로|와|과|도|만|별|인)?"
    return bool(
        re.search(
            rf"(?<![0-9A-Za-z가-힣_]){body}(?={particles}(?:[^0-9A-Za-z가-힣_]|$))",
            question or "",
            flags=re.IGNORECASE,
        )
    )


def load_v1_columns(schema_path: Path) -> dict[str, dict[str, dict]]:
    schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
    tables: dict[str, dict[str, dict]] = {}
    for table in schema.get("tables", []):
        columns: dict[str, dict] = {}
        for section in ("dimensions", "measures", "time_dimensions"):
            for column in table.get(section) or []:
                columns[str(column.get("name"))] = dict(column)
        primary_keys = {str(key) for key in table.get("primary_key") or []}
        for name, column in columns.items():
            column["_is_primary_key"] = name in primary_keys
        tables[str(table.get("name"))] = columns
    return tables


def alias_map(sql: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for table, alias in _ALIAS_RE.findall(sql):
        physical = table.lower()
        if alias and alias.lower() not in _NON_ALIAS:
            mapping[alias.lower()] = physical
        mapping.setdefault(physical, physical)
    return mapping


def collect_column_gaps(rows: list[dict], tables: dict[str, dict[str, dict]]) -> list[dict]:
    """질문이 컬럼을 지목하는데도 v1 매칭이 실패하는 조합."""
    gaps: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        sql = row["expected_sql"]
        question = row["question"]
        mapping = alias_map(sql)
        for alias, column_name in _REF_RE.findall(sql):
            table = mapping.get(alias.lower())
            if not table or table not in tables or column_name not in tables[table]:
                continue
            column = tables[table][column_name]
            # primary key 는 프롬프트 선택 점수가 이미 최상위라 잘리지 않는다.
            if column.get("_is_primary_key"):
                continue
            terms = [column_name, *(column.get("synonyms") or [])]
            if any(v1_phrase_in_text(question, term) for term in terms):
                continue
            # 질문이 그 컬럼을 실제로 지목하는지 확인한다(어간 3글자 공유).
            stem = re.sub(r"(구분코드|코드|여부|구분|금액|건수|번호|명)$", "", column_name)
            if len(stem) < 3 or stem[:3] not in re.sub(r"\s", "", question):
                continue
            key = (table, column_name, question)
            if key in seen:
                continue
            seen.add(key)
            gaps.append(
                {
                    "id": row["id"],
                    "table": table,
                    "column": column_name,
                    "question": question,
                    "v1_synonyms": list(column.get("synonyms") or []),
                }
            )
    return gaps


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path)
    parser.add_argument("--schema", type=Path, default=V1_ROOT / "semantic_layer.yaml")
    parser.add_argument(
        "--output", type=Path, default=PROJECT_ROOT / "tests" / "fixtures" / "goldenset_v2_sql_errors.json"
    )
    args = parser.parse_args()

    with args.csv.expanduser().open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))

    tables = load_v1_columns(args.schema)
    gaps = collect_column_gaps(rows, tables)
    current_tables = load_v1_columns(PROJECT_ROOT / "semantic_layer.yaml")
    gaps = [
        gap
        for gap in gaps
        if gap["column"] in current_tables.get(gap["table"], {})
    ]

    dialect = [
        {
            "id": row["id"],
            "error_kind": row["error_kind"],
            "question": row["question"],
            "error_first_line": row["error_first_line"][:200],
            "agent_sql": row["agent_sql"],
        }
        for row in rows
        if row["error_kind"] in {"syntax", "group_by"} or "EXPRESSION_NOT" in row["error_full"]
    ]

    prose = [
        {"id": row["id"], "question": row["question"], "agent_response": row["agent_sql"][:_PROSE_TEXT_LIMIT]}
        for row in rows
        if row["error_kind"] == "guard"
    ][:_PROSE_SAMPLE_LIMIT]

    document = {
        "metadata": {
            "source": "goldenset-v2-sql-errors.csv",
            "total_error_rows": len(rows),
            "error_kind_counts": dict(Counter(row["error_kind"] for row in rows)),
            "baseline": "v1 semantic_layer.yaml + v1 _phrase_in_text",
            "notes": [
                "column_gaps 는 v1 기준으로 질문↔컬럼 매칭이 실패한 조합이다. 회귀 테스트의 기준선.",
                "현재 semantic layer에서 제거된 컬럼은 column_gaps에서도 제외한다.",
                "dialect_failures 의 agent_sql 은 Athena가 거절한 원문이므로 수정하지 않는다.",
            ],
        },
        "column_gaps": gaps,
        "dialect_failures": dialect,
        "prose_failures": prose,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"fixture -> {args.output}")
    print(f"  error rows      : {len(rows)} {document['metadata']['error_kind_counts']}")
    print(f"  column gaps     : {len(gaps)} (distinct columns: {len({(g['table'], g['column']) for g in gaps})})")
    print(f"  dialect failures: {len(dialect)}")
    print(f"  prose samples   : {len(prose)}")
    print(f"  size            : {args.output.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()

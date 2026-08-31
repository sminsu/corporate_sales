#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""골든셋 정답 SQL을 시맨틱 레이어 기준으로 정적 감사한다.

Athena에 던지기 전에, 실행하면 반드시 실패할 SQL을 찾아낸다. 실행 비용이 0이므로
`semantic_layer.yaml` 이 바뀔 때마다 돌려서 골든셋이 여전히 유효한지 확인한다.

    python scripts/audit_goldenset_sql.py
    python scripts/audit_goldenset_sql.py --cases tests/fixtures/other.jsonl --json reports/audit.json

검사 항목

| 코드 | 뜻 | 실제 Athena 오류 |
|---|---|---|
| `parse` | Trino 방언으로 파싱 실패 | SYNTAX_ERROR |
| `dup_alias` | 같은 스코프에서 한 별칭이 서로 다른 관계에 바인딩 | 별칭 중복/모호 |
| `wrong_table` | `a."컬럼"` 의 컬럼이 별칭 a 의 테이블에 없음 | COLUMN_NOT_FOUND |
| `unknown_column` | 어느 테이블에도 없고 쿼리 안에서 정의되지도 않은 이름 | COLUMN_NOT_FOUND |
| `ambiguous` | 관계 2개 이상 스코프에서 한정자 없는 공통 컬럼 | AMBIGUOUS_NAME |
| `not_aggregated` | GROUP BY 쿼리인데 집계 밖 컬럼이 그룹키에 없음 | EXPRESSION_NOT_AGGREGATE |
| `non_numeric_agg` | VARCHAR 컬럼에 SUM/AVG | 타입 불일치 |
| `removed_column` | 시맨틱 레이어에서 삭제된 컬럼 참조 | COLUMN_NOT_FOUND |
| `restricted_table` | 개인정보 테이블(`tbdaaat18`) 참조 | 정책 위반 |
| `table_mismatch` | `expected_tables` 메타가 SQL 실제 참조와 다름 | (메타 오류) |

종료 코드는 문제가 하나라도 있으면 1이다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml
import sqlglot
from sqlglot import exp

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES = ROOT / "tests" / "fixtures" / "corporate_sales_text2sql_goldenset_v2.jsonl"
DEFAULT_SEMANTIC = ROOT / "semantic_layer.yaml"

RESTRICTED_TABLES = {"tbdaaat18"}
NUMERIC_TYPES = {"DECIMAL", "BIGINT", "INTEGER", "DOUBLE", "REAL", "SMALLINT", "TINYINT"}
TABLE_REF_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def load_semantic(path: Path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    columns, types = {}, {}
    for table in doc["tables"]:
        names = set()
        for kind in ("dimensions", "measures", "time_dimensions"):
            for col in table.get(kind) or []:
                names.add(col["name"])
                types[(table["name"], col["name"])] = (col.get("data_type") or "").upper()
        columns[table["name"]] = names
    version = (doc.get("semantic_layer_metadata") or {}).get("version", "")
    return columns, types, version


def value_columns(node):
    """집계 대상 '값' 위치의 컬럼만 모은다. CASE 조건절과 중첩 집계는 값이 아니다."""
    if node is None:
        return []
    if isinstance(node, exp.Column):
        return [node]
    if isinstance(node, exp.AggFunc):
        return []
    if isinstance(node, exp.Case):
        out = []
        for when in node.args.get("ifs") or []:
            out += value_columns(when.args.get("true"))
        return out + value_columns(node.args.get("default"))
    if isinstance(node, exp.If):
        return value_columns(node.args.get("true")) + value_columns(node.args.get("false"))
    out = []
    for arg in node.args.values():
        if isinstance(arg, list):
            for item in arg:
                if isinstance(item, exp.Expression):
                    out += value_columns(item)
        elif isinstance(arg, exp.Expression):
            out += value_columns(arg)
    return out


def scope_bindings(scope, tables):
    """이 스코프에 직접 매달린 물리 테이블의 별칭 → 테이블 이름."""
    bind = {}
    for src in scope.find_all(exp.Table):
        if src.parent_select is not scope:
            continue
        if src.name.lower() in tables:
            bind[(src.alias or src.name).lower()] = src.name.lower()
    return bind


def audit_sql(sql: str, columns: dict, types: dict) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    tables = set(columns)
    try:
        tree = sqlglot.parse_one(sql, read="trino")
    except Exception as exc:  # noqa: BLE001
        return [("parse", str(exc)[:200])]

    declared = {a.alias_or_name for a in tree.find_all(exp.Alias)}

    for scope in tree.find_all(exp.Select):
        # 별칭 중복 바인딩
        seen: dict[str, str] = {}
        for src in scope.find_all(exp.Table):
            if src.parent_select is not scope:
                continue
            alias, name = (src.alias or src.name).lower(), src.name.lower()
            if alias in seen and seen[alias] != name:
                issues.append(("dup_alias", "%s → %s / %s" % (alias, seen[alias], name)))
            seen[alias] = name

        bind = scope_bindings(scope, tables)
        if not bind:
            continue
        union = set()
        for table in bind.values():
            union |= columns[table]

        for col in scope.find_all(exp.Column):
            if col.parent_select is not scope or isinstance(col.this, exp.Star):
                continue
            owner = (col.table or "").lower()
            if owner in bind:
                if col.name not in columns[bind[owner]]:
                    issues.append(("wrong_table",
                                   "%s.%s → %s 에 없음" % (owner, col.name, bind[owner])))
            elif not owner:
                if col.name not in union and col.name not in declared:
                    issues.append(("unknown_column", col.name))
                elif len(set(bind.values())) > 1:
                    owners = [t for t in set(bind.values()) if col.name in columns[t]]
                    if len(owners) > 1:
                        issues.append(("ambiguous",
                                       "%s (%s)" % (col.name, ", ".join(sorted(owners)))))

        # SUM/AVG 대상 타입
        for fn in scope.find_all(exp.Sum, exp.Avg):
            for col in value_columns(fn.this):
                if isinstance(col.this, exp.Star):
                    continue
                owner = (col.table or "").lower()
                if owner in bind:
                    dtype = types.get((bind[owner], col.name), "")
                    if dtype and dtype not in NUMERIC_TYPES:
                        issues.append(("non_numeric_agg",
                                       "%s.%s (%s)" % (owner, col.name, dtype)))

    # GROUP BY 집계 정합성
    for scope in tree.find_all(exp.Select):
        group = scope.args.get("group")
        if not group:
            continue
        keys = {e.sql(dialect="trino") for e in group.expressions}
        for proj in scope.expressions:
            target = proj.this if isinstance(proj, exp.Alias) else proj
            if target.sql(dialect="trino") in keys:
                continue
            for col in target.find_all(exp.Column):
                if col.sql(dialect="trino") in keys:
                    continue
                node, ok = col, False
                while node is not None and node is not target.parent:
                    if isinstance(node, (exp.AggFunc, exp.Window)):
                        ok = True
                        break
                    if node.sql(dialect="trino") in keys:
                        ok = True
                        break
                    node = node.parent
                if not ok:
                    issues.append(("not_aggregated", col.sql(dialect="trino")))
    return issues


def audit_case(case: dict, columns: dict, types: dict) -> list[tuple[str, str]]:
    sql = case["sql"]
    issues = audit_sql(sql, columns, types)

    used = {t.lower() for t in TABLE_REF_RE.findall(sql) if t.lower() in columns}
    for table in sorted(used & RESTRICTED_TABLES):
        issues.append(("restricted_table", table))

    declared_tables = {str(t).split(".")[-1].lower() for t in case.get("expected_tables") or []}
    if declared_tables and declared_tables != used:
        issues.append(("table_mismatch",
                       "메타 %s / 실제 %s" % (sorted(declared_tables), sorted(used))))

    # 시맨틱 레이어에서 사라진 컬럼을 아직 쓰고 있는지
    # (쿼리 안에서 AS 로 정의한 파생 이름은 컬럼이 아니므로 제외한다)
    declared = set(re.findall(r'AS\s+"([^"]+)"', sql, re.IGNORECASE))
    for name in set(re.findall(r'"([^"]+)"', sql)):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name) or name in declared:
            continue
        if any(name in columns[t] for t in used):
            continue
        if any(name in cols for cols in columns.values()):
            issues.append(("removed_column", "%s (선택 테이블에 없음)" % name))
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--semantic", type=Path, default=DEFAULT_SEMANTIC)
    parser.add_argument("--json", type=Path, default=None, help="문제 목록을 JSON으로 저장")
    parser.add_argument("--show", type=int, default=30, help="화면에 출력할 케이스 수")
    args = parser.parse_args()

    columns, types, version = load_semantic(args.semantic)
    cases = [json.loads(line) for line in args.cases.read_text(encoding="utf-8").splitlines() if line.strip()]

    problems: dict[str, list] = {}
    for case in cases:
        found = sorted(set(audit_case(case, columns, types)))
        if found:
            problems[case["id"]] = found

    kinds = Counter(kind for found in problems.values() for kind, _ in found)
    print("시맨틱 레이어 %s / 케이스 %d건" % (version or args.semantic.name, len(cases)))
    print("문제 케이스 %d건" % len(problems))
    for kind, count in kinds.most_common():
        print("  - %s: %d" % (kind, count))
    for cid, found in list(problems.items())[: args.show]:
        print("  %s" % cid)
        for kind, detail in found[:5]:
            print("      %-16s %s" % (kind, detail))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"semantic_version": version, "problems": problems},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        print("저장: %s" % args.json)

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

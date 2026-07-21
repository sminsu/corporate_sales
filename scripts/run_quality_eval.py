#!/usr/bin/env python3
"""Offline quality contracts for the NL2SQL and multi-turn pipeline.

The runner deliberately avoids both the configured LLM endpoint and the real
database.  It exercises deterministic parsing, follow-up routing, continuation
parameter extraction, SQL safety, and retry routing against a small set of
adversarial cases that are common with compact models.

Usage::

    python scripts/run_quality_eval.py
    python scripts/run_quality_eval.py --json
    python scripts/run_quality_eval.py --category sql_safety

The process exits with status 1 when any contract fails, which makes the same
runner suitable for a deployment smoke check or CI job.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CASES_PATH = ROOT / "tests" / "fixtures" / "nl2sql_quality_cases.json"


@dataclass(frozen=True)
class EvalResult:
    category: str
    name: str
    passed: bool
    expected: Any
    actual: Any
    detail: str = ""


def _load_cases(path: Path) -> dict[str, list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError(f"quality case file must contain an object: {path}")
    return data


def _previous_ym(now: datetime | None = None) -> str:
    current = now or datetime.now()
    if current.month == 1:
        return f"{current.year - 1}12"
    return f"{current.year}{current.month - 1:02d}"


def _resolve_dynamic_tokens(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_dynamic_tokens(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_dynamic_tokens(item) for item in value]
    if value == "$CURRENT_YM":
        now = datetime.now()
        return f"{now.year}{now.month:02d}"
    if value == "$PREVIOUS_YM":
        return _previous_ym()
    return value


def _actual_from_join_tables(sql: str) -> set[str]:
    """Extract physical FROM/JOIN tables, excluding CTE names.

    This parser is intentionally independent from the runtime verified-query
    loader so the eval can catch a regression in that loader.  It supports the
    simple/qualified quoted identifiers used by the repository's SQL templates.
    """

    def normalize(identifier: str) -> str:
        return identifier.replace('"', "").replace(" ", "").rsplit(".", 1)[-1].lower()

    cte_names = {
        normalize(match.group("name"))
        for match in re.finditer(
            r'(?:\bWITH|,)\s+(?:RECURSIVE\s+)?(?P<name>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
            r"\s*(?:\([^)]*\))?\s+AS\s*\(",
            sql or "",
            flags=re.IGNORECASE,
        )
    }
    references = set()
    for match in re.finditer(
        r'\b(?:FROM|JOIN)\s+(?P<name>(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
        r'(?:\s*\.\s*(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*))?)',
        sql or "",
        flags=re.IGNORECASE,
    ):
        table_name = normalize(match.group("name"))
        if table_name not in cte_names and table_name not in {"select", "unnest", "values"}:
            references.add(table_name)
    return references


def _result(
    category: str,
    case: dict[str, Any],
    *,
    expected: Any,
    actual: Any,
    detail: str = "",
) -> EvalResult:
    return EvalResult(
        category=category,
        name=str(case.get("name") or "unnamed"),
        passed=actual == expected,
        expected=expected,
        actual=actual,
        detail=detail,
    )


def run_contract_evaluations(
    *,
    cases_path: Path = DEFAULT_CASES_PATH,
    categories: Iterable[str] | None = None,
) -> list[EvalResult]:
    """Run all requested deterministic contracts and return structured results."""

    from text2sql_agent import db, schema as schema_module, workflow
    import web_service

    cases = _load_cases(cases_path)
    selected = set(categories) if categories else {*cases.keys(), "retry_routing", "semantic_layer"}
    results: list[EvalResult] = []

    if "json_parsing" in selected:
        for case in cases.get("json_parsing", []):
            expected = case["expected"]
            try:
                actual = workflow._parse_llm_json(case["input"])
                results.append(_result("json_parsing", case, expected=expected, actual=actual))
            except Exception as exc:  # noqa: BLE001 - an eval must report, not abort.
                results.append(
                    _result(
                        "json_parsing",
                        case,
                        expected=expected,
                        actual=f"{type(exc).__name__}: {exc}",
                        detail="parser raised",
                    )
                )

    if "sql_extraction" in selected:
        for case in cases.get("sql_extraction", []):
            expected = case["expected"]
            try:
                # This is the extractor used by SQL generation.  It handles
                # fences as well as compact-model preambles/reasoning wrappers.
                actual = workflow._extract_sql_from_llm(case["input"])
                results.append(_result("sql_extraction", case, expected=expected, actual=actual))
            except Exception as exc:  # noqa: BLE001
                results.append(
                    _result(
                        "sql_extraction",
                        case,
                        expected=expected,
                        actual=f"{type(exc).__name__}: {exc}",
                        detail="extractor raised",
                    )
                )

    if "followup_intent" in selected:
        for case in cases.get("followup_intent", []):
            expected = case["expected"]
            actual = web_service._classify_followup_intent(case["input"])
            results.append(_result("followup_intent", case, expected=expected, actual=actual))

    if "continuation_params" in selected:
        for case in cases.get("continuation_params", []):
            expected = _resolve_dynamic_tokens(case["expected"])
            actual = web_service._natural_params_by_rule(case["input"], case["missing_params"])
            results.append(_result("continuation_params", case, expected=expected, actual=actual))

    if "sql_safety" in selected:
        for case in cases.get("sql_safety", []):
            executed = False

            def fake_execute(_: str) -> tuple[list[str], list[tuple[int]]]:
                nonlocal executed
                executed = True
                return ["value"], [(1,)]

            with (
                patch.object(db, "DB_BACKEND", "postgres"),
                patch.object(db, "_execute_postgres", fake_execute),
                patch.object(db, "_log_db_query"),
            ):
                _, _, error = db.execute_sql(case["sql"])

            allowed = bool(case["allowed"])
            actual = {
                "allowed": executed and error is None,
                "execution_reached": executed,
                "blocked_with_error": bool(error),
            }
            expected = {
                "allowed": allowed,
                "execution_reached": allowed,
                "blocked_with_error": not allowed,
            }
            results.append(_result("sql_safety", case, expected=expected, actual=actual, detail=error or ""))

    if "retry_routing" in selected:
        retry_cases = [
            ("validation_first_failure", workflow.after_validate, {"is_valid": False, "retry_count": 1}, "generate_sql"),
            ("validation_last_retry", workflow.after_validate, {"is_valid": False, "retry_count": 2}, "generate_sql"),
            ("validation_exhausted", workflow.after_validate, {"is_valid": False, "retry_count": 3}, "handle_error"),
            ("validation_success", workflow.after_validate, {"is_valid": True, "retry_count": 0}, "run_query"),
            ("query_first_failure", workflow.after_query, {"query_error": "boom", "retry_count": 1}, "generate_sql"),
            ("query_exhausted", workflow.after_query, {"query_error": "boom", "retry_count": 3}, "handle_error"),
            ("query_success", workflow.after_query, {"query_error": "", "retry_count": 0}, "generate_answer"),
        ]
        for name, function, state, expected in retry_cases:
            actual = function(state)
            results.append(
                EvalResult(
                    category="retry_routing",
                    name=name,
                    passed=actual == expected,
                    expected=expected,
                    actual=actual,
                )
            )

    if "semantic_layer" in selected:
        semantic = schema_module.SCHEMA
        required_top_level = {
            "canonical_domains",
            "semantic_entities",
            "canonical_metrics",
            "semantic_join_graph",
            "llm_semantic_contract",
            "time_resolution_rules",
        }
        required_contract = {"purpose", "sql_output_rules", "evidence_priority", "ambiguity_policy"}
        missing = sorted(required_top_level - semantic.keys())
        contract = semantic.get("llm_semantic_contract") or {}
        missing.extend(f"llm_semantic_contract.{key}" for key in sorted(required_contract - contract.keys()))
        results.append(
            EvalResult(
                category="semantic_layer",
                name="required_contract_keys",
                passed=not missing,
                expected=[],
                actual=missing,
            )
        )

        domain_cases = [
            ("merchant_domain", "지난달 가맹점 업종별 매출 순위", "merchant_sales"),
            ("card_usage_domain", "법인카드의 월별 국내 이용금액과 거래건수", "card_usage"),
            ("credit_risk_domain", "기업별 연체율과 한도 사용률", "credit_risk"),
        ]
        for name, question, expected in domain_cases:
            ranked = schema_module._weighted_domain_scores(
                schema_module._keyword_rule_domain_scores(semantic, question),
                schema_module._metric_entity_domain_scores(semantic, question),
                {},
            )
            actual = ranked[0]["domain"] if ranked else ""
            results.append(
                EvalResult(
                    category="semantic_layer",
                    name=name,
                    passed=actual == expected,
                    expected=expected,
                    actual=actual,
                    detail=f"question={question}",
                )
            )

        tables = {
            str(table.get("physical_table") or table.get("name") or "").rsplit(".", 1)[-1]: table
            for table in semantic.get("tables", [])
        }
        entities = {str(entity.get("name") or ""): entity for entity in semantic.get("semantic_entities", [])}
        path_issues: list[str] = []
        paths = (semantic.get("semantic_join_graph") or {}).get("safe_paths") or []
        for path in paths:
            path_name = str(path.get("name") or "(unnamed)")
            endpoint_tables: set[str] = set()
            for endpoint in ("from_entity", "to_entity"):
                entity_name = str(path.get(endpoint) or "")
                entity = entities.get(entity_name)
                if entity is None:
                    path_issues.append(f"{path_name}: unknown {endpoint}={entity_name}")
                    continue
                table_name = str(entity.get("physical_table") or "").rsplit(".", 1)[-1]
                endpoint_tables.add(table_name)
                if table_name not in tables:
                    path_issues.append(f"{path_name}: entity {entity_name} uses unknown table {table_name}")

            sql = str(path.get("sql") or "")
            referenced_tables = set(re.findall(r"\b([a-z][a-z0-9_]*)\.\s*\"", sql, flags=re.IGNORECASE))
            for table_name in sorted(referenced_tables - endpoint_tables):
                path_issues.append(f"{path_name}: join SQL references non-endpoint table {table_name}")
            for table_name, column_name in re.findall(r"\b([a-z][a-z0-9_]*)\.\s*\"([^\"]+)\"", sql, flags=re.IGNORECASE):
                table = tables.get(table_name)
                if table is None:
                    path_issues.append(f"{path_name}: join SQL uses unknown table {table_name}")
                    continue
                known_columns = {
                    str(column.get("name") or "")
                    for section in ("dimensions", "measures", "time_dimensions")
                    for column in table.get(section, [])
                }
                if column_name not in known_columns:
                    path_issues.append(f"{path_name}: unknown column {table_name}.{column_name}")
        if not paths:
            path_issues.append("semantic_join_graph.safe_paths is empty")
        results.append(
            EvalResult(
                category="semantic_layer",
                name="safe_join_path_references",
                passed=not path_issues,
                expected=[],
                actual=path_issues,
                detail=f"checked_paths={len(paths)}",
            )
        )

        known_tables = {
            str(table.get("physical_table") or table.get("name") or "").rsplit(".", 1)[-1].lower()
            for table in semantic.get("tables", [])
        }
        verified_query_issues = []
        for verified_query in schema_module.VERIFIED_QUERIES:
            used_tables = _actual_from_join_tables(str(verified_query.get("sql") or ""))
            unknown_tables = sorted(used_tables - known_tables)
            if unknown_tables:
                verified_query_issues.append(
                    f"{verified_query.get('name', '(unnamed)')}: {', '.join(unknown_tables)}"
                )
        results.append(
            EvalResult(
                category="semantic_layer",
                name="active_verified_query_table_subset",
                passed=not verified_query_issues and bool(schema_module.VERIFIED_QUERIES),
                expected=[],
                actual=verified_query_issues,
                detail=f"active_queries={len(schema_module.VERIFIED_QUERIES)}, known_tables={len(known_tables)}",
            )
        )

    return results


def _print_text(results: list[EvalResult]) -> None:
    for result in results:
        marker = "PASS" if result.passed else "FAIL"
        print(f"[{marker}] {result.category}/{result.name}")
        if not result.passed:
            print(f"       expected: {result.expected!r}")
            print(f"       actual:   {result.actual!r}")
            if result.detail:
                print(f"       detail:   {result.detail}")
    passed = sum(result.passed for result in results)
    print(f"\nquality eval: {passed}/{len(results)} passed, {len(results) - passed} failed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH, help="JSON case file")
    parser.add_argument(
        "--category",
        action="append",
        choices=[
            "json_parsing",
            "sql_extraction",
            "followup_intent",
            "continuation_params",
            "sql_safety",
            "retry_routing",
            "semantic_layer",
        ],
        help="run only this category (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    results = run_contract_evaluations(cases_path=args.cases, categories=args.category)
    if args.json:
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        _print_text(results)
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

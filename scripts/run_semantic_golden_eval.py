#!/usr/bin/env python3
"""Run the semantic-layer golden set without an LLM or database.

The evaluator has two deliberately separate outputs:

1. Gating contract checks validate every golden answer against the current
   semantic SSOT and verified-query registry.
2. Non-gating runtime observations measure the current deterministic domain,
   contract, verified-query, and table selectors.

Both JSON and Markdown reports are written before a failing exit code is
returned, so CI keeps the diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.generate_semantic_golden_set import (  # noqa: E402
    DEFAULT_OUTPUT as DEFAULT_CASES_PATH,
    _has_parameter_evidence,
)
from scripts.run_quality_eval import _actual_from_join_tables  # noqa: E402
from text2sql_agent.schema import (  # noqa: E402
    SCHEMA,
    VERIFIED_QUERIES,
    _needs_domain_adjudication,
    _validate_sql_against_schema,
    semantic_query_contract_candidates,
)
from text2sql_agent.workflow import (  # noqa: E402
    _keyword_rule_domain_scores,
    _match_vq_by_semantic_contract,
    _match_vq_by_rules,
    _metric_entity_domain_scores,
    _reference_domain_by_rule,
    _rule_rank_tables,
    _weighted_domain_scores,
)
from text2sql_agent.tools.sql_builders import VQ_PARAM_SPECS  # noqa: E402


DEFAULT_JSON_OUT = ROOT / "reports" / "semantic-golden-eval.json"
DEFAULT_MARKDOWN_OUT = ROOT / "reports" / "semantic-golden-eval.md"
REPORT_SCHEMA_VERSION = "1.0"
CHECK_NAMES = (
    "semantic_references",
    "action_contract",
    "required_parameters",
    "sql_answer",
    "semantic_contract_route",
)


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"golden case must be an object at {path}:{line_number}")
            cases.append(value)
    return cases


def _normalized_question(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[^0-9a-z가-힣]+", "", value)


def _failure(
    code: str,
    *,
    expected: Any = None,
    actual: Any = None,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "code": code,
        "expected": expected,
        "actual": actual,
        "detail": detail,
    }


def _check(name: str, failures: list[dict[str, Any]] | None) -> dict[str, Any]:
    if failures is None:
        return {"name": name, "status": "not_applicable", "failures": []}
    return {
        "name": name,
        "status": "fail" if failures else "pass",
        "failures": failures,
    }


def _catalog() -> dict[str, Any]:
    domains = {str(item["name"]) for item in SCHEMA.get("canonical_domains", [])}
    contracts = {
        str(item["name"]): item for item in SCHEMA.get("semantic_query_contracts", [])
    }
    metrics = {str(item["name"]): item for item in SCHEMA.get("canonical_metrics", [])}
    attributes = {
        str(item["name"]): item for item in SCHEMA.get("semantic_attributes", [])
    }
    paths = {
        str(item["name"]): item
        for item in SCHEMA.get("semantic_join_graph", {}).get("safe_paths", [])
    }
    tables = {
        str(item.get("physical_table") or item["name"]).rsplit(".", 1)[-1]
        for item in SCHEMA.get("tables", [])
    }
    queries = {str(item["name"]): item for item in VERIFIED_QUERIES}
    query_facts: dict[str, dict[str, Any]] = {}
    for name, query in queries.items():
        sql = str(query.get("sql") or "").strip()
        physical_tables = _actual_from_join_tables(sql)
        query_facts[name] = {
            "runtime_mode": str(query.get("runtime_mode") or "active"),
            "sha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            "tables": sorted(physical_tables),
            "schema_errors": _validate_sql_against_schema(sql, sorted(physical_tables)),
        }
    return {
        "domains": domains,
        "contracts": contracts,
        "metrics": metrics,
        "attributes": attributes,
        "paths": paths,
        "tables": tables,
        "queries": queries,
        "query_facts": query_facts,
    }


def _semantic_reference_check(case: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    source = case.get("source") if isinstance(case.get("source"), dict) else {}
    domains = [] if case.get("expected_action") == "unsupported" else [case.get("domain")]
    comparisons = (
        ("unknown_domain", domains, catalog["domains"]),
        ("unknown_table", case.get("expected_tables") or [], catalog["tables"]),
        ("unknown_metric", source.get("metrics") or [], catalog["metrics"]),
        ("unknown_attribute", source.get("attributes") or [], catalog["attributes"]),
        ("unknown_join_path", source.get("join_paths") or [], catalog["paths"]),
    )
    for code, values, known in comparisons:
        unknown = sorted({str(value) for value in values if str(value) not in known})
        if unknown:
            failures.append(
                _failure(code, expected="known semantic reference", actual=unknown)
            )
    return _check("semantic_references", failures)


def _action_contract_check(case: dict[str, Any]) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    action = str(case.get("expected_action") or "")
    if action not in {"sql", "clarify", "unsupported"}:
        failures.append(
            _failure(
                "unknown_action",
                expected=["sql", "clarify", "unsupported"],
                actual=action,
            )
        )
    elif action == "sql":
        if not isinstance(case.get("expected_sql"), dict):
            failures.append(
                _failure("missing_sql_answer", expected="object", actual=case.get("expected_sql"))
            )
    else:
        if case.get("expected_sql") is not None:
            failures.append(
                _failure("unexpected_sql_answer", expected=None, actual=case.get("expected_sql"))
            )
        if action == "clarify" and not case.get("expected_missing_parameters"):
            failures.append(
                _failure(
                    "missing_clarification_parameters",
                    expected="one or more missing parameters",
                    actual=case.get("expected_missing_parameters"),
                )
            )
        if action == "unsupported" and case.get("domain") not in (None, ""):
            failures.append(
                _failure(
                    "unexpected_unsupported_domain",
                    expected=None,
                    actual=case.get("domain"),
                )
            )
        if not case.get("reason_code"):
            failures.append(
                _failure("missing_reason_code", expected="non-empty reason code", actual=None)
            )
        if not case.get("evidence_path"):
            failures.append(
                _failure("missing_evidence_path", expected="non-empty evidence path", actual=None)
            )
    return _check("action_contract", failures)


def _required_parameter_check(
    case: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    if case.get("expected_action") != "sql":
        return _check("required_parameters", None)
    failures: list[dict[str, Any]] = []
    parameters = case.get("parameters") if isinstance(case.get("parameters"), dict) else {}
    source = case.get("source") if isinstance(case.get("source"), dict) else {}
    expected_required: set[str] = set()
    query: dict[str, Any] | None = None
    if source.get("kind") == "semantic_contract":
        contract = catalog["contracts"].get(str(source.get("id") or "")) or {}
        query_name = str(contract.get("verified_query") or contract.get("reference_query") or "")
        query = catalog["queries"].get(query_name)
        bindings = contract.get("entity_binding") or []
        bindings = bindings if isinstance(bindings, list) else [bindings]
        expected_required.update(
            str(binding["parameter"])
            for binding in bindings
            if isinstance(binding, dict)
            and binding.get("required")
            and binding.get("parameter")
        )
        time_policy = contract.get("time_policy") or {}
        if isinstance(time_policy, dict):
            for key in ("parameter", "required_parameter"):
                if time_policy.get(key):
                    expected_required.add(str(time_policy[key]).split("(", 1)[0])
        match = contract.get("match") or {}
        if isinstance(match, dict):
            expected_required.update(
                f"semantic_attribute:{name}"
                for name in match.get("required_attribute_values") or []
            )
            if match.get("require_explicit_period"):
                expected_required.add("기준년월")
    elif source.get("kind") == "canonical_composition" and source.get("verified_query"):
        query = catalog["queries"].get(str(source["verified_query"]))

    if query:
        merged = {
            str(spec["name"]): dict(spec)
            for spec in VQ_PARAM_SPECS.get(str(query.get("name") or ""), [])
        }
        for name, info in (query.get("parameters") or {}).items():
            merged.setdefault(str(name), {"name": str(name)}).update(
                info if isinstance(info, dict) else {}
            )
        expected_required.update(
            name for name, spec in merged.items() if spec.get("required")
        )

    actual_required = {str(value) for value in parameters.get("required") or []}
    if actual_required != expected_required:
        failures.append(
            _failure(
                "required_parameter_contract_mismatch",
                expected=sorted(expected_required),
                actual=sorted(actual_required),
            )
        )
    for parameter in parameters.get("required") or []:
        if not _has_parameter_evidence(str(case.get("question_ko") or ""), str(parameter)):
            failures.append(
                _failure(
                    "required_parameter_evidence_missing",
                    expected=parameter,
                    actual=case.get("question_ko"),
                )
            )
    return _check("required_parameters", failures)


def _sql_answer_check(case: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    if case.get("expected_action") != "sql":
        return _check("sql_answer", None)

    failures: list[dict[str, Any]] = []
    expected_tables = sorted(str(value) for value in case.get("expected_tables") or [])
    if "tbdaaat18" in expected_tables:
        failures.append(
            _failure("restricted_table", expected="table excluded", actual="tbdaaat18")
        )

    answer = case.get("expected_sql") if isinstance(case.get("expected_sql"), dict) else {}
    kind = str(answer.get("kind") or "")
    source = case.get("source") if isinstance(case.get("source"), dict) else {}
    source_kind = str(source.get("kind") or "")
    if source_kind == "semantic_contract":
        source_id = str(source.get("id") or "")
        contract = catalog["contracts"].get(source_id)
        if contract is None:
            failures.append(
                _failure("source_contract_missing", expected=source_id, actual=None)
            )
        else:
            expected_vq = str(contract.get("verified_query") or "")
            expected_reference = str(contract.get("reference_query") or "")
            if source.get("verified_query") != contract.get("verified_query"):
                failures.append(
                    _failure(
                        "source_verified_query_mismatch",
                        expected=contract.get("verified_query"),
                        actual=source.get("verified_query"),
                    )
                )
            if source.get("reference_query") != contract.get("reference_query"):
                failures.append(
                    _failure(
                        "source_reference_query_mismatch",
                        expected=contract.get("reference_query"),
                        actual=source.get("reference_query"),
                    )
                )
            if expected_vq:
                facts = catalog["query_facts"].get(expected_vq)
                expected_kind = (
                    "verified_query" if facts and not facts["schema_errors"] else "semantic_spec"
                )
            else:
                expected_kind = "semantic_generation"
            if kind != expected_kind:
                failures.append(
                    _failure(
                        "source_answer_kind_mismatch",
                        expected=expected_kind,
                        actual=kind,
                    )
                )
            if kind == "verified_query" and answer.get("name") != expected_vq:
                failures.append(
                    _failure(
                        "source_verified_answer_mismatch",
                        expected=expected_vq,
                        actual=answer.get("name"),
                    )
                )
            if kind == "semantic_generation" and answer.get("contract") != source_id:
                failures.append(
                    _failure(
                        "source_generation_contract_mismatch",
                        expected=source_id,
                        actual=answer.get("contract"),
                    )
                )
            reference = answer.get("verified_query_reference") or answer.get("reference_query")
            expected_answer_reference = expected_vq or expected_reference
            if (
                kind != "verified_query"
                and expected_answer_reference
                and (
                    not isinstance(reference, dict)
                    or reference.get("name") != expected_answer_reference
                )
            ):
                failures.append(
                    _failure(
                        "source_answer_reference_mismatch",
                        expected=expected_answer_reference,
                        actual=reference.get("name") if isinstance(reference, dict) else None,
                    )
                )
    elif source_kind == "canonical_composition":
        expected_vq = str(source.get("verified_query") or "")
        expected_kind = "verified_query" if expected_vq else "semantic_spec"
        if kind != expected_kind:
            failures.append(
                _failure(
                    "source_answer_kind_mismatch",
                    expected=expected_kind,
                    actual=kind,
                )
            )
        if expected_vq and answer.get("name") != expected_vq:
            failures.append(
                _failure(
                    "source_verified_answer_mismatch",
                    expected=expected_vq,
                    actual=answer.get("name"),
                )
            )
    else:
        failures.append(
            _failure(
                "unknown_sql_source_kind",
                expected=["semantic_contract", "canonical_composition"],
                actual=source_kind,
            )
        )
    if kind == "verified_query":
        name = str(answer.get("name") or "")
        query = catalog["queries"].get(name)
        facts = catalog["query_facts"].get(name)
        if query is None or facts is None:
            failures.append(
                _failure("verified_query_missing", expected=name, actual=None)
            )
        else:
            if facts["runtime_mode"] == "reference_only":
                failures.append(
                    _failure(
                        "verified_query_not_executable",
                        expected="active",
                        actual=facts["runtime_mode"],
                    )
                )
            if facts["sha256"] != answer.get("template_sha256"):
                failures.append(
                    _failure(
                        "verified_query_hash_mismatch",
                        expected=answer.get("template_sha256"),
                        actual=facts["sha256"],
                    )
                )
            if facts["tables"] != expected_tables:
                failures.append(
                    _failure(
                        "verified_query_table_mismatch",
                        expected=expected_tables,
                        actual=facts["tables"],
                    )
                )
            if facts["schema_errors"]:
                failures.append(
                    _failure(
                        "verified_query_schema_error",
                        expected=[],
                        actual=facts["schema_errors"],
                    )
                )
    elif kind == "semantic_generation":
        name = str(answer.get("contract") or "")
        contract = catalog["contracts"].get(name)
        if contract is None:
            failures.append(
                _failure("semantic_contract_missing", expected=name, actual=None)
            )
        else:
            if contract.get("execution_mode") != "semantic_generation":
                failures.append(
                    _failure(
                        "semantic_generation_mode_mismatch",
                        expected="semantic_generation",
                        actual=contract.get("execution_mode"),
                    )
                )
            contract_tables = sorted(
                str(value).rsplit(".", 1)[-1] for value in contract.get("source_tables") or []
            )
            if contract_tables != expected_tables:
                failures.append(
                    _failure(
                        "semantic_generation_table_mismatch",
                        expected=expected_tables,
                        actual=contract_tables,
                    )
                )
            expected_expressions = {
                name: catalog["metrics"][name]["expression"]
                for name in contract.get("metric_names") or []
                if name in catalog["metrics"]
            }
            if answer.get("metric_expressions") != expected_expressions:
                failures.append(
                    _failure(
                        "semantic_generation_metric_mismatch",
                        expected=expected_expressions,
                        actual=answer.get("metric_expressions"),
                    )
                )
            if answer.get("required_filters") != list(contract.get("filters") or []):
                failures.append(
                    _failure(
                        "semantic_generation_filter_mismatch",
                        expected=list(contract.get("filters") or []),
                        actual=answer.get("required_filters"),
                    )
                )
            if answer.get("calculation") != contract.get("calculation"):
                failures.append(
                    _failure(
                        "semantic_generation_calculation_mismatch",
                        expected=contract.get("calculation"),
                        actual=answer.get("calculation"),
                    )
                )
            if case.get("expected_grain") != contract.get("result_grain"):
                failures.append(
                    _failure(
                        "semantic_generation_grain_mismatch",
                        expected=contract.get("result_grain"),
                        actual=case.get("expected_grain"),
                    )
                )
        reference_query = answer.get("reference_query")
        if isinstance(reference_query, dict) and reference_query.get("name"):
            query = catalog["queries"].get(str(reference_query["name"]))
            mode = str((query or {}).get("runtime_mode") or "")
            if mode != "reference_only":
                failures.append(
                    _failure(
                        "reference_query_mode_mismatch",
                        expected="reference_only",
                        actual=mode or None,
                    )
                )
            facts = catalog["query_facts"].get(str(reference_query["name"]))
            if not facts or facts["sha256"] != reference_query.get("template_sha256"):
                failures.append(
                    _failure(
                        "reference_query_hash_mismatch",
                        expected=reference_query.get("template_sha256"),
                        actual=(facts or {}).get("sha256"),
                    )
                )
    elif kind == "semantic_spec":
        metric_names = set((answer.get("metric_expressions") or {}).keys())
        unknown_metrics = sorted(metric_names - set(catalog["metrics"]))
        if unknown_metrics:
            failures.append(
                _failure(
                    "semantic_spec_unknown_metric",
                    expected="known canonical metrics",
                    actual=unknown_metrics,
                )
            )
        expected_expressions = {
            name: catalog["metrics"][name]["expression"]
            for name in metric_names
            if name in catalog["metrics"]
        }
        if answer.get("metric_expressions") != expected_expressions:
            failures.append(
                _failure(
                    "semantic_spec_expression_mismatch",
                    expected=expected_expressions,
                    actual=answer.get("metric_expressions"),
                )
            )
        expected_aggregation = {
            name: catalog["metrics"][name].get("aggregation_behavior")
            for name in metric_names
            if name in catalog["metrics"]
        }
        if answer.get("aggregation_behavior") != expected_aggregation:
            failures.append(
                _failure(
                    "semantic_spec_aggregation_mismatch",
                    expected=expected_aggregation,
                    actual=answer.get("aggregation_behavior"),
                )
            )
        source = case.get("source") if isinstance(case.get("source"), dict) else {}
        required_metric_filters = {
            str(value)
            for name in metric_names
            if name in catalog["metrics"]
            for value in catalog["metrics"][name].get("required_filters") or []
        }
        actual_filters = {str(value) for value in answer.get("required_filters") or []}
        missing_filters = sorted(required_metric_filters - actual_filters)
        if missing_filters:
            failures.append(
                _failure(
                    "semantic_spec_filter_mismatch",
                    expected=sorted(required_metric_filters),
                    actual=sorted(actual_filters),
                )
            )
        expected_joins = [
            catalog["paths"][str(name)]["sql"]
            for name in source.get("join_paths") or []
            if str(name) in catalog["paths"]
        ]
        if answer.get("join_conditions") != expected_joins:
            failures.append(
                _failure(
                    "semantic_spec_join_mismatch",
                    expected=expected_joins,
                    actual=answer.get("join_conditions"),
                )
            )
        for path_name in source.get("join_paths") or []:
            path = catalog["paths"].get(str(path_name))
            if path and path.get("confidence") != "curated":
                failures.append(
                    _failure(
                        "semantic_spec_uncurated_join",
                        expected="curated",
                        actual=path.get("confidence"),
                        detail=str(path_name),
                    )
                )
        if source.get("kind") == "semantic_contract":
            contract = catalog["contracts"].get(str(source.get("id") or ""))
            if contract:
                contract_filters = {str(value) for value in contract.get("filters") or []}
                if not contract_filters.issubset(actual_filters):
                    failures.append(
                        _failure(
                            "semantic_spec_contract_filter_mismatch",
                            expected=sorted(contract_filters),
                            actual=sorted(actual_filters),
                        )
                    )
                if answer.get("calculation") != contract.get("calculation"):
                    failures.append(
                        _failure(
                            "semantic_spec_calculation_mismatch",
                            expected=contract.get("calculation"),
                            actual=answer.get("calculation"),
                        )
                    )
                if case.get("expected_grain") != contract.get("result_grain"):
                    failures.append(
                        _failure(
                            "semantic_spec_grain_mismatch",
                            expected=contract.get("result_grain"),
                            actual=case.get("expected_grain"),
                        )
                    )
        reference = answer.get("verified_query_reference")
        if isinstance(reference, dict) and reference.get("name"):
            facts = catalog["query_facts"].get(str(reference["name"]))
            if not facts or facts["sha256"] != reference.get("template_sha256"):
                failures.append(
                    _failure(
                        "semantic_spec_reference_hash_mismatch",
                        expected=reference.get("template_sha256"),
                        actual=(facts or {}).get("sha256"),
                    )
                )
            if facts and not facts["schema_errors"]:
                failures.append(
                    _failure(
                        "semantic_spec_reference_exclusion_mismatch",
                        expected="schema validation failure",
                        actual="schema valid",
                    )
                )
    else:
        failures.append(
            _failure(
                "unknown_sql_answer_kind",
                expected=["verified_query", "semantic_generation", "semantic_spec"],
                actual=kind,
            )
        )
    return _check("sql_answer", failures)


def _semantic_contract_route_check(case: dict[str, Any]) -> dict[str, Any]:
    source = case.get("source") if isinstance(case.get("source"), dict) else {}
    if source.get("kind") != "semantic_contract":
        return _check("semantic_contract_route", None)
    candidates = semantic_query_contract_candidates(
        SCHEMA,
        str(case.get("question_ko") or ""),
        max_count=1,
        routing_only=True,
    )
    expected = str(source.get("id") or "")
    actual = str(candidates[0].get("name") or "") if candidates else None
    failures = [] if actual == expected else [
        _failure("semantic_contract_route_mismatch", expected=expected, actual=actual)
    ]
    return _check("semantic_contract_route", failures)


def _observation(expected: Any, actual: Any, *, error: str = "") -> dict[str, Any]:
    decided = actual not in (None, "", []) and not error
    return {
        "eligible": True,
        "decided": decided,
        "correct": bool(decided and actual == expected),
        "expected": expected,
        "actual": actual,
        "error": error,
    }


def _deterministic_domain(question: str) -> str | None:
    reference = _reference_domain_by_rule(question)
    if reference:
        return reference
    candidates = _weighted_domain_scores(
        _keyword_rule_domain_scores(SCHEMA, question),
        _metric_entity_domain_scores(SCHEMA, question),
        {},
    )
    if _needs_domain_adjudication(candidates):
        return None
    return str(candidates[0].get("domain") or "") if candidates else None


def _runtime_observations(
    case: dict[str, Any], *, include_table_runtime: bool
) -> dict[str, Any]:
    question = str(case.get("question_ko") or "")
    observations: dict[str, Any] = {}

    if case.get("expected_action") == "unsupported":
        observations["domain"] = {"eligible": False, "reason": "unsupported"}
    else:
        try:
            actual_domain = _deterministic_domain(question)
            observations["domain"] = _observation(case.get("domain"), actual_domain)
        except Exception as exc:  # noqa: BLE001 - an eval reports errors and continues.
            observations["domain"] = _observation(
                case.get("domain"), None, error=f"{type(exc).__name__}: {exc}"
            )

    source = case.get("source") if isinstance(case.get("source"), dict) else {}
    if source.get("kind") == "semantic_contract":
        try:
            candidates = semantic_query_contract_candidates(
                SCHEMA, question, max_count=1, routing_only=True
            )
            actual_contract = str(candidates[0].get("name") or "") if candidates else None
            observations["semantic_contract"] = _observation(
                source.get("id"), actual_contract
            )
        except Exception as exc:  # noqa: BLE001
            observations["semantic_contract"] = _observation(
                source.get("id"), None, error=f"{type(exc).__name__}: {exc}"
            )
    else:
        observations["semantic_contract"] = {"eligible": False}

    answer = case.get("expected_sql") if isinstance(case.get("expected_sql"), dict) else {}
    if answer.get("kind") == "verified_query":
        try:
            match = _match_vq_by_semantic_contract(question) or _match_vq_by_rules(question)
            actual_query = str(match.get("matched_query_name") or "") if match else None
            observations["verified_query"] = _observation(answer.get("name"), actual_query)
        except Exception as exc:  # noqa: BLE001
            observations["verified_query"] = _observation(
                answer.get("name"), None, error=f"{type(exc).__name__}: {exc}"
            )
    else:
        observations["verified_query"] = {"eligible": False}

    core_table_route = source.get("kind") == "semantic_contract"
    if case.get("expected_action") == "sql" and (
        core_table_route or include_table_runtime
    ):
        expected_tables = sorted(str(value) for value in case.get("expected_tables") or [])
        try:
            actual_tables = sorted(
                _rule_rank_tables(question, max_tables=max(8, len(expected_tables)))
            )
            observations["tables"] = _observation(expected_tables, actual_tables)
        except Exception as exc:  # noqa: BLE001
            observations["tables"] = _observation(
                expected_tables, None, error=f"{type(exc).__name__}: {exc}"
            )
    else:
        observations["tables"] = {
            "eligible": False,
            "reason": "non_authoritative_disabled"
            if case.get("expected_action") == "sql"
            else "not_sql",
        }
    return observations


def _dataset_failures(
    cases: list[dict[str, Any]], expected_count: int | None
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if expected_count is not None and len(cases) != expected_count:
        failures.append(
            _failure("case_count_mismatch", expected=expected_count, actual=len(cases))
        )
    actual_ids = [str(case.get("id") or "") for case in cases]
    duplicate_ids = sorted(value for value, count in Counter(actual_ids).items() if count > 1)
    if duplicate_ids:
        failures.append(
            _failure("duplicate_case_id", expected="unique IDs", actual=duplicate_ids[:20])
        )
    expected_ids = [f"gs-{index:06d}" for index in range(1, len(cases) + 1)]
    if expected_count is not None and actual_ids != expected_ids:
        failures.append(
            _failure("case_id_sequence_mismatch", expected="gs-000001..N", actual="out of sequence")
        )
    normalized = [_normalized_question(str(case.get("question_ko") or "")) for case in cases]
    duplicates = sorted(value for value, count in Counter(normalized).items() if count > 1)
    if duplicates:
        failures.append(
            _failure("duplicate_question", expected="unique normalized questions", actual=duplicates[:20])
        )
    expected_version = str(SCHEMA.get("semantic_layer_metadata", {}).get("version") or "")
    actual_versions = sorted({str(case.get("semantic_layer_version") or "") for case in cases})
    if actual_versions != [expected_version]:
        failures.append(
            _failure(
                "semantic_layer_version_mismatch",
                expected=expected_version,
                actual=actual_versions,
            )
        )
    return failures


def _runtime_summary(case_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for name in ("domain", "semantic_contract", "verified_query", "tables"):
        values = [
            case["runtime"][name]
            for case in case_results
            if case["runtime"][name].get("eligible")
        ]
        eligible = len(values)
        decided = sum(bool(value.get("decided")) for value in values)
        correct = sum(bool(value.get("correct")) for value in values)
        errors = sum(bool(value.get("error")) for value in values)
        summary[name] = {
            "eligible": eligible,
            "decided": decided,
            "correct": correct,
            "errors": errors,
            "coverage_percent": round(decided / eligible * 100, 2) if eligible else None,
            "end_to_end_accuracy_percent": round(correct / eligible * 100, 2) if eligible else None,
            "accuracy_when_decided_percent": round(correct / decided * 100, 2) if decided else None,
        }
    return summary


def _check_summary(case_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    summary: dict[str, dict[str, Any]] = {}
    for name in CHECK_NAMES:
        values = [
            check
            for case in case_results
            for check in case["checks"]
            if check["name"] == name
        ]
        applied = sum(check["status"] != "not_applicable" for check in values)
        passed = sum(check["status"] == "pass" for check in values)
        failed = sum(check["status"] == "fail" for check in values)
        summary[name] = {
            "applied": applied,
            "not_applicable": len(values) - applied,
            "passed": passed,
            "failed": failed,
            "pass_rate_percent": round(passed / applied * 100, 2) if applied else None,
        }
    return summary


def _breakdowns(case_results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    dimensions = ("expected_action", "domain", "bucket", "difficulty", "sql_kind")
    output: dict[str, list[dict[str, Any]]] = {}
    for dimension in dimensions:
        groups: dict[str, list[dict[str, Any]]] = {}
        for case in case_results:
            value = str(case["expected"].get(dimension) or "n/a")
            groups.setdefault(value, []).append(case)
        output[dimension] = []
        for value in sorted(groups):
            grouped = groups[value]
            passed = sum(bool(case["passed"]) for case in grouped)
            output[dimension].append(
                {
                    "value": value,
                    "cases": len(grouped),
                    "passed": passed,
                    "failed": len(grouped) - passed,
                    "pass_rate_percent": round(passed / len(grouped) * 100, 2),
                }
            )
    return output


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def evaluate(
    cases_path: Path = DEFAULT_CASES_PATH,
    *,
    expected_count: int | None = 1000,
    include_table_runtime: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    cases = load_cases(cases_path)
    catalog = _catalog()
    dataset_failures = _dataset_failures(cases, expected_count)
    case_results: list[dict[str, Any]] = []

    for case in cases:
        case_started = time.perf_counter()
        checks = [
            _semantic_reference_check(case, catalog),
            _action_contract_check(case),
            _required_parameter_check(case, catalog),
            _sql_answer_check(case, catalog),
            _semantic_contract_route_check(case),
        ]
        answer = case.get("expected_sql") if isinstance(case.get("expected_sql"), dict) else {}
        labels = case.get("labels") if isinstance(case.get("labels"), dict) else {}
        source = case.get("source") if isinstance(case.get("source"), dict) else {}
        case_results.append(
            {
                "id": case.get("id"),
                "question_ko": case.get("question_ko"),
                "passed": all(check["status"] != "fail" for check in checks),
                "duration_ms": round((time.perf_counter() - case_started) * 1000, 3),
                "expected": {
                    "expected_action": case.get("expected_action"),
                    "domain": case.get("domain"),
                    "bucket": labels.get("bucket"),
                    "difficulty": labels.get("difficulty"),
                    "sql_kind": answer.get("kind") or "n/a",
                    "source_id": source.get("id"),
                    "tables": case.get("expected_tables") or [],
                },
                "checks": checks,
                "runtime": _runtime_observations(
                    case, include_table_runtime=include_table_runtime
                ),
            }
        )

    passed = sum(bool(case["passed"]) for case in case_results)
    all_failures = [
        failure
        for case in case_results
        for check in case["checks"]
        for failure in check["failures"]
    ]
    failure_counts = Counter(str(failure["code"]) for failure in all_failures)
    duration = time.perf_counter() - started
    status = "passed" if not dataset_failures and passed == len(case_results) else "failed"
    dataset_bytes = cases_path.read_bytes()
    versions = sorted({str(case.get("semantic_layer_version") or "") for case in cases})
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run": {
            "status": status,
            "mode": "offline_deterministic",
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "duration_seconds": round(duration, 3),
            "git_commit": _git_commit(),
            "include_table_runtime": include_table_runtime,
        },
        "dataset": {
            "path": str(cases_path.resolve()),
            "sha256": hashlib.sha256(dataset_bytes).hexdigest(),
            "semantic_layer_versions": versions,
            "case_count": len(cases),
            "expected_count": expected_count,
            "failures": dataset_failures,
        },
        "summary": {
            "total": len(case_results),
            "passed": passed,
            "failed": len(case_results) - passed,
            "pass_rate_percent": round(passed / len(case_results) * 100, 2)
            if case_results
            else 0.0,
        },
        "check_summary": _check_summary(case_results),
        "runtime_summary": _runtime_summary(case_results),
        "breakdowns": _breakdowns(case_results),
        "failure_counts": dict(sorted(failure_counts.items())),
        "cases": case_results,
    }


def _percent(value: Any) -> str:
    return "-" if value is None else f"{value:.2f}%"


def _cell(value: Any, limit: int = 120) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    text = text.replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_markdown(report: dict[str, Any]) -> str:
    run = report["run"]
    dataset = report["dataset"]
    summary = report["summary"]
    lines = [
        "# 시맨틱 골든셋 평가 결과",
        "",
        "> 이 문서는 `scripts/run_semantic_golden_eval.py`가 자동 생성했습니다.",
        "",
        "## 종합 결과",
        "",
        f"**{run['status'].upper()}** — {summary['total']:,}건 중 "
        f"{summary['passed']:,}건 통과, {summary['failed']:,}건 실패 "
        f"({_percent(summary['pass_rate_percent'])})",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| 실행 모드 | `{run['mode']}` |",
        f"| 실행 시각 | `{run['generated_at']}` |",
        f"| 소요 시간 | {run['duration_seconds']:.3f}초 |",
        f"| Git commit | `{run['git_commit']}` |",
        f"| 전체 테이블 선택 진단 | `{'on' if run['include_table_runtime'] else 'off'}` |",
        f"| 데이터셋 | `{dataset['path']}` |",
        f"| 데이터셋 SHA-256 | `{dataset['sha256']}` |",
        f"| 시맨틱 버전 | `{', '.join(dataset['semantic_layer_versions'])}` |",
        "",
        "기본 합격 판정은 현재 시맨틱 SSOT와 골든 정답의 계약 일치 여부만 사용합니다. "
        "아래 런타임 선택 지표는 개선 현황을 보여주는 비차단(non-gating) 관측값입니다.",
        "",
        "## 계약 검사",
        "",
        "| 검사 | 적용 | N/A | 통과 | 실패 | 통과율 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, values in report["check_summary"].items():
        lines.append(
            f"| `{name}` | {values['applied']:,} | {values['not_applicable']:,} | "
            f"{values['passed']:,} | {values['failed']:,} | "
            f"{_percent(values['pass_rate_percent'])} |"
        )

    lines.extend(
        [
            "",
            "## 결정적 런타임 관측",
            "",
            "| 선택기 | 대상 | 결정 | 정답 | 커버리지 | 전체 정확도 | 결정 시 정확도 | 오류 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for name, values in report["runtime_summary"].items():
        lines.append(
            f"| `{name}` | {values['eligible']:,} | {values['decided']:,} | "
            f"{values['correct']:,} | {_percent(values['coverage_percent'])} | "
            f"{_percent(values['end_to_end_accuracy_percent'])} | "
            f"{_percent(values['accuracy_when_decided_percent'])} | {values['errors']:,} |"
        )
    if not run["include_table_runtime"]:
        lines.extend(
            [
                "",
                "테이블 지표는 기본 실행에서 authoritative core 600건만 평가합니다. "
                "비-authoritative composition까지 전수 평가하려면 매우 느릴 수 있는 "
                "`--include-table-runtime`을 사용합니다.",
            ]
        )
    lines.extend(
        [
            "",
            "`domain`의 미결정 건은 실제 코드가 LLM adjudication으로 넘기는 질문입니다. "
            f"`verified_query`는 골든 정답이 VQ인 "
            f"{report['runtime_summary']['verified_query']['eligible']:,}건에 대한 선택 재현율이며, "
            "비-VQ 질문에 대한 정밀도 평가는 포함하지 않습니다.",
        ]
    )

    lines.extend(["", "## 구간별 계약 결과", ""])
    labels = {
        "expected_action": "예상 액션",
        "domain": "도메인",
        "bucket": "버킷",
        "difficulty": "난이도",
        "sql_kind": "SQL 정답 종류",
    }
    for dimension, rows in report["breakdowns"].items():
        lines.extend(
            [
                f"### {labels[dimension]}",
                "",
                "| 값 | 케이스 | 통과 | 실패 | 통과율 |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for row in rows:
            lines.append(
                f"| `{_cell(row['value'])}` | {row['cases']:,} | {row['passed']:,} | "
                f"{row['failed']:,} | {_percent(row['pass_rate_percent'])} |"
            )
        lines.append("")

    lines.extend(["## 실패 요약", ""])
    if dataset["failures"]:
        lines.append("데이터셋 수준 실패:")
        lines.append("")
        for failure in dataset["failures"]:
            lines.append(
                f"- `{failure['code']}`: expected={_cell(failure['expected'])}, "
                f"actual={_cell(failure['actual'])}"
            )
        lines.append("")
    if report["failure_counts"]:
        lines.extend(["| 실패 코드 | 건수 |", "|---|---:|"])
        for code, count in report["failure_counts"].items():
            lines.append(f"| `{code}` | {count:,} |")
        lines.append("")
    else:
        lines.extend(["계약 검사 실패가 없습니다.", ""])

    failed_cases = [case for case in report["cases"] if not case["passed"]]
    if failed_cases:
        lines.extend(
            [
                "### 실패 상세 (최대 50건)",
                "",
                "| ID | 질문 | 실패 코드 | expected | actual |",
                "|---|---|---|---|---|",
            ]
        )
        for case in failed_cases[:50]:
            failures = [
                failure for check in case["checks"] for failure in check["failures"]
            ]
            first = failures[0]
            lines.append(
                f"| `{case['id']}` | {_cell(case['question_ko'], 80)} | "
                f"`{first['code']}` | {_cell(first['expected'], 80)} | "
                f"{_cell(first['actual'], 80)} |"
            )
        lines.append("")

    runtime_misses: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for case in report["cases"]:
        for name, value in case["runtime"].items():
            if value.get("eligible") and not value.get("correct"):
                runtime_misses.append((name, case, value))
    lines.extend(["## 런타임 관측 불일치 예시 (최대 30건)", ""])
    if runtime_misses:
        lines.extend(
            [
                "| 선택기 | ID | expected | actual |",
                "|---|---|---|---|",
            ]
        )
        for name, case, value in runtime_misses[:30]:
            lines.append(
                f"| `{name}` | `{case['id']}` | {_cell(value.get('expected'), 90)} | "
                f"{_cell(value.get('actual'), 90)} |"
            )
    else:
        lines.append("관측 불일치가 없습니다.")

    lines.extend(
        [
            "",
            "## 해석 범위",
            "",
            "이 평가는 외부 LLM과 실제 DB를 호출하지 않습니다. 따라서 시맨틱 참조, "
            "verified query hash·schema, 필수 파라미터, 계약 라우팅의 재현 가능한 회귀를 "
            "검사하지만, 생성 SQL의 실행 결과(denotation), 자연어 응답 품질, 현업의 문장별 "
            "승인을 보장하지 않습니다. 전체 케이스와 관측값은 함께 생성된 JSON을 사용합니다.",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(report: dict[str, Any], json_out: Path, markdown_out: Path) -> None:
    json_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_out.write_text(render_markdown(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument(
        "--expected-count",
        type=int,
        default=1000,
        help="expected JSONL record count; use 0 to disable the count gate",
    )
    parser.add_argument(
        "--include-table-runtime",
        action="store_true",
        help="also diagnose non-authoritative composition table selection (very slow)",
    )
    args = parser.parse_args()

    report = evaluate(
        args.cases,
        expected_count=args.expected_count if args.expected_count > 0 else None,
        include_table_runtime=args.include_table_runtime,
    )
    write_reports(report, args.json_out, args.markdown_out)
    summary = report["summary"]
    print(
        f"{report['run']['status'].upper()}: {summary['passed']}/{summary['total']} "
        f"contract cases passed"
    )
    print(f"JSON: {args.json_out.resolve()}")
    print(f"Markdown: {args.markdown_out.resolve()}")
    return 0 if report["run"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

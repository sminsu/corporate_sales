from __future__ import annotations

import json
from copy import deepcopy

from scripts.run_semantic_golden_eval import (
    DEFAULT_CASES_PATH,
    evaluate,
    write_reports,
)


def test_semantic_golden_runner_writes_machine_and_human_reports(tmp_path) -> None:
    cases_path = tmp_path / "semantic-golden-smoke.jsonl"
    cases_path.write_text(
        DEFAULT_CASES_PATH.read_text(encoding="utf-8").splitlines()[0] + "\n",
        encoding="utf-8",
    )
    report = evaluate(cases_path, expected_count=1)

    assert report["run"]["status"] == "passed"
    assert report["summary"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "pass_rate_percent": 100.0,
    }
    assert report["runtime_summary"]["semantic_contract"]["eligible"] == 1
    assert report["runtime_summary"]["verified_query"]["eligible"] == 0

    json_out = tmp_path / "semantic-golden-eval.json"
    markdown_out = tmp_path / "semantic-golden-eval.md"
    write_reports(report, json_out, markdown_out)

    assert json.loads(json_out.read_text(encoding="utf-8"))["summary"]["total"] == 1
    markdown = markdown_out.read_text(encoding="utf-8")
    assert "**PASSED** — 1건 중 1건 통과" in markdown
    assert "## 결정적 런타임 관측" in markdown
    assert "VQ인 0건에 대한 선택 재현율" in markdown


def test_semantic_golden_runner_rejects_broken_source_and_parameters(tmp_path) -> None:
    cases = [json.loads(line) for line in DEFAULT_CASES_PATH.read_text(encoding="utf-8").splitlines()]
    with_required = deepcopy(next(case for case in cases if case["parameters"]["required"]))
    with_required["parameters"]["required"] = []

    broken_source = deepcopy(next(case for case in cases if case["expected_sql"]["kind"] == "verified_query"))
    broken_source["source"] = "invalid"

    cases_path = tmp_path / "broken.jsonl"
    cases_path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in (with_required, broken_source))
        + "\n",
        encoding="utf-8",
    )
    report = evaluate(cases_path, expected_count=None)
    failure_codes = {
        failure["code"]
        for case in report["cases"]
        for check in case["checks"]
        for failure in check["failures"]
    }

    assert report["run"]["status"] == "failed"
    assert "required_parameter_contract_mismatch" in failure_codes
    assert "unknown_sql_source_kind" in failure_codes

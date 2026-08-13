from __future__ import annotations

import json

import pytest

from scripts.run_semantic_golden_live import (
    DEFAULT_CASES,
    _default_params,
    build_summary,
    invoke_real_agent,
    load_jsonl,
    render_summary,
    run_cases,
)


def test_live_runner_has_defaults_for_every_golden_missing_parameter() -> None:
    for case in load_jsonl(DEFAULT_CASES):
        missing = case.get("expected_missing_parameters") or []
        defaults = _default_params(case, missing)
        assert set(defaults) == set(missing), case["id"]
        assert all(value not in (None, "", [], {}) for value in defaults.values()), case["id"]

    period_defaults = _default_params(
        {"parameters": {"reference_date": "2026-08-04"}},
        ["기간_시작", "기간_종료"],
    )
    assert period_defaults == {"기간_시작": "202603", "기간_종료": "202608"}


def test_real_agent_invoker_rebuilds_verified_query_continuation(monkeypatch) -> None:
    from text2sql_agent import workflow

    captured: dict = {}

    class FakeApp:
        def invoke(self, state: dict) -> dict:
            captured.update(state)
            return {"answer": "완료"}

    monkeypatch.setattr(workflow, "_get_app", lambda: FakeApp())
    result = invoke_real_agent(
        "질문",
        continuation={
            "selected_domain": "card_usage",
            "matched_query_name": "sample_vq",
            "matched_query_sql": "SELECT 1 WHERE ym = '{기준년월}'",
            "matched_query_params": {"기준년월": {"required": True}},
            "extracted_params": {"조회개월수": 6},
            "user_provided_params": {},
        },
        params={"기준년월": "202608"},
    )

    assert result == {"answer": "완료"}
    assert captured["question_type"] == "need_sql"
    assert captured["selected_domain"] == "card_usage"
    assert captured["matched_query_name"] == "sample_vq"
    assert captured["user_provided_params"] == {
        "조회개월수": 6,
        "기준년월": "202608",
    }


def test_live_runner_saves_answers_errors_and_resumes(tmp_path) -> None:
    cases = [
        {
            "id": f"gs-{index:06d}",
            "question_ko": name,
            "expected_action": action,
            "parameters": {"reference_date": "2026-08-04"},
        }
        for index, (name, action) in enumerate(
            [("ok", "sql"), ("params", "clarify"), ("handled", "sql"), ("boom", "sql")],
            1,
        )
    ]
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    results_path = tmp_path / "results.jsonl"

    calls: list[tuple[str, dict | None, dict | None]] = []

    def fake_agent(
        question: str,
        *,
        continuation: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        calls.append((question, continuation, params))
        if question == "ok":
            return {"answer": "정상 답변", "query_rows": [(1,)]}
        if question == "params":
            if continuation is None:
                return {
                    "answer": "",
                    "param_stage": "need_params",
                    "missing_params": [{"name": "기준년월"}],
                }
            if "기업명" not in (params or {}):
                return {
                    "answer": "",
                    "param_stage": "need_params",
                    "missing_params": [{"name": "기업명"}],
                }
            return {
                "answer": "자동 기본값 답변",
                "param_stage": "done",
                "extracted_params": dict(params or {}),
            }
        if question == "handled":
            return {"answer": "오류 안내 답변", "error_message": "DB 오류"}
        raise RuntimeError("LLM 연결 실패")

    rows = run_cases(cases, results_path, invoke=fake_agent)
    summary = build_summary(cases, rows, cases_path)
    by_id = {row["id"]: row for row in rows}
    param_calls = [call for call in calls if call[0] == "params"]

    assert len(load_jsonl(results_path)) == 4
    assert summary["records_saved"] == 4
    assert summary["answers_saved"] == 3
    assert summary["errors"] == 2
    assert summary["defaulted_cases"] == 1
    assert summary["default_param_rounds"] == 2
    assert summary["requires_params"] == 0
    assert summary["status_counts"] == {
        "agent_error": 1,
        "answered": 2,
        "execution_error": 1,
    }
    assert len(param_calls) == 3
    assert param_calls[0][1:] == (None, None)
    assert param_calls[1][1]["param_stage"] == "need_params"
    assert param_calls[1][2] == {"기준년월": "202608"}
    assert param_calls[2][2] == {"기준년월": "202608", "기업명": "삼성전자"}
    assert by_id["gs-000002"]["status"] == "answered"
    assert by_id["gs-000002"]["actual"]["answer"] == "자동 기본값 답변"
    assert by_id["gs-000002"]["default_params_applied"] == {
        "기준년월": "202608",
        "기업명": "삼성전자",
    }
    assert by_id["gs-000002"]["default_param_reference_date"] == "2026-08-04"
    rendered = render_summary(summary, results_path)
    assert "| 응답 문구 저장 | 3 (75.00%) |" in rendered
    assert "| 기본 파라미터 자동 적용 | 1 |" in rendered

    run_cases(cases, results_path, invoke=lambda question: (_ for _ in ()).throw(AssertionError(question)))
    assert len(load_jsonl(results_path)) == 4

    with results_path.open("a", encoding="utf-8") as file:
        file.write('{"truncated":')
    run_cases(cases, results_path, invoke=lambda question: (_ for _ in ()).throw(AssertionError(question)))
    assert len(load_jsonl(results_path)) == 4

    retry_calls: list[str] = []

    def recovered(question: str) -> dict:
        retry_calls.append(question)
        return {"answer": "재시도 성공"}

    retried = run_cases(cases, results_path, retry_errors=True, invoke=recovered)
    assert retry_calls == ["handled", "boom"]
    assert len(retried) == 6
    assert build_summary(cases, retried, cases_path)["errors"] == 0

    with pytest.raises(ValueError, match="duplicate golden case ids"):
        run_cases([cases[0], cases[0]], tmp_path / "duplicate.jsonl", invoke=fake_agent)
    with pytest.raises(ValueError, match="at least one row"):
        run_cases([], tmp_path / "empty.jsonl", invoke=fake_agent)


def test_live_runner_bounds_repeated_parameter_requests_and_retries_them(tmp_path) -> None:
    cases = [
        {
            "id": "gs-000001",
            "question_ko": "stuck",
            "expected_action": "clarify",
            "parameters": {"reference_date": "2026-08-04"},
        }
    ]
    results_path = tmp_path / "results.jsonl"
    calls: list[tuple[dict | None, dict | None]] = []

    def stuck_agent(
        question: str,
        *,
        continuation: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        calls.append((continuation, params))
        return {
            "answer": "",
            "param_stage": "need_params",
            "missing_params": [{"name": "기준년월"}],
        }

    rows = run_cases(cases, results_path, invoke=stuck_agent)

    assert len(calls) == 2
    assert calls[0] == (None, None)
    assert calls[1][1] == {"기준년월": "202608"}
    assert rows[-1]["status"] == "requires_params"
    assert rows[-1]["default_param_rounds"] == 1

    run_cases(
        cases,
        results_path,
        invoke=lambda question: (_ for _ in ()).throw(AssertionError(question)),
    )
    assert len(load_jsonl(results_path)) == 1

    retried_questions: list[str] = []
    retried = run_cases(
        cases,
        results_path,
        retry_errors=True,
        invoke=lambda question: retried_questions.append(question) or {"answer": "재실행 완료"},
    )
    assert retried_questions == ["stuck"]
    assert len(retried) == 2


def test_live_runner_accepts_only_non_sql_rejection_for_unsupported(tmp_path) -> None:
    cases = [
        {
            "id": f"gs-{index:06d}",
            "question_ko": question,
            "expected_action": "unsupported",
            "parameters": {"reference_date": "2026-08-04"},
        }
        for index, question in enumerate(("reject", "sql", "blank"), 1)
    ]
    cases_path = tmp_path / "cases.jsonl"
    cases_path.write_text(
        "\n".join(json.dumps(case, ensure_ascii=False) for case in cases) + "\n",
        encoding="utf-8",
    )
    results_path = tmp_path / "results.jsonl"

    def fake_agent(question: str) -> dict:
        if question == "reject":
            return {
                "question_type": "reject",
                "answer": "현재 데이터 범위에서는 지원하지 않는 질문입니다.",
            }
        if question == "sql":
            return {
                "question_type": "need_sql",
                "answer": "조회 결과는 1건입니다.",
                "final_sql": "SELECT 1",
                "query_rows": [(1,)],
            }
        return {"question_type": "reject", "answer": ""}

    rows = run_cases(cases, results_path, invoke=fake_agent)
    summary = build_summary(cases, rows, cases_path)
    by_id = {row["id"]: row for row in rows}

    assert by_id["gs-000001"]["status"] == "unsupported"
    assert by_id["gs-000001"]["answer_saved"] is True
    assert by_id["gs-000001"]["data_answer_saved"] is False
    assert by_id["gs-000001"]["unsupported_handled"] is True
    assert by_id["gs-000002"]["status"] == "action_mismatch"
    assert by_id["gs-000002"]["data_answer_saved"] is True
    assert by_id["gs-000003"]["status"] == "missing_answer"
    assert summary["unsupported_handled"] == 1
    assert summary["data_answers_saved"] == 1
    assert summary["outcome_failures"] == 2


def test_live_runner_retries_matching_legacy_parameter_result(tmp_path) -> None:
    case = {
        "id": "gs-000001",
        "question_ko": "legacy",
        "semantic_layer_version": "v1",
        "parameters": {"reference_date": "2026-08-04"},
    }
    results_path = tmp_path / "results.jsonl"
    results_path.write_text(
        json.dumps(
            {
                "id": case["id"],
                "question_ko": case["question_ko"],
                "semantic_layer_version": case["semantic_layer_version"],
                "status": "requires_params",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    rows = run_cases(
        [case],
        results_path,
        invoke=lambda question: calls.append(question) or {"answer": "완료"},
    )

    assert calls == ["legacy"]
    assert len(rows) == 2
    assert rows[-1]["status"] == "answered"

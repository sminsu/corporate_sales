from __future__ import annotations

import json
from unittest.mock import patch

from fastapi.testclient import TestClient

import web_service
from text2sql_agent import workflow


def test_health_endpoint_does_not_expose_provider_or_internal_details() -> None:
    sensitive_url = "http://10.75.221.91:13086"
    sensitive_endpoint = "/api/v2/openai/chat/completions"
    sensitive_error = "ConnectError: bearer=secret-token connection refused"
    sensitive_path = "/Users/internal/project/config/models.local.yaml"
    probe = {
        "llm_ready": False,
        "llm_status": "auth_error",
        "llm_detail": f"{sensitive_error} url={sensitive_url}{sensitive_endpoint} config={sensitive_path}",
        "base_url": sensitive_url,
        "endpoint_path": sensitive_endpoint,
        "provider_error": sensitive_error,
        "config_path": sensitive_path,
    }
    session_status = {
        "kind": "memory",
        "persistent": False,
        "dsn": f"postgresql://user:password@{sensitive_url}/sessions",
        "file_path": sensitive_path,
    }

    with (
        patch.object(web_service, "_LLM_HEALTH_CACHE", {"checked_at": 0.0, "data": None}),
        patch.object(web_service.agent, "probe_llm", return_value=probe),
        patch.object(web_service._SESSION_STORE, "status", return_value=session_status),
        TestClient(web_service.app) as client,
    ):
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    serialized = json.dumps(payload, ensure_ascii=False)
    assert payload["llm_ready"] is False
    assert payload["llm_status"] == "auth_error"
    assert payload["llm_detail"] == "모델 서버 연결 또는 인증 상태를 확인해 주세요."
    assert payload["session_store"] == {"kind": "memory", "persistent": False}
    for sensitive in (sensitive_url, sensitive_endpoint, sensitive_error, sensitive_path, "secret-token", "password"):
        assert sensitive not in serialized
    for internal_key in ("base_url", "endpoint_path", "provider_error", "config_path", "dsn", "file_path"):
        assert internal_key not in serialized


def _base_followup_result() -> dict:
    return {
        "question": "2025년 12월 도미노피자 가맹점별 매출을 보여줘",
        "followup_question": "상위 10개만 보여줘",
        "final_sql": "SELECT 가맹점명, 매출금액 FROM tmdaa5e11 ORDER BY 매출금액 DESC LIMIT 10",
        "answer": "도미노피자 상위 10개 가맹점 결과입니다.",
        "original_answer": "도미노피자 전체 가맹점 매출 결과입니다.",
        "analysis_history": [
            {
                "question": "상위 10개만 보여줘",
                "answer": "도미노피자 상위 10개 가맹점 결과입니다.",
                "mode": "rewrite_sql",
                "sql": "SELECT 가맹점명, 매출금액 FROM tmdaa5e11 ORDER BY 매출금액 DESC LIMIT 10",
            }
        ],
        "selected_domain": "merchant_sales",
        "selected_tables": ["tmdaa5e11"],
        "table_details": "name: tmdaa5e11\nphysical_table: tmdaa5e11",
        "query_columns": ["가맹점명", "매출금액"],
        "query_rows": [("도미노 강남점", 100_000_000)],
    }


def test_followup_query_state_explicitly_preserves_previous_turn_context() -> None:
    base_result = _base_followup_result()
    followup_question = "그럼 전월 기준으로 바꿔줘"

    state = web_service._followup_query_state(base_result, followup_question)

    assert state["question"] == followup_question
    assert state["previous_question"] == "상위 10개만 보여줘"
    assert state["previous_sql"] == base_result["final_sql"]
    assert state["previous_answer"] == base_result["answer"]
    assert state["followup_question"] == followup_question
    assert state["skip_tool_selection"] is True
    assert state["skip_verified_query_matching"] is True

    context = workflow._multiturn_sql_context(state)
    assert f"이전 질문: {state['previous_question']}" in context
    assert f"실제 후속 질문: {followup_question}" in context
    assert f"이전 답변 요약: {state['previous_answer']}" in context
    assert state["previous_sql"] in context


def test_followup_context_reaches_sql_prompt_and_final_result() -> None:
    base_result = _base_followup_result()
    followup_question = "그럼 전월 기준으로 바꿔줘"
    state = web_service._followup_query_state(base_result, followup_question)
    captured: dict[str, str] = {}

    def fake_llm(prompt: str, **_: object) -> str:
        captured["prompt"] = prompt
        return "SELECT 가맹점명, 매출금액 FROM tmdaa5e11 WHERE 기준년월 = '202511'"

    with patch.object(workflow, "_call_llm", side_effect=fake_llm):
        generated = workflow.generate_sql(state)

    prompt = captured["prompt"]
    for value in (
        state["previous_question"],
        state["previous_sql"],
        state["previous_answer"],
        state["followup_question"],
    ):
        assert value in prompt
    assert generated["generated_sql"].startswith("SELECT")

    followup_result = {
        **state,
        **generated,
        "final_sql": generated["generated_sql"],
        "answer": "",
        "query_columns": ["가맹점명", "매출금액"],
        "query_rows": [("도미노 강남점", 90_000_000)],
    }
    finalized = web_service._finalize_followup_result(
        base_result,
        followup_question,
        followup_result,
        "전월 기준 결과입니다.",
        "rewrite_sql",
        "후속 SQL",
    )

    assert finalized["previous_question"] == state["previous_question"]
    assert finalized["previous_sql"] == state["previous_sql"]
    assert finalized["previous_answer"] == state["previous_answer"]
    assert finalized["followup_question"] == followup_question
    assert finalized["analysis_history"][-1]["question"] == followup_question
    assert finalized["analysis_history"][-1]["sql"] == generated["generated_sql"]

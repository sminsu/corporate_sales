from __future__ import annotations

from unittest.mock import patch

from text2sql_agent import workflow
from text2sql_agent.schema import _validate_sql_against_schema


QUESTION = "2026년 해외업종에서 가장 많이 사용한 달과 이용금액을 알려줘"


def test_overseas_peak_month_query_routes_and_builds_sql_without_llm() -> None:
    capability = workflow._select_verified_query_capability(QUESTION, {})

    assert capability is not None
    assert capability["matched_query_name"] == "overseas_peak_month_usage"

    state = {
        "question": QUESTION,
        "user_provided_params": {},
        **capability,
    }
    with patch.object(workflow, "_call_llm", return_value="{}"):
        result = workflow.extract_and_apply_params(state)

    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {
        "기간_시작": "202601",
        "기간_종료": "202612",
    }
    # 해외 이용금액은 카드 월실적의 해외 일시불+CA 이용금액이다. tbdaabt08의 매출금액은
    # 가맹점이 받은 해외 매출이라 이용금액 질문의 원천이 아니다.
    assert 'FROM card_system.tmdaa3e16' in result["final_sql"]
    assert '"기준년월" BETWEEN \'202601\' AND \'202612\'' in result["final_sql"]
    assert 'f."개인기업구분코드" = \'2\'' in result["final_sql"]
    assert 'COALESCE(f."금월해외일시불이용금액", 0) + COALESCE(f."금월해외CA이용금액", 0)' in result["final_sql"]
    assert 'AS "이용금액"' in result["final_sql"]
    assert 'ORDER BY "이용금액" DESC' in result["final_sql"]
    assert "LIMIT 1" in result["final_sql"]
    assert not _validate_sql_against_schema(result["final_sql"], ["tmdaa3e16"])

"""generate_sql 의 프롬프트 조립 경로를 실제로 한 번 통과시킨다.

workflow 는 schema 의 컨텍스트 빌더에 `table_names` 를 넘긴다. 두 파일이 서로
다른 시점의 코드로 만나면(포팅 누락, 오래된 세션의 부분 reload) 런타임에서만
`find_relevant_queries() got an unexpected keyword argument 'table_names'` 로
터진다. 어떤 테스트도 generate_sql 을 부르지 않아 전체 pytest 가 통과했다.
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest

from text2sql_agent import schema, workflow

QUESTION = "2026년 5월 가맹점 수를 업종별로 알려줘"
TABLES = ["tmdaa5d01", "tbdaadb17"]


@pytest.mark.parametrize(
    "builder",
    [
        schema.find_relevant_queries,
        schema.find_relevant_references,
        schema.build_metrics_summary,
        schema.build_semantic_attributes_summary,
        schema.build_semantic_join_context,
    ],
)
def test_prompt_builders_accept_table_names(builder) -> None:
    assert "table_names" in inspect.signature(builder).parameters


def test_generate_sql_assembles_its_prompt() -> None:
    state = {
        "question": QUESTION,
        "retrieval_query": "",
        "selected_tables": TABLES,
        "table_details": workflow._table_details(TABLES, QUESTION),
        "selected_domain": "merchant_sales",
        "retry_count": 0,
    }
    with patch.object(workflow, "_call_llm", return_value="SELECT 1") as llm:
        result = workflow.generate_sql(state)

    assert result["generated_sql"]
    prompt = llm.call_args.args[0]
    for section in ("## 테이블 상세 정보", "## 안전 조인 그래프", "## SQL 작성 규칙"):
        assert section in prompt
    for table in TABLES:
        assert table in prompt


def test_generate_sql_retry_prompt_carries_previous_failure() -> None:
    state = {
        "question": QUESTION,
        "retrieval_query": "",
        "selected_tables": TABLES,
        "table_details": workflow._table_details(TABLES, QUESTION),
        "selected_domain": "merchant_sales",
        "retry_count": 1,
        "validation_result": "스키마에 없는 테이블 card_system.없는테이블 이 사용되었습니다.",
        "generated_sql": "SELECT 1 FROM card_system.없는테이블",
    }
    with patch.object(workflow, "_call_llm", return_value="SELECT 2") as llm:
        workflow.generate_sql(state)

    prompt = llm.call_args.args[0]
    assert "## 이전 시도 실패" in prompt
    assert "없는테이블" in prompt

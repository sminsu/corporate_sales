"""가맹점의 신규·재가입·해지는 상태가 아니라 사건이다. 기간은 사건 컬럼에 건다.

    지역별 신규 가맹점수는

이 질문이 tmdaa5d01."기준년월" = 'YYYYMM' 하나로 나갔다. 월 스냅샷의 기준년월은
읽을 월을 고르는 축이라, 그 조건만으로는 그 달에 살아있던 전체 가맹점이 나온다.
"신규" 를 실어나르는 컬럼은 따로 있다 — 가맹점신규년월일(time_role=event_date).

프롬프트 컬럼은 멀쩡히 살아 있었다. 빠진 건 규칙 두 줄이었다.

    1. SQL 작성 규칙 11번이 "신규/가입/등록 고객" 만 최초등록년월일로 못 박고
       가맹점 대응 규칙이 없었다.
    2. 적재 정책 지시문이 "일자 요청도 적재 기준 조건은 기준년월의 YYYYMM으로
       축약합니다" 라고 해서, 모델이 사용자의 기간을 적재 축 하나로 다 써버렸다.

지역 축은 또 다른 낟알이다. 한글시도명·한글시군구명은 가맹점요약(tbdaaus01·
tmdaaus01)에만 있고 사건 컬럼은 그 두 테이블에 없다. 한 테이블로는 못 내므로
라우팅이 양쪽을 함께 데려와야 한다.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import SCHEMA

REGION_TABLES = ("tbdaaus01", "tmdaaus01")
MERCHANT_EVENT_TABLES = ("tbdaadt01", "tmdaa5d01", "tmdaa5e11")
REGION_COLUMN = "한글시도명"
NEW_MERCHANT_COLUMN = "가맹점신규년월일"

NEW_MERCHANT_BY_REGION = [
    "지역별 신규 가맹점수는",
    "2026년 7월 지역별 신규 가맹점수 알려줘",
    "시도별 신규 가맹점 수 알려줘",
    "2026년 7월에 신규 가입한 가맹점 수를 지역별로 알려줘",
]


def _columns(table_name: str) -> set[str]:
    table = next(item for item in SCHEMA["tables"] if item["name"] == table_name)
    return {
        column["name"]
        for group in ("dimensions", "measures", "time_dimensions")
        for column in (table.get(group) or [])
    }


@pytest.mark.parametrize("question", NEW_MERCHANT_BY_REGION)
def test_new_merchant_questions_keep_both_the_event_and_the_region_column(
    question: str,
) -> None:
    details = workflow._table_details(workflow._rule_rank_tables(question), question)

    assert f"- {NEW_MERCHANT_COLUMN} [" in details, question
    assert f"- {REGION_COLUMN} [" in details, question


@pytest.mark.parametrize("question", NEW_MERCHANT_BY_REGION)
def test_new_merchant_questions_route_to_both_grains(question: str) -> None:
    """지역과 사건이 다른 테이블에 있다. 한쪽만 오면 조인할 상대가 없다."""
    ranked = set(workflow._rule_rank_tables(question))

    assert ranked & set(REGION_TABLES), (question, ranked)
    assert ranked & set(MERCHANT_EVENT_TABLES), (question, ranked)


def test_the_region_tables_do_not_carry_the_event_column() -> None:
    """조인이 필요한 이유. 이 사실이 깨지면 위 두 테스트의 전제가 바뀐다."""
    for table in REGION_TABLES:
        assert NEW_MERCHANT_COLUMN not in _columns(table), table
    for table in MERCHANT_EVENT_TABLES:
        assert REGION_COLUMN not in _columns(table), table
        assert NEW_MERCHANT_COLUMN in _columns(table), table


def test_the_monthly_load_rule_asks_for_a_separate_event_condition() -> None:
    instruction = workflow._accumulation_policy_instruction(["tmdaa5d01"])

    assert "기준년월" in instruction
    assert "event_date 컬럼의 조건으로 따로" in instruction
    assert "사건이 일어난 달을 뜻하지 않으므로" in instruction


def test_the_sql_rules_name_the_merchant_event_columns() -> None:
    question = NEW_MERCHANT_BY_REGION[1]
    tables = ["tmdaa5d01", "tmdaaus01"]
    state = {
        "question": question,
        "retrieval_query": "",
        "selected_tables": tables,
        "table_details": workflow._table_details(tables, question),
        "selected_domain": "merchant_sales",
        "retry_count": 0,
    }
    with patch.object(workflow, "_call_llm", return_value="SELECT 1") as llm:
        workflow.generate_sql(state)

    prompt = llm.call_args.args[0]
    for column in ("가맹점신규년월일", "가맹점재가입년월일", "가맹점해지년월일"):
        assert column in prompt, column
    assert "적재 축 조건은 읽을 월을 고를 뿐" in prompt

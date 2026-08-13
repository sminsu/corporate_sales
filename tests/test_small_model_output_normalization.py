"""소형 오픈웨이트 모델(gpt-oss/Gemma)의 실제 출력 형태에 대한 정규화 회귀 테스트."""

from __future__ import annotations

import pytest

from text2sql_agent import llm, workflow


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # gpt-oss harmony 채널이 텍스트로 이어붙어 나오는 형태
        ("analysisWe need to classify the question.assistantfinalneed_sql", "need_sql"),
        # harmony 특수 토큰이 렌더링되지 않고 그대로 나오는 형태
        (
            "<|channel|>analysis<|message|>internal reasoning<|end|>"
            "<|start|>assistant<|channel|>final<|message|>SELECT 1",
            "SELECT 1",
        ),
        ('analysis...assistantfinal{"기간_시작": "202501"}', '{"기간_시작": "202501"}'),
        # 일반 답변 속의 'analysis' 단어는 오탐하지 않는다
        ("이 분석(analysis) 결과는 다음과 같습니다.", "이 분석(analysis) 결과는 다음과 같습니다."),
        ("VALID", "VALID"),
    ],
)
def test_normalize_llm_text_handles_harmony_channel_leakage(raw: str, expected: str) -> None:
    assert llm._normalize_llm_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 세미콜론 뒤에 이어지는 설명 프로즈 제거
        ("SELECT a FROM t;\n\n설명: 이 쿼리는 월별 집계입니다.", "SELECT a FROM t;"),
        # 문자열 리터럴/주석 안의 세미콜론은 종결자가 아니다
        ("SELECT ';' AS x; extra prose", "SELECT ';' AS x;"),
        ("SELECT a -- 주석; 유지\nFROM t; 뒤 설명", "SELECT a -- 주석; 유지\nFROM t;"),
        ("SELECT a /* 주석; */ FROM t; extra", "SELECT a /* 주석; */ FROM t;"),
        # 종결자가 없으면 그대로 둔다
        ("SELECT a FROM t", "SELECT a FROM t"),
        # 코드 fence 안의 SQL도 종결자 이후를 제거
        ("```sql\nSELECT a FROM t;\n```\n이 쿼리는 ...", "SELECT a FROM t;"),
    ],
)
def test_extract_sql_truncates_trailing_prose_after_terminator(raw: str, expected: str) -> None:
    assert workflow._extract_sql_from_llm(raw) == expected


def test_empty_result_answer_is_actionable() -> None:
    result = workflow.generate_answer(
        {
            "question": "2099년 도미노피자 매출 알려줘",
            "final_sql": "SELECT 1",
            "query_columns": ["가맹점명"],
            "query_rows": [],
        }
    )
    answer = result["answer"]
    assert "0건" in answer
    assert "다시 시도" in answer

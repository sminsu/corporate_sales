from __future__ import annotations

import unittest
from unittest.mock import patch

from text2sql_agent import workflow


class WorkflowValidationTest(unittest.TestCase):
    def test_validate_sql_treats_user_params_as_explicit_conditions(self) -> None:
        prompts: list[str] = []

        def fake_call_llm(prompt: str, system: str | None = None) -> str:
            prompts.append(prompt)
            return (
                "- 날짜 조건 오류: 사용자 질문에 특정 시점에 대한 언급이 없음에도 "
                "SQL에서 SUBSTR(a.\"실적기준년월일\",1,6) = '202604' 라는 임의의 과거 날짜 조건이 포함되어 있습니다."
            )

        state = workflow._new_initial_state("파파존스 가맹점주 중에 KB 국민카드 기업카드 소지하고 있는 사람이 몇명이야?")
        state.update(
            {
                "generated_sql": (
                    'SELECT COUNT(DISTINCT a."회원일련번호") AS "기업카드소지회원수" '
                    'FROM card_system.tbdaadt01 g '
                    'JOIN card_system.tbdaaat03 a ON g."기업고객식별자" = a."고객식별자" '
                    "WHERE LOWER(g.\"가맹점명\") LIKE LOWER('%파파존스%') "
                    "AND a.\"개인기업구분코드\" = '2' "
                    "AND SUBSTR(a.\"실적기준년월일\",1,6) = '202604'"
                ),
                "selected_tables": ["tbdaadt01", "tbdaaat03"],
                "user_provided_params": {"기준년월": "202604"},
            }
        )

        with patch.object(workflow, "_validate_sql_against_schema", return_value=[]):
            with patch.object(workflow, "_call_llm", side_effect=fake_call_llm):
                result = workflow.validate_sql(state)

        self.assertTrue(result["is_valid"])
        self.assertEqual(result["validation_result"], "VALID")
        self.assertIn("추가 입력 파라미터:", prompts[0])
        self.assertIn("- 기준년월: 202604", prompts[0])


if __name__ == "__main__":
    unittest.main()

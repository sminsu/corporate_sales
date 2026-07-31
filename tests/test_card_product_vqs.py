from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

from text2sql_agent import workflow
from text2sql_agent.schema import SCHEMA, _validate_sql_against_schema, semantic_query_contract_candidates


VALID_COUNT_QUESTION = "현재기준 KB아시아나 기업카드 유효카드수 알려줘"
BRAND_COUNT_QUESTION = "2026년 6월 기준 기업카드의 브랜드별 유효 좌수를 알고 싶어"
ISSUED_CANCELLED_QUESTION = (
    "기업카드 발급을 26년 1,2,3월에 하고 4,5,6월 안에 해지한 경우의 "
    "카드좌수와 업체수를 알려줘"
)
NEW_CHECK_QUESTION = (
    "현재 kb카드 기업카드 보유회원중에 상반기 전기요금전용 체크카드를 "
    "신규로 몇좌 발급했는지와 리스트를 뽑아줘"
)


def test_card_product_questions_are_routed_deterministically() -> None:
    cases = [
        (
            VALID_COUNT_QUESTION,
            "KB아시아나",
            "card_product_current_valid_corporate_count",
            ["tbdaaat05", "tbdaada72"],
        ),
        (
            NEW_CHECK_QUESTION,
            "전기요금전용",
            "current_corporate_member_half_year_new_check_card_issuance",
            ["tbdaaat03", "tbdaaat05", "tbdaada72", "tbdaa1d12"],
        ),
    ]

    for question, product_name, vq_name, tables in cases:
        contracts = semantic_query_contract_candidates(SCHEMA, question, max_count=1)
        capability = workflow._select_verified_query_capability(question, {})

        assert workflow._rule_classify_question(question) is True
        assert workflow._extract_card_product_name_by_rule(question) == product_name
        assert contracts[0]["name"] == vq_name
        assert workflow._reference_domain_by_rule(question) == "customer_card_portfolio"
        assert workflow._rule_rank_tables(question) == tables
        assert capability is not None
        assert capability["matched_query_name"] == vq_name


def test_current_product_valid_card_count_sql_is_built_without_llm() -> None:
    capability = workflow._select_verified_query_capability(VALID_COUNT_QUESTION, {})
    assert capability is not None

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {
                "question": VALID_COUNT_QUESTION,
                **capability,
                "user_provided_params": {},
            }
        )

    sql = result["final_sql"]
    assert result["param_stage"] == "done"
    assert result["extracted_params"]["상품명"] == "KB아시아나"
    assert 'MAX(c."실적기준년월일")' in sql
    assert 'c."개인기업구분코드" = \'2\'' in sql
    assert 'c."유효신용카드여부" = \'1\'' in sql
    assert 'c."유효체크카드여부" = \'1\'' in sql
    assert "CONCAT('%', 'KB아시아나', '%')" in sql
    assert 'AS "유효카드수"' in sql
    assert _validate_sql_against_schema(sql, ["tbdaaat05", "tbdaada72"]) == []


def test_monthly_corporate_valid_card_count_by_brand_is_built_without_llm() -> None:
    contracts = semantic_query_contract_candidates(SCHEMA, BRAND_COUNT_QUESTION, max_count=1)
    capability = workflow._select_verified_query_capability(BRAND_COUNT_QUESTION, {})

    assert workflow._rule_classify_question(BRAND_COUNT_QUESTION) is True
    assert workflow._extract_period_by_rule(BRAND_COUNT_QUESTION) == ("202606", "202606", "")
    assert contracts[0]["name"] == "corporate_valid_card_count_by_brand_at_month"
    assert workflow._reference_domain_by_rule(BRAND_COUNT_QUESTION) == "customer_card_portfolio"
    assert workflow._rule_rank_tables(BRAND_COUNT_QUESTION) == ["tbdaaat05"]
    assert capability is not None
    assert capability["matched_query_name"] == "corporate_valid_card_count_by_brand_at_month"

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {
                "question": BRAND_COUNT_QUESTION,
                **capability,
                "user_provided_params": {},
            }
        )

    sql = result["final_sql"]
    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {"기준년월": "202606"}
    assert "BETWEEN CONCAT('202606', '01') AND CONCAT('202606', '31')" in sql
    assert 'c."개인기업구분코드" = \'2\'' in sql
    assert 'c."유효신용카드여부" = \'1\'' in sql
    assert 'c."유효체크카드여부" = \'1\'' in sql
    assert 'WHEN \'1\' THEN \'로칼(국민)\'' in sql
    assert 'WHEN \'4\' THEN \'JCB\'' in sql
    assert 'AS "유효좌수"' in sql
    assert _validate_sql_against_schema(sql, ["tbdaaat05"]) == []


def test_corporate_cards_issued_then_cancelled_count_is_built_without_llm() -> None:
    contracts = semantic_query_contract_candidates(SCHEMA, ISSUED_CANCELLED_QUESTION, max_count=1)
    capability = workflow._select_verified_query_capability(ISSUED_CANCELLED_QUESTION, {})

    assert workflow._rule_classify_question(ISSUED_CANCELLED_QUESTION) is True
    assert workflow._extract_card_issue_cancel_periods_by_rule(ISSUED_CANCELLED_QUESTION) == (
        "202601",
        "202603",
        "202604",
        "202606",
    )
    assert contracts[0]["name"] == "corporate_cards_issued_then_cancelled_count"
    assert workflow._reference_domain_by_rule(ISSUED_CANCELLED_QUESTION) == "customer_card_portfolio"
    assert workflow._rule_rank_tables(ISSUED_CANCELLED_QUESTION) == ["tbdaaat03", "tbdaaat05"]
    assert capability is not None
    assert capability["matched_query_name"] == "corporate_cards_issued_then_cancelled_count"

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {
                "question": ISSUED_CANCELLED_QUESTION,
                **capability,
                "user_provided_params": {},
            }
        )

    sql = result["final_sql"]
    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {
        "발급기간_시작": "202601",
        "발급기간_종료": "202603",
        "해지기간_시작": "202604",
        "해지기간_종료": "202606",
    }
    assert "BETWEEN CONCAT('202601', '01') AND CONCAT('202603', '31')" in sql
    assert "BETWEEN CONCAT('202604', '01') AND CONCAT('202606', '31')" in sql
    assert 'c."개인기업구분코드" = \'2\'' in sql
    assert 'COUNT(m."카드구분키번호") AS "카드좌수"' in sql
    assert 'COUNT(DISTINCT m."고객식별자") AS "업체수"' in sql
    assert _validate_sql_against_schema(sql, ["tbdaaat03", "tbdaaat05"]) == []


def test_half_year_new_check_card_issuance_sql_is_built_without_llm() -> None:
    capability = workflow._select_verified_query_capability(NEW_CHECK_QUESTION, {})
    assert capability is not None

    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        result = workflow.extract_and_apply_params(
            {
                "question": NEW_CHECK_QUESTION,
                **capability,
                "user_provided_params": {},
            }
        )

    year = datetime.now().year
    sql = result["final_sql"]
    assert result["param_stage"] == "done"
    assert result["extracted_params"] == {
        "상품명": "전기요금전용",
        "기간_시작": f"{year}01",
        "기간_종료": f"{year}06",
    }
    assert f"CONCAT('{year}01', '01')" in sql
    assert f"CONCAT('{year}06', '31')" in sql
    assert "CONCAT('%', '전기요금전용', '%')" in sql
    assert 'c."상품중분류구분코드" IN (' in sql
    assert "'CP53'" in sql
    assert "'CP54'" in sql
    assert 'AS "신규발급좌수"' in sql
    assert "LIMIT 500" in sql
    assert _validate_sql_against_schema(
        sql,
        ["tbdaaat03", "tbdaaat05", "tbdaada72", "tbdaa1d12"],
    ) == []

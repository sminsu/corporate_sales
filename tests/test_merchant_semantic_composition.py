from __future__ import annotations

from unittest.mock import patch

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import (
    SCHEMA,
    _validate_sql_against_schema,
    build_semantic_attributes_summary,
    resolve_semantic_attribute_value,
    semantic_query_contract_candidates,
)


def _offline_vq_result(question: str) -> dict:
    capability = workflow._select_verified_query_capability(question, {})
    assert capability is not None
    with patch.object(workflow, "_call_llm", side_effect=RuntimeError("offline")):
        return workflow.extract_and_apply_params(
            {
                "question": question,
                **capability,
                "user_provided_params": {},
            }
        )


def test_semantic_attribute_mappings_reference_real_columns() -> None:
    tables = {
        str(table.get("physical_table") or table["name"]).rsplit(".", 1)[-1]: table
        for table in SCHEMA["tables"]
    }

    issues = []
    for attribute in SCHEMA["semantic_attributes"]:
        for mapping in attribute.get("source_mappings", []):
            table_name = str(mapping.get("table") or "").rsplit(".", 1)[-1]
            table = tables.get(table_name)
            if table is None:
                issues.append(f"{attribute['name']}: unknown table {table_name}")
                continue
            known_columns = {
                column["name"]
                for section in ("dimensions", "measures", "time_dimensions")
                for column in table.get(section, [])
            }
            columns = mapping.get("columns", mapping.get("column", []))
            if isinstance(columns, str):
                columns = [columns]
            for column in columns:
                if column not in known_columns:
                    issues.append(f"{attribute['name']}: unknown {table_name}.{column}")

    assert issues == []


def test_payment_institution_is_a_reusable_semantic_attribute() -> None:
    attribute = next(
        item
        for item in SCHEMA["semantic_attributes"]
        if item["name"] == "merchant_payment_institution"
    )

    assert attribute["parameter_name"] == "결제금융기관코드"
    assert attribute["value_semantics"]["004"]["label"] == "KB국민은행"
    assert resolve_semantic_attribute_value(
        SCHEMA,
        "merchant_payment_institution",
        "가맹점 계좌가 국민은행인 곳",
    ) == "004"
    assert resolve_semantic_attribute_value(
        SCHEMA,
        "결제금융기관코드",
        "결제은행이 KB국민은행인 가맹점",
    ) == "004"

    context = build_semantic_attributes_summary(
        SCHEMA,
        "파파존스 가맹점 계좌가 KB국민은행인 가맹점 수",
        "merchant_sales",
    )
    assert "가맹점 결제금융기관" in context
    assert "현금카드결제기관구분코드" in context
    assert '"004"' in context
    assert "계좌번호가 아니라" in context


def test_yymm_shorthand_is_resolved_generically() -> None:
    assert workflow._extract_period_by_rule("2605 기준 매출")[:2] == ("202605", "202605")
    assert workflow._extract_ym_from_question("2605 기준") == "202605"
    assert workflow._extract_period_by_rule("2605")[:2] == ("202605", "202605")
    assert workflow._has_time_expression("2605 기준 가맹점 매출액") is True


@pytest.mark.parametrize(
    ("question", "contract_name", "query_name", "tables"),
    [
        (
            "꾸석지 가맹점 중에 기업카드 소지하고 있는 사람 기업고객식별자 리스트 알려줘(2605 기준)",
            "brand_merchant_corporate_card_customer_list",
            "brand_merchants_with_corporate_card",
            ["tbdaaus01"],
        ),
        (
            "마초스테이크하우스 가맹점 기본 정보 알려줘",
            "merchant_profile_by_name",
            "merchant_detail_by_name",
            ["tbdaadt01"],
        ),
        (
            "도미노피자 가맹점주의 2605 기준 가맹점 매출액 알려줘",
            "named_merchant_store_sales_at_month",
            "merchant_named_store_sales_at_month",
            ["tmdaa5e11"],
        ),
        (
            "파파존스 가맹점주 가맹점 계좌 알려줘",
            "merchant_payment_institution_list",
            "merchant_payment_institution_list",
            ["tbdaadt01"],
        ),
        (
            "파파존스 가맹점주 중 가맹점 계좌가 KB국민은행인 가맹점 수 알려줘",
            "merchant_count_by_payment_institution",
            "merchant_count_by_payment_institution",
            ["tbdaadt01"],
        ),
        (
            "꾸석지 가맹점주 2605 기준 대손율 알려줘",
            "merchant_portfolio_allowance_rate",
            "merchant_portfolio_allowance_rate",
            ["tmdaa5d01", "tbmewcm94"],
        ),
    ],
)
def test_merchant_questions_compose_through_semantic_contracts(
    question: str,
    contract_name: str,
    query_name: str,
    tables: list[str],
) -> None:
    contracts = semantic_query_contract_candidates(SCHEMA, question, max_count=2)
    capability = workflow._select_verified_query_capability(question, {})

    assert contracts
    assert contracts[0]["name"] == contract_name
    assert capability is not None
    assert capability["matched_query_name"] == query_name
    assert workflow._rule_rank_tables(question) == tables

    result = _offline_vq_result(question)
    assert result["param_stage"] == "done"
    assert _validate_sql_against_schema(result["final_sql"], tables) == []


def test_composed_sql_uses_correct_grains_roles_and_code_values() -> None:
    customer_list = _offline_vq_result(
        "꾸석지 가맹점 중 기업카드 보유 기업고객식별자 목록 알려줘. 2605 기준"
    )
    customer_sql = customer_list["final_sql"]
    assert customer_list["extracted_params"]["기준년월"] == "202605"
    assert 'SELECT DISTINCT r."기업고객식별자"' in customer_sql
    assert 'COALESCE(r."유효기업신용카드수", 0)' in customer_sql
    assert 'COALESCE(r."유효기업체크카드수", 0)' in customer_sql
    assert "유효기업개별카드수" not in customer_sql
    assert "유효기업공용카드수" not in customer_sql

    sales = _offline_vq_result(
        "도미노피자 가맹점주의 2605 기준 가맹점 매출액 알려줘"
    )
    sales_sql = sales["final_sql"]
    assert sales["extracted_params"]["기준년월"] == "202605"
    assert 's."가맹점번호"' in sales_sql
    assert 'COALESCE(s."가맹점일시불매출금액", 0)' in sales_sql
    assert 'COALESCE(s."가맹점할부매출금액", 0)' in sales_sql

    bank_count = _offline_vq_result(
        "파파존스 가맹점주 중 가맹점 계좌가 KB국민은행인 가맹점 수 알려줘"
    )
    bank_sql = bank_count["final_sql"]
    assert bank_count["extracted_params"]["결제금융기관코드"] == "004"
    assert 'COUNT(DISTINCT m."가맹점번호")' in bank_sql
    assert 'm."현금카드결제기관구분코드" = \'004\'' in bank_sql
    assert "대표고객식별자" not in bank_sql


def test_bare_merchant_bad_debt_rate_requests_only_the_snapshot_month() -> None:
    question = "꾸석지 가맹점주 대손율 알려줘"
    result = _offline_vq_result(question)

    assert result["param_stage"] == "need_params"
    assert result["extracted_params"] == {"가맹점명": "꾸석지"}
    assert [item["name"] for item in result["missing_params"]] == ["기준년월"]
    assert "LS" not in {item["name"] for item in result["missing_params"]}
    assert "IS" not in {item["name"] for item in result["missing_params"]}


def test_merchant_allowance_rate_deduplicates_customers_before_risk_join() -> None:
    result = _offline_vq_result("꾸석지 가맹점주 2605 기준 대손율 알려줘")
    sql = result["final_sql"]

    assert 'SELECT DISTINCT m."기업고객식별자"' in sql
    assert 'p."고객식별자" = c."기업고객식별자"' in sql
    assert sql.count('"기준년월" = \'202605\'') == 2
    assert 'p."개인기업구분코드" = \'2\'' in sql
    assert 'SUM(p."기대대손충당금")' in sql
    assert 'SUM(p."원화대출잔액")' in sql
    assert "대표고객식별자" not in sql

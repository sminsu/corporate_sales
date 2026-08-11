from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import (
    SCHEMA,
    build_semantic_attributes_summary,
    semantic_query_contract_candidates,
)


ROOT = Path(__file__).resolve().parents[1]
GOLDENSET = ROOT / "tests" / "fixtures" / "corporate_sales_text2sql_goldenset_v2.jsonl"


def _golden_cases() -> dict[str, dict]:
    return {
        case["id"]: case
        for line in GOLDENSET.read_text(encoding="utf-8").splitlines()
        if (case := json.loads(line))
    }


# 확인 필요-2.xlsx의 20개 실패 질의를 기존 정답 SQL fixture ID로 고정한다.
XLSX_FAILURE_CASES = [
    ("cs-golden-v2-0276", "monthly_card_metric_by_card_attribute", ["tmdaa3e16"], ["기준년월", "개인기업구분코드", "상품중분류구분코드", "금월일시불이용금액"]),
    ("cs-golden-v2-0277", "monthly_corporate_metric_by_customer_attribute", ["tbdaa1d12"], ["기준년월일", "고객식별자", "표준산업분류코드", "유효기업신용카드수", "유효기업체크카드수"]),
    ("cs-golden-v2-0278", "daily_delinquency_metric_by_dimension", ["tddaa3l01"], ["기준년월일", "개인기업구분코드", "회원자산관리부점코드", "연체원금"]),
    ("cs-golden-v2-0248", "daily_delinquency_metric_by_dimension", ["tddaa3l01"], ["기준년월일", "개인기업구분코드", "연체관리부점코드", "연체원금"]),
    ("cs-golden-v2-0241", "monthly_corporate_metric_by_customer_attribute", ["tbdaa1d12"], ["기준년월일", "고객식별자", "표준산업분류코드", "금월신용카드이용금액", "금월체크카드이용금액"]),
    ("cs-golden-v2-0272", "monthly_card_metric_by_card_attribute", ["tmdaa3e16"], ["기준년월", "개인기업구분코드", "카드등급그룹코드", "금월이용합계건수"]),
    ("cs-golden-v2-0273", "monthly_corporate_metric_by_customer_attribute", ["tbdaa1d12"], ["기준년월일", "고객식별자", "그룹최종평가신용등급코드", "표준산업분류코드", "금월국세이용금액"]),
    ("cs-golden-v2-0267", "merchant_monthly_sales_count_above_average_by_industry", ["tmdaa5e11", "tbdaadb17"], ["기준년월", "가맹점업종코드", "가맹점업종명", "가맹점일시불매출건수", "가맹점할부매출건수"]),
    ("cs-golden-v2-0268", "monthly_card_metric_by_card_attribute", ["tmdaa3e16"], ["기준년월", "개인기업구분코드", "카드브랜드구분코드", "회원자격코드", "금월일시불이용건수"]),
    ("cs-golden-v2-0269", "monthly_corporate_metric_by_customer_attribute", ["tbdaa1d12"], ["기준년월일", "고객식별자", "소매비소매구분코드", "그룹최고고객구분코드", "금월신용카드이용금액", "금월체크카드이용금액"]),
    ("cs-golden-v2-0281", "monthly_corporate_metric_by_customer_attribute", ["tbdaa1d12"], ["기준년월일", "고객식별자", "사업자자산건전성구분코드", "금월KB페이이용금액"]),
    ("cs-golden-v2-0282", "daily_delinquency_metric_by_dimension", ["tddaa3l01"], ["기준년월일", "개인기업구분코드", "카드브랜드구분코드", "연체수수료"]),
    ("cs-golden-v2-0293", "monthly_corporate_metric_by_customer_attribute", ["tbdaa1d12"], ["기준년월일", "고객식별자", "소매비소매구분코드", "유효기업신용카드수", "유효기업체크카드수"]),
    ("cs-golden-v2-0294", "daily_delinquency_metric_by_dimension", ["tddaa3l01"], ["기준년월일", "개인기업구분코드", "회원자산관리부점코드", "연체합계금액"]),
    ("cs-golden-v2-0304", "current_valid_credit_card_count_by_grade", ["tbdaaat03"], ["개인기업구분코드", "카드등급구분코드", "유효신용카드수"]),
    ("cs-golden-v2-0308", "daily_delinquency_metric_by_dimension", ["tddaa3l01"], ["기준년월일", "개인기업구분코드", "회원자산관리부점코드", "회원일련번호", "채권잔액"]),
    ("cs-golden-v2-0313", "daily_delinquency_metric_by_dimension", ["tddaa3l01"], ["기준년월일", "개인기업구분코드", "연체관리부점코드", "연체료"]),
    ("cs-golden-v2-0345", "merchant_monthly_lumpsum_sales_share_by_status", ["tmdaa5e11"], ["기준년월", "가맹점상태구분코드", "가맹점일시불매출금액"]),
    ("cs-golden-v2-0367", "monthly_corporate_metric_by_customer_attribute", ["tbdaa1d12"], ["기준년월일", "고객식별자", "카드결제기관구분코드", "유효기업신용카드수", "유효기업체크카드수"]),
    ("cs-golden-v2-0368", "daily_delinquency_metric_by_dimension", ["tddaa3l01"], ["기준년월일", "개인기업구분코드", "회원자산관리부점코드", "연체료"]),
]


@pytest.mark.parametrize(
    ("case_id", "contract_name", "expected_tables", "required_columns"),
    XLSX_FAILURE_CASES,
)
def test_xlsx_failure_routes_to_authoritative_contract_with_required_columns(
    case_id: str,
    contract_name: str,
    expected_tables: list[str],
    required_columns: list[str],
) -> None:
    case = _golden_cases()[case_id]
    question = case["question"]
    candidates = semantic_query_contract_candidates(SCHEMA, question, max_count=2)

    assert candidates
    assert candidates[0]["name"] == contract_name
    assert candidates[0]["table_selection_mode"] == "authoritative"
    assert workflow._reference_domain_by_rule(question) == case["domain"]
    assert set(workflow._rule_rank_tables(question)) == set(expected_tables)
    assert set(case["expected_tables"]) == set(expected_tables)
    assert workflow._rule_classify_question(question)

    details = workflow._table_details(expected_tables, question)
    for column in required_columns:
        assert f"- {column} [" in details, f"{case_id}: {column} missing from prompt context"


@pytest.mark.parametrize(
    ("case_id", "contract_name", "expected_tables", "required_columns"),
    XLSX_FAILURE_CASES,
)
def test_xlsx_failure_authoritative_analysis_does_not_call_llm_table_selector(
    case_id: str,
    contract_name: str,
    expected_tables: list[str],
    required_columns: list[str],
) -> None:
    del contract_name, required_columns
    case = _golden_cases()[case_id]
    with patch.object(workflow, "_call_llm", side_effect=AssertionError("LLM table selector called")):
        analysis = workflow.analyze_question(
            {"question": case["question"], "selected_domain": case["domain"]}
        )

    assert set(analysis["selected_tables"]) == set(expected_tables)
    assert "tsmagcca1" not in analysis["selected_tables"]


def test_xlsx_failure_metrics_have_canonical_formulas() -> None:
    metrics = {metric["name"]: metric for metric in SCHEMA["canonical_metrics"]}
    expected = {
        "카드월일시불이용금액",
        "카드월이용합계건수",
        "카드월일시불이용건수",
        "기업월국세이용금액",
        "기업월KB페이이용금액",
        "가맹점월매출건수",
        "가맹점월일시불매출금액",
        "연체원금",
        "연체수수료",
        "연체료",
        "연체합계금액",
        "채권잔액",
        "회원당평균채권잔액",
    }

    assert expected <= metrics.keys()
    assert "가맹점일시불매출건수" in metrics["가맹점월매출건수"]["expression"]
    assert "가맹점할부매출건수" in metrics["가맹점월매출건수"]["expression"]
    assert "COUNT(DISTINCT" in metrics["회원당평균채권잔액"]["expression"]
    assert metrics["기업유효카드수"]["expression"].startswith("SUM(")


def test_credit_risk_card_brand_semantics_are_visible() -> None:
    context = build_semantic_attributes_summary(
        SCHEMA,
        "2026년 3월 31일 기준 연체수수료를 카드브랜드별 비중으로 보여줘",
        "credit_risk",
    )

    assert "card_brand" in context
    assert "tddaa3l01" in context
    assert "카드브랜드구분코드" in context


def test_bond_concept_explanation_is_not_forced_to_sql() -> None:
    assert not workflow._rule_classify_question("기업 채권이 무엇인지 설명해줘")


def test_merchant_industry_contract_exposes_its_curated_join() -> None:
    question = "2026년 3월 가맹점매출건수가 전체 평균보다 높은 가맹점 업종명만 골라줘"
    details = workflow._table_details(["tmdaa5e11", "tbdaadb17"], question)

    assert 'tmdaa5e11."가맹점업종코드" = tbdaadb17."가맹점업종코드"' in details


def test_refined_question_keeps_contract_column_budget_and_evidence() -> None:
    retrieval_question = "표준산업분류별로 2025년 9월 기업카드이용금액을 보여줘"
    with patch.object(workflow, "_call_llm", side_effect=AssertionError("LLM table selector called")):
        analysis = workflow.analyze_question(
            {
                "question": "그 기준으로 다시 보여줘",
                "retrieval_query": retrieval_question,
                "selected_domain": "corporate_sales_targeting",
            }
        )

    assert analysis["selected_tables"] == ["tbdaa1d12"]
    assert "- 금월신용카드이용금액 [" in analysis["table_details"]
    assert "- 금월체크카드이용금액 [" in analysis["table_details"]


def test_delinquency_contract_requires_a_single_day_and_excludes_personal_scope() -> None:
    monthly = "2026년 6월 회원자산관리부점별 채권잔액을 알려줘"
    personal = "2026년 6월 30일 기준 개인회원의 회원자산관리부점별 채권잔액을 알려줘"

    assert "daily_delinquency_metric_by_dimension" not in {
        item["name"] for item in semantic_query_contract_candidates(SCHEMA, monthly, max_count=4)
    }
    assert "daily_delinquency_metric_by_dimension" not in {
        item["name"] for item in semantic_query_contract_candidates(SCHEMA, personal, max_count=4)
    }

    metrics = {metric["name"]: metric for metric in SCHEMA["canonical_metrics"]}
    assert metrics["연체원금"]["aggregation_behavior"] == (
        "semi_additive_single_snapshot_then_aggregate"
    )

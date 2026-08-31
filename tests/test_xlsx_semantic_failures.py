from __future__ import annotations

from unittest.mock import patch

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import (
    SCHEMA,
    build_semantic_attributes_summary,
    semantic_query_contract_candidates,
)


# 확인 필요-2.xlsx의 20개 실패 질의를 질의문 그대로 고정한다.
# 골든셋 fixture는 통째로 재생성되면서 id와 질의의 짝이 바뀌므로, id로 걸어두면
# 어느 순간 조용히 다른 질의를 검사하게 된다. 여기서 필요한 건 질의문과 도메인뿐이다.
XLSX_FAILURE_CASES = [
    (
        "2025년 9월 금월일시불이용금액이 전체 평균보다 높은 상품중분류만 골라줘",
        "card_usage",
        "monthly_card_metric_by_card_attribute",
        ["tmdaa3e16"],
        ["기준년월", "개인기업구분코드", "상품중분류구분코드", "금월일시불이용금액"],
    ),
    (
        "2025년 6월 표준산업분류별 유효기업카드수를 알려줘",
        "corporate_sales_targeting",
        "monthly_corporate_metric_by_customer_attribute",
        ["tmdaa1d12"],
        ["기준년월일", "고객식별자", "표준산업분류코드", "유효기업신용카드수", "유효기업체크카드수"],
    ),
    (
        "2026년 4월 30일 기준 회원자산관리부점별 연체원금 순위를 5위까지 매겨줘",
        "credit_risk",
        "daily_delinquency_metric_by_dimension",
        ["tddaa3l01"],
        ["기준년월일", "개인기업구분코드", "회원자산관리부점코드", "연체원금"],
    ),
    (
        "2026년 5월 31일 기준 연체관리부점별 연체원금을 알려줘",
        "credit_risk",
        "daily_delinquency_metric_by_dimension",
        ["tddaa3l01"],
        ["기준년월일", "개인기업구분코드", "연체관리부점코드", "연체원금"],
    ),
    (
        "표준산업분류별로 2025년 9월 기업카드이용금액을 보여줘",
        "corporate_sales_targeting",
        "monthly_corporate_metric_by_customer_attribute",
        ["tmdaa1d12"],
        ["기준년월일", "고객식별자", "표준산업분류코드", "금월신용카드이용금액", "금월체크카드이용금액"],
    ),
    (
        "2025년 11월 카드등급그룹별 금월이용합계건수가 100건 이상인 것만 보여줘",
        "card_usage",
        "monthly_card_metric_by_card_attribute",
        ["tmdaa3e16"],
        ["기준년월", "개인기업구분코드", "카드등급그룹코드", "금월이용합계건수"],
    ),
    (
        "2026년 5월 그룹 최종평가 신용등급과 표준산업분류를 교차해서 금월국세이용금액을 보여줘",
        "corporate_sales_targeting",
        "monthly_corporate_metric_by_customer_attribute",
        ["tmdaa1d12"],
        ["기준년월일", "고객식별자", "그룹최종평가신용등급코드", "표준산업분류코드", "금월국세이용금액"],
    ),
    (
        "2026년 3월 가맹점매출건수가 전체 평균보다 높은 가맹점 업종명만 골라줘",
        "merchant_sales",
        "merchant_monthly_sales_count_above_average_by_industry",
        ["tmdaa5e11", "tbdaadb17"],
        ["기준년월", "가맹점업종코드", "가맹점업종명", "가맹점일시불매출건수", "가맹점할부매출건수"],
    ),
    (
        "2025년 8월 카드브랜드와 회원자격을 교차해서 금월일시불이용건수를 보여줘",
        "card_usage",
        "monthly_card_metric_by_card_attribute",
        ["tmdaa3e16"],
        ["기준년월", "개인기업구분코드", "카드브랜드구분코드", "회원자격코드", "금월일시불이용건수"],
    ),
    (
        "2025년 8월 소매·비소매 구분과 그룹 최고고객 구분을 교차해서 기업카드이용금액을 보여줘",
        "corporate_sales_targeting",
        "monthly_corporate_metric_by_customer_attribute",
        ["tmdaa1d12"],
        ["기준년월일", "고객식별자", "소매비소매구분코드", "그룹최고고객구분코드", "금월신용카드이용금액", "금월체크카드이용금액"],
    ),
    (
        "2026년 6월 금월KB페이이용금액이 전체 평균보다 높은 사업자 자산건전성만 골라줘",
        "corporate_sales_targeting",
        "monthly_corporate_metric_by_customer_attribute",
        ["tmdaa1d12"],
        ["기준년월일", "고객식별자", "사업자자산건전성구분코드", "금월KB페이이용금액"],
    ),
    (
        "2026년 3월 31일 기준 연체수수료를 카드브랜드별 비중으로 나눠서 보여줘",
        "credit_risk",
        "daily_delinquency_metric_by_dimension",
        ["tddaa3l01"],
        ["기준년월일", "개인기업구분코드", "카드브랜드구분코드", "연체수수료"],
    ),
    (
        "2025년 5월 소매·비소매 구분별 유효기업카드수가 500 이상인 것만 보여줘",
        "corporate_sales_targeting",
        "monthly_corporate_metric_by_customer_attribute",
        ["tmdaa1d12"],
        ["기준년월일", "고객식별자", "소매비소매구분코드", "유효기업신용카드수", "유효기업체크카드수"],
    ),
    (
        "2025년 9월 30일 기준 연체합계금액을 회원자산관리부점별 비중으로 나눠서 보여줘",
        "credit_risk",
        "daily_delinquency_metric_by_dimension",
        ["tddaa3l01"],
        ["기준년월일", "개인기업구분코드", "회원자산관리부점코드", "연체합계금액"],
    ),
    (
        "현재 기준 유효신용카드수가 가장 많은 카드등급 상위 10개를 뽑아줘",
        "customer_card_portfolio",
        "current_valid_credit_card_count_by_grade",
        ["tbdaaat03"],
        ["개인기업구분코드", "카드등급구분코드", "유효신용카드수"],
    ),
    (
        "2026년 6월 30일 기준 회원자산관리부점별 회원당 평균 채권잔액을 알려줘",
        "credit_risk",
        "daily_delinquency_metric_by_dimension",
        ["tddaa3l01"],
        ["기준년월일", "개인기업구분코드", "회원자산관리부점코드", "회원일련번호", "채권잔액"],
    ),
    (
        "2026년 4월 30일 기준 연체관리부점별 연체료 상위 50개를 알려줘",
        "credit_risk",
        "daily_delinquency_metric_by_dimension",
        ["tddaa3l01"],
        ["기준년월일", "개인기업구분코드", "연체관리부점코드", "연체료"],
    ),
    (
        "2025년 9월 가맹점상태별 가맹점일시불매출금액 구성비를 알려줘",
        "merchant_sales",
        "merchant_monthly_lumpsum_sales_share_by_status",
        ["tmdaa5e11"],
        ["기준년월", "가맹점상태구분코드", "가맹점일시불매출금액"],
    ),
    (
        "2025년 3월 카드결제기관별 유효기업카드수를 알려줘",
        "corporate_sales_targeting",
        "monthly_corporate_metric_by_customer_attribute",
        ["tmdaa1d12"],
        ["기준년월일", "고객식별자", "카드결제기관구분코드", "유효기업신용카드수", "유효기업체크카드수"],
    ),
    (
        "2026년 3월 31일 기준 연체료를 회원자산관리부점 기준으로 집계해줘",
        "credit_risk",
        "daily_delinquency_metric_by_dimension",
        ["tddaa3l01"],
        ["기준년월일", "개인기업구분코드", "회원자산관리부점코드", "연체료"],
    ),
]

XLSX_FAILURE_IDS = [
    f"{index:02d}-{case[2]}" for index, case in enumerate(XLSX_FAILURE_CASES, start=1)
]


@pytest.mark.parametrize(
    ("question", "domain", "contract_name", "expected_tables", "required_columns"),
    XLSX_FAILURE_CASES,
    ids=XLSX_FAILURE_IDS,
)
def test_xlsx_failure_routes_to_authoritative_contract_with_required_columns(
    question: str,
    domain: str,
    contract_name: str,
    expected_tables: list[str],
    required_columns: list[str],
) -> None:
    candidates = semantic_query_contract_candidates(SCHEMA, question, max_count=2)

    assert candidates
    assert candidates[0]["name"] == contract_name
    assert candidates[0]["table_selection_mode"] == "authoritative"
    assert workflow._reference_domain_by_rule(question) == domain
    assert set(workflow._rule_rank_tables(question)) == set(expected_tables)
    assert workflow._rule_classify_question(question)

    details = workflow._table_details(expected_tables, question)
    for column in required_columns:
        assert f"- {column} [" in details, f"{question}: {column} missing from prompt context"


@pytest.mark.parametrize(
    ("question", "domain", "contract_name", "expected_tables", "required_columns"),
    XLSX_FAILURE_CASES,
    ids=XLSX_FAILURE_IDS,
)
def test_xlsx_failure_authoritative_analysis_does_not_call_llm_table_selector(
    question: str,
    domain: str,
    contract_name: str,
    expected_tables: list[str],
    required_columns: list[str],
) -> None:
    del contract_name, required_columns
    with patch.object(workflow, "_call_llm", side_effect=AssertionError("LLM table selector called")):
        analysis = workflow.analyze_question({"question": question, "selected_domain": domain})

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

    assert analysis["selected_tables"] == ["tmdaa1d12"]
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

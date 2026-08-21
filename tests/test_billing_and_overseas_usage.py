"""해외 이용금액은 카드 월실적, 청구금액은 청구금액 컬럼을 본다.

이용금액/매출금액 구분 규칙이 해외와 청구에서 두 군데 새고 있었다.

  "해외 이용금액" → tbdaabt08(해외 매출전표)
    해외매출금액 지표가 해외이용금액·해외사용액을 동의어로 들고 있어서 _rule_rank_tables
    가 tbdaabt08 에 +20 을, tbdaabt30 은 table synonym "이용금액" 으로 +14 를 받았다.
    해외 이용금액은 tmdaa3e16 의 금월해외일시불이용금액 + 금월해외CA이용금액 이다.

  "청구금액" → 금월이용합계금액
    청구금액 컬럼을 가진 테이블은 tmdaa3e16 하나인데 컬럼명이 "금월청구금액" 이라
    질문 표면형과 맞지 않고 지표·용어도 없었다. 도메인 라우팅은 merchant_sales 로 빠지고
    프롬프트에 청구 컬럼이 한 줄도 안 실려, 모델이 가장 가까운 이용금액 컬럼을 골랐다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from text2sql_agent import schema, workflow

ROOT = Path(__file__).resolve().parents[1]
RAW_SEMANTIC = yaml.safe_load((ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))
METRICS = {item["name"]: item for item in RAW_SEMANTIC["canonical_metrics"]}
GLOSSARY = {item["term"]: item for item in RAW_SEMANTIC["glossary"]}
DOMAINS = {item["name"]: item for item in RAW_SEMANTIC["canonical_domains"]}
REFERENCES = {item["intent"]: item for item in RAW_SEMANTIC["query_references"]}


def test_overseas_sales_metric_no_longer_claims_usage_wording() -> None:
    metric = METRICS["해외매출금액"]

    assert "해외이용금액" not in metric["synonyms"]
    assert "해외사용액" not in metric["synonyms"]
    assert any("카드월해외이용금액" in caution for caution in metric["semantic_cautions"])


def test_overseas_card_usage_metric_sums_the_two_overseas_columns() -> None:
    metric = METRICS["카드월해외이용금액"]

    assert metric["source_table"] == "tmdaa3e16"
    assert metric["expression"] == (
        'SUM(COALESCE(tmdaa3e16."금월해외일시불이용금액", 0) '
        '+ COALESCE(tmdaa3e16."금월해외CA이용금액", 0))'
    )
    assert metric["required_filters"] == ["tmdaa3e16.\"개인기업구분코드\" = '2'"]
    assert "해외이용금액" in metric["synonyms"]
    # 카드 월실적에 없는 축(국가·MCC업종)은 해외 매출전표로 간다.
    assert any("tbdaabt08" in caution for caution in metric["semantic_cautions"])


def test_card_billing_metric_uses_the_billing_column() -> None:
    metric = METRICS["카드월청구금액"]

    assert metric["source_table"] == "tmdaa3e16"
    assert metric["expression"] == 'SUM(COALESCE(tmdaa3e16."금월청구금액", 0))'
    assert "청구금액" in metric["synonyms"]
    assert any("금월이용합계금액" in caution for caution in metric["semantic_cautions"])
    assert any("미청구금액" in caution for caution in metric["semantic_cautions"])


def test_billing_glossary_term_separates_billing_from_usage() -> None:
    term = GLOSSARY["청구금액"]

    assert "금월청구금액" in term["canonical"]
    assert "금월청구금액" in term["sql_hint"]
    assert "미청구금액" in term["sql_hint"]
    # 10번 규칙의 이용금액 용어도 해외·청구 갈림길을 갖는다.
    usage_hint = GLOSSARY["이용금액"]["sql_hint"]
    assert "금월해외일시불이용금액+금월해외CA이용금액" in usage_hint
    assert "청구금액은 이용금액이 아니라" in usage_hint


def test_card_usage_domain_carries_the_billing_cues() -> None:
    domain = DOMAINS["card_usage"]

    assert "청구금액" in domain["keywords"]
    assert "카드월청구금액" in domain["preferred_metrics"]
    assert "카드월해외이용금액" in domain["preferred_metrics"]
    # "카드별 청구금액" 은 카드 축 때문에 회원 포트폴리오 도메인으로도 갈린다.
    assert "카드월청구금액" in DOMAINS["customer_card_portfolio"]["preferred_metrics"]


def test_overseas_usage_reference_points_at_card_monthly() -> None:
    reference = REFERENCES["해외이용금액_조회"]

    assert reference["primary_table"] == "tmdaa3e16"
    assert "해외 이용금액" in reference["when_user_says"]
    assert reference["recommended_columns"]["해외이용금액"] == (
        'SUM(COALESCE("금월해외일시불이용금액", 0) + COALESCE("금월해외CA이용금액", 0))'
    )
    assert any("tbdaabt08" in rule for rule in reference["rules"])


@pytest.mark.parametrize(
    ("question", "first_table"),
    [
        ("2026년 7월 해외 이용금액을 알려줘", "tmdaa3e16"),
        ("2026년 7월 카드별 해외 이용금액을 알려줘", "tmdaa3e16"),
        ("2026년 7월 법인카드 해외 이용금액 알려줘", "tmdaa3e16"),
        ("2026년 7월 해외 사용액을 알려줘", "tmdaa3e16"),
        ("2026년 7월 청구금액을 알려줘", "tmdaa3e16"),
        ("2026년 7월 카드별 청구금액을 알려줘", "tmdaa3e16"),
        # 매출금액 표면형은 그대로 해외 매출전표다.
        ("2026년 7월 해외 매출금액을 알려줘", "tbdaabt08"),
    ],
)
def test_rule_routing_picks_the_wording_the_question_used(
    question: str, first_table: str
) -> None:
    assert workflow._rule_rank_tables(question)[0] == first_table


@pytest.mark.parametrize(
    "question",
    [
        "2026년 1월부터 7월까지 해외 카드 이용금액을 국가별 상위 10개로 보여줘",
        "2026년 1월부터 7월까지 해외 카드 이용금액을 MCC 업종명 기준 상위 10개로 보여줘",
    ],
)
def test_overseas_slip_axes_keep_the_slip_table_as_a_candidate(question: str) -> None:
    """국가·MCC업종은 카드 월실적에 없는 축이라 해외 매출전표가 남아 있어야 한다."""
    assert "tbdaabt08" in workflow._rule_rank_tables(question)


def test_billing_question_prompt_carries_the_billing_metric() -> None:
    question = "2026년 7월 카드별 청구금액을 알려줘"
    keyword_scores = workflow._keyword_rule_domain_scores(schema.SCHEMA, question)
    metric_scores = workflow._metric_entity_domain_scores(schema.SCHEMA, question)
    candidates = workflow._weighted_domain_scores(keyword_scores, metric_scores, {})
    domain = candidates[0]["domain"]

    assert domain == "card_usage"
    metrics_summary = schema.build_metrics_summary(schema.SCHEMA, question, domain, 4)
    expressions = [
        line for line in metrics_summary.splitlines() if line.strip().startswith("expression:")
    ]

    assert "- **카드월청구금액**" in metrics_summary
    assert any("금월청구금액" in line for line in expressions), expressions
    # 이용금액 공식이 청구금액 질문의 정답 공식으로 실리지 않는다.
    assert not any("금월이용합계금액" in line for line in expressions), expressions


def test_overseas_usage_question_prompt_carries_the_card_monthly_metric() -> None:
    question = "2026년 7월 해외 이용금액을 알려줘"
    metrics_summary = schema.build_metrics_summary(schema.SCHEMA, question, "card_usage", 4)

    assert "금월해외일시불이용금액" in metrics_summary
    glossary_summary = schema.build_glossary_summary(
        schema.SCHEMA, question, "card_usage", max_count=4
    )
    assert "금월해외일시불이용금액+금월해외CA이용금액" in glossary_summary

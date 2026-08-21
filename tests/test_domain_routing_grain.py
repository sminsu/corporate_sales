"""기업 카드 보유 수 질의는 회원(회원일련번호)이 아니라 기업고객(고객식별자)으로 간다.

"26년 4월 유효 기업개별카드 수 알려줘" 가 customer_card_portfolio 로 갔다. 그
도메인은 default_fact_table 이 tbdaaat03(회원 마스터)이고 primary entity 의 grain 이
회원일련번호라, 테이블 선택 프롬프트가 "도메인의 default_fact_table 과
primary_entities 를 우선 검토" 하라며 회원 마스터를 먼저 들이밀었다. 정작 질문이
부른 유효기업개별카드수는 기업고객 월 스냅샷(tmdaa1d12, 고객식별자)에만 있다.

도메인 점수 18.8 중 18.0 이 semantic entity 의 use_when *문장* 을 낱말로 쪼갠
조각이었다. member.use_when 의 "카드" 세 번, member_card.use_when 의 "개별" 한 번이
3점씩 붙었다. 반대로 tmdaa1d12 엔티티가 canonical_facts 로 선언해 둔
유효기업개별카드수는, 비교가 raw substring 이라 사용자가 띄어 쓴 표면형
("유효 기업개별카드 수")과 한 글자도 맞지 않았다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import (
    SCHEMA,
    _keyword_rule_domain_scores,
    _metric_entity_domain_scores,
    _needs_domain_adjudication,
    _weighted_domain_scores,
)

ROOT = Path(__file__).resolve().parents[1]

# (질문, 도메인) — 같은 지표라도 질문이 세는 단위가 다르면 도메인이 갈린다.
ENTITY_GRAIN_QUESTIONS = [
    ("26년 4월 유효 기업개별카드 수 알려줘", "corporate_sales_targeting"),
    ("2026년 4월 유효 기업 공용카드 수 알려줘", "corporate_sales_targeting"),
    (
        "2026년 4월 가맹점 유효 기업 신용카드 수를 사업자과세유형별로 보여줘",
        "merchant_sales",
    ),
    ("기업 회원의 유효 신용카드 수를 회원자격별로 알려줘", "customer_card_portfolio"),
]


def _ranked_domains(question: str) -> list[dict]:
    return _weighted_domain_scores(
        _keyword_rule_domain_scores(SCHEMA, question),
        _metric_entity_domain_scores(SCHEMA, question),
        {},
    )


@pytest.mark.parametrize(("question", "domain"), ENTITY_GRAIN_QUESTIONS)
def test_card_count_question_routes_to_the_entity_it_counts(
    question: str, domain: str
) -> None:
    ranked = _ranked_domains(question)

    assert ranked[0]["domain"] == domain, [
        (item["domain"], item["score"]) for item in ranked[:3]
    ]
    # 근거가 뚜렷하면 LLM 판정까지 가지 않는다. 판정은 매 실행 결과가 달라진다.
    assert not _needs_domain_adjudication(ranked), question


def test_corporate_card_count_stays_on_the_customer_id_snapshot() -> None:
    """도메인이 맞아도 테이블이 가맹점 enrichment 로 가면 grain 이 가맹점번호가 된다."""
    ranked = workflow._rule_rank_tables("26년 4월 유효 기업개별카드 수 알려줘")

    assert ranked[0] == "tmdaa1d12", ranked


def test_use_when_sentences_do_not_score_a_domain_by_themselves() -> None:
    """use_when 은 설명 문장이다. 그 안의 낱말은 선언된 이름이 아니다."""
    entities = {item["name"]: item for item in SCHEMA["semantic_entities"]}
    prose = " ".join(entities["member"].get("use_when", []))

    assert "카드" in prose
    # 문장에 "카드"가 세 번 나와도 member 를 가진 도메인이 그 낱말로 앞서지 않는다.
    scores = _metric_entity_domain_scores(SCHEMA, "26년 4월 유효 기업개별카드 수 알려줘")
    assert scores["corporate_sales_targeting"] > scores["customer_card_portfolio"]


def test_tied_domain_scores_keep_a_stable_order() -> None:
    """후보를 set 에서 꺼내 담으므로 동점이면 실행마다 순서가 달라졌다."""
    tied = {"card_usage": 3.0, "merchant_sales": 3.0, "credit_risk": 1.0}

    ranked = _weighted_domain_scores({}, tied, {})

    assert [item["domain"] for item in ranked[:2]] == ["card_usage", "merchant_sales"]


def test_goldenset_domain_routing_does_not_regress() -> None:
    """골든셋 v3 500건의 도메인 1순위 적중률. 바닥값이지 목표값이 아니다.

    use_when 조각 점수를 걷어내기 전 65.8%, 걷어낸 뒤 71.4% 였다.
    """
    cases = [
        json.loads(line)
        for line in (
            ROOT / "tests" / "fixtures" / "corporate_sales_text2sql_goldenset_v3.jsonl"
        )
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    hits = sum(
        _ranked_domains(case["question"])[0]["domain"] == case.get("domain")
        for case in cases
    )

    assert hits / len(cases) >= 0.70, f"{hits}/{len(cases)}"

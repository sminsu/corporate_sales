"""고객식별자(기업 1곳)와 회원일련번호(카드 계약 1건)를 섞지 않는지.

customer_to_member 는 one_to_many 라 두 키의 COUNT DISTINCT 값이 다르다.
그런데 v1 은 "고객수" 를 회원수 지표의, "연체고객수" 를 연체회원수 지표의
동의어로 선언해 두었다. build_metrics_summary() 는 질문에 등장한 가장 긴
동의어로 지표를 고르므로 "기업고객 수" 를 물어도 프롬프트의 정답 지표가
COUNT(DISTINCT tbdaaat03."회원일련번호") 였다. 골든셋 정답은
COUNT(DISTINCT tbdaaat01."고객식별자") 다.

같은 혼동이 프롬프트 밖 두 군데에도 있었다.
  workflow._RANK_METRIC_ALIASES  — "기업고객 수 상위" 요청에 회원수로 정렬한 SQL 통과
  followup_ops.METRIC_ALIASES    — 이전 결과의 기업고객수 컬럼을 회원수로 인정
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
import yaml

from text2sql_agent import workflow
from text2sql_agent.followup_ops import METRIC_ALIASES, _requested_metrics
from text2sql_agent.schema import build_glossary_summary, build_metrics_summary

ROOT = Path(__file__).resolve().parents[1]
RAW_SEMANTIC = yaml.safe_load((ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))
METRICS = {metric["name"]: metric for metric in RAW_SEMANTIC["canonical_metrics"]}
GLOSSARY = {term["term"]: term for term in RAW_SEMANTIC["glossary"]}

CUSTOMER_KEY = 'COUNT(DISTINCT tbdaaat01."고객식별자")'
MEMBER_KEY = 'COUNT(DISTINCT tbdaaat03."회원일련번호")'


# ---------------------------------------------------------------------------
# 지표 정의
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("metric_name", "customer_synonym"),
    [("회원수", "고객수"), ("연체회원수", "연체고객수")],
)
def test_member_metrics_do_not_claim_customer_wording(
    metric_name: str,
    customer_synonym: str,
) -> None:
    """회원일련번호를 세는 지표가 고객 단위 표현을 동의어로 가져가면 안 된다."""
    metric = METRICS[metric_name]

    assert "회원일련번호" in metric["expression"]
    assert customer_synonym not in (metric.get("synonyms") or [])


@pytest.mark.parametrize(
    ("metric_name", "source_table", "expression"),
    [
        ("기업고객수", "tbdaaat01", CUSTOMER_KEY),
        ("연체기업수", "tbdaa1d12", 'COUNT(DISTINCT tbdaa1d12."고객식별자")'),
    ],
)
def test_corporate_count_metrics_count_the_customer_key(
    metric_name: str,
    source_table: str,
    expression: str,
) -> None:
    """기업 수를 세는 지표는 고객식별자를 DISTINCT 집계한다."""
    metric = METRICS[metric_name]

    assert metric["expression"] == expression
    assert metric["source_table"] == source_table
    assert "회원일련번호" not in metric["expression"]
    # 두 키를 헷갈리지 말라는 근거를 지표 자체가 들고 있어야 한다.
    assert any("회원일련번호" in caution for caution in metric["semantic_cautions"])


def test_corporate_customer_count_keeps_the_corporate_scope_filter() -> None:
    """개인기업구분코드 = '2' 가 빠지면 개인 고객까지 세어 값이 달라진다."""
    filters = " ".join(METRICS["기업고객수"]["required_filters"])

    assert "개인기업구분코드" in filters
    assert "'2'" in filters


# ---------------------------------------------------------------------------
# 프롬프트 라우팅 — 회귀의 본체
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "domain", "asked", "other_key"),
    [
        ("기업형태 구분별 기업고객 수 분포를 보여줘", "customer_card_portfolio", "기업고객수", "회원수"),
        ("고객 업종구분코드별 기업고객 수 상위 20개를 보여줘", "customer_card_portfolio", "기업고객수", "회원수"),
        ("2026년 7월 기업카드 국세 이용금액 합계와 이용 기업 수를 알려줘", "corporate_sales_targeting", "기업고객수", "회원수"),
        ("기업 회원의 회원자격코드별 회원 수를 알려줘", "customer_card_portfolio", "회원수", "기업고객수"),
        ("현재 유효한 기업카드를 보유한 회원 수와 카드 좌수를 알려줘", "customer_card_portfolio", "회원수", "기업고객수"),
        ("2026년 월별 연체 기업 수와 연체원금 합계를 보여줘", "credit_risk", "연체기업수", "연체회원수"),
        ("2026년 7월 31일 기준 기업 회원의 카드 연체 회원 수를 알려줘", "credit_risk", "연체회원수", "연체기업수"),
    ],
)
def test_count_question_ranks_the_key_it_asked_for_above_the_other_one(
    question: str,
    domain: str,
    asked: str,
    other_key: str,
) -> None:
    """골든셋 정답이 세는 키의 지표가 반대 키의 지표보다 위에 있어야 한다.

    금액과 개수를 함께 묻는 질문은 금액 지표가 1등일 수 있으므로 순위만 본다.
    """
    summary = build_metrics_summary(RAW_SEMANTIC, question, domain, max_count=8)
    ranked = [line[4 : line.index("**", 4)] for line in summary.splitlines() if line.startswith("- **")]

    assert asked in ranked, ranked
    if other_key in ranked:
        assert ranked.index(asked) < ranked.index(other_key), ranked


def test_corporate_count_question_no_longer_shows_the_member_expression() -> None:
    """v1 에서는 이 질문에 회원일련번호 집계식이 정답 지표로 실렸다."""
    summary = build_metrics_summary(
        RAW_SEMANTIC,
        "기업형태 구분별 기업고객 수 분포를 보여줘",
        "customer_card_portfolio",
        max_count=1,
    )

    assert CUSTOMER_KEY in summary
    assert MEMBER_KEY not in summary


# ---------------------------------------------------------------------------
# 용어
# ---------------------------------------------------------------------------
def test_glossary_contrasts_the_two_identifiers() -> None:
    corporate = GLOSSARY["기업고객"]
    member = GLOSSARY["카드회원"]

    assert "고객식별자" in corporate["canonical"]
    assert "회원일련번호" in corporate["description"]
    assert corporate["sql_hint"].count("회원일련번호") == 1  # "회원일련번호로 세지 않는다"
    assert member["canonical"] == "tbdaaat03.회원일련번호"
    assert "고객식별자" in member["sql_hint"]


@pytest.mark.parametrize(
    ("question", "expected_term"),
    [
        ("기업형태 구분별 기업고객 수 분포를 보여줘", "기업고객"),
        ("기업 회원의 회원자격코드별 회원 수를 알려줘", "카드회원"),
    ],
)
def test_glossary_term_reaches_the_prompt_for_count_questions(
    question: str,
    expected_term: str,
) -> None:
    summary = build_glossary_summary(RAW_SEMANTIC, question, "customer_card_portfolio", max_count=6)

    assert f"- **{expected_term}**" in summary


# ---------------------------------------------------------------------------
# grain 규칙
# ---------------------------------------------------------------------------
def test_grain_rules_state_which_key_counts_what() -> None:
    rules = RAW_SEMANTIC["sql_generation_contract"]["grain_and_aggregation_rules"]
    identity_rule = next(
        (rule for rule in rules if "고객식별자" in rule and "회원일련번호" in rule),
        "",
    )

    assert identity_rule, rules
    assert "기업고객식별자" in identity_rule  # 전표·가맹점 테이블의 다른 이름
    assert "법인고객식별자" in identity_rule  # 부서사업장회원의 다른 이름
    # 세는 것만이 아니라 목록·순위의 단위도 같은 키를 따른다.
    assert "상위 N개 업체·회사·기업·법인" in identity_rule
    assert "카드구분키번호" in identity_rule


# ---------------------------------------------------------------------------
# 프롬프트 밖 — 순위 지표 게이트와 후속질문
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected_bucket"),
    [
        ("기업고객수", "customer_count"),
        ("법인고객 수", "customer_count"),
        ("유실적업체수", "customer_count"),
        ("고객수", "customer_count"),
        ("회원수", "member_count"),
        ("연체회원수", "member_count"),
        ("가맹점수", "merchant_count"),
    ],
)
def test_rank_metric_separates_customer_and_member_counts(
    text: str,
    expected_bucket: str,
) -> None:
    assert workflow._rank_metric(text) == expected_bucket


def test_member_ordered_vq_cannot_answer_a_customer_count_ranking() -> None:
    """"기업고객 수 상위" 요청에 회원수로 정렬한 SQL이 통과하면 안 된다."""
    question = "고객 업종구분코드별 기업고객 수 상위 20개를 보여줘"
    requested = workflow._explicit_rank_metric(question)

    assert requested == "customer_count"
    assert workflow._rank_metric('COUNT(DISTINCT m."회원일련번호") AS "회원수" DESC') != requested
    assert workflow._rank_metric('COUNT(DISTINCT c."고객식별자") AS "기업고객수" DESC') == requested


def test_followup_keeps_customer_count_apart_from_member_count() -> None:
    buckets = dict(METRIC_ALIASES)

    assert "고객수" not in buckets["회원수"]
    assert _requested_metrics("고객수는 얼마야") == ["고객수"]
    assert _requested_metrics("회원수는 얼마야") == ["회원수"]


# ---------------------------------------------------------------------------
# 프롬프트 밖 — 업체·회사 단위 순위의 결과 grain
#
# cs-workbook-017("상위 10개 업체")과 cs-workbook-018("상위 10개 회사")은 표현만
# 다른 같은 질문이고 정답 SQL도 같다. 그런데 라우팅이 동일해 두 질문 모두 LLM
# 생성 경로로 내려가므로, "업체"로 물었을 때만 회원일련번호·카드구분키번호로 쪼갠
# 카드 목록이 나오는 일이 있었다. 표현과 무관하게 고객식별자 1행이어야 한다.
# ---------------------------------------------------------------------------
WORKBOOK_GOLDENSET = {
    row["id"]: row
    for row in csv.DictReader(
        (ROOT / "tests" / "fixtures" / "corporate_sales_workbook_goldenset_v1.csv").read_text(
            encoding="utf-8-sig"
        ).splitlines()
    )
}
CARD_GRAIN_SQL = """
SELECT c."회원일련번호", c."카드구분키번호", c."사업자등록번호",
       SUM(c."금월이용합계금액") AS "이용금액"
FROM tmdaa3e16 c
GROUP BY c."회원일련번호", c."카드구분키번호", c."사업자등록번호"
ORDER BY "이용금액" DESC
LIMIT 10
"""


@pytest.mark.parametrize("goldenset_id", ["cs-workbook-017", "cs-workbook-018"])
def test_corporate_ranking_rejects_card_grain_for_either_wording(goldenset_id: str) -> None:
    question = WORKBOOK_GOLDENSET[goldenset_id]["question"]
    expected_sql = WORKBOOK_GOLDENSET[goldenset_id]["sql"]

    issues = workflow._validate_corporate_entity_grain(question, CARD_GRAIN_SQL)

    assert any("고객식별자" in issue for issue in issues)
    assert any("카드구분키번호" in issue for issue in issues)
    assert workflow._validate_corporate_entity_grain(question, expected_sql) == []


def test_corporate_ranking_allows_card_counts_over_the_customer_key() -> None:
    """고객식별자로 묶은 뒤 카드 좌수를 세는 건 카드 단위 목록이 아니다."""
    sql = """
    SELECT c."고객식별자", a."사업자등록번호",
           COUNT(DISTINCT c."카드구분키번호") AS "이용카드좌수",
           SUM(c."금월이용합계금액") AS "이용금액"
    FROM tmdaa3e16 c JOIN tmdaa1d12 a ON c."고객식별자" = a."고객식별자"
    GROUP BY c."고객식별자", a."사업자등록번호"
    ORDER BY "이용금액" DESC
    LIMIT 10
    """

    assert workflow._validate_corporate_entity_grain("이용금액 상위 10개 업체를 알려줘", sql) == []


@pytest.mark.parametrize(
    "question",
    [
        "2025년 12월 매출 상위 10개 가맹점을 보여줘",  # 단위가 가맹점번호다
        "고객 업종구분코드별 기업고객 수 상위 20개를 보여줘",  # 단위가 축 값이다
        "상위 10개 업체의 카드별 이용금액을 알려줘",  # 카드 단위를 질문이 요구했다
    ],
)
def test_corporate_ranking_check_skips_other_units(question: str) -> None:
    assert workflow._validate_corporate_entity_grain(question, CARD_GRAIN_SQL) == []

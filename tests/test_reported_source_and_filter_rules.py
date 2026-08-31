"""사용자가 잡아 준 다섯 자리 — 지표를 실제로 든 원천으로 보내고 코드 필터를 못 박는다.

    1. "현재 기준 기업카드를 카드등급그룹별로 몇 좌인지" 가 회원카드기본(tbdaaat05)과
       기업고객 스냅샷(tbdaa1d12)을 함께 썼다. 카드등급그룹은 스냅샷에 없고, 스냅샷의
       유효기업신용카드수는 기업고객당 이미 더해 놓은 수라 카드 한 장의 속성으로 쪼갤
       수 없다.
    2. "해외가맹점매출건수" 가 가맹점 월스냅샷(tmdaa5d01)·해외매출전표(tbdaabt08)로
       갔다. 그 컬럼을 든 테이블은 가맹점 월실적(tmdaa5e11) 하나뿐이다.
    3. 기업 유효카드 좌수·기업 매출건수를 셀 때 거는 코드 필터가 선언되어 있지 않았다.
    4. 가맹점 수수료 금액을 물으면 전표(tbdaabt30)의 전표당 가맹점수수료가 올라왔다.
       월 단위 수수료 수입은 tmdaa5e11 의 가맹점수입수수료다.
    5. "최근 3개월 평균 매출과 6개월 평균 매출 비교" 는 tmdaa5e11 한 행이 이미 들고
       있는 누적 컬럼으로 답한다. 그 컬럼은 기간 합계라 평균은 개월 수로 나눈다.

공통 원인은 하나다. 질문이 부르는 지표·축이 어느 테이블에 있는지 선언되어 있지 않으면,
낱말 하나로 걸린 semantic attribute(+36)나 낱말 두 개로 걸린 query_reference 가 그 값을
실제로 든 테이블을 이긴다.
"""

from __future__ import annotations

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import SCHEMA


def _metric(name: str) -> dict:
    for metric in SCHEMA.get("canonical_metrics", []):
        if metric.get("name") == name:
            return metric
    raise AssertionError(f"canonical metric 없음: {name}")


def _attribute(name: str) -> dict:
    for attribute in SCHEMA.get("semantic_attributes", []):
        if attribute.get("name") == name:
            return attribute
    raise AssertionError(f"semantic attribute 없음: {name}")


def _table(name: str) -> dict:
    for table in SCHEMA.get("tables", []):
        if table.get("name") == name:
            return table
    raise AssertionError(f"테이블 없음: {name}")


def _column(table_name: str, column_name: str) -> dict:
    table = _table(table_name)
    for section in ("dimensions", "measures", "time_dimensions"):
        for column in table.get(section) or []:
            if column.get("name") == column_name:
                return column
    raise AssertionError(f"{table_name}.{column_name} 없음")


# ---------------------------------------------------------------------------
# 1. 카드 속성 축으로 쪼개는 좌수는 회원카드기본 한 곳에서 센다
# ---------------------------------------------------------------------------
CARD_AXIS_COUNT_QUESTIONS = [
    "현재 기준 기업카드를 카드등급그룹별로 몇 좌인지 알려줘",
    "현재 기준 기업카드를 브랜드와 카드등급 구분으로 교차해서 좌수를 보여줘",
    "2026년 7월 기준 기업카드 등급 구분별 유효 좌수를 알려줘",
    "모바일 카드로 발급된 기업카드 좌수",
]


@pytest.mark.parametrize("question", CARD_AXIS_COUNT_QUESTIONS)
def test_card_attribute_axis_counts_in_the_card_master(question: str) -> None:
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == "tbdaaat05", (question, ranked)


@pytest.mark.parametrize("question", CARD_AXIS_COUNT_QUESTIONS)
def test_card_axis_count_does_not_join_the_corporate_snapshot(question: str) -> None:
    """스냅샷의 유효기업신용카드수는 기업고객당 합계다. 조인하면 좌수가 부풀려진다."""
    ranked = workflow._rule_rank_tables(question)

    assert "tbdaa1d12" not in ranked, (question, ranked)
    assert "tmdaa1d12" not in ranked, (question, ranked)


# 축이 스냅샷에 있으면 스냅샷이 답할 곳이다. 카드 마스터로 넘기지 않는다.
SNAPSHOT_AXIS_QUESTIONS = [
    ("현재 기준 기업 유효 기업 신용카드 수를 소매비소매별로 알려줘", "tbdaa1d12"),
    ("2025년 12월 기준 유효 기업 신용카드 수를 기업거래정지 여부별로 알려줘", "tmdaa1d12"),
]


@pytest.mark.parametrize(("question", "table"), SNAPSHOT_AXIS_QUESTIONS)
def test_snapshot_axis_stays_in_the_snapshot(question: str, table: str) -> None:
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == table, (question, ranked)


def test_axis_rule_yields_when_the_question_names_a_measure_elsewhere() -> None:
    """발급유형구분코드는 회원카드기본에도 있지만, 체크카드 이용금액은 거기 없다.

    행을 세는 질의(좌수)는 지표 컬럼이 필요 없다. 질문이 어딘가의 지표 컬럼을 이름으로
    불렀다면 그 컬럼을 든 테이블이 답할 곳이고, 축이 카드 마스터에도 있다는 사실은
    근거가 못 된다.
    """
    question = "2025년 11월 기업카드 체크카드 이용금액을 발급유형별로 보여줘"

    assert "발급유형구분코드" in {
        column["name"] for column in _table("tbdaaat05")["dimensions"]
    }
    assert workflow._rule_rank_tables(question)[0] == "tmdaa3e16"


def test_card_master_survives_when_the_question_has_no_axis() -> None:
    """축 원천은 스냅샷의 대안이 아니라 다른 낟알이다. 축이 없다고 빼지 않는다."""
    ranked = workflow._rule_rank_tables("현재 기준 기업카드 좌수를 알려줘")

    assert "tbdaaat05" in ranked, ranked


def test_card_level_source_is_declared_with_an_axis_policy() -> None:
    attribute = _attribute("corporate_card_holding")
    mapping = next(
        item
        for item in attribute["source_mappings"]
        if item.get("table") == "tbdaaat05"
    )

    assert mapping["role"] == "card_level_corporate_card_holding"
    assert attribute["source_selection"]["axis_role_prefix"] == "card_level_"
    assert "카드소유자구분코드" in mapping["columns"]


# ---------------------------------------------------------------------------
# 2. 해외 가맹점 매출은 가맹점 월실적이 컬럼으로 들고 있다
# ---------------------------------------------------------------------------
OVERSEAS_MERCHANT_QUESTIONS = [
    "2026년 3월 해외가맹점매출건수를 알려줘",
    "2026년 3월 해외 가맹점 매출 건수를 알려줘",
    "해외가맹점 매출건수가 가장 많은 가맹점",
    "해외 가맹점 매출금액 상위 10곳",
]


@pytest.mark.parametrize("question", OVERSEAS_MERCHANT_QUESTIONS)
def test_overseas_merchant_sales_ranks_the_only_owner_first(question: str) -> None:
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == "tmdaa5e11", (question, ranked)


@pytest.mark.parametrize(
    ("metric", "column"),
    [
        ("해외가맹점월매출건수", "해외가맹점매출건수"),
        ("해외가맹점월매출금액", "해외가맹점매출금액"),
    ],
)
def test_overseas_merchant_metrics_point_at_the_declared_column(
    metric: str, column: str
) -> None:
    declared = _metric(metric)

    assert declared["source_table"] == "tmdaa5e11"
    assert column in declared["expression"]
    assert column in declared["synonyms"]
    # 이 컬럼을 든 테이블은 정말 한 곳뿐이다. 그래서 원천을 못 박을 수 있다.
    owners = [
        table["name"]
        for table in SCHEMA["tables"]
        for item in table.get("measures") or []
        if item["name"] == column
    ]
    assert owners == ["tmdaa5e11"]


@pytest.mark.parametrize("question", OVERSEAS_MERCHANT_QUESTIONS)
def test_overseas_merchant_column_reaches_the_prompt(question: str) -> None:
    """프롬프트는 테이블당 12~16개만 싣는다. 지표가 잘리면 SQL을 쓸 수 없다."""
    ranked = workflow._rule_rank_tables(question)
    details = workflow._table_details(ranked, question)

    assert "- 해외가맹점매출건수 [" in details or "- 해외가맹점매출금액 [" in details


def test_fuzzy_reference_cannot_outrank_a_column_the_question_named() -> None:
    """표면형이 하나도 안 맞은 참조는 컬럼 두 개 겹침과 같은 급까지만 올린다."""
    assert workflow._FUZZY_REFERENCE_WEIGHT == 6


# ---------------------------------------------------------------------------
# 3. 기업 집계의 코드 필터
# ---------------------------------------------------------------------------
def test_corporate_card_owner_codes_are_declared_on_the_count_metric() -> None:
    """기업 명의 카드는 기업대표(3)·기업개별(4)·기업공용(5)이다."""
    required = " ".join(_metric("기업유효카드좌수")["required_filters"])

    assert "\"카드소유자구분코드\" IN ('3','4','5')" in required
    assert "\"개인기업구분코드\" = '2'" in required
    assert "\"유효신용카드여부\" = '1'" in required


def test_card_owner_codebook_covers_the_declared_codes() -> None:
    semantics = _column("tbdaaat05", "카드소유자구분코드")["value_semantics"]

    assert semantics["3"] == "기업대표"
    assert semantics["4"] == "기업개별"
    assert semantics["5"] == "기업공용"


def test_slip_count_metric_declares_the_card_sales_type_filter() -> None:
    """일반(1)·할부(2)·리볼빙(4)만 카드 매출이다. 현금서비스·연회비는 아니다."""
    required = " ".join(_metric("매출건수")["required_filters"])

    assert "\"카드매출유형구분코드\" IN ('1','2','4')" in required


def test_slip_count_metric_declares_the_own_company_filter() -> None:
    required = " ".join(_metric("매출건수")["required_filters"])

    assert "\"회원소속회사구분코드\" = '1'" in required
    assert "\"가맹점소속회사구분코드\" = '1'" in required


def test_slip_count_filters_warn_about_the_shared_where_clause() -> None:
    """필터는 건수 지표에만 걸었다. 한 쿼리가 금액과 함께 뽑으면 WHERE 를 공유한다."""
    cautions = " ".join(_metric("매출건수")["semantic_cautions"])

    assert "WHERE 절" in cautions
    assert "CASE WHEN" in cautions


def test_merchant_company_code_shares_the_member_company_codebook() -> None:
    """가맹점소속회사구분코드는 이름이 달라 코드북을 못 받고 있었다."""
    for table in ("tbdaabt08", "tbdaabt30"):
        semantics = _column(table, "가맹점소속회사구분코드")["value_semantics"]
        assert semantics["1"] == "당사", table


def test_overseas_slip_has_no_member_company_column() -> None:
    """해외 전표에는 회원소속회사구분코드가 없다. 규칙이 그 사실을 적어 두어야 한다."""
    names = {column["name"] for column in _table("tbdaabt08")["dimensions"]}

    assert "가맹점소속회사구분코드" in names
    assert "회원소속회사구분코드" not in names
    assert any(
        "회원소속회사구분코드를 갖고 있지 않다" in caution
        for caution in _metric("매출건수")["semantic_cautions"]
    )


# ---------------------------------------------------------------------------
# 4. 가맹점 수수료 금액은 월 실적의 수입수수료다
# ---------------------------------------------------------------------------
MERCHANT_FEE_QUESTIONS = [
    "가맹점 수수료가 가장 높은 가맹점 알려줘",
    "2026년 4월 가맹점 수수료 금액 상위 10개 가맹점",
    "가맹점수입수수료 상위 10개 가맹점 알려줘",
]


@pytest.mark.parametrize("question", MERCHANT_FEE_QUESTIONS)
def test_merchant_fee_column_reaches_the_prompt(question: str) -> None:
    """전표(tbdaabt30)는 "가맹점수수료" 를 컬럼명으로 그대로 들고 있다.

    질문이 컬럼명을 불렀다는 가점(+40)이 전표에 붙어서, 월 실적의 가맹점수입수수료가
    테이블당 12칸 예산 밖으로 밀려났다. 모델이 쓸 수 있는 컬럼 중에 정답이 없었다.
    """
    ranked = workflow._rule_rank_tables(question)
    details = workflow._table_details(ranked, question)

    assert "tmdaa5e11" in ranked, (question, ranked)
    assert "- 가맹점수입수수료 [" in details, question


def test_merchant_fee_revenue_is_a_single_source_attribute() -> None:
    attribute = _attribute("merchant_fee_revenue")
    tables = {mapping["table"] for mapping in attribute["source_mappings"]}

    assert tables == {"tmdaa5e11"}
    assert "가맹점 수수료" in attribute["aliases"]


def test_merchant_fee_metric_reads_the_monthly_revenue_column() -> None:
    declared = _metric("가맹점월수입수수료")

    assert declared["source_table"] == "tmdaa5e11"
    assert "가맹점수입수수료" in declared["expression"]
    assert any("수수료율" in caution for caution in declared["semantic_cautions"])


def test_fee_rate_and_card_side_fees_keep_their_own_sources() -> None:
    """사용자가 고른 범위는 가맹점 수수료 금액이다. 율과 카드 수수료는 건드리지 않는다."""
    assert workflow._rule_rank_tables("2026년 3월 법인카드 할부수수료를 알려줘")[0] == "tmdaa3e16"
    assert workflow._rule_rank_tables("2026년 3월 CA수수료를 알려줘")[0] == "tbdaabt30"


def test_attribute_declared_columns_survive_the_prompt_budget() -> None:
    """속성이 "이 값은 이 컬럼에서 읽는다" 고 선언한 컬럼은 예산에서 잘리지 않는다."""
    question = "가맹점 수수료가 가장 높은 가맹점 알려줘"
    columns = workflow._matched_attribute_columns(
        question, workflow._rule_rank_tables(question)
    )

    assert "가맹점수입수수료" in columns["tmdaa5e11"]


# ---------------------------------------------------------------------------
# 5. 최근 N개월 컬럼은 기간 합계다. 평균은 개월 수로 나눈다
# ---------------------------------------------------------------------------
RECENT_PERIOD_QUESTIONS = [
    "가맹점중 최근 3개월 평균 매출과 6개월 평균 매출을 비교해줘",
    "최근 3개월 평균 매출과 6개월 평균 매출 비교해줘",
]


@pytest.mark.parametrize("question", RECENT_PERIOD_QUESTIONS)
def test_recent_period_comparison_stays_in_the_monthly_performance(
    question: str,
) -> None:
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == "tmdaa5e11", (question, ranked)


@pytest.mark.parametrize("question", RECENT_PERIOD_QUESTIONS)
def test_recent_period_columns_reach_the_prompt(question: str) -> None:
    details = workflow._table_details(workflow._rule_rank_tables(question), question)

    assert "- 최근3개월가맹점매출금액 [" in details, question
    assert "- 최근6개월가맹점매출금액 [" in details, question


def test_recent_period_reference_divides_by_the_month_count() -> None:
    """최근3개월가맹점매출금액은 3개월 합계다. 그대로 평균이라고 답하면 3배 크다."""
    reference = next(
        item
        for item in SCHEMA["query_references"]
        if item.get("intent") == "가맹점_최근기간_평균매출_비교"
    )

    assert reference["primary_table"] == "tmdaa5e11"
    assert reference["join_tables"] == []
    assert "/ 3" in reference["recommended_columns"]["최근3개월 평균매출"]
    assert "/ 6" in reference["recommended_columns"]["최근6개월 평균매출"]
    assert any("개월 수" in rule for rule in reference["rules"])


def test_recent_period_glossary_explains_the_cumulative_shape() -> None:
    term = next(
        item for item in SCHEMA["glossary"] if item.get("term") == "최근 N개월 실적"
    )

    assert "기간 합계" in term["canonical"]
    assert "3개월 평균 매출" in term["aliases"]


# ---------------------------------------------------------------------------
# 모집단을 가리키는 낱말은 테이블을 지목하지 않는다
# ---------------------------------------------------------------------------
def test_bare_member_word_no_longer_identifies_the_member_master() -> None:
    """"기업 회원의 신용카드 할부 이용금액" 이 낱말 '회원' 으로 회원 마스터를 올렸다."""
    synonyms = _table("tbdaaat03").get("synonyms") or []

    assert "회원" not in synonyms
    assert "회원기본" in synonyms or "회원마스터" in synonyms
    assert "tbdaaat03" not in workflow._rule_rank_tables(
        "2026년 5월 기업 회원의 신용카드 할부 이용금액은?"
    )

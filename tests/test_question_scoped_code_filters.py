"""질문이 지목한 값까지 필터를 좁힌다 — 사용자가 잡아 준 세 자리.

거를 컬럼은 이미 프롬프트에 있는데 어느 값을 걸지가 선언되어 있지 않으면, 모델은
라벨 글자가 겹치는 쪽이나 먼저 선언된 쪽을 집는다.

    1. "유효한 기업회원의 체크카드·직불카드 이용금액" 에 유효신용카드여부 = '1' 이
       걸렸다. 유효 여부는 카드 종류마다 컬럼이 따로인데 어느 쪽을 걸지가 없었다.
    2. "기업신용매출건수" 에 상품중분류구분코드 = 'CP51' 하나만 걸렸다. 기업신용은
       CP51·CP52 두 코드인데, 라벨에 '신용' 이 든 코드가 CP51 하나뿐이다.
    3. 가맹점이 모수인 질의에 가맹점상태구분코드가 안 걸려 거래정지·해지 가맹점까지
       세고 있었다.
"""

from __future__ import annotations

import re

import pytest

from text2sql_agent.schema import (
    SCHEMA,
    VERIFIED_QUERIES,
    build_glossary_summary,
    build_metrics_summary,
)


def _metric(name: str) -> dict:
    for metric in SCHEMA.get("canonical_metrics", []):
        if metric.get("name") == name:
            return metric
    raise AssertionError(f"canonical metric 없음: {name}")


def _glossary(term: str) -> dict:
    for item in SCHEMA.get("glossary", []):
        if item.get("term") == term:
            return item
    raise AssertionError(f"용어 없음: {term}")


def _attribute(name: str) -> dict:
    for item in SCHEMA.get("semantic_attributes", []):
        if item.get("name") == name:
            return item
    raise AssertionError(f"semantic attribute 없음: {name}")


def _column(table_name: str, column_name: str) -> dict:
    for table in SCHEMA.get("tables", []):
        if table.get("name") != table_name:
            continue
        for section in ("dimensions", "measures", "time_dimensions"):
            for column in table.get(section) or []:
                if column.get("name") == column_name:
                    return column
    raise AssertionError(f"{table_name}.{column_name} 없음")


# ---------------------------------------------------------------------------
# 1. 유효 여부는 카드 종류마다 컬럼이 따로다
# ---------------------------------------------------------------------------
CHECK_CARD_QUESTIONS = [
    "유효한 기업회원의 체크카드 직불카드 이용금액 및 체크카드 비용 알려줘",
    "유효한 기업회원의 체크카드 이용금액 알려줘",
    "유효 기업카드 중 직불카드 이용금액을 보여줘",
]


def test_valid_card_glossary_splits_credit_from_check() -> None:
    """'유효한' 은 종류를 정하지 않는다. 어느 컬럼을 걸지는 질문이 부른 종류가 정한다."""
    term = _glossary("유효카드")

    assert "유효신용카드여부" in term["canonical"]
    assert "유효체크카드여부" in term["canonical"]
    assert "유효체크카드여부" in term["sql_hint"]
    assert "유효기업체크카드수" in term["sql_hint"]
    # 유효직불카드 컬럼은 스키마에 없다. 직불을 물어도 체크 쪽으로 판정한다.
    assert "유효직불카드" in term["description"]


@pytest.mark.parametrize("question", CHECK_CARD_QUESTIONS)
def test_check_card_question_reaches_the_check_flag(question: str) -> None:
    """용어 블록은 프롬프트 맨 앞이다. 여기 없으면 모델이 신용 컬럼을 집는다."""
    summary = build_glossary_summary(SCHEMA, question, "")

    assert "유효체크카드여부" in summary, question
    assert "유효기업체크카드수" in summary, question


def test_card_holding_attribute_names_the_check_column() -> None:
    cautions = " ".join(_attribute("corporate_card_holding")["semantic_cautions"])

    assert "유효체크카드여부" in cautions
    assert "유효직불카드" in cautions


def test_card_count_metric_filter_says_which_flag_to_pick() -> None:
    """OR 로만 적어 두면 종류를 지목한 질문에서도 신용 쪽이 남는다."""
    required = " ".join(_metric("기업유효카드좌수")["required_filters"])

    assert "\"유효신용카드여부\" = '1' OR tbdaaat05.\"유효체크카드여부\" = '1'" in required
    assert "체크·직불만 물으면" in required


def test_card_holding_glossary_marks_the_sum_as_the_unnamed_kind() -> None:
    """기업카드보유의 신용+체크 합은 종류를 말하지 않은 질의의 정의다."""
    description = _glossary("기업카드보유")["description"]

    assert "카드 종류를 말하지 않은" in description
    assert "그 컬럼 하나만 쓴다" in description


# ---------------------------------------------------------------------------
# 2. 기업신용 매출은 상품중분류 두 코드다
# ---------------------------------------------------------------------------
CORPORATE_CREDIT_QUESTIONS = [
    "26년 7월 9일 기준 기업신용매출건수랑 금액 알려줘",
    "2026년 7월 법인신용매출 건수 알려줘",
    "2026년 7월 기업신용매출금액 알려줘",
]


def test_corporate_credit_glossary_declares_both_codes() -> None:
    term = _glossary("기업신용매출")

    assert term["canonical"] == "상품중분류구분코드 IN ('CP51','CP52')"
    assert "CP51" in term["sql_hint"] and "CP52" in term["sql_hint"]
    # 체크(CP53)·직불(CP54)과 갈라 놓아야 한 코드만 남는 실수를 반대로도 막는다.
    assert "CP53" in term["sql_hint"] and "CP54" in term["sql_hint"]


@pytest.mark.parametrize("question", CORPORATE_CREDIT_QUESTIONS)
def test_corporate_credit_code_set_reaches_the_prompt(question: str) -> None:
    summary = build_glossary_summary(SCHEMA, question, "")

    assert "CP51" in summary, question
    assert "CP52" in summary, question


@pytest.mark.parametrize("metric", ["매출건수", "카드매출금액"])
def test_slip_metrics_carry_the_card_kind_caution(metric: str) -> None:
    """전표를 세는 지표와 더하는 지표가 같은 WHERE 를 공유한다."""
    cautions = " ".join(_metric(metric)["semantic_cautions"])

    assert "IN ('CP51','CP52')" in cautions
    assert "CP55~CP57" in cautions


def test_product_class_aliases_still_tie_cp52_to_credit() -> None:
    """라벨은 '기업정부구매카드' 다. 별칭이 빠지면 컬럼조차 프롬프트에 못 든다."""
    aliases = _column("tbdaabt30", "상품중분류구분코드")["value_aliases"]

    assert "기업신용" in aliases["CP51"]
    assert "기업신용" in aliases["CP52"]
    assert "기업신용" not in aliases.get("CP53", [])


# ---------------------------------------------------------------------------
# 3. 가맹점이 모수면 정상('1') 상태만 센다
# ---------------------------------------------------------------------------
MERCHANT_POPULATION_METRICS = [
    ("가맹점수", "tbdaadt01"),
    ("결제금융기관별가맹점수", "tbdaadt01"),
    ("가맹점월매출금액", "tmdaa5e11"),
    ("가맹점월매출건수", "tmdaa5e11"),
    ("가맹점월일시불매출금액", "tmdaa5e11"),
    ("가맹점월수입수수료", "tmdaa5e11"),
    ("해외가맹점월매출금액", "tmdaa5e11"),
    ("해외가맹점월매출건수", "tmdaa5e11"),
    ("신용카드가맹점수수료율", "tmdaa5e11"),
    ("가맹점월지급금액", "tmdaaus01"),
    ("브랜드가맹점주수", "tbdaaus01"),
    ("기업카드소지가맹점주수", "tbdaaus01"),
]


@pytest.mark.parametrize(("metric", "table"), MERCHANT_POPULATION_METRICS)
def test_merchant_population_metrics_declare_the_status_filter(
    metric: str, table: str
) -> None:
    required = " ".join(_metric(metric)["required_filters"])

    assert f"{table}.\"가맹점상태구분코드\" = '1'" in required


def test_closure_metric_is_exempt_from_the_status_filter() -> None:
    """폐업이 곧 모수인 지표에 정상 필터를 걸면 늘 0건이 된다."""
    required = " ".join(_metric("최근기간폐업가맹점수")["required_filters"])

    assert "가맹점상태구분코드" not in required
    assert "휴폐업여부" in required


def test_merchant_status_codebook_covers_the_three_codes() -> None:
    semantics = _column("tbdaadt01", "가맹점상태구분코드")["value_semantics"]

    assert semantics == {"1": "정상", "2": "거래정지", "3": "해지"}


@pytest.mark.parametrize(
    "question",
    [
        "2026년 7월 기준 가맹점 수 알려줘",
        "업종별 가맹점수를 알려줘",
        "가맹점별 매출금액 상위 10곳을 보여줘",
    ],
)
def test_merchant_status_filter_reaches_the_prompt(question: str) -> None:
    summary = build_glossary_summary(SCHEMA, question, "")

    assert "가맹점상태구분코드 = '1'" in summary, question


def test_merchant_count_metric_filter_reaches_the_prompt() -> None:
    """용어가 잘려도 지표의 required_filters 가 같은 말을 한 번 더 한다."""
    summary = build_metrics_summary(SCHEMA, "2026년 7월 기준 가맹점 수 알려줘", "")

    assert "tbdaadt01.\"가맹점상태구분코드\" = '1'" in summary


def test_brand_merchant_reference_no_longer_doubts_the_status_code() -> None:
    """규칙이 "운영 코드값을 검증한다" 라고 말하는 동안은 필터가 붙어도 흔들린다."""
    reference = next(
        item
        for item in SCHEMA["query_references"]
        if item.get("intent") == "브랜드_활성가맹점수"
    )
    rules = " ".join(reference["rules"])

    assert "참고문서 가정" not in rules
    assert "\"가맹점상태구분코드\" = '1'" in rules


# ---------------------------------------------------------------------------
# 4. 검증 쿼리도 같은 모수를 쓴다
#
# authoritative 라우팅은 생성을 건너뛰고 검증 쿼리 SQL 을 그대로 내보낸다. 지표에만
# 필터를 걸어 두면 같은 질문이 경로에 따라 다른 수를 낸다.
# ---------------------------------------------------------------------------
MERCHANT_TABLES = ("tbdaadt01", "tbdaaus01", "tmdaa5d01", "tmdaa5e11", "tmdaaus01")
# 폐업이 곧 모수인 검증 쿼리. 정상만 남기면 늘 0건이 된다.
MERCHANT_STATUS_EXEMPT_QUERIES = {"recent_closed_brand_merchant_count"}


def _merchant_verified_queries() -> list[dict]:
    return [
        query
        for query in VERIFIED_QUERIES
        if any(table in str(query.get("sql") or "") for table in MERCHANT_TABLES)
    ]


def test_merchant_verified_queries_filter_the_status_code() -> None:
    queries = _merchant_verified_queries()
    # yaml 에 SQL 을 적어 둔 것과 sql_builders 가 조립하는 것이 함께 들어온다.
    assert len(queries) >= 23, "가맹점 검증 쿼리가 줄었다. 목록을 다시 센다"

    missing = sorted(
        str(query["name"])
        for query in queries
        if "가맹점상태구분코드" not in str(query["sql"])
        and str(query["name"]) not in MERCHANT_STATUS_EXEMPT_QUERIES
    )
    assert not missing, missing


def test_closure_verified_query_stays_exempt() -> None:
    """휴폐업여부와 가맹점상태구분코드는 다른 축이다. 함께 걸면 폐업 수가 사라진다."""
    query = next(
        item for item in VERIFIED_QUERIES if item["name"] == "recent_closed_brand_merchant_count"
    )

    assert "휴폐업여부" in query["sql"]
    assert "가맹점상태구분코드" not in query["sql"]


_STATUS_COMPARISON_RE = re.compile(r"가맹점상태구분코드\"?\s*=\s*('[^']*'|\{[^}]*\})")


def test_status_code_is_a_literal_not_an_unverified_parameter() -> None:
    """코드값이 확정됐다. '참고질의가 가정한' 파라미터는 걷어냈다.

    컬럼을 결과로 내보내기만 하는 자리는 건드리지 않는다 — 비교하는 자리만 본다.
    """
    for query in _merchant_verified_queries():
        name = str(query["name"])
        sql = str(query["sql"])
        assert "{정상가맹점상태코드}" not in sql, name
        compared = set(_STATUS_COMPARISON_RE.findall(sql))
        expected = set() if name in MERCHANT_STATUS_EXEMPT_QUERIES else {"'1'"}
        assert compared == expected, (name, sorted(compared))

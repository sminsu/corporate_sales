"""가맹점 사업장 주소를 물으면 주소 본문 컬럼이 프롬프트에 있어야 한다.

"한신포차 가맹점주 가맹점 도로명 주소 2026년 5월 기준으로 알려줘" 가 답을 못 냈다.
라우팅은 맞았다 — 가맹점기본(tbdaadt01) 한 곳이고, merchant_address 속성이 주소 본문은
가맹점상세주소라고 선언해 두었다. 그런데 프롬프트에 실린 컬럼은 도로명우편번호·
도로명구역번호·도로명건물본번호 같은 조각뿐이고 정작 가맹점상세주소가 없었다.

민감 컬럼 이름 패턴('상세주소' 등)이 두 군데에 있었던 탓이다. schema 쪽에는 사업장
주소 예외(가맹점상세주소·한글부점상세주소·영문부점상세주소)가 들어갔는데, 프롬프트
컬럼을 고르는 workflow 쪽 복사본에는 안 들어가서 그 컬럼이 통째로 빠졌다.
"""

from __future__ import annotations

import pytest

from text2sql_agent import schema, workflow

ADDRESS_QUESTION = "한신포차 가맹점주 가맹점 도로명 주소 2026년 5월 기준으로 알려줘"


def test_business_address_reaches_the_prompt() -> None:
    ranked = workflow._rule_rank_tables(ADDRESS_QUESTION)

    assert ranked == ["tbdaadt01"], ranked
    assert "- 가맹점상세주소 [" in workflow._table_details(ranked, ADDRESS_QUESTION)


@pytest.mark.parametrize(
    ("table_name", "column"),
    [
        ("tbdaadt01", "가맹점상세주소"),
        ("tmdaa5d01", "가맹점상세주소"),
        ("tbdaacb02", "한글부점상세주소"),
    ],
)
def test_prompt_and_sql_agree_on_business_addresses(table_name: str, column: str) -> None:
    """두 판정이 갈리면 프롬프트엔 없는 컬럼이 검증엔 통과하거나 그 반대가 된다."""
    table = next(item for item in schema.SCHEMA["tables"] if item["name"] == table_name)
    visible = {item["name"] for item in workflow._visible_table_columns(table, "dimensions")}

    assert column in visible
    assert not schema._is_restricted_column(table, column)


@pytest.mark.parametrize(
    ("table_name", "column"),
    [
        ("tbdaaat18", "자택상세주소"),
        ("tbdaaat18", "고객대표상세주소"),
    ],
)
def test_personal_addresses_stay_out_of_the_prompt(table_name: str, column: str) -> None:
    table = next(item for item in schema.SCHEMA["tables"] if item["name"] == table_name)
    visible = {item["name"] for item in workflow._visible_table_columns(table, "dimensions")}

    assert column not in visible
    assert schema._is_restricted_column(table, column)


def test_missing_month_answers_from_the_master_and_says_so() -> None:
    """마스터에 없는 달을 물으면 최신 실적기준일자로 답하고 그 사실을 밝힌다."""
    sql = (
        'SELECT m."가맹점번호", m."가맹점명", m."가맹점상세주소", m."주소표시구분코드"\n'
        "FROM tbdaadt01 m\n"
        "WHERE LOWER(COALESCE(m.\"가맹점명\", '')) LIKE LOWER(CONCAT('%', '한신포차', '%'))"
    )
    routed = workflow._apply_accumulation_historical_sources(ADDRESS_QUESTION, sql)
    note = workflow._implicit_time_basis_note(ADDRESS_QUESTION, routed)

    assert "tbdaadt01" in routed
    assert "tmdaa5d01" not in routed
    assert '"실적기준년월일" = ' in routed
    assert "202605" in note
    assert "최신 실적기준년월일" in note

# 우편번호도 같은 속성이 답한다. 전표(tbdaabt30)가 같은 값을 "가맹점우편번호" 라는
# 이름으로 복사해 갖고 있어서, 질문이 컬럼명을 그대로 불렀다는 가점이 사본 쪽에
# 붙고 마스터가 밀렸다.
POSTAL_QUESTIONS = [
    "한신포차 가맹점 우편번호 알려줘",
    "도미노피자 가맹점 우편번호 알려줘",
    "한신포차 가맹점주 가맹점 우편번호 2026년 5월 기준으로 알려줘",
]


@pytest.mark.parametrize("question", POSTAL_QUESTIONS)
def test_merchant_postal_code_reads_the_master(question: str) -> None:
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == "tbdaadt01", (question, ranked)
    details = workflow._table_details(ranked, question)
    assert "- 우편번호 [" in details
    assert "- 도로명우편번호 [" in details


def test_postal_question_is_a_master_attribute_question() -> None:
    """마스터 한 곳만 가리키는 속성이 걸려야 "없는 달은 최신 기준" 안내까지 이어진다."""
    question = "한신포차 가맹점주 가맹점 우편번호 2026년 5월 기준으로 알려줘"

    assert workflow._merchant_master_attribute_question(question)
    assert workflow._recent_merchant_time_route(question)[0] == "master"


def test_customer_postal_questions_stay_out_of_the_merchant_master() -> None:
    """"우편번호" 한 낱말은 별칭에 넣지 않았다. 고객 자택·직장 주소는 다른 질문이다."""
    ranked = workflow._rule_rank_tables("고객 자택 우편번호 알려줘")

    assert ranked[0] != "tbdaadt01", ranked
    assert not workflow._merchant_master_attribute_question("고객 자택 우편번호 알려줘")


def test_slip_copy_is_called_out_in_the_prompt() -> None:
    summary = schema.build_semantic_attributes_summary(
        schema.SCHEMA, "한신포차 가맹점 우편번호 알려줘", "merchant_sales"
    )

    assert "가맹점 우편번호" in summary
    assert "전표(tbdaabt30)의 가맹점우편번호는 전표 시점의 사본이다" in summary


def test_single_source_attribute_outranks_a_denormalized_copy() -> None:
    """원천을 한 곳만 선언한 속성은 그 원천이 1순위여야 한다."""
    attribute = next(
        item
        for item in schema.SCHEMA["semantic_attributes"]
        if item["name"] == "merchant_address"
    )
    sources = {mapping["table"] for mapping in attribute["source_mappings"]}

    assert sources == {"tbdaadt01"}
    assert workflow._SINGLE_SOURCE_ATTRIBUTE_WEIGHT >= 40

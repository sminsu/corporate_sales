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
UNDATED_ADDRESS_QUESTION = "한신포차 가맹점주 가맹점 도로명 주소 알려줘"


@pytest.mark.parametrize(
    ("question", "expected_source"),
    [
        (UNDATED_ADDRESS_QUESTION, "tbdaadt01"),
        (ADDRESS_QUESTION, "tmdaa5d01"),
    ],
)
def test_business_address_reaches_the_prompt(question: str, expected_source: str) -> None:
    """기간에 맞는 원천이 1순위이고, 그 원천의 주소 본문 컬럼이 프롬프트에 있어야 한다."""
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == expected_source, (question, ranked)
    assert "- 가맹점상세주소 [" in workflow._table_details(ranked, question)


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


def _address_sql(table: str) -> str:
    return (
        'SELECT m."가맹점번호", m."가맹점명", m."가맹점상세주소", m."주소표시구분코드"\n'
        f"FROM {table} m\n"
        "WHERE LOWER(COALESCE(m.\"가맹점명\", '')) LIKE LOWER(CONCAT('%', '한신포차', '%'))"
    )


def test_a_past_month_reads_the_monthly_snapshot() -> None:
    """마스터는 최근 며칠만 들고 있다. 지난 달 주소는 월 스냅샷이 답해야 한다."""
    routed = workflow._apply_accumulation_historical_sources(
        ADDRESS_QUESTION, _address_sql("tbdaadt01")
    )

    assert "tmdaa5d01" in routed
    assert "tbdaadt01" not in routed
    assert '"기준년월" = \'202605\'' in routed
    # 요청한 달을 그대로 읽었으니 "다른 시점으로 대신 답했다" 는 안내가 필요 없다.
    assert workflow._implicit_time_basis_note(ADDRESS_QUESTION, routed) == ""


def test_the_open_month_answers_from_the_master_by_month() -> None:
    """월 스냅샷이 아직 없는 달은 마스터가 답하되, 단위는 일자가 아니라 달이다."""
    question = "한신포차 가맹점주 가맹점 도로명 주소 이번 달 기준으로 알려줘"
    routed = workflow._apply_accumulation_historical_sources(
        question, _address_sql("tbdaadt01")
    )
    note = workflow._implicit_time_basis_note(question, routed)

    assert "tbdaadt01" in routed
    assert f'SUBSTR(m."{workflow._TBDAADT01_TIME_COLUMN}", 1, 6) = ' in routed
    assert workflow._current_ym() in routed
    assert workflow._TBDAADT01_MONTH_LABEL in note

# 우편번호도 같은 속성이 답한다. 전표(tbdaabt30)가 같은 값을 "가맹점우편번호" 라는
# 이름으로 복사해 갖고 있어서, 질문이 컬럼명을 그대로 불렀다는 가점이 사본 쪽에
# 붙고 마스터가 밀렸다.
POSTAL_QUESTIONS = [
    ("한신포차 가맹점 우편번호 알려줘", "tbdaadt01"),
    ("도미노피자 가맹점 우편번호 알려줘", "tbdaadt01"),
    ("한신포차 가맹점주 가맹점 우편번호 2026년 5월 기준으로 알려줘", "tmdaa5d01"),
]


@pytest.mark.parametrize(("question", "expected_source"), POSTAL_QUESTIONS)
def test_merchant_postal_code_reads_the_declared_source(
    question: str, expected_source: str
) -> None:
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == expected_source, (question, ranked)
    details = workflow._table_details(ranked, question)
    assert "- 우편번호 [" in details
    assert "- 도로명우편번호 [" in details


def test_an_undated_postal_question_is_a_master_attribute_question() -> None:
    """기간을 말하지 않으면 현재 상태를 묻는 것이고, 그 원천은 마스터다."""
    question = "한신포차 가맹점주 가맹점 우편번호 알려줘"

    assert workflow._merchant_master_attribute_question(question)
    assert workflow._recent_merchant_time_route(question)[0] == ""


def test_a_past_month_postal_question_leaves_the_master() -> None:
    """지난 달을 물으면 마스터가 아니라 그 달을 들고 있는 월 스냅샷이 답한다."""
    question = "한신포차 가맹점주 가맹점 우편번호 2026년 5월 기준으로 알려줘"

    assert not workflow._merchant_master_attribute_question(question)
    assert workflow._recent_merchant_time_route(question) == ("monthly", "202605", "202605")


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


@pytest.mark.parametrize(
    ("question", "expected_source"),
    [
        ("한신포차 가맹점 우편번호 알려줘", "tbdaadt01"),
        ("한신포차 가맹점주 가맹점 우편번호 2026년 5월 기준으로 알려줘", "tmdaa5d01"),
    ],
)
def test_single_source_attribute_outranks_a_denormalized_copy(
    question: str, expected_source: str
) -> None:
    """한 기간에 원천을 하나만 고르는 속성은 그 원천이 1순위여야 한다.

    속성은 현재·과거 원천 둘을 선언하지만 한 질문이 고르는 원천은 하나다. 가점은
    그 해석된 원천에 붙어야 전표의 사본("가맹점우편번호")을 이긴다.
    """
    attribute = next(
        item
        for item in schema.SCHEMA["semantic_attributes"]
        if item["name"] == "merchant_address"
    )
    sources = {
        mapping["table"]
        for mapping in workflow._attribute_source_mappings(question, attribute)
    }

    assert sources == {expected_source}
    assert workflow._SINGLE_SOURCE_ATTRIBUTE_WEIGHT >= 40

"""지역별 질의는 행정구역 컬럼을 든 테이블과 지표 테이블을 함께 받아야 한다.

    도미노피자 2026년 매출액 광역시도별 증감내역 알려줘

이 질문이 지역 없이 답했다. 세 가지가 겹쳐 있었다.

1. 시도·시군구·동 컬럼(한글시도명·한글시군구명·법정동명·행정동명)은 가맹점요약
   tbdaaus01·tmdaaus01 두 곳에만 있는데, 그 사실을 말하는 semantic attribute 가
   없었다. 주소 속성(merchant_address)은 가맹점상세주소라는 한 덩어리 문자열만
   가리켜 지역으로 묶는 데 못 쓴다.
2. 표면형이 없었다. 동의어 매칭이 어절 경계를 보므로 "광역시도별" 의 '시도' 는
   앞에 '광역' 이 붙어 동의어 '시도' 에 안 걸린다. 사용자가 부르는 말(광역시도·
   광역시·지역·소재지·권역)이 컬럼 동의어에 하나도 없어서, 테이블이 후보에
   들어도 지역 컬럼이 프롬프트 컬럼 예산에서 잘렸다.
3. 매출 지표는 가맹점월실적(tmdaa5e11)에 있다. 지역으로 쪼개려면 가맹점요약을
   기준년월+가맹점번호로 조인해야 하는데, 라우팅은 매출을 전표(tbdaabt30)에서
   찾았고 전표에는 가맹점요약으로 가는 조인 경로가 없다.
"""

from __future__ import annotations

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import SCHEMA, semantic_query_contract_candidates
from text2sql_agent.v2.vq_output_guard import _axis_terms

REGION_TABLES = ("tbdaaus01", "tmdaaus01")
SALES_TABLE = "tmdaa5e11"
REGION_CONTRACT = "merchant_monthly_sales_by_region"

# 사용자가 지역 단계를 부르는 말. 어느 것으로 물어도 시도 컬럼이 프롬프트에 남아야 한다.
REGION_PHRASINGS = [
    "도미노피자 2026년 매출액 광역시도별 증감내역 알려줘",
    "지역별 가맹점 매출 알려줘",
    "시도별 가맹점 매출금액 보여줘",
    "시군구별 가맹점 매출 상위 20곳",
    "경기도 시별 가맹점 매출",
    "서울 구별 가맹점 매출",
    "도별 가맹점 매출",
    "동별 가맹점 매출",
    "권역별 가맹점 매출",
    "가맹점 소재지별 매출",
    "시군구별 가맹점 수 알려줘",
]

# 지역을 묻지 않은 질문. 두 글자 표면형이 다른 말에 걸리면 가맹점요약이 끌려온다.
NON_REGION_QUESTIONS = [
    "연도별 매출 추이 보여줘",
    "업종별 매출 알려줘",
    "신용카드와 체크카드를 구별해서 이용금액 알려줘",
    "일시별 매출 알려줘",
    "활동별 매출 알려줘",
    "도미노피자 최근 1년 매출액 알려줘",
]


def _prompt(question: str) -> str:
    ranked = workflow._rule_rank_tables(question, max_tables=6)
    return workflow._table_details(ranked, question, max_columns=24, max_total_columns=48)


def _has_column(details: str, column: str) -> bool:
    return f"- {column} [" in details


@pytest.mark.parametrize("question", REGION_PHRASINGS)
def test_region_questions_keep_the_province_column_in_the_prompt(question: str) -> None:
    assert _has_column(_prompt(question), "한글시도명"), question


@pytest.mark.parametrize("question", NON_REGION_QUESTIONS)
def test_non_region_questions_do_not_pull_in_the_region_tables(question: str) -> None:
    ranked = workflow._rule_rank_tables(question, max_tables=6)
    assert not set(ranked) & set(REGION_TABLES), (question, ranked)


def test_region_sales_questions_get_both_the_metric_and_the_region_table() -> None:
    """지역 컬럼과 매출 컬럼이 다른 테이블에 있다. 둘이 함께 와야 조인할 수 있다."""
    for question in (
        "도미노피자 2026년 매출액 광역시도별 증감내역 알려줘",
        "지역별 가맹점 매출 알려줘",
        "시군구별 가맹점 매출 상위 20곳",
    ):
        ranked = workflow._rule_rank_tables(question, max_tables=6)
        assert set(ranked) == {SALES_TABLE, "tmdaaus01"}, (question, ranked)

        details = _prompt(question)
        assert _has_column(details, "한글시도명"), question
        assert _has_column(details, "가맹점일시불매출금액"), question
        assert 'tmdaaus01."기준년월" = tmdaa5e11."기준년월"' in details, question


def test_previous_month_acquisition_by_region_stays_on_the_summary_table() -> None:
    """전월매입금액은 가맹점요약이 제 컬럼으로 들고 있다. 조인이 필요 없다."""
    question = "2025년 11월 가맹점 전월 할부 매입금액의 시도명별 구성비를 알려줘"
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == "tmdaaus01", ranked
    assert not [c["name"] for c in semantic_query_contract_candidates(SCHEMA, question, max_count=2)]


def test_region_contract_declares_the_join_it_needs() -> None:
    contract = next(
        item
        for item in SCHEMA["semantic_query_contracts"]
        if item["name"] == REGION_CONTRACT
    )
    paths = {
        path["name"]: path
        for path in SCHEMA["semantic_join_graph"]["safe_paths"]
    }

    assert contract["source_tables"] == [SALES_TABLE, "tmdaaus01"]
    assert contract["join_paths"] == ["merchant_enrichment_to_monthly_performance"]

    path = paths["merchant_enrichment_to_monthly_performance"]
    assert {path["from_table"], path["to_table"]} == {"tmdaaus01", SALES_TABLE}
    assert '"기준년월"' in path["sql"] and '"가맹점번호"' in path["sql"]


def test_region_columns_live_only_in_the_two_summary_tables() -> None:
    """속성이 원천을 두 곳으로 못 박는다. 세 번째 사본이 생기면 선언이 거짓이 된다."""
    owners = {
        str(table["name"])
        for table in SCHEMA["tables"]
        for section in ("dimensions", "measures", "time_dimensions")
        for column in table.get(section) or []
        if str(column.get("name")) == "한글시도명"
    }

    assert owners == set(REGION_TABLES)


def test_region_attribute_maps_every_level_to_its_own_column() -> None:
    attribute = next(
        item
        for item in SCHEMA["semantic_attributes"]
        if item["name"] == "merchant_region"
    )

    for mapping in attribute["source_mappings"]:
        assert mapping["table"] in REGION_TABLES
        assert mapping["columns"][:4] == ["한글시도명", "한글시군구명", "법정동명", "행정동명"]
    assert {mapping["role"] for mapping in attribute["source_mappings"]} == {
        "current_merchant_region",
        "monthly_merchant_region",
    }


def test_region_sales_template_answers_the_question_as_typed() -> None:
    """참고 SQL 이 이 말투에 실제로 걸리고, 되묻기 없이 파라미터가 다 채워져야 한다."""
    question = "도미노피자 2026년 매출액 광역시도별 증감내역 알려줘"
    query = next(
        item
        for item in workflow.VERIFIED_QUERIES
        if item["name"] == "merchant_sales_by_region_comparison"
    )
    specs = [{"name": name, **spec} for name, spec in query["parameters"].items()]
    extracted = workflow._extract_params_by_rule(question, specs)

    assert workflow._verified_query_matches_intent(question, query)
    assert extracted == {"가맹점명": "도미노피자", "기간_시작": "202601", "기간_종료": "202612"}
    assert workflow._missing_vq_required_params(query["parameters"], extracted) == []
    # 비교 기준은 전년 동기 고정이라 되묻지 않는다. 202601 - 100 = 202501.
    assert "CAST('{기간_시작}' AS INTEGER) - 100" in query["sql"]


def test_metric_word_before_the_axis_is_not_read_as_part_of_the_axis() -> None:
    """"2026년 매출액 광역시도별" 의 축은 '매출액 광역시도' 가 아니라 '광역시도' 다.

    조사가 없으면 앞말 규칙이 지표 이름을 못 떼어 내, 답을 낼 수 있는 참고 SQL 까지
    "그 축이 GROUP BY 에 없다" 는 이유로 거부됐다.
    """
    assert _axis_terms("도미노피자 2026년 매출액 광역시도별 증감내역 알려줘") == ["광역시도"]
    assert _axis_terms("2026년 매출 지역별 추이") == ["지역"]
    assert _axis_terms("2026년 1월 이용금액 등급별로 보여줘") == ["등급"]
    # 축 자체가 지표로 끝나면 뗄 것이 없다.
    assert _axis_terms("통합한도별로 현재 기준 유효신용카드수를 보여줘") == ["통합한도"]
    assert _axis_terms("업종별 매출금액을 보여줘") == ["업종"]

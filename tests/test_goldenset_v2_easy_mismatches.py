"""goldenset v2 mismatch 중 difficulty=easy 63건에서 역산한 회귀 테스트.

원인은 네 가지였다.

1. 참고 SQL 템플릿 통째 실행/복사 (29건)
   매칭 게이트가 어휘 겹침만 봐서, 같은 업무 영역 단어를 쓰기만 하면 전혀 다른
   컬럼 구성의 완제품 SQL이 그대로 나갔다.
2. 그룹핑 축 어구가 엔티티 이름으로 추출 (3건)
   "회원자격별로 현재 기준 ..." 의 기업명 파라미터가 '회원자격별로' 로 채워져
   LIKE '%회원자격별로%' 가 걸리고 0행이 나왔다.
3. 기업영업 스코프 필터 누락 (18건)
   정답 452건 중 276건이 개인기업구분코드 = '2' 를 쓰는데 모델은 99건만 붙였다.
4. tbmaisd06 연 단위 적재 조회 정책 누락 (7건)
   물리 primary_time_dimension 은 편입일을 유지하되, 연 단위 조회는
   accumulation_policy 의 기준년을 쓰도록 분리한다.

medium 130건을 같은 방식으로 본 뒤 아래 세 가지를 덧붙였다(맨 아래 절).

5. 기간 어구가 가맹점명으로 추출 — "2026년 상반기 … 가맹점" 의 '상반기'
6. 개수 질문에 목록형 VQ 매칭 — "기업 수와 총한도" 에 기업별 한도 목록
7. 소수 테이블만 가진 컬럼을 테이블 선택 근거로 사용
"""

from __future__ import annotations

import pytest

from text2sql_agent import workflow
from text2sql_agent.schema import (
    SCHEMA,
    find_relevant_queries,
    semantic_query_contract_candidates,
)
from text2sql_agent.v2.vq_output_guard import vq_output_gap


def _vq(name: str) -> dict:
    matched = next((vq for vq in workflow.VERIFIED_QUERIES if vq.get("name") == name), None)
    assert matched is not None, f"verified query 없음: {name}"
    return matched


# ---------------------------------------------------------------------------
# 1. 참고 SQL 템플릿 bleed-through
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "vq_name"),
    [
        # 기간: VQ가 전 기간 집계라 "2024년"을 표현할 수 없다.
        ("2024년 자산건전성 분류별 특수채권편입원금을 알려줘", "special_debt_by_asset_quality"),
        ("2024년 평균 연체회차를 자산건전성 분류 기준으로 집계해줘", "special_debt_by_asset_quality"),
        ("2026년 7월 법인카드 일별 매출금액을 보여줘", "daily_sales_amount"),
        # 축: 질문이 지목한 축이 VQ의 GROUP BY에 없다.
        ("회원자격별로 현재 기준 통합한도금액을 보여줘", "corporate_limit_status_at_month"),
        ("통합한도별로 현재 기준 유효신용카드수를 보여줘", "corporate_limit_status_at_month"),
        ("2026년 7월 31일 기준 연체연회비를 회원자격 기준으로 집계해줘", "card_delinquency_by_grade"),
        # 지표: 질문이 컬럼명을 그대로 불렀는데 VQ SQL에 없다.
        ("카드등급별로 2025년 6월 30일 기준 할부연체원금을 보여줘", "card_delinquency_by_grade"),
        ("2026년 3월 31일 기준 CA연체원금을 카드등급 기준으로 집계해줘", "card_delinquency_by_grade"),
        ("카드등급별로 현재 기준 유효체크카드수를 보여줘", "card_delinquency_by_grade"),
        ("카드등급별로 현재 기준 평균 통합 BSS 평점을 보여줘", "card_delinquency_by_grade"),
        ("카드등급별로 2026년 1월 금월이용합계건수를 보여줘", "card_delinquency_by_grade"),
        ("2025년 8월 금월취소이용금액을 카드브랜드 기준으로 집계해줘", "card_brand_usage_analysis"),
        ("MCC 업종코드와 업종코드명 목록을 보여줘", "active_merchant_industry_codes"),
        # 대상 정의 개념: VQ만 "신규 가입" 으로 좁힌다.
        ("2026년 7월 기준 소기업로 분류된 기업고객이 몇 곳인지 알려줘", "new_customers_by_month"),
        ("고객 상태구분코드별 기업고객 수를 알려줘", "new_customers_by_month"),
    ],
)
def test_vq_is_rejected_when_it_cannot_produce_the_requested_output(
    question: str,
    vq_name: str,
) -> None:
    assert vq_output_gap(question, _vq(vq_name), column_names=workflow._schema_column_names())
    assert not workflow._verified_query_matches_intent(question, _vq(vq_name))


@pytest.mark.parametrize(
    ("question", "vq_name"),
    [
        ("기업 특수채권을 자산건전성별로 요약해줘", "special_debt_by_asset_quality"),
        ("카드등급별 연체 현황은?", "card_delinquency_by_grade"),
        ("일별 매출금액을 알려줘", "daily_sales_amount"),
        ("사용 중인 가맹점 업종 코드명 알려줘", "active_merchant_industry_codes"),
        ("12월 신규 고객 알려줘", "new_customers_by_month"),
        ("업종별 매출금액을 보여줘", "sales_by_industry"),
        ("업종별 평균 수수료율은?", "fee_rate_by_industry"),
        ("2026년 1월부터 6월까지 카드 브랜드별 법인카드 이용 현황을 분석해줘", "card_brand_usage_analysis"),
        ("2026년 6월 기준 기업별 총한도, 잔여한도와 한도소진율을 알려줘", "corporate_limit_status_at_month"),
        (
            "내가 관리하고 있는 기업회원 중 최근 6개월 이용금액 추이를 보여주고, 특이사항 있는 업체를 알려줘",
            "managed_company_usage_anomalies",
        ),
    ],
)
def test_matching_vq_is_still_accepted(question: str, vq_name: str) -> None:
    assert vq_output_gap(question, _vq(vq_name), column_names=workflow._schema_column_names()) == ""


@pytest.mark.parametrize(
    ("question", "domain", "leaked_table"),
    [
        ("2024년 자산건전성 분류별 특수채권편입원금을 알려줘", "credit_risk", "tbmaisd06"),
        ("카드등급별로 2025년 6월 30일 기준 할부연체원금을 보여줘", "credit_risk", "tmdaa3e16"),
        ("2026년 7월 법인카드 일별 매출금액을 보여줘", "card_usage", "tbdaabt30"),
    ],
)
def test_prompt_does_not_show_a_reference_sql_that_cannot_answer(
    question: str,
    domain: str,
    leaked_table: str,
) -> None:
    """로컬 모델은 프롬프트의 참고 SQL을 거의 그대로 베낀다. 애초에 안 보여준다."""
    rendered = find_relevant_queries(SCHEMA, question, domain_name=domain)
    assert leaked_table not in rendered


# ---------------------------------------------------------------------------
# 2. 그룹핑 축 어구는 엔티티 이름이 아니다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question",
    [
        "회원자격별로 현재 기준 통합한도금액을 보여줘",
        "CA한도별로 2026년 4월 30일 기준 채권잔액을 보여줘",
        "통합한도별로 현재 기준 유효신용카드수를 보여줘",
        "연체채권 종류별로 2026년 특수채권편입가지급금을 보여줘",
    ],
)
def test_grouping_axis_is_not_bound_as_an_entity_name(question: str) -> None:
    specs = [{"name": "기업명", "type": "string", "description": "기업명"}]
    assert workflow._extract_params_by_rule(question, specs) == {}
    assert workflow._extract_merchant_name_by_rule(question) == ""


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("쿠팡의 2026년 상반기 월평균 기업카드 이용금액을 알려줘", "쿠팡"),
        ("삼성전자 현재 총한도와 잔여한도를 알려줘", "삼성전자"),
    ],
)
def test_real_company_names_are_still_extracted(question: str, expected: str) -> None:
    specs = [{"name": "기업명", "type": "string", "description": "기업명"}]
    assert workflow._extract_params_by_rule(question, specs) == {"기업명": expected}


# ---------------------------------------------------------------------------
# 3. 기업영업 스코프
# ---------------------------------------------------------------------------
def test_corporate_scope_rule_names_the_tables_that_carry_the_discriminator() -> None:
    rule = workflow._corporate_scope_rule(["tbmaisd06", "tmdaa5e11"])
    assert '"개인기업구분코드" = \'2\'' in rule
    assert "tbmaisd06" in rule
    # 컬럼이 없는 테이블에는 조건을 만들라고 하지 않는다.
    assert "tmdaa5e11" not in rule


def test_corporate_scope_rule_is_silent_without_a_discriminator_column() -> None:
    assert workflow._corporate_scope_rule(["tmdaa5e11", "tbdaabf07"]) == ""


def test_corporate_scope_is_a_default_in_the_sql_generation_contract() -> None:
    rules = " ".join(SCHEMA["sql_generation_contract"]["ambiguity_rules"])
    assert "질문이 개인·전체를 명시하지 않는 한" in rules


# ---------------------------------------------------------------------------
# 4. tbmaisd06 시간축
# ---------------------------------------------------------------------------
def test_special_debt_keeps_physical_axis_and_uses_yearly_query_policy() -> None:
    table = next(t for t in SCHEMA["tables"] if t["name"] == "tbmaisd06")
    assert table["primary_time_dimension"] == "특수채권편입기준년월일"
    year = next(c for c in table["time_dimensions"] if c["name"] == "기준년")
    assert year["format"] == "YYYY"
    assert table["accumulation_policy"] == {
        "cadence": "yearly",
        "query_time_dimension": "기준년",
        "format": "YYYY",
    }

    metrics = [m for m in SCHEMA["canonical_metrics"] if m.get("source_table") == "tbmaisd06"]
    assert metrics
    assert all(m["default_time_dimension"] == "특수채권편입기준년월일" for m in metrics)


def test_yearly_tables_tell_the_model_to_filter_on_the_load_year() -> None:
    question = "2024년 특수채권편입수수료를 자산건전성 분류 기준으로 집계해줘"
    instruction = workflow._time_resolution_instruction(question, ["tbmaisd06"])
    assert "tbmaisd06: 매년 · 기간 컬럼 기준년(YYYY)" in instruction
    assert "`기준년`의 YYYY로 축약합니다" in instruction


# ---------------------------------------------------------------------------
# 5. 테이블 선택
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "expected_table"),
    [
        ("현재 기준 CA한도별 CA한도금액을 알려줘", "tbdaaat03"),
        ("현재 기준 카드등급별 부서사업장이용한도금액을 알려줘", "tbdaaat03"),
        ("현재 기준 통합한도별 기업회원이용한도금액을 알려줘", "tbdaaat03"),
        ("현재 기준 회원자격별 결제단위수를 알려줘", "tbdaaat03"),
        ("2026년 7월 31일 기준 연체합계금액을 CA한도 기준으로 집계해줘", "tddaa3l01"),
        ("카드등급별로 2025년 6월 30일 기준 할부연체원금을 보여줘", "tddaa3l01"),
        ("2026년 7월 기준 가맹점 월 승인한도금액이 큰 가맹점 20곳을 알려줘", "tmdaa5e11"),
        ("2026년 7월 가맹점 미수금 등록금액과 회수금액을 알려줘", "tmdaa5e11"),
        ("2026년 7월 법인카드 금월 이용금액과 금월 청구금액을 비교해서 보여줘", "tmdaa3e16"),
    ],
)
def test_a_table_that_owns_the_named_column_becomes_a_candidate(
    question: str,
    expected_table: str,
) -> None:
    """질문이 컬럼명을 그대로 부르면 그 컬럼을 가진 테이블이 후보에 들어야 한다.

    이전에는 named_corporate_limit_status_at_month 계약이 "한도" 한 단어로 매칭돼
    tbdaa1d12 를 authoritative 로 못박고 점수 계산을 통째로 건너뛰었다.
    """
    assert expected_table in workflow._rule_rank_tables(question)


@pytest.mark.parametrize(
    "question",
    [
        "쿠팡의 2026년 6월 한도를 알려줘",
        "쿠팡의 2026년 6월 총한도와 잔여한도를 알려줘",
    ],
)
def test_named_limit_questions_still_pin_the_authoritative_table(question: str) -> None:
    """기업명이 있으면 계약이 계속 테이블을 확정한다.

    어느 물리 테이블인지는 계약의 source_table_policy(현재 스냅샷 vs 과거 월)가
    정하므로 이름을 박지 않는다. 여기서 지키려는 건 "authoritative 지름길이
    살아 있다" 는 것이다.
    """
    contract = semantic_query_contract_candidates(SCHEMA, question, max_count=1)[0]
    assert str(contract.get("table_selection_mode")).lower() == "authoritative"
    assert workflow._contract_entity_bindings_available(question, contract)
    assert workflow._rule_rank_tables(question) == [
        str(table).rsplit(".", 1)[-1]
        for table in workflow._contract_source_tables(question, contract)
    ]


@pytest.mark.parametrize(
    "question",
    [
        "현재 기준 CA한도별 CA한도금액을 알려줘",
        "현재 기준 통합한도별 기업회원이용한도금액을 알려줘",
    ],
)
def test_unnamed_limit_questions_do_not_take_the_authoritative_shortcut(question: str) -> None:
    contract = semantic_query_contract_candidates(SCHEMA, question, max_count=1)[0]
    assert str(contract.get("table_selection_mode")).lower() == "authoritative"
    assert not workflow._contract_entity_bindings_available(question, contract)


def test_table_ranking_stays_interactive() -> None:
    """구분자 클래스가 한글 음절 전체라 패턴 캐시가 없으면 한 번에 18초가 걸린다."""
    import time

    question = "현재 기준 CA한도별 CA한도금액을 알려줘"
    workflow._rule_rank_tables(question)
    started = time.perf_counter()
    for _ in range(5):
        workflow._rule_rank_tables(question)
    assert (time.perf_counter() - started) / 5 < 0.5


# ---------------------------------------------------------------------------
# 5~7. medium 130건에서 추가로 나온 것
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question",
    [
        "2026년 상반기 매출 상위 10개 가맹점을 알려줘",
        "2026년 상반기 가맹점매출금액이 월별로 어떻게 변했는지 알려줘",
        "2026년 7월 기준 가맹점 대표자로 등록된 관계를 관계구분별로 보여줘",
    ],
)
def test_period_words_are_not_merchant_names(question: str) -> None:
    """'상반기'가 이름으로 잡혀 LIKE '%상반기%' 가 붙고 0행이 나왔다."""
    assert workflow._extract_merchant_name_by_rule(question) == ""


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("도미노피자 최근 6개월 월별 가맹점 매출액 추이를 보여줘", "도미노피자"),
        ("마초스테이크하우스 가맹점 기본 정보를 알려줘", "마초스테이크하우스"),
        ("교촌 치킨 가맹점주 매출 알려줘", "교촌 치킨"),
    ],
)
def test_real_merchant_names_survive(question: str, expected: str) -> None:
    assert workflow._extract_merchant_name_by_rule(question) == expected


def test_count_questions_do_not_get_a_per_entity_list_vq() -> None:
    """개수를 물었는데 기업별 한도 목록 VQ가 그대로 실행되던 자리."""
    question = "2026년 7월 기준 KB국민은행 법인여신을 보유한 기업 수와 총한도를 알려줘"
    assert workflow._asks_scalar_count(question)
    assert workflow._select_verified_query_capability(question, {}) is None


def test_grouped_count_is_blocked_by_the_axis_guard_not_the_scalar_guard() -> None:
    """"우수고객 구분별 기업 수" 는 축이 있으니 스칼라가 아니다. 축 가드가 막는다."""
    question = "2026년 7월 기준 우수고객 구분별 기업 수와 총한도 합계를 보여줘"
    assert not workflow._asks_scalar_count(question)
    assert workflow._select_verified_query_capability(question, {}) is None


@pytest.mark.parametrize(
    ("question", "matched"),
    [
        ("2026년 6월 기준 법인회원의 유실적 업체수를 알려줘", "corporate_member_with_usage_count"),
        ("도미노 브랜드 가맹점 수 몇 개야", "brand_active_merchant_count"),
        ("2026년 6월 기준 기업별 총한도, 잔여한도와 한도소진율을 알려줘", "corporate_limit_status_at_month"),
    ],
)
def test_scalar_guard_keeps_matching_vqs(question: str, matched: str) -> None:
    capability = workflow._select_verified_query_capability(question, {})
    assert capability and capability["matched_query_name"] == matched


def test_fee_rate_question_is_not_read_as_a_count() -> None:
    """'수수료'를 개수로 오인하면 업종별 수수료율 VQ가 막힌다."""
    assert not workflow._asks_scalar_count("기업 수수료율을 알려줘")


@pytest.mark.parametrize(
    ("question", "expected_table"),
    [
        ("2026년 7월 가맹점 관리부점별 법인카드 매출금액 상위 10곳을 보여줘", "tbdaabt30"),
        ("가맹점 업종 마스터에서 신용카드 수수료율이 가장 높은 업종 20개를 알려줘", "tbdaadb17"),
    ],
)
def test_distinctive_columns_pull_in_their_table(question: str, expected_table: str) -> None:
    """소수 테이블만 가진 컬럼이 질문에 잡히면 그 테이블이 후보에 올라야 한다."""
    assert expected_table in workflow._rule_rank_tables(question)

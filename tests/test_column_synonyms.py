"""컬럼명 → 질문 표면형 동의어 유도 규칙."""

from __future__ import annotations

import pytest

from text2sql_agent.v2.column_synonyms import compact, derive_column_synonyms, phrase_in_text


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        # 실패 질문에서 관측된 표면형이 결과에 있어야 한다.
        ("가맹점상태구분코드", "가맹점상태구분"),
        ("상품중분류구분코드", "상품중분류"),
        ("교부유형구분코드", "교부유형"),
        ("발급유형구분코드", "발급유형"),
        ("카드브랜드구분코드", "카드브랜드"),
        ("자산건전성분류구분코드", "자산건전성 분류"),
        ("연체채권종류구분코드", "연체채권 종류"),
        ("회계계정과목코드", "회계계정과목"),
        ("채권관리부점코드", "채권관리부점"),
        ("업종대분류코드명", "업종 대분류"),
        ("CA한도등급코드", "CA한도"),
        ("통합한도등급코드", "통합한도"),
        ("통합BSS평점", "통합 BSS 평점"),
        ("KB카드BC카드구분코드", "KB 카드 BC 카드"),
        # 여부 컬럼은 서술어만 남긴 형태가 필요하다.
        ("가맹점배달가능여부", "배달"),
        ("직영점여부", "직영점"),
        ("갱신카드여부", "갱신카드"),
    ],
)
def test_derives_observed_surface_form(column: str, expected: str) -> None:
    assert expected in derive_column_synonyms(column)


def test_skips_synonyms_already_present() -> None:
    derived = derive_column_synonyms("가맹점상태구분코드", ["가맹점상태"])
    assert "가맹점상태" not in derived
    assert "가맹점상태구분" in derived


def test_drops_generic_stems() -> None:
    """접미사를 벗겨 흔한 명사만 남으면 버린다. 아니면 모든 카드 컬럼이 '카드'로 뭉친다."""
    assert "카드" not in derive_column_synonyms("카드등급구분코드")
    assert "가맹점" not in derive_column_synonyms("가맹점상태구분코드")
    assert "고객" not in derive_column_synonyms("고객구분코드")


def test_keeps_spacing_variants_of_the_column_name_itself() -> None:
    """'통합 BSS 평점' 은 compact 하면 컬럼명과 같지만 v1 매처에는 새로운 표면형이다.

    v1 매처는 동의어에 적힌 띄어쓰기를 그대로 요구하므로, semantic layer 만 교체하는
    1단계 적용에서는 이 변형이 있어야 매칭된다. 중복으로 보고 버리면 안 된다.
    """
    derived = derive_column_synonyms("통합BSS평점")
    assert "통합 BSS 평점" in derived
    assert all(compact(term) == "통합BSS평점" for term in derived)


def test_derivation_is_stable() -> None:
    first = derive_column_synonyms("사업자자산건전성구분코드")
    second = derive_column_synonyms("사업자자산건전성구분코드")
    assert first == second
    assert len(first) == len(set(first))


@pytest.mark.parametrize(
    ("question", "phrase"),
    [
        # v1 매처가 놓쳤던 조합.
        ("2026년 7월 기준 가맹점 상태구분별 가맹점 수를 알려줘", "가맹점상태구분"),
        ("현재 기준 회원자격별 결제단위수를 알려줘", "회원자격"),          # 조사 '별'
        ("카드론평가상품별로 2026년 특수채권편입수수료를 보여줘", "카드론평가상품"),  # 조사 '별로'
        ("2024년 자산건전성 분류별 특수채권편입원금을 알려줘", "자산건전성분류"),
        ("현재 기준 평균 기업카드 신용한도금액을 보여줘", "기업카드신용한도금액"),
        ("평균 통합 BSS 평점을 보여줘", "통합BSS평점"),
        ("2026년 1월 KB카드·BC카드별 금월이용합계금액", "KB카드BC카드"),
        ("회계계정과목별로 2025년 4월 평균 기대 1년 PD율을 보여줘", "기대1년PD율"),
    ],
)
def test_matcher_ignores_spacing_and_stacked_particles(question: str, phrase: str) -> None:
    assert phrase_in_text(question, phrase)


@pytest.mark.parametrize(
    ("question", "phrase"),
    [
        # 어절 경계를 넘어 매칭되면 엉뚱한 컬럼이 선택된다.
        ("카드론평가상품별로 보여줘", "카드"),
        ("카드등급그룹별 평균 한도를 보여줘", "카드등급"),
        ("연체채권종류별 건수", "연체채"),
    ],
)
def test_matcher_respects_word_boundary(question: str, phrase: str) -> None:
    assert not phrase_in_text(question, phrase)


def test_matcher_handles_empty_input() -> None:
    assert not phrase_in_text("질문", "")
    assert not phrase_in_text("", "가맹점상태구분")
    assert not phrase_in_text("질문", None)


def test_compact_strips_separators() -> None:
    assert compact("KB카드 · BC카드") == "KB카드BC카드"
    assert compact("통합 BSS 평점") == "통합BSS평점"

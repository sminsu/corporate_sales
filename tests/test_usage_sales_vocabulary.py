"""질문이 쓴 단어가 이용금액 컬럼과 매출금액 컬럼 중 어느 쪽을 고르는지.

두 단어는 테이블마다 섞여 쓰인다. 전표(tbdaabt30·tbdaabt08)는 "매출금액",
기업 월 스냅샷(tmdaa1d12)은 "금월신용카드이용금액", 가맹점 월실적(tmdaa5e11)은
"가맹점일시불매출금액" 이다.

둘은 같은 뜻이 아니다. 이용금액은 카드로 쓴 돈, 매출금액은 가맹점이 받은 돈이라
같은 기업이라도 값이 다르다. tmdaa5e11 이 기업고객식별자를 직접 들고 있어서
"기업별 매출금액" 은 tmdaa1d12 의 이용금액과 다른 답을 낼 수 있다.
골든셋도 "법인카드 매출금액"→tbdaabt30."매출금액",
"기업 … 이용금액"→tbdaa1d12."금월*이용금액" 으로 단어를 따라 컬럼을 고른다.

그런데 어느 단어가 어느 컬럼 계열인지는 어디에도 없어서 build_glossary_summary() 가
  "가맹점별 이용금액"  → (질문과 직접 관련된 용어 없음)
  "기업별 매출금액"    → 가맹점 지표인 가맹점월매출
을 내놓고 있었다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from text2sql_agent.schema import build_glossary_summary

ROOT = Path(__file__).resolve().parents[1]
RAW_SEMANTIC = yaml.safe_load((ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))
GLOSSARY = {term["term"]: term for term in RAW_SEMANTIC["glossary"]}
TERM = GLOSSARY["이용금액"]

COLUMN_SECTIONS = ("dimensions", "measures", "time_dimensions")
TABLE_COLUMNS = {
    table["name"]: {
        column["name"]
        for section in COLUMN_SECTIONS
        for column in (table.get(section) or [])
    }
    for table in RAW_SEMANTIC["tables"]
}


# ---------------------------------------------------------------------------
# 용어 정의
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("tmdaa1d12", "금월신용카드이용금액"),
        ("tmdaa1d12", "금월체크카드이용금액"),
        ("tmdaa3e16", "금월이용합계금액"),
        ("tmdaa5e11", "가맹점일시불매출금액"),
        ("tmdaa5e11", "가맹점할부매출금액"),
        ("tbdaabt30", "매출금액"),
        ("tbdaabt08", "매출금액"),
    ],
)
def test_every_source_the_term_names_still_exists(table: str, column: str) -> None:
    """용어가 가리키는 원천이 지워지면 잘못된 컬럼을 권하게 된다."""
    assert column in TABLE_COLUMNS[table]
    assert table in TERM["sql_hint"]
    assert column in TERM["sql_hint"]


def test_term_keeps_the_two_words_apart() -> None:
    """두 단어를 같은 뜻으로 뭉치면 기업이 쓴 돈과 받은 돈이 섞인다."""
    assert "다른 지표" in TERM["canonical"]
    assert "바꿔 쓰지 않는다" in TERM["description"]


def test_hint_sends_each_word_to_its_own_column_family() -> None:
    """이용금액 안내가 이용금액 컬럼보다 매출금액 컬럼을 먼저 말하면 안 된다."""
    hint = TERM["sql_hint"]
    usage_side, _, sales_side = hint.partition("매출금액을 물으면")

    assert sales_side, hint
    # 이용금액 쪽 안내에는 이용금액 컬럼만, 매출금액 쪽에는 매출금액 컬럼만 나온다.
    assert "금월신용카드이용금액" in usage_side and "금월이용합계금액" in usage_side
    assert "가맹점일시불매출금액" not in usage_side
    assert "가맹점일시불매출금액" in sales_side and "tbdaabt30" in sales_side
    assert "금월신용카드이용금액" not in sales_side


def test_hint_names_the_key_that_makes_corporate_sales_answerable() -> None:
    """기업 단위 매출은 tmdaa5e11 의 기업고객식별자로 묶어야 나온다."""
    assert "기업고객식별자" in TERM["sql_hint"]
    assert "기업고객식별자" in TABLE_COLUMNS["tmdaa5e11"]


# ---------------------------------------------------------------------------
# 프롬프트 라우팅 — 회귀의 본체
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("question", "domain"),
    [
        ("2025년 12월 가맹점별 이용금액 상위 10곳을 보여줘", "merchant_sales"),
        ("2025년 12월 가맹점별 매출금액 상위 10곳을 보여줘", "merchant_sales"),
        ("2025년 12월 기업별 매출금액을 알려줘", "corporate_sales_targeting"),
        ("2025년 12월 기업별 이용금액을 알려줘", "corporate_sales_targeting"),
        ("작년 법인카드 이용금액 추이를 보여줘", "card_usage"),
        ("2026년 7월 법인카드 일별 매출금액을 보여줘", "card_usage"),
        ("이번달 기업별 카드 사용금액을 알려줘", "corporate_sales_targeting"),
    ],
)
def test_amount_question_reaches_the_term_in_any_domain(question: str, domain: str) -> None:
    """어느 단어로 묻든 어느 도메인으로 라우팅되든 컬럼 선택 규칙이 실려야 한다."""
    summary = build_glossary_summary(RAW_SEMANTIC, question, domain, max_count=6)

    assert "- **이용금액**" in summary, summary


def test_merchant_usage_question_no_longer_comes_back_empty() -> None:
    """v1 에서는 "가맹점별 이용금액" 에 관련 용어가 하나도 붙지 않았다."""
    summary = build_glossary_summary(
        RAW_SEMANTIC,
        "2025년 12월 가맹점별 이용금액 상위 10곳을 보여줘",
        "",
        max_count=6,
    )

    assert "가맹점일시불매출금액" in summary


def test_corporate_amount_question_ranks_the_term_above_the_merchant_metric() -> None:
    """v1 에서는 기업 질문인데 가맹점월매출만 실려 tmdaa5e11 로 끌려갔다."""
    summary = build_glossary_summary(
        RAW_SEMANTIC,
        "2025년 12월 기업별 매출금액을 알려줘",
        "",
        max_count=6,
    )
    ranked = [line[4 : line.index("**", 4)] for line in summary.splitlines() if line.startswith("- **")]

    assert "이용금액" in ranked, ranked
    if "가맹점월매출" in ranked:
        assert ranked.index("이용금액") < ranked.index("가맹점월매출"), ranked

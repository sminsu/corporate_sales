"""xlsx 상세 정의서 없이 대화로 받은 코드 목록이 실제 컬럼에 붙었는지.

사용자가 알려준 다섯 컬럼(카드소유자구분코드·카드매출유형구분코드·회원소속회사구분코드·
가맹점해지사유구분코드·가맹점거래정지여부)은 0811 xlsx 8종에 없다. 코드북 없이는 모델이
`카드소유자구분코드 = '5'` 가 기업공용이라는 것을 몰라서 코드 대신 라벨을 넣거나
없는 코드를 만들어 낸다.

코드북은 컬럼명 기준이라 같은 이름의 컬럼이 있는 테이블 전부에 적용된다. 여기서는
사용자가 지목한 테이블에 실제로 들어갔는지를 고정한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COLUMN_SECTIONS = ("dimensions", "measures", "time_dimensions")

# (컬럼, 사용자가 지목한 테이블, 전체 코드 → 라벨)
CHAT_CODEBOOKS = {
    "카드소유자구분코드": (
        "tmdaa3e16",
        {
            "1": "개인본인",
            "2": "개인가족",
            "3": "기업대표",
            "4": "기업개별",
            "5": "기업공용",
        },
    ),
    "카드매출유형구분코드": (
        "tbdaabt30",
        {
            "1": "일반",
            "2": "할부",
            "3": "현금서비스",
            "4": "리볼빙",
            "5": "예수금",
            "6": "선급급",
            "7": "현금지급(해외)",
            "9": "연회비",
            "A": "계좌매출환출",
            "B": "법적비용",
            "C": "과잉금",
        },
    ),
    "회원소속회사구분코드": (
        "tbdaabt30",
        {
            "0": "비대상",
            "1": "당사",
            "2": "제휴사",
            "3": "공동망",
            "4": "해외회원(JCB포함)",
        },
    ),
    "가맹점거래정지여부": (
        "tbdaadt01",
        {"0": "거래정지 아님", "1": "거래정지"},
    ),
    "가맹점해지사유구분코드": (
        "tmdaa5e11",
        {
            "01": "장기무실적",
            "02": "가맹점 양도*양수",
            "03": "대표자*사업자번호 변경",
            "04": "기업형태*법인번호 변경",
            "05": "신청서 허위기재",
            "06": "휴업",
            "07": "영업정지",
            "08": "자진폐업",
            "09": "강제폐업",
            "10": "현금융통 발생",
            "11": "현금융통 및 전표 대리청구 발생",
            "20": "가맹점 가입 부적격",
            "21": "카드거래 거절",
            "22": "신청서 허위기재",
            "31": "도난*분실카드 매출 다발",
            "35": "손실추정 가맹점",
            "36": "대표자 가입제한 대상",
            "38": "가맹점 수수료 불만",
            "99": "기타 해지 필요",
        },
    ),
}

# 사용자가 알려준 두 코드북은 이미 xlsx 로 들어와 있다. 같은 값인지만 확인한다.
ALREADY_COVERED = {
    ("tbdaabt30", "상품중분류구분코드"): {
        "CP51": "기업신용카드",
        "CP52": "기업정부구매카드",
        "CP53": "기업체크카드",
        "CP54": "기업직불카드",
        "CP55": "기업순구매카드",
        "CP56": "기업역구매카드",
        "CP57": "기업후납우편카드",
    },
    ("tmdaa5e11", "가맹점상태구분코드"): {"1": "정상", "2": "거래정지", "3": "해지"},
}


@pytest.fixture(scope="module")
def schema() -> dict:
    return yaml.safe_load((PROJECT_ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))


def _column(schema: dict, table_name: str, column_name: str) -> dict:
    for table in schema.get("tables", []):
        if str(table.get("name")) != table_name:
            continue
        for section in COLUMN_SECTIONS:
            for column in table.get(section) or []:
                if str(column.get("name")) == column_name:
                    return column
    raise AssertionError(f"{table_name}.{column_name} 컬럼이 없다")


@pytest.mark.parametrize("column_name", sorted(CHAT_CODEBOOKS))
def test_chat_codebook_lands_on_named_table(schema: dict, column_name: str) -> None:
    table_name, values = CHAT_CODEBOOKS[column_name]
    column = _column(schema, table_name, column_name)
    assert column.get("value_semantics") == values
    assert column.get("value_semantics_provenance") == "user_provided_business_codebook"
    assert column.get("codebook_ref") == column_name


@pytest.mark.parametrize("column_name", sorted(CHAT_CODEBOOKS))
def test_chat_codebook_applies_to_every_table_with_the_column(
    schema: dict, column_name: str
) -> None:
    """코드북은 컬럼명 기준이므로 같은 이름 컬럼은 어느 테이블에서든 같은 코드를 갖는다."""
    _table, values = CHAT_CODEBOOKS[column_name]
    holders = [
        (str(table.get("name")), column)
        for table in schema.get("tables", [])
        for section in COLUMN_SECTIONS
        for column in table.get(section) or []
        if str(column.get("name")) == column_name
    ]
    assert holders, f"{column_name} 컬럼이 없다"
    mismatched = [name for name, column in holders if column.get("value_semantics") != values]
    assert mismatched == [], f"{column_name} 코드가 다른 테이블: {mismatched}"


def test_two_digit_termination_reason_codes_keep_their_zero_pad(schema: dict) -> None:
    """'4' 가 아니라 '04' 다. VARCHAR 컬럼에서 자릿수가 어긋나면 조용히 0건이 나온다."""
    column = _column(schema, "tmdaa5e11", "가맹점해지사유구분코드")
    codes = set(column["value_semantics"])
    assert "04" in codes and "4" not in codes
    assert all(len(code) == 2 for code in codes)


@pytest.mark.parametrize(("key", "values"), sorted(ALREADY_COVERED.items()))
def test_already_covered_codebooks_match_what_the_user_gave(
    schema: dict, key: tuple[str, str], values: dict[str, str]
) -> None:
    """xlsx 로 들어온 코드북이 사용자가 알려준 값과 어긋나지 않는지."""
    table_name, column_name = key
    column = _column(schema, table_name, column_name)
    semantics = column.get("value_semantics") or {}
    assert {code: semantics.get(code) for code in values} == values


def test_chat_codebooks_are_in_the_embedded_codebook_section(schema: dict) -> None:
    """semantic_layer.yaml 에 실린 코드북 원본에도 출처가 남아 있어야 한다."""
    books = {str(book.get("column")): book for book in schema.get("column_codebooks", [])}
    for column_name, (_table, values) in CHAT_CODEBOOKS.items():
        book = books.get(column_name)
        assert book is not None, f"{column_name} 코드북이 없다"
        assert {str(v["code"]): v["label"] for v in book["values"]} == values
        assert all(str(source).startswith("chat/") for source in book["source_files"])

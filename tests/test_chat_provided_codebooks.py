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
        {"0": "정상영업", "1": "거래정지"},
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


def test_active_merchant_term_reads_the_suspension_codebook() -> None:
    """용어 "활성가맹점" 은 컬럼 상세보다 프롬프트 앞에 붙는다. 두 곳이 어긋나면
    모델은 앞엣것을 쓴다. v1 은 여기서 = 'N' 이라고 말하고 있었다."""
    schema = yaml.safe_load((PROJECT_ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))
    term = next(item for item in schema["glossary"] if item.get("term") == "활성가맹점")
    text = " ".join(str(term.get(key) or "") for key in ("canonical", "description", "sql_hint"))
    assert "'0'" in text and "'1'" in text
    assert "'N'" not in term["canonical"]
    assert "= 'N'" not in term["sql_hint"] and "'Y'" not in term["canonical"]


def test_verified_queries_filter_suspension_with_the_codebook_values() -> None:
    """참고 SQL 예시는 프롬프트에 그대로 들어가므로 여기 있는 값을 모델이 베낀다."""
    document = yaml.safe_load(
        (PROJECT_ROOT / "text2sql_agent" / "tools" / "sql_verified_queries.yaml").read_text(
            encoding="utf-8"
        )
    )
    offenders = [
        str(query.get("name"))
        for query in document.get("verified_queries") or []
        for line in str(query.get("sql") or "").splitlines()
        if "가맹점거래정지여부" in line and ("'Y'" in line or "'N'" in line)
    ]
    assert offenders == [], f"'Y'/'N' 로 거래정지를 거르는 예시: {offenders}"


@pytest.mark.parametrize(
    "question",
    ["정상영업 중인 가맹점 수 알려줘", "영업중인 도미노 가맹점 수", "거래정지된 가맹점 수 알려줘"],
)
def test_suspension_column_reaches_the_prompt_for_either_side(question: str) -> None:
    """용어가 컬럼 이름을 불러도 테이블 상세에 그 컬럼이 없으면 모델은 못 쓴다.
    '0' 쪽을 부르는 말('정상영업'·'영업중')에도 컬럼이 예산 안에 들어야 한다."""
    from text2sql_agent.workflow import _table_details

    detail = _table_details(["tbdaadt01"], question)
    line = next(
        (row for row in detail.splitlines() if "가맹점거래정지여부" in row),
        "",
    )
    assert line, f"{question}: 가맹점거래정지여부가 프롬프트 컬럼에 없다"
    assert '"0": "정상영업"' in line and '"1": "거래정지"' in line


@pytest.mark.parametrize(
    ("question", "column_name", "code", "label"),
    [
        ("2025년 6월 기업체크카드 매출금액 알려줘", "상품중분류구분코드", "CP53", "기업체크카드"),
        ("기업정부구매카드 이용금액 알려줘", "상품중분류구분코드", "CP52", "기업정부구매카드"),
        ("기업신용카드와 기업체크카드 매출 비교해줘", "상품중분류구분코드", "CP51", "기업신용카드"),
        ("2025년 6월 할부 매출금액 알려줘", "카드매출유형구분코드", "2", "할부"),
        ("현금서비스 이용금액 얼마야", "카드매출유형구분코드", "3", "현금서비스"),
        ("리볼빙 매출 건수 알려줘", "카드매출유형구분코드", "4", "리볼빙"),
        ("연회비 매출 알려줘", "카드매출유형구분코드", "9", "연회비"),
    ],
)
def test_code_label_pulls_its_column_into_the_prompt(
    question: str, column_name: str, code: str, label: str
) -> None:
    """질문이 코드 라벨만 부를 때도 그 코드 컬럼이 프롬프트 예산 안에 들어야 한다.

    코드 컬럼은 이름으로 불리지 않는다. "기업체크카드 매출" 은 상품중분류구분코드를
    한 번도 말하지 않는데, 그 컬럼이 테이블 상세에서 잘려나가면 모델은 CP53 으로
    거르지 못하고 개인카드까지 합친 전체 매출을 답한다.
    """
    from text2sql_agent.workflow import _table_details

    detail = _table_details(["tbdaabt30"], question)
    line = next((row for row in detail.splitlines() if column_name in row), "")
    assert line, f"{question}: {column_name} 이 프롬프트 컬럼에 없다"
    assert f'"{code}": "{label}"' in line


@pytest.mark.parametrize(
    ("question", "column_name"),
    [
        ("법인체크카드 매출 알려줘", "상품중분류구분코드"),
        ("법인신용카드 매출 알려줘", "상품중분류구분코드"),
        ("법인 정부구매카드 이용금액", "상품중분류구분코드"),
        ("일시불 매출금액 알려줘", "카드매출유형구분코드"),
        ("법인카드 일시불 매출 건수", "카드매출유형구분코드"),
    ],
)
def test_business_wording_reaches_the_same_code_column(question: str, column_name: str) -> None:
    """코드북 라벨과 현업 표현이 다른 두 자리를 잇는다.

    라벨은 '기업체크카드'·'일반' 인데 질문은 '법인체크카드'·'일시불' 로 온다.
    '기업'↔'법인' 은 라벨 표면형으로, '일시불' 은 코드북 별칭으로 처리한다.
    """
    from text2sql_agent.workflow import _table_details

    detail = _table_details(["tbdaabt30"], question)
    assert any(column_name in row for row in detail.splitlines()), (
        f"{question}: {column_name} 이 프롬프트 컬럼에 없다"
    )


def test_installment_free_alias_reaches_the_prompt_with_its_code(schema: dict) -> None:
    """별칭이 컬럼에만 붙고 프롬프트에 안 실리면, 모델은 일시불이 '1' 인 줄 모른다."""
    from text2sql_agent.workflow import _table_details

    column = _column(schema, "tbdaabt30", "카드매출유형구분코드")
    assert column.get("value_aliases") == {"1": ["일시불"]}

    detail = _table_details(["tbdaabt30"], "일시불 매출금액 알려줘")
    line = next(row for row in detail.splitlines() if "카드매출유형구분코드" in row)
    assert '"1": ["일시불"]' in line


CREDIT_CHECK_DEBIT_ALIASES = {
    "CP51": ["기업신용", "신용카드"],
    "CP52": ["기업신용", "신용카드"],
    "CP53": ["기업체크", "체크카드"],
    "CP54": ["기업직불", "직불카드"],
}


def test_card_kind_aliases_split_the_product_class_codes(schema: dict) -> None:
    """신용·체크·직불을 가르는 키는 상품중분류구분코드 하나다.

    라벨만으로는 CP52(기업정부구매카드)가 신용 쪽이라는 것을 알 수 없다. 사용자가
    지정한 구분(CP51·CP52 신용 / CP53 체크 / CP54 직불)을 별칭으로 못 박는다.
    """
    column = _column(schema, "tbdaabt30", "상품중분류구분코드")
    assert column.get("value_aliases") == CREDIT_CHECK_DEBIT_ALIASES


@pytest.mark.parametrize(
    "question",
    [
        # 라벨은 '기업신용카드' 인데 질문은 복합어로 붙여 쓴다.
        "26년 7월 9일 기준 기업신용매출건수랑 금액",
        "2026년 7월 기업체크매출 금액 알려줘",
        "2026년 7월 법인신용매출 건수 알려줘",
        "2026년 7월 신용카드매출금액 알려줘",
    ],
)
def test_compound_card_kind_wording_reaches_the_product_class_column(question: str) -> None:
    """"기업신용매출건수" 는 라벨도 컬럼명도 부르지 않은 채 코드 컬럼을 요구한다.

    이 컬럼이 프롬프트에서 잘리면 모델은 신용·체크를 가를 수단 자체가 없어
    개인·체크·직불까지 합친 전체 전표 매출을 답한다.
    """
    from text2sql_agent.workflow import _table_details

    detail = _table_details(["tbdaabt30"], question)
    line = next((row for row in detail.splitlines() if "상품중분류구분코드" in row), "")
    assert line, f"{question}: 상품중분류구분코드가 프롬프트 컬럼에 없다"
    # 코드 매핑과 별칭이 함께 실려야 CP51·CP52 를 신용으로 묶을 수 있다.
    assert '"CP51": "기업신용카드"' in line
    assert '"CP51": ["기업신용", "신용카드"]' in line


def test_two_letter_card_kind_words_do_not_pull_the_column_alone() -> None:
    """'신용'·'체크' 두 글자가 복합어 앞머리로 걸리면 엉뚱한 질문까지 끌려온다."""
    from text2sql_agent.workflow import _value_label_in_question

    column = {
        "value_semantics": {"CP51": "기업신용카드", "CP53": "기업체크카드"},
        "value_aliases": CREDIT_CHECK_DEBIT_ALIASES,
    }
    assert not _value_label_in_question("기업 신용등급별 회원 수 알려줘", column)
    assert _value_label_in_question("기업신용매출건수랑 금액", column)


def test_termination_reason_label_pulls_its_column_into_the_prompt() -> None:
    """해지사유는 코드로도 컬럼명으로도 불리지 않는다. 라벨이 유일한 단서다."""
    from text2sql_agent.workflow import _table_details

    for question, code, label in [
        ("장기무실적으로 해지된 가맹점 수 알려줘", "01", "장기무실적"),
        ("자진폐업으로 해지된 가맹점", "08", "자진폐업"),
        ("강제폐업 가맹점 수", "09", "강제폐업"),
    ]:
        detail = _table_details(["tmdaa5e11"], question)
        line = next(
            (row for row in detail.splitlines() if "가맹점해지사유구분코드" in row), ""
        )
        assert line, f"{question}: 가맹점해지사유구분코드가 프롬프트 컬럼에 없다"
        assert f'"{code}": "{label}"' in line

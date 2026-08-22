"""0811 상세 컬럼 xlsx를 codebooks/column_codebooks.yaml 로 변환한다.

xlsx 한 장은 `컬럼명` 하나의 유효값 목록이다. 같은 이름의 컬럼은 다른 테이블에서도
같은 코드 도메인을 쓰므로, 여기서는 테이블이 아니라 **컬럼명 기준**으로 코드북을
만든다. 테이블별 적용은 build_semantic_layer_v2.py 가 담당한다.

xlsx 상세 정의서가 없고 대화로만 전달받은 코드 목록은 CHAT_PROVIDED_CODEBOOKS 에
적어 두고 같은 산출물에 합친다.

usage:
    python corporate_sales_fable_v2/scripts/build_column_codebooks.py \
        --source ~/Downloads/goldenset_error/0811
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import unicodedata
from pathlib import Path

import openpyxl
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # v2 적용본 루트
V1_ROOT = PROJECT_ROOT.parent / "corporate_sales_fable"   # 변환 원본(v1 저장소)
DEFAULT_OUTPUT = PROJECT_ROOT / "codebooks" / "column_codebooks.yaml"

HEADERS = ("번호", "업무구분", "컬럼명", "도메인명", "유효값", "유효값명", "유효시작일", "유효종료일")

# 파일명이 가리키는 컬럼과 시트 내용의 `컬럼명`이 다른 경우가 있다.
# tmdaa3e16_카드등급그룹코드.xlsx 의 시트 내용은 카드등급구분코드이며, 두 컬럼이
# 같은 코드 도메인을 공유한다는 뜻이므로 파일명 쪽 컬럼도 함께 등록한다.
FILENAME_COLUMN_RE = re.compile(r"^[a-z][a-z0-9]+_(?P<column>.+)$")

# 코드값이 문서상 종료된 값도 유효값명을 알아야 과거 데이터를 해석할 수 있으므로
# 버리지 않고 status 로 구분한다.
OPEN_ENDED = "9999-12-31"


class CodeText(str):
    """항상 따옴표로 덤프되는 문자열.

    코드값은 '004' 처럼 앞자리 0이 의미를 갖는다. 그냥 str로 덤프하면 pyyaml은
    '004'(YAML 1.1에서 8진수로 읽힐 수 있음)만 따옴표로 감싸고 '089'·'092'는
    모호하지 않다고 보아 맨몸으로 쓴다. 그 파일을 YAML 1.2로 읽으면 089가 정수 89가
    되어 앞자리 0이 사라진다(토스뱅크 '092' → '92'). 그래서 코드·날짜는 강제로 따옴표를 씌운다.
    """


def _represent_code_text(dumper: yaml.Dumper, data: CodeText):
    return dumper.represent_scalar("tag:yaml.org,2002:str", str(data), style="'")


yaml.SafeDumper.add_representer(CodeText, _represent_code_text)


# 사용자가 대화로 준 코드 목록. xlsx 상세 정의서가 없는 컬럼이라 여기에 적어 두고
# xlsx 산출물과 같은 모양으로 합친다. 유효기간을 모르므로 valid_from/valid_to 는 비우고
# status 만 active 로 둔다(유효종료된 코드는 애초에 전달받지 않았다).
#
# 코드값 표기는 전달받은 목록의 자릿수를 그대로 쓰되, 같은 목록 안에서 자릿수가 어긋난
# 값은 맞춘다. 가맹점해지사유구분코드는 01~99 두 자리 체계인데 '4'만 한 자리로 적혀
# 있었다. VARCHAR 컬럼에서 '4' 와 '04' 는 다른 값이라 조용히 어긋나므로 '04' 로 쓴다.
CHAT_PROVIDED_CODEBOOKS: list[dict] = [
    {
        "column": "카드소유자구분코드",
        "source_tables": ["tmdaa3e16"],
        "values": [
            ("1", "개인본인"),
            ("2", "개인가족"),
            ("3", "기업대표"),
            ("4", "기업개별"),
            ("5", "기업공용"),
        ],
    },
    {
        "column": "카드매출유형구분코드",
        "source_tables": ["tbdaabt30"],
        "values": [
            ("1", "일반"),
            ("2", "할부"),
            ("3", "현금서비스"),
            ("4", "리볼빙"),
            ("5", "예수금"),
            ("6", "선급급"),
            ("7", "현금지급(해외)"),
            ("9", "연회비"),
            ("A", "계좌매출환출"),
            ("B", "법적비용"),
            ("C", "과잉금"),
        ],
        "notes": [
            "전달받은 목록에 '8' 이 없고 'C' 뒤가 끊겨 있다. D 이후 코드가 더 있으면 여기에 추가한다.",
        ],
    },
    {
        "column": "회원소속회사구분코드",
        # 가맹점소속회사구분코드는 이름이 달라 코드북을 못 받았다. 설명이
        # "가맹점의 소속를 관리하는 코드" 로 회원 쪽("회원소지카드의 소속사를 관리하는
        # 코드")과 같은 소속사 축이고, 사용자가 두 컬럼 모두 '1'(당사)로 거르라고
        # 지정했다. 같은 코드 도메인으로 선언해 '1' 의 뜻을 붙인다.
        "applies_to_columns": ["회원소속회사구분코드", "가맹점소속회사구분코드"],
        "source_tables": ["tbdaabt30", "tbdaabt08"],
        "values": [
            ("0", "비대상"),
            ("1", "당사"),
            ("2", "제휴사"),
            ("3", "공동망"),
            ("4", "해외회원(JCB포함)"),
        ],
    },
    {
        # 여부 컬럼이라 코드가 두 개뿐이지만, 0 쪽을 적어 두지 않으면 모델이
        # 'Y'/'N' 이나 '정상' 같은 값을 지어낸다.
        "column": "가맹점거래정지여부",
        "source_tables": ["tbdaadt01"],
        "values": [
            ("0", "거래정지 아님"),
            ("1", "거래정지"),
        ],
    },
    {
        "column": "가맹점해지사유구분코드",
        "source_tables": ["tmdaa5e11"],
        "values": [
            ("01", "장기무실적"),
            ("02", "가맹점 양도*양수"),
            ("03", "대표자*사업자번호 변경"),
            ("04", "기업형태*법인번호 변경"),
            ("05", "신청서 허위기재"),
            ("06", "휴업"),
            ("07", "영업정지"),
            ("08", "자진폐업"),
            ("09", "강제폐업"),
            ("10", "현금융통 발생"),
            ("11", "현금융통 및 전표 대리청구 발생"),
            ("20", "가맹점 가입 부적격"),
            ("21", "카드거래 거절"),
            ("22", "신청서 허위기재"),
            ("31", "도난*분실카드 매출 다발"),
            ("35", "손실추정 가맹점"),
            ("36", "대표자 가입제한 대상"),
            ("38", "가맹점 수수료 불만"),
            ("99", "기타 해지 필요"),
        ],
        "notes": [
            "05 와 22 는 라벨이 같다(신청서 허위기재). 전달받은 목록 그대로다.",
        ],
    },
]

CHAT_SOURCE = "chat/2026-08-21 사용자 제공 코드 목록"


def chat_provided_books() -> dict[str, dict]:
    """CHAT_PROVIDED_CODEBOOKS 를 xlsx 산출물과 같은 구조로 펼친다."""
    books: dict[str, dict] = {}
    for entry in CHAT_PROVIDED_CODEBOOKS:
        column = entry["column"]
        book = {
            "column": column,
            "domain": entry.get("domain", column),
            "applies_to_columns": list(entry.get("applies_to_columns") or [column]),
            "source_tables": list(entry.get("source_tables", [])),
            "source_files": [CHAT_SOURCE],
            "provenance": "user_provided_business_codebook",
            "values": [
                {"code": CodeText(code), "label": label, "status": "active"}
                for code, label in entry["values"]
            ],
        }
        if entry.get("notes"):
            book["notes"] = list(entry["notes"])
        books[column] = book
    return books


def _nfc(value: str) -> str:
    """macOS 파일명은 한글이 NFD로 분해되어 있어 시트 내용(NFC)과 문자열 비교가 어긋난다."""
    return unicodedata.normalize("NFC", value)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _nfc(str(value).strip())


def _read_sheet(path: Path) -> tuple[str, str, list[dict]]:
    workbook = openpyxl.load_workbook(path, data_only=True)
    sheet = workbook.worksheets[0]
    rows = [[_text(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
    rows = [row for row in rows if any(row)]
    if not rows:
        raise ValueError(f"{path.name}: 빈 시트")

    header = rows[0]
    index = {name: header.index(name) for name in HEADERS if name in header}
    missing = [name for name in ("컬럼명", "유효값", "유효값명") if name not in index]
    if missing:
        raise ValueError(f"{path.name}: 필수 헤더 누락 {missing}")

    column_names: list[str] = []
    domain_names: list[str] = []
    values: list[dict] = []
    for row in rows[1:]:
        code = row[index["유효값"]]
        label = row[index["유효값명"]]
        if not code:
            continue
        column_name = row[index["컬럼명"]]
        if column_name and column_name not in column_names:
            column_names.append(column_name)
        domain = row[index.get("도메인명", -1)] if "도메인명" in index else ""
        if domain and domain not in domain_names:
            domain_names.append(domain)

        end = row[index["유효종료일"]] if "유효종료일" in index else ""
        entry = {"code": CodeText(code), "label": label}
        start = row[index["유효시작일"]] if "유효시작일" in index else ""
        if start:
            entry["valid_from"] = CodeText(start)
        if end:
            entry["valid_to"] = CodeText(end)
        entry["status"] = "active" if (not end or end == OPEN_ENDED) else "retired"
        values.append(entry)

    if not column_names:
        raise ValueError(f"{path.name}: 컬럼명 없음")
    return column_names[0], (domain_names[0] if domain_names else ""), values


def build(source_dir: Path) -> dict:
    files = sorted(source_dir.glob("*.xlsx"))
    if not files:
        raise SystemExit(f"xlsx 없음: {source_dir}")

    codebooks: dict[str, dict] = {}
    for path in files:
        sheet_column, domain, values = _read_sheet(path)
        stem = _nfc(path.stem)
        stem_match = FILENAME_COLUMN_RE.match(stem)
        filename_column = stem_match.group("column") if stem_match else ""
        source_table = stem.split("_", 1)[0] if stem_match else ""

        # 시트의 컬럼명이 코드북 정체성이고, 파일명 컬럼은 같은 도메인을 쓰는 별칭이다.
        applies_to = [sheet_column]
        if filename_column and filename_column != sheet_column:
            applies_to.append(filename_column)

        existing = codebooks.get(sheet_column)
        if existing:
            for column in applies_to:
                if column not in existing["applies_to_columns"]:
                    existing["applies_to_columns"].append(column)
            if source_table and source_table not in existing["source_tables"]:
                existing["source_tables"].append(source_table)
            existing["source_files"].append(_nfc(path.name))
            continue

        codebooks[sheet_column] = {
            "column": sheet_column,
            "domain": domain,
            "applies_to_columns": applies_to,
            "source_tables": [source_table] if source_table else [],
            "source_files": [_nfc(path.name)],
            "provenance": "user_provided_business_codebook",
            "values": values,
        }

    # xlsx 가 이미 정의한 컬럼을 대화 목록이 다시 정의하면 어느 쪽이 맞는지 알 수 없다.
    # 조용히 한쪽을 이기게 두지 않고 세운다.
    chat_books = chat_provided_books()
    collision = sorted(set(chat_books) & set(codebooks))
    if collision:
        raise SystemExit(f"xlsx 코드북과 겹치는 대화 제공 코드북: {collision}")
    codebooks.update(chat_books)

    document = {
        "column_codebooks_metadata": {
            "generated_from": "goldenset_error/0811/*.xlsx + 대화 제공 코드 목록",
            "source_file_count": len(files),
            "chat_provided_codebook_count": len(chat_books),
            "codebook_count": len(codebooks),
            "provenance": "user_provided_business_codebook",
            "notes": [
                "코드북은 컬럼명 기준이다. 같은 이름의 컬럼은 어느 테이블에 있어도 동일 코드 도메인으로 본다.",
                "status=retired 는 유효종료일이 지난 코드로, 과거 기간 조회 결과 해석에만 쓰고 신규 필터로 제안하지 않는다.",
                "applies_to_columns 에 여러 컬럼이 있으면 그 컬럼들이 같은 유효값 목록을 공유한다는 뜻이다.",
                "source_files 가 chat/ 로 시작하면 상세 정의서 xlsx 없이 대화로 받은 목록이다. 유효기간을 모르므로 valid_from/valid_to 가 없다.",
            ],
        },
        "column_codebooks": [codebooks[name] for name in sorted(codebooks)],
    }
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="0811 xlsx 디렉터리")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    document = build(args.source.expanduser())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(document, handle, allow_unicode=True, sort_keys=False, width=120)

    # 앞자리 0이 살아 있는지 다시 읽어 확인한다. 여기서 깨지면 코드값이 조용히 틀어진다.
    reloaded = yaml.safe_load(args.output.read_text(encoding="utf-8"))
    for original, written in zip(document["column_codebooks"], reloaded["column_codebooks"]):
        expected = [str(value["code"]) for value in original["values"]]
        actual = [str(value["code"]) for value in written["values"]]
        if expected != actual:
            drifted = [(a, b) for a, b in zip(expected, actual) if a != b]
            raise SystemExit(f"{original['column']}: 코드값이 왕복에서 변형됐다 {drifted[:5]}")

    meta = document["column_codebooks_metadata"]
    print(f"codebooks: {meta['codebook_count']} (from {meta['source_file_count']} files) -> {args.output}")
    for book in document["column_codebooks"]:
        active = sum(1 for value in book["values"] if value["status"] == "active")
        print(f"  {book['column']}: {len(book['values'])} values ({active} active) -> {book['applies_to_columns']}")


if __name__ == "__main__":
    main()

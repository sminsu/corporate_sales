"""0811 상세 컬럼 xlsx를 codebooks/column_codebooks.yaml 로 변환한다.

xlsx 한 장은 `컬럼명` 하나의 유효값 목록이다. 같은 이름의 컬럼은 다른 테이블에서도
같은 코드 도메인을 쓰므로, 여기서는 테이블이 아니라 **컬럼명 기준**으로 코드북을
만든다. 테이블별 적용은 build_semantic_layer_v2.py 가 담당한다.

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

V2_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = V2_DIR / "codebooks" / "column_codebooks.yaml"

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

    document = {
        "column_codebooks_metadata": {
            "generated_from": "goldenset_error/0811/*.xlsx",
            "source_file_count": len(files),
            "codebook_count": len(codebooks),
            "provenance": "user_provided_business_codebook",
            "notes": [
                "코드북은 컬럼명 기준이다. 같은 이름의 컬럼은 어느 테이블에 있어도 동일 코드 도메인으로 본다.",
                "status=retired 는 유효종료일이 지난 코드로, 과거 기간 조회 결과 해석에만 쓰고 신규 필터로 제안하지 않는다.",
                "applies_to_columns 에 여러 컬럼이 있으면 그 컬럼들이 같은 유효값 목록을 공유한다는 뜻이다.",
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

"""v1 semantic_layer.yaml → corporate_sales_fable_v2/semantic_layer.yaml.

goldenset v2 SQL 오류 400건의 원인별 대응을 한 번에 적용한다.

컬럼 레벨 (guard 332건 / column_not_found 57건)
  1. 같은 이름의 컬럼은 어느 테이블에 있든 동의어를 합친다.
     tmdaaus01."가맹점상태구분코드" 에는 동의어가 없고 tmdaa5e11 의 같은 컬럼에는
     ['가맹점상태','가맹점 상태'] 가 있어서, 질문이 어느 테이블로 라우팅되는지에
     따라 컬럼이 프롬프트에 들어가거나 빠졌다.
  2. 컬럼명에서 질문 표면형 동의어를 유도해 추가한다(column_synonyms).
  3. 0811 코드북(7종)을 같은 이름의 모든 컬럼에 value_semantics 로 적용한다.
  4. 설명이 빈 컬럼은 같은 이름 컬럼의 설명으로 채운다(기존 설명은 덮지 않는다).

계약 레벨 (guard 332건 / syntax 10건)
  5. ambiguity_rules 가 "추가 입력을 요청한다" 라고 지시하고 있어서 모델이 SQL 대신
     자연어로 되물었다. 항상 SQL을 내보내고 기본값을 문서화하는 규칙으로 바꾼다.
  6. Athena 방언 규칙에 QUALIFY 금지, expr::type 금지, 윈도함수 WHERE 금지를 넣는다.

렌더링 제약
  build_semantic_contract_summary() 는 리스트를 앞 12개까지만 프롬프트에 넣는다.
  현재 grain_and_aggregation_rules 는 13개라 마지막 규칙이 조용히 버려지고 있다.
  이 스크립트는 12개를 넘는 리스트가 있으면 실패한다.

usage:
    python corporate_sales_fable_v2/scripts/build_semantic_layer_v2.py
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

from ruamel.yaml import YAML

V2_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = V2_DIR.parent
sys.path.insert(0, str(V2_DIR))

from text2sql_v2.column_synonyms import derive_column_synonyms  # noqa: E402
from text2sql_v2.sql_contract import (  # noqa: E402
    ATHENA_RULES_V2,
    GRAIN_RULES_V2,
    OUTPUT_CONTRACT_V2,
    RESOLUTION_DEFAULTS_V2,
    PROMPT_LIST_RENDER_LIMIT,
)

COLUMN_SECTIONS = ("dimensions", "measures", "time_dimensions")
V2_VERSION = "2026-08-11.2-v2"


def _yaml() -> YAML:
    parser = YAML()
    parser.preserve_quotes = True
    parser.width = 4096
    parser.indent(mapping=2, sequence=2, offset=0)
    return parser


# ---------------------------------------------------------------------------
# 1~4. 컬럼 메타데이터
# ---------------------------------------------------------------------------
def collect_column_profiles(schema: dict) -> dict[str, dict]:
    """컬럼명 기준으로 테이블 전체의 메타데이터를 모은다."""
    profiles: dict[str, dict] = defaultdict(
        lambda: {"synonyms": [], "description": "", "tables": []}
    )
    for table in schema.get("tables", []):
        table_name = str(table.get("name") or "")
        for section in COLUMN_SECTIONS:
            for column in table.get(section) or []:
                name = str(column.get("name") or "")
                if not name:
                    continue
                profile = profiles[name]
                profile["tables"].append(table_name)
                for synonym in column.get("synonyms") or []:
                    text = str(synonym).strip()
                    if text and text not in profile["synonyms"]:
                        profile["synonyms"].append(text)
                description = str(column.get("description") or "").strip()
                if len(description) > len(profile["description"]):
                    profile["description"] = description
    return dict(profiles)


def load_codebooks(path: Path) -> dict[str, dict]:
    """applies_to_columns 를 펼쳐 컬럼명 → 코드북으로 만든다."""
    document = _yaml().load(path.read_text(encoding="utf-8"))
    by_column: dict[str, dict] = {}
    for book in document.get("column_codebooks", []):
        for column in book.get("applies_to_columns") or [book.get("column")]:
            by_column[str(column)] = book
    return by_column


def apply_column_metadata(schema: dict, codebooks: dict[str, dict]) -> dict:
    profiles = collect_column_profiles(schema)
    stats = {
        "synonyms_propagated": 0,
        "synonyms_derived": 0,
        "columns_touched": 0,
        "descriptions_filled": 0,
        "codebooks_applied": 0,
        "codebook_columns": set(),
    }

    for table in schema.get("tables", []):
        for section in COLUMN_SECTIONS:
            for column in table.get(section) or []:
                name = str(column.get("name") or "")
                if not name:
                    continue
                profile = profiles.get(name, {})
                existing = [str(value) for value in column.get("synonyms") or []]
                merged = list(existing)
                changed = False

                # 1. 같은 이름 컬럼의 동의어 합치기
                for synonym in profile.get("synonyms", []):
                    if synonym != name and synonym not in merged:
                        merged.append(synonym)
                        stats["synonyms_propagated"] += 1
                        changed = True

                # 2. 컬럼명에서 유도한 표면형
                for synonym in derive_column_synonyms(name, merged):
                    if synonym not in merged:
                        merged.append(synonym)
                        stats["synonyms_derived"] += 1
                        changed = True

                if changed:
                    column["synonyms"] = merged
                    stats["columns_touched"] += 1

                # 3. 코드북 적용.
                # 사용자가 준 코드북이 최종 근거이므로, 기존 값이 추정(assumption)이면
                # 덮어쓴다. 예: tbdaadt01.가맹점상태구분코드 는 {"1": "활성 가맹점"} 이라는
                # athena_reference_assumption 하나만 갖고 있어 '거래정지'·'해지'를 몰랐다.
                book = codebooks.get(name)
                existing_provenance = str(column.get("value_semantics_provenance") or "")
                authoritative = existing_provenance == "user_provided_business_codebook"
                if book and (not column.get("value_semantics") or not authoritative):
                    active = {
                        str(value["code"]): str(value["label"])
                        for value in book.get("values", [])
                        if value.get("status") == "active"
                    }
                    if active:
                        column["value_semantics"] = active
                        column["value_semantics_provenance"] = book.get(
                            "provenance", "user_provided_business_codebook"
                        )
                        column["codebook_ref"] = str(book.get("column"))
                        stats["codebooks_applied"] += 1
                        stats["codebook_columns"].add(name)

                # 4. 빈 설명만 채운다
                if not str(column.get("description") or "").strip() and profile.get("description"):
                    column["description"] = profile["description"]
                    stats["descriptions_filled"] += 1

    stats["codebook_columns"] = sorted(stats["codebook_columns"])
    return stats


# ---------------------------------------------------------------------------
# 5~6. SQL 생성 계약
# ---------------------------------------------------------------------------
def apply_sql_contract(schema: dict) -> dict:
    contract = schema.get("sql_generation_contract")
    if not isinstance(contract, dict):
        raise SystemExit("sql_generation_contract 없음")

    before = {
        "athena_rules": len(contract.get("athena_rules") or []),
        "ambiguity_rules": len(contract.get("ambiguity_rules") or []),
        "grain_and_aggregation_rules": len(contract.get("grain_and_aggregation_rules") or []),
    }

    contract["output_contract"] = list(OUTPUT_CONTRACT_V2)
    contract["athena_rules"] = list(ATHENA_RULES_V2)
    contract["grain_and_aggregation_rules"] = list(GRAIN_RULES_V2)
    # 되묻기를 지시하던 ambiguity_rules 를 기본값 해석 규칙으로 교체한다.
    contract.pop("ambiguity_rules", None)
    contract["resolution_defaults"] = list(RESOLUTION_DEFAULTS_V2)

    # output_contract 를 evidence_order 앞으로 옮겨 프롬프트 맨 위에 오게 한다.
    order = [
        "purpose",
        "output_contract",
        "evidence_order",
        "athena_rules",
        "grain_and_aggregation_rules",
        "resolution_defaults",
        "time_resolution",
        "result_defaults",
    ]
    for key in reversed(order):
        if key in contract:
            contract.move_to_end(key, last=False)

    return {
        "before": before,
        "after": {key: len(value) for key, value in contract.items() if isinstance(value, list)},
    }


def check_render_limits(schema: dict) -> list[str]:
    """프롬프트에 잘려 들어가는 리스트를 찾아낸다."""
    problems = []
    contract = schema.get("sql_generation_contract") or {}
    for key, value in contract.items():
        if isinstance(value, list) and len(value) > PROMPT_LIST_RENDER_LIMIT:
            problems.append(
                f"sql_generation_contract.{key}: {len(value)}개 "
                f"(프롬프트는 {PROMPT_LIST_RENDER_LIMIT}개까지만 렌더)"
            )
    return problems


# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=REPO_ROOT / "semantic_layer.yaml")
    parser.add_argument("--codebooks", type=Path, default=V2_DIR / "codebooks" / "column_codebooks.yaml")
    parser.add_argument("--output", type=Path, default=V2_DIR / "semantic_layer.yaml")
    args = parser.parse_args()

    yaml = _yaml()
    schema = yaml.load(args.source.read_text(encoding="utf-8"))
    codebooks = load_codebooks(args.codebooks)

    column_stats = apply_column_metadata(schema, codebooks)
    contract_stats = apply_sql_contract(schema)

    metadata = schema.get("semantic_layer_metadata")
    if metadata is not None:
        metadata["version"] = V2_VERSION
        metadata["derived_from"] = "semantic_layer.yaml (v1)"
        metadata["v2_changes"] = [
            "같은 이름의 컬럼은 테이블을 넘어 동의어를 공유한다.",
            "컬럼명에서 질문 표면형 동의어를 유도해 추가했다(구분코드/여부 접미사 제거, 띄어쓰기 변형).",
            "0811 상세 컬럼 코드북 7종을 같은 이름의 모든 컬럼에 value_semantics 로 적용했다.",
            "SQL 생성 계약에서 되묻기 지시를 제거하고 항상 SQL을 내보내는 output_contract 로 교체했다.",
            "Athena 방언 규칙에 QUALIFY·expr::type·윈도함수 WHERE 금지를 명시했다.",
            f"프롬프트 렌더 한도({PROMPT_LIST_RENDER_LIMIT}개)를 넘겨 잘리던 규칙 리스트를 정리했다.",
        ]

    schema["column_codebooks"] = _yaml().load(args.codebooks.read_text(encoding="utf-8"))[
        "column_codebooks"
    ]

    problems = check_render_limits(schema)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        raise SystemExit(1)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        yaml.dump(schema, handle)

    print(f"semantic layer v2 -> {args.output}")
    print(f"  columns touched      : {column_stats['columns_touched']}")
    print(f"  synonyms propagated  : {column_stats['synonyms_propagated']}")
    print(f"  synonyms derived     : {column_stats['synonyms_derived']}")
    print(f"  descriptions filled  : {column_stats['descriptions_filled']}")
    print(f"  codebooks applied    : {column_stats['codebooks_applied']} column instances")
    print(f"    codebook columns   : {', '.join(column_stats['codebook_columns'])}")
    print(f"  contract list sizes  : {contract_stats['before']} -> {contract_stats['after']}")


if __name__ == "__main__":
    main()

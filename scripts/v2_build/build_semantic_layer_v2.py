"""v1 semantic_layer.yaml → corporate_sales_fable_v2/semantic_layer.yaml.

goldenset v2 SQL 오류 400건의 원인별 대응을 한 번에 적용한다.

컬럼 레벨 (guard 332건 / column_not_found 57건)
  0. 실제 스키마에 없는 평가·JCB 관련 컬럼 20개를 제거한다.
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
import re
import sys
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from ruamel.yaml import YAML

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # v2 적용본 루트
V1_ROOT = PROJECT_ROOT.parent / "corporate_sales_fable"   # 변환 원본(v1 저장소)
sys.path.insert(0, str(PROJECT_ROOT))

from text2sql_agent.v2.column_synonyms import compact, derive_column_synonyms  # noqa: E402
from text2sql_agent.time_policy import TABLE_ACCUMULATION_POLICIES  # noqa: E402
from text2sql_agent.v2.sql_contract import (  # noqa: E402
    ATHENA_RULES_V2,
    GRAIN_RULES_V2,
    OUTPUT_CONTRACT_V2,
    AMBIGUITY_RULES_V2,
    PROMPT_LIST_RENDER_LIMIT,
)

COLUMN_SECTIONS = ("dimensions", "measures", "time_dimensions")

# 한 글자 동의어는 어절 경계만 지키면 어디에나 걸린다. v1 은 11개 테이블의
# "기준년월" 에 동의어 '월' 을 달아 두었고, 그래서 "가맹점 월 승인한도금액" 같은
# 질문에서 기준년월을 가진 테이블 전부가 컬럼 단서 점수를 받았다. 테이블 랭킹은
# "기준년월" 자체를 이미 흔한 용어로 빼 두는데 동의어가 그 구멍을 다시 열었다.
_MIN_DECLARED_SYNONYM_LENGTH = 2


def _useful_synonyms(values: object) -> list[str]:
    """어절 단서가 되지 못하는 한 글자 동의어를 걷어낸다."""
    return [
        text
        for value in values or []
        if len(compact(text := str(value).strip())) >= _MIN_DECLARED_SYNONYM_LENGTH
    ]
V2_VERSION = "2026-08-14.3-v2"

REMOVED_COLUMNS_BY_TABLE: dict[str, frozenset[str]] = {
    "tbdaa1d12": frozenset({"소호로지스틱BSS등급구분코드"}),
    "tbdaaat03": frozenset(
        {"ASS평점", "BSS평점", "통합ASS평점", "통합BSS평점", "통합BSS한도등급"}
    ),
    "tbdaadb17": frozenset({"JCB카드수수료율", "JCB카드한도금액"}),
    "tbdaadt01": frozenset({"JCB카드수수료율"}),
    "tmdaaus01": frozenset({"JCB카드수수료율"}),
    "tmdaa5e11": frozenset({"JCB카드수수료율"}),
    "tmdaa5d01": frozenset({"JCB카드수수료율"}),
    "tddaa3l01": frozenset({"통합BSS한도등급"}),
    "tddaa3e21": frozenset({"통합ASS평점", "통합BSS평점", "통합BSS한도등급"}),
    "tddaa3e23": frozenset({"통합ASS평점", "통합BSS한도등급"}),
    "tmdaa3e16": frozenset({"ASS평점", "BSS평점"}),
}


def _yaml() -> YAML:
    parser = YAML()
    parser.preserve_quotes = True
    parser.width = 4096
    parser.indent(mapping=2, sequence=2, offset=0)
    return parser


# ---------------------------------------------------------------------------
# 0. 실제 스키마에 없는 컬럼 제거
# ---------------------------------------------------------------------------
def remove_retired_columns(schema: dict) -> list[str]:
    removed: list[str] = []
    for table in schema.get("tables", []):
        table_name = str(table.get("name") or "").lower()
        targets = REMOVED_COLUMNS_BY_TABLE.get(table_name, frozenset())
        for section in COLUMN_SECTIONS:
            kept = []
            for column in table.get(section) or []:
                name = str(column.get("name") or "")
                if name in targets:
                    removed.append(f"{table_name}.{name}")
                else:
                    kept.append(column)
            if section in table:
                table[section] = kept

        metadata = table.get("source_metadata") or {}
        if metadata.get("source_column_count") is not None:
            queryable = sum(len(table.get(section) or []) for section in COLUMN_SECTIONS)
            excluded = len(metadata.get("excluded_technical_columns") or [])
            metadata["queryable_column_count"] = queryable
            metadata["source_column_count"] = queryable + excluded

    inventory = schema.get("semantic_layer_metadata", {}).get("source_inventory")
    if inventory is not None:
        physical = [
            table
            for table in schema.get("tables", [])
            if table.get("relation_type") != "request_scoped_values_cte"
        ]
        inventory["source_columns"] = sum(
            int((table.get("source_metadata") or {}).get("source_column_count") or 0)
            for table in physical
        )
        inventory["queryable_business_columns"] = sum(
            int((table.get("source_metadata") or {}).get("queryable_column_count") or 0)
            for table in physical
        )

    return removed


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
                for text in _useful_synonyms(column.get("synonyms")):
                    if text not in profile["synonyms"]:
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
    # 접두사를 벗긴 어간이 다른 컬럼의 이름 그대로면 그 표면형은 그 컬럼의 것이다.
    declared_columns = frozenset(profiles)
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
                declared = [str(value) for value in column.get("synonyms") or []]
                existing = _useful_synonyms(declared)
                merged = list(existing)
                changed = len(existing) != len(declared)

                # 1. 같은 이름 컬럼의 동의어 합치기
                for synonym in profile.get("synonyms", []):
                    if synonym != name and synonym not in merged:
                        merged.append(synonym)
                        stats["synonyms_propagated"] += 1
                        changed = True

                # 2. 컬럼명에서 유도한 표면형
                for synonym in derive_column_synonyms(name, merged, declared_columns):
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


def apply_accumulation_policies(schema: dict) -> int:
    """Apply user-provided table load cadence independently of table grain."""
    applied: set[str] = set()
    for table in schema.get("tables", []):
        table_name = str(table.get("physical_table") or table.get("name") or "").lower()
        policy = TABLE_ACCUMULATION_POLICIES.get(table_name)
        if policy:
            table["accumulation_policy"] = deepcopy(policy)
            applied.add(table_name)

    missing = set(TABLE_ACCUMULATION_POLICIES) - applied
    if missing:
        raise SystemExit(f"적재 주기 대상 테이블 없음: {', '.join(sorted(missing))}")
    return len(applied)


# ---------------------------------------------------------------------------
# 5. 시간 컬럼 메타데이터 교정
#
# tbmaisd06 의 물리 primary_time_dimension(특수채권편입기준년월일)은
# 유지한다. 연 단위 적재 조회 컬럼은 accumulation_policy.query_time_dimension
# (기준년)이 별도로 정의한다. 여기서는 기준년의 format 만 바로잡는다.
# ---------------------------------------------------------------------------
TIME_AXIS_FIXES: dict[str, dict] = {
    "tbmaisd06": {
        "columns": {
            "기준년": {
                "format": "YYYY",
                "role": "snapshot_date",
                "description": "특수채권 편입 내역의 배치 기준년(YYYY). 연 단위 조회는 이 컬럼을 쓴다.",
            },
        },
    },
}


def apply_time_axis_fixes(schema: dict) -> int:
    fixed = 0
    tables = {str(table.get("name") or ""): table for table in schema.get("tables", [])}
    for table_name, spec in TIME_AXIS_FIXES.items():
        table = tables.get(table_name)
        if table is None:
            raise SystemExit(f"시간축 교정 대상 테이블 없음: {table_name}")
        for column_name, updates in spec.get("columns", {}).items():
            column = next(
                (
                    item
                    for section in COLUMN_SECTIONS
                    for item in table.get(section) or []
                    if str(item.get("name") or "") == column_name
                ),
                None,
            )
            if column is None:
                raise SystemExit(f"시간축 교정 대상 컬럼 없음: {table_name}.{column_name}")
            column.update(updates)
        fixed += 1

    return fixed


# ---------------------------------------------------------------------------
# 6. 최근 10일 가맹점 월 이력 소스·조인 교정
# ---------------------------------------------------------------------------
def apply_recent_merchant_semantic_overrides(schema: dict) -> int:
    """Keep historical merchant semantics on the monthly snapshot grain."""
    contracts = {
        str(item.get("name") or ""): item
        for item in schema.get("semantic_query_contracts", [])
    }
    references = {
        str(item.get("intent") or ""): item for item in schema.get("query_references", [])
    }
    glossary = {
        str(item.get("term") or ""): item for item in schema.get("glossary", [])
    }
    safe_paths = schema.get("semantic_join_graph", {}).get("safe_paths", [])
    paths = {str(item.get("name") or ""): item for item in safe_paths}

    contract = contracts.get("named_merchant_store_sales_at_month")
    if contract is None:
        raise SystemExit(
            "월 가맹점 소스 교정 대상 계약 없음: named_merchant_store_sales_at_month"
        )
    filters = contract.get("filters") or []
    contract["filters"] = [
        str(item).replace("tbdaadt01.", "tmdaa5d01.") for item in filters
    ]

    path_updates = {
        "domestic_sales_to_merchant": {
            "to_entity": "merchant_monthly_snapshot",
            "to_table": "tmdaa5d01",
            "join_type": "many_to_one",
            "sql": (
                'tbdaabt30."기준년월" = tmdaa5d01."기준년월" AND '
                'tbdaabt30."가맹점번호" = tmdaa5d01."가맹점번호"'
            ),
            "use_when": ["월 매출전표에 같은 월 가맹점명·상태 추가"],
            "caution": "두 테이블의 기준년월을 같은 요청 기간으로 제한하고 매출 합계 grain을 유지",
            "provenance": "user_provided_load_cadence",
        },
        "merchant_to_monthly_performance": {
            "from_entity": "merchant_monthly_snapshot",
            "from_table": "tmdaa5d01",
            "join_type": "one_to_one",
            "sql": (
                'tmdaa5d01."기준년월" = tmdaa5e11."기준년월" AND '
                'tmdaa5d01."가맹점번호" = tmdaa5e11."가맹점번호"'
            ),
            "use_when": ["월 가맹점 속성과 같은 월 매출·수수료 결합"],
            "caution": "두 테이블의 기준년월을 모두 같은 요청 기간으로 제한",
            "provenance": "user_provided_load_cadence",
        },
    }
    for name, updates in path_updates.items():
        path = paths.get(name)
        if path is None:
            raise SystemExit(f"월 가맹점 조인 교정 대상 경로 없음: {name}")
        path.update(updates)

    # 일별 스냅샷을 월별 스냅샷에 가맹점번호로만 연결하던 중복 경로는 제거한다.
    safe_paths[:] = [
        item
        for item in safe_paths
        if str(item.get("name") or "") != "merchant_to_monthly_snapshot"
    ]

    monthly_sales = references.get("월별_카드매출_조회")
    merchant_ranking = references.get("가맹점별_매출순위")
    if monthly_sales is None or merchant_ranking is None:
        raise SystemExit("월 가맹점 교정 대상 query_references 없음")

    monthly_sales["join_tables"] = ["tmdaa5d01", "tbdaadb17"]
    monthly_sales["join_rule"] = (
        "매출전표(tbdaabt30)의 기준년월+가맹점번호를 가맹점월스냅샷(tmdaa5d01)과 "
        "조인하고, 업종명이 필요하면 매출전표의 가맹점업종코드로 tbdaadb17을 조인한다."
    )

    merchant_ranking["join_tables"] = ["tmdaa5d01", "tbdaadb17"]
    merchant_ranking["join_rule"] = (
        "가맹점월실적(tmdaa5e11)의 기준년월+가맹점번호를 "
        "가맹점월스냅샷(tmdaa5d01)과 조인해 같은 월의 가맹점명을 가져온다."
    )
    merchant_ranking["recommended_columns"]["가맹점명"] = 'tmdaa5d01."가맹점명"'
    merchant_ranking["rules"][1] = (
        '특정 월이면 tmdaa5e11과 tmdaa5d01의 "기준년월" 필터를 같게 적용한다.'
    )
    return 6


# ---------------------------------------------------------------------------
# 6-1. 가맹점 주소 속성
#
# "한신포차 가맹점 도로명 주소" 는 어떤 매처에도 걸리지 않았다. 스키마에
# 도로명주소라는 컬럼이 없고(주소 본문은 가맹점상세주소, 표기 방식은
# 주소표시구분코드다) 주소 계열 컬럼에 동의어가 하나도 없어서다. 질문이 남긴
# 유일한 단서가 "가맹점주"(merchant_owner) 였고, 그 속성은 마스터·일별·월별
# 스냅샷 4개를 동점으로 올린다. 그래서 주소 컬럼이 아예 없는 tmdaaus01 까지
# 후보가 됐다. 주소를 원천이 선언된 속성으로 만들어 그 넷 중 둘만 남긴다.
#
# 원천을 마스터 하나로 두었더니 이번에는 지난 달 이전을 물을 수 없었다.
# _merchant_master_attribute_question() 이 이 속성을 마스터 전용으로 읽어 시간
# 라우팅을 'master' 로 고정하고, 마스터는 최근 며칠만 보관하므로 요청한 달이
# 최신 적재 시점으로 바뀌었다("2026년 1월 도로명 주소" -> 8월 상태). 월 스냅샷
# tmdaa5d01 이 주소 컬럼 9개를 하나도 빠짐없이 갖고 있으므로, 신용판매 매출금액과
# 같은 방식으로 현재·과거 원천을 나눠 선언한다.
# ---------------------------------------------------------------------------
# 마스터와 월 스냅샷이 같은 9개 컬럼을 갖는다. 한쪽만 고치는 일이 없도록 공유한다.
MERCHANT_ADDRESS_COLUMNS = [
    "가맹점상세주소",
    "주소표시구분코드",
    "우편번호",
    "도로명우편번호",
    "도로명우편읍면번호",
    "도로명구역번호",
    "도로명건물본번호",
    "도로명건물부번호",
    "도로명지하구분코드",
]

MERCHANT_ADDRESS_ATTRIBUTE = {
    "name": "merchant_address",
    "korean_name": "가맹점 주소",
    "domains": ["merchant_sales", "corporate_sales_targeting"],
    "business_definition": "가맹점 사업장의 소재지 주소. 도로명·지번 표기는 주소표시구분코드가 구분한다",
    "aliases": [
        "가맹점 주소",
        "가맹점주소",
        "도로명 주소",
        "도로명주소",
        "지번 주소",
        "지번주소",
        "사업장 주소",
        "사업장주소",
        "가맹점 소재지",
        "가맹점소재지",
    ],
    "source_mappings": [
        {
            "entity": "merchant",
            "table": "tbdaadt01",
            "columns": list(MERCHANT_ADDRESS_COLUMNS),
            "role": "current_merchant_address",
        },
        {
            "entity": "merchant_monthly_snapshot",
            "table": "tmdaa5d01",
            "columns": list(MERCHANT_ADDRESS_COLUMNS),
            "role": "monthly_merchant_address",
        },
    ],
    "source_selection": {
        "default_role_prefix": "current_",
        "period_role_prefix": "monthly_",
        "current_terms": ["이번 달", "이번달", "당월", "현재", "최신", "지금", "오늘"],
        # 요청 월이 실행일이 속한 달이면 월 스냅샷에 아직 그 달 행이 없다.
        "open_month_uses_current": True,
    },
    "semantic_cautions": [
        "도로명주소라는 단일 컬럼은 없다. 주소 본문은 가맹점상세주소이고 도로명·지번 표기 여부는 주소표시구분코드로 판별한다.",
        "도로명 주소를 물으면 가맹점상세주소를 주소표시구분코드·우편번호와 함께 내보내고, 도로명 번호 컬럼만으로 주소를 조립하지 않는다.",
        "지난 달 이전의 주소는 tmdaa5d01의 기준년월 행에서 읽는다. 마스터(tbdaadt01)는 최근 며칠만 보관하므로 과거 월을 최신 적재분으로 대체하지 않는다.",
    ],
}


def apply_merchant_address_semantics(schema: dict) -> int:
    """Give 가맹점 주소 a declared source per period instead of no attribute at all."""
    attributes = schema.get("semantic_attributes")
    if not isinstance(attributes, list):
        raise SystemExit("semantic_attributes 없음")
    if any(
        item.get("name") == MERCHANT_ADDRESS_ATTRIBUTE["name"] for item in attributes
    ):
        raise SystemExit(f"이미 있는 속성: {MERCHANT_ADDRESS_ATTRIBUTE['name']}")

    # 월 원천이 주소 컬럼을 하나라도 빠뜨리면 과거 월 답변이 컬럼 오류가 된다.
    tables = {str(item.get("name") or ""): item for item in schema.get("tables", [])}
    for mapping in MERCHANT_ADDRESS_ATTRIBUTE["source_mappings"]:
        table = tables.get(str(mapping["table"]))
        if table is None:
            raise SystemExit(f"테이블 없음: {mapping['table']}")
        declared = {
            str(column.get("name") or "")
            for section in COLUMN_SECTIONS
            for column in table.get(section) or []
        }
        missing = [name for name in mapping["columns"] if name not in declared]
        if missing:
            raise SystemExit(f"{mapping['table']} 에 없는 컬럼: {', '.join(missing)}")

    anchor = next(
        (
            position
            for position, item in enumerate(attributes)
            if item.get("name") == "merchant_owner"
        ),
        len(attributes) - 1,
    )
    attributes.insert(anchor + 1, deepcopy(MERCHANT_ADDRESS_ATTRIBUTE))
    return 1


# ---------------------------------------------------------------------------
# 7. 전일 적재 테이블을 참조하는 지표·계약·질의 교정
# ---------------------------------------------------------------------------
KST_PREVIOUS_DAY_SQL = (
    "DATE_FORMAT(DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'), '%Y%m%d')"
)
KST_RECENT_PERIOD_START_SQL = (
    "DATE_FORMAT(DATE_ADD('month', -:조회기간개월수, "
    "DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul')), '%Y%m%d')"
)


def apply_previous_day_semantic_overrides(schema: dict) -> int:
    """Keep tbdaaus01 semantics aligned with its KST D-1 load policy."""
    metrics = {
        str(item.get("name") or ""): item for item in schema.get("canonical_metrics", [])
    }
    contracts = {
        str(item.get("name") or ""): item
        for item in schema.get("semantic_query_contracts", [])
    }
    references = {
        str(item.get("intent") or ""): item for item in schema.get("query_references", [])
    }

    owner_filter = f'tbdaaus01."기준년월일" = {KST_PREVIOUS_DAY_SQL}'
    for metric_name in ("브랜드가맹점주수", "기업카드소지가맹점주수"):
        metric = metrics.get(metric_name)
        if metric is None:
            raise SystemExit(f"전일 적재 교정 대상 지표 없음: {metric_name}")
        metric["result_grain"] = "KST 전일 스냅샷 1행"
        metric["required_filters"][0] = owner_filter
        metric["aggregation_behavior"] = "distinct_count_previous_day_snapshot"

    closed_metric = metrics.get("최근기간폐업가맹점수")
    if closed_metric is None:
        raise SystemExit("전일 적재 교정 대상 지표 없음: 최근기간폐업가맹점수")
    closed_metric["expression"] = (
        'COUNT(DISTINCT "가맹점번호") WHERE "최초폐업관측일" BETWEEN '
        f"{KST_RECENT_PERIOD_START_SQL} AND {KST_PREVIOUS_DAY_SQL}"
    )
    closed_metric["required_filters"][2] = (
        "최초폐업관측일이 KST 전일 - 조회기간개월수부터 KST 전일까지"
    )
    closed_metric["time_policy"]["range_start"] = KST_RECENT_PERIOD_START_SQL
    closed_metric["time_policy"]["range_end"] = KST_PREVIOUS_DAY_SQL

    owner_contract = contracts.get("brand_owner_corporate_card_count")
    if owner_contract is None:
        raise SystemExit("전일 적재 교정 대상 계약 없음: brand_owner_corporate_card_count")
    owner_contract["description"] = (
        "지정 브랜드 가맹점주의 기업카드 보유 인원을 KST 전일 스냅샷의 "
        "고유 주대표자 기준으로 센다."
    )
    owner_contract["result_grain"] = "KST 전일 스냅샷 1행"
    owner_contract["time_policy"]["snapshot"] = KST_PREVIOUS_DAY_SQL

    closed_contract = contracts.get("recent_closed_brand_merchant_count")
    if closed_contract is None:
        raise SystemExit("전일 적재 교정 대상 계약 없음: recent_closed_brand_merchant_count")
    closed_contract["time_policy"]["range"] = (
        "KST 전일 - 조회기간개월수부터 KST 전일까지"
    )
    closed_contract["time_policy"]["range_start"] = KST_RECENT_PERIOD_START_SQL
    closed_contract["time_policy"]["range_end"] = KST_PREVIOUS_DAY_SQL

    closed_reference = references.get("최근기간_브랜드가맹점_폐업수")
    if closed_reference is None:
        raise SystemExit("전일 적재 교정 대상 질의 참조 없음: 최근기간_브랜드가맹점_폐업수")
    closed_reference["rules"][0] = (
        "최근 N개월·N달은 KST 전일에서 N개월 전부터 KST 전일까지, "
        "최근 N년은 N×12개월 전부터 KST 전일까지 양 끝을 포함한다."
    )

    owner_reference = references.get("브랜드가맹점주_기업카드보유인원")
    if owner_reference is None:
        raise SystemExit("전일 적재 교정 대상 질의 참조 없음: 브랜드가맹점주_기업카드보유인원")
    owner_reference["join_rule"] = (
        "가맹점일별요약의 KST 전일 기준년월일에서 가맹점명 또는 브랜드명을 부분일치 "
        "검색하고 기업 신용+체크카드 수가 양수인 대표고객식별자를 중복 제거해 집계한다."
    )
    owner_reference["rules"][2] = (
        "질문에 기준일이 없으면 tbdaaus01의 KST 전일 기준년월일을 사용하고 "
        "결과에 조회기준일을 포함한다."
    )
    return 7


# ---------------------------------------------------------------------------
# 8. 기업고객 전일 스냅샷과 월 이력의 역할 분리
# ---------------------------------------------------------------------------
_CORPORATE_CURRENT_TERMS = [
    "현재",
    "현재기준",
    "현재 시점",
    "오늘",
    "전일",
    "어제",
    "최신",
    "지금",
]


def _corporate_snapshot_policy(*, mixed: bool) -> dict:
    current_tables = ["tbdaa1d12", "tmdaa1d12"] if mixed else ["tbdaa1d12"]
    return {
        "default": current_tables,
        "current_snapshot_when": list(_CORPORATE_CURRENT_TERMS),
        "current_snapshot": current_tables,
        "period_snapshot": ["tmdaa1d12"],
    }


def _replace_corporate_daily_with_monthly(value):
    if isinstance(value, str):
        return (
            value.replace("tbdaa1d12", "tmdaa1d12")
            .replace("기업고객일별기본", "기업고객월별기본")
            .replace("기업 일 스냅샷", "기업 월 스냅샷")
            .replace("SUBSTR(기준년월일, 1, 6)", "기준년월")
            .replace("SUBSTR(기준년월일,1,6)", "기준년월")
        )
    if isinstance(value, list):
        return [_replace_corporate_daily_with_monthly(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _replace_corporate_daily_with_monthly(item)
            for key, item in value.items()
        }
    return value


# 유효 기업카드 보유 속성의 원천 4개. 기업고객 스냅샷이 이 속성의 본래 원천이고,
# 가맹점 enrichment 두 개는 "기업카드를 가진 가맹점주" 를 물을 때만 쓰는 부수 원천이다.
# v1 은 이 속성에 source_selection 이 없어서 "법인카드"·"기업카드" 라는 낱말 하나로
# 네 테이블이 모두 최상위 후보 점수를 받았다. "현재 기준 유효 기업 신용카드 수" 는
# 월말 스냅샷(tmdaa1d12)이 일 스냅샷(tbdaa1d12)을 눌러 과거 달을 답했다.
_CORPORATE_CARD_HOLDING_ROLES = {
    "tbdaa1d12": "current_corporate_card_holding",
    "tmdaa1d12": "monthly_corporate_card_holding",
    "tbdaaus01": "current_merchant_card_holding",
    "tmdaaus01": "monthly_merchant_card_holding",
}


def _apply_corporate_card_holding_source_selection(schema: dict) -> None:
    """유효 기업카드 보유 원천을 현재(D-1)와 명시 과거 월로 갈라 놓는다."""
    attribute = next(
        (
            item
            for item in schema.get("semantic_attributes", [])
            if item.get("name") == "corporate_card_holding"
        ),
        None,
    )
    if attribute is None:
        raise SystemExit("corporate_card_holding 속성 없음")

    mappings = list(attribute.get("source_mappings") or [])
    by_table = {str(item.get("table") or ""): item for item in mappings}
    missing = sorted(set(_CORPORATE_CARD_HOLDING_ROLES) - set(by_table))
    if missing:
        raise SystemExit(f"기업카드 보유 원천 누락: {', '.join(missing)}")
    for table, role in _CORPORATE_CARD_HOLDING_ROLES.items():
        by_table[table]["role"] = role
    # 후보 점수가 같으면 선언 순서가 순위를 가른다. 기업고객 스냅샷을 앞에 둔다.
    attribute["source_mappings"] = [
        by_table[table] for table in _CORPORATE_CARD_HOLDING_ROLES
    ]
    attribute["source_selection"] = {
        "default_role_prefix": "current_",
        "period_role_prefix": "monthly_",
        "current_terms": list(_CORPORATE_CURRENT_TERMS),
        # 월말 스냅샷은 달이 닫힌 뒤 적재된다. 실행일이 속한 달은 일 스냅샷만 갖고 있다.
        "open_month_uses_current": True,
    }
    cautions = attribute.setdefault("semantic_cautions", [])
    caution = (
        "현재·최신 보유는 tbdaa1d12 KST 전일 기준년월일, 명시한 과거 월은 "
        "tmdaa1d12 기준년월에서 읽는다."
    )
    if caution not in cautions:
        cautions.append(caution)


def apply_corporate_customer_semantic_overrides(schema: dict) -> int:
    """Route current corporate snapshots to D-1 and history to monthly rows."""
    _apply_corporate_card_holding_source_selection(schema)
    entities = {
        str(item.get("name") or ""): item for item in schema.get("semantic_entities", [])
    }
    metrics = {
        str(item.get("name") or ""): item for item in schema.get("canonical_metrics", [])
    }
    contracts = {
        str(item.get("name") or ""): item
        for item in schema.get("semantic_query_contracts", [])
    }
    references = {
        str(item.get("intent") or ""): item for item in schema.get("query_references", [])
    }
    glossary = {
        str(item.get("term") or ""): item for item in schema.get("glossary", [])
    }

    daily_entity = entities.get("corporate_customer_daily")
    monthly_entity = entities.get("corporate_customer_no_usage_snapshot")
    if daily_entity is None or monthly_entity is None:
        raise SystemExit("기업고객 전일/월별 semantic entity 없음")
    daily_entity["use_when"] = [
        "현재·오늘·전일·어제·최신 기업고객 상태를 KST 전일 기준년월일로 조회할 때"
    ]
    monthly_entity["use_when"] = [
        "명시한 과거 기준년월 또는 여러 달의 기업고객 카드·이용·한도·연체 이력을 조회할 때"
    ]

    monthly_metric_names = (
        "기업월카드이용금액",
        "기업월평균카드이용금액",
        "관리기업월이용금액",
        "기업월국세이용금액",
        "기업월KB페이이용금액",
    )
    for name in monthly_metric_names:
        metric = metrics.get(name)
        if metric is None:
            raise SystemExit(f"기업고객 월 이력 교정 대상 지표 없음: {name}")
        rewritten = _replace_corporate_daily_with_monthly(metric)
        metric.clear()
        metric.update(rewritten)
        metric["source_table"] = "tmdaa1d12"
        metric["source_entity"] = "corporate_customer_no_usage_snapshot"
        metric["default_time_dimension"] = "기준년월"

    daily_snapshot_metric_names = (
        "기업유효카드수",
        "기업한도소진율",
        "기업잔여한도금액",
        "기업한도사용금액",
        "총여신한도금액",
        "관리기업연체원금",
    )
    for name in daily_snapshot_metric_names:
        metric = metrics.get(name)
        if metric is None:
            raise SystemExit(f"기업고객 전일 교정 대상 지표 없음: {name}")
        filters = [
            str(value)
            for value in metric.get("required_filters", [])
            if "고객별 월 최신" not in str(value)
            and "특정 기준월 또는 기준일 제한" not in str(value)
        ]
        metric["required_filters"] = [
            *filters,
            "현재 조회는 tbdaa1d12.기준년월일 = KST 전일(D-1); 명시 과거 월은 계약의 tmdaa1d12 원천 사용",
        ]

    # unit 은 % 인데 expression 은 1.0 - 잔여/총한도, 즉 0~1 비율이다. 프롬프트에
    # unit=% 만 보이면 임계값 없는 "소진율 높은" 질문에서 모델이 90 을 써 늘 0건이
    # 된다. 비율이라는 사실을 unit 에 적어 둔다.
    metrics["기업한도소진율"]["unit"] = "비율 0~1 (90% 는 0.9)"

    fixed_monthly_contracts = (
        "corporate_member_with_monthly_usage_count",
        "corporate_monthly_average_usage_by_company",
        "named_corporate_monthly_average_usage",
        "named_corporate_half_year_usage",
        "monthly_corporate_metric_by_customer_attribute",
    )
    mixed_contracts = (
        "corporate_card_active_without_recent_usage",
        "corporate_card_churn_after_usage",
        "corporate_check_card_only_high_average",
    )
    snapshot_contracts = (
        "corporate_limit_status_at_month",
        "named_corporate_limit_status_at_month",
        "corporate_card_low_limit_utilization",
    )

    for name in fixed_monthly_contracts:
        contract = contracts.get(name)
        if contract is None:
            raise SystemExit(f"기업고객 월 이력 교정 대상 계약 없음: {name}")
        rewritten = _replace_corporate_daily_with_monthly(contract)
        contract.clear()
        contract.update(rewritten)
        contract["source_tables"] = ["tmdaa1d12"]
        contract.pop("source_table_policy", None)

    for name in mixed_contracts:
        contract = contracts.get(name)
        if contract is None:
            raise SystemExit(f"기업고객 혼합 교정 대상 계약 없음: {name}")
        contract["source_tables"] = ["tbdaa1d12", "tmdaa1d12"]
        contract["source_table_policy"] = _corporate_snapshot_policy(mixed=True)
        contract.setdefault("time_policy", {})["accumulation_route"] = (
            "현재 모집단은 tbdaa1d12 KST 전일, 과거 월 이용은 tmdaa1d12 기준년월; "
            "명시한 과거 기준월만 조회하면 tmdaa1d12만 사용"
        )

    active_without_usage = contracts["corporate_card_active_without_recent_usage"]
    active_without_usage["description"] = (
        "명시 기준월이면 tmdaa1d12 월말 모집단과 월 이력을 사용하고, 현재를 명시하면 "
        "tbdaa1d12 KST 전일 모집단과 tmdaa1d12 과거 월 이력을 결합해 무실적 기업을 반환한다."
    )
    active_without_usage["time_policy"].update(
        {
            "required_parameter": (
                "기준년월(YYYYMM); 명시 현재는 실행월로 해석하고, 기간·현재 표현이 모두 없으면 추가 입력 요청"
            ),
            "current_snapshot": "명시 현재는 tbdaa1d12 KST 전일 기준년월일 1건",
            "period_snapshot": "명시 과거 기준월은 tmdaa1d12 고객×월 최신행",
        }
    )
    churn = contracts["corporate_card_churn_after_usage"]
    churn["time_policy"]["current_snapshot"] = (
        "명시 현재는 tbdaa1d12 KST 전일 기준년월일 1건; 과거 as-of 월은 tmdaa1d12 고객×월 최신행"
    )
    check_only = contracts["corporate_check_card_only_high_average"]
    check_only["time_policy"]["current_snapshot"] = (
        "명시 현재는 tbdaa1d12 KST 전일 기준년월일 1건; 과거 as-of 월은 tmdaa1d12 고객×월 최신행"
    )
    check_only["time_policy"]["average_window"] = (
        "현재 조회는 완료월 tmdaa1d12와 현재월 tbdaa1d12 D-1 누적금액, 과거 조회는 해당 연도 tmdaa1d12"
    )

    for name in snapshot_contracts:
        contract = contracts.get(name)
        if contract is None:
            raise SystemExit(f"기업고객 스냅샷 교정 대상 계약 없음: {name}")
        contract["source_tables"] = ["tbdaa1d12", "tmdaa1d12"]
        contract["source_table_policy"] = _corporate_snapshot_policy(mixed=False)
        contract.setdefault("time_policy", {})["accumulation_route"] = (
            "현재·기간 미지정은 tbdaa1d12 KST 전일, 명시한 과거 월은 "
            "tmdaa1d12 기준년월"
        )

    for name in ("corporate_limit_status_at_month", "named_corporate_limit_status_at_month"):
        contract = contracts[name]
        contract["description"] = (
            "현재·기간 미지정이면 tbdaa1d12 KST 전일 고객별 1행에서, 명시한 과거 월이면 "
            "tmdaa1d12 고객×월 최신행에서 총한도·잔여한도·사용한도와 한도소진율을 반환한다."
        )
        contract["time_policy"].update(
            {
                "current": "tbdaa1d12 KST 전일 기준년월일",
                "explicit": "tmdaa1d12의 질문 지정 기준년월",
            }
        )
        contract["deduplication"]["snapshot"] = (
            "현재는 KST 전일 고객별 1행; 과거 월은 고객식별자별 기준년월일 DESC ROW_NUMBER() = 1"
        )

    low_utilization = contracts["corporate_card_low_limit_utilization"]
    low_utilization["description"] = (
        "현재·기간 미지정이면 tbdaa1d12 KST 전일, 명시 과거 월이면 tmdaa1d12 고객×월 "
        "최신행에서 신용카드 보유·총한도·한도소진율 조건을 적용한다."
    )
    low_utilization["filters"][0] = (
        "현재는 KST 전일 고객별 1행; 과거 월은 고객별 최신 기준년월일 1건"
    )

    decline = contracts.get("current_corporate_half_year_usage_decline")
    issuance = contracts.get("current_corporate_member_half_year_new_check_card_issuance")
    if decline is None or issuance is None:
        raise SystemExit("기업고객 현재/이력 혼합 계약 없음")
    decline["source_tables"] = ["tbdaa1d12", "tmdaa1d12"]
    decline.setdefault("time_policy", {})["accumulation_route"] = (
        "현재 기업 모집단은 tbdaa1d12 KST 전일, 반기 이용은 tmdaa1d12 기준년월"
    )
    issuance.setdefault("time_policy", {})["corporate_snapshot"] = (
        "기업명·사업자번호는 tbdaa1d12 KST 전일 기준년월일에서 조회"
    )

    reference_policies = {
        "기업고객_여신한도": _corporate_snapshot_policy(mixed=False),
        "기업카드_이탈회원": _corporate_snapshot_policy(mixed=True),
        "체크카드만_고액이용": _corporate_snapshot_policy(mixed=True),
        "저소진율_한도영업대상": _corporate_snapshot_policy(mixed=False),
        "관리기업_연체": _corporate_snapshot_policy(mixed=False),
    }
    for intent, policy in reference_policies.items():
        reference = references.get(intent)
        if reference is None:
            raise SystemExit(f"기업고객 주기 교정 대상 질의 참조 없음: {intent}")
        reference["source_table_policy"] = policy

    references["기업카드_이탈회원"]["join_rule"] = (
        "현재 상태는 tbdaa1d12 KST 전일 고객별 1행, 과거 이용은 tmdaa1d12 고객×월 최신행으로 "
        "축소해 고객식별자로 결합한다. 과거 as-of 월 질문은 tmdaa1d12만 사용한다."
    )
    references["체크카드만_고액이용"]["join_rule"] = (
        "현재 보유 상태는 tbdaa1d12 KST 전일, 완료월 이용은 tmdaa1d12 월 최신행으로 결합하며 "
        "현재월 누적 이용은 D-1 행을 포함한다. 과거 as-of 월 질문은 tmdaa1d12만 사용한다."
    )
    references["관리기업_연체"]["join_rule"] = (
        "현재·기간 미지정은 관리기업목록 VALUES CTE와 tbdaa1d12 KST 전일 행을 사업자등록번호로 "
        "조인하고, 명시 과거 월은 tmdaa1d12 고객×월 최신행에서 카드·여신 연체를 판정한다."
    )
    references["기업고객_여신한도"].update(
        {
            "join_rule": (
                "현재·기간 미지정은 tbdaa1d12 KST 전일 행, 명시 과거 월은 tmdaa1d12 "
                "고객×월 최신행을 고객식별자로 고객기본과 조인해 기업명을 가져온다."
            ),
            "recommended_columns": {
                "기업명": '"기업명"',
                "한도": '"기업총한도금액"',
            },
            "rules": [
                "현재는 tbdaa1d12.기준년월일 = KST 전일(D-1), 명시 과거 월은 "
                "tmdaa1d12.기준년월 = 요청월로 제한한다."
            ],
        }
    )
    references["저소진율_한도영업대상"]["join_rule"] = (
        "현재·기간 미지정은 tbdaa1d12 KST 전일 고객별 1행, 명시 과거 월은 tmdaa1d12 "
        "고객×월 최신행에서 기업카드 보유, 총한도 하한과 소진율 상한을 적용한다."
    )

    for intent in ("기업카드_이탈회원", "체크카드만_고액이용"):
        reference = references[intent]
        joins = list(reference.get("join_tables") or [])
        if "tmdaa1d12" not in joins:
            joins.append("tmdaa1d12")
        reference["join_tables"] = joins

    industry_reference = references.get("영업대상군_결제처업종별이용금액")
    if industry_reference is None:
        raise SystemExit("기업고객 혼합 업종 질의 참조 없음")
    industry_reference["source_table_policy"] = _corporate_snapshot_policy(mixed=True)
    industry_joins = list(industry_reference.get("join_tables") or [])
    if "tmdaa1d12" not in industry_joins:
        industry_joins.insert(1, "tmdaa1d12")
    industry_reference["join_tables"] = industry_joins

    for intent in ("관리기업_6개월_이상징후", "관리기업_한도감액"):
        reference = references.get(intent)
        if reference is None:
            raise SystemExit(f"기업고객 월 이력 교정 대상 질의 참조 없음: {intent}")
        rewritten = _replace_corporate_daily_with_monthly(reference)
        reference.clear()
        reference.update(rewritten)
        reference["primary_table"] = "tmdaa1d12"

    safe_paths = schema.get("semantic_join_graph", {}).get("safe_paths", [])
    paths = {str(item.get("name") or ""): item for item in safe_paths}
    path_specs = {
        "corporate_monthly_to_domestic_sales": {
            "from_entity": "corporate_customer_no_usage_snapshot",
            "to_entity": "domestic_sales",
            "join_type": "one_to_many",
            "sql": (
                'tmdaa1d12."기준년월" = tbdaabt30."기준년월" AND '
                'tmdaa1d12."고객식별자" = tbdaabt30."기업고객식별자"'
            ),
            "use_when": ["과거 월 영업 대상 기업의 같은 월 결제처 업종별 이용금액"],
            "caution": "기업고객과 전표의 기준년월을 함께 제한한 뒤 고객식별자로 조인",
            "from_table": "tmdaa1d12",
            "to_table": "tbdaabt30",
            "confidence": "curated",
            "provenance": "user_provided_load_cadence",
        },
        "customer_to_corporate_monthly": {
            "from_entity": "customer",
            "to_entity": "corporate_customer_no_usage_snapshot",
            "join_type": "one_to_many",
            "sql": 'tbdaaat01."고객식별자" = tmdaa1d12."고객식별자"',
            "use_when": ["기업명·고객 속성과 과거 월 여신한도 결합"],
            "caution": "tmdaa1d12 기준년월을 반드시 제한",
            "from_table": "tbdaaat01",
            "to_table": "tmdaa1d12",
            "confidence": "curated",
            "provenance": "user_provided_load_cadence",
        },
    }
    for name, spec in path_specs.items():
        if name in paths:
            paths[name].update(spec)
        else:
            safe_paths.append({"name": name, **spec})

    daily_sales_path = paths.get("corporate_daily_to_domestic_sales")
    if daily_sales_path is None:
        raise SystemExit("기업고객 전일·국내이용 결합 경로 없음")
    daily_sales_path["caution"] = (
        "현재 모집단은 tbdaa1d12 KST 전일 1행으로 제한하고, 과거 월 대상군은 "
        "corporate_monthly_to_domestic_sales 경로를 사용한다. 전표 이용월은 요청기간으로 제한한다."
    )

    glossary_updates = {
        "유실적": {
            "sql_hint": (
                "tmdaa1d12를 지정 기준년월의 고객별 최신 기준년월일 1건으로 축소하고 "
                "신용+체크 이용금액 합계가 0보다 큰 고객식별자를 COUNT(DISTINCT)한다."
            )
        },
        "기업한도": {
            "canonical": (
                "현재는 tbdaa1d12, 명시 과거 월은 tmdaa1d12의 "
                "기업총한도금액·기업총잔여한도금액"
            ),
            "description": (
                "기업고객에게 부여된 총한도와 남은 잔여한도. 현재는 KST 전일 고객별 1행, "
                "과거 월은 고객×월 최신 기준년월일 1건에서 계산한다."
            ),
            "sql_hint": (
                "현재는 tbdaa1d12.기준년월일 = KST 전일(D-1), 명시 과거 월은 "
                "tmdaa1d12.기준년월 = 요청월로 제한하고 고객별 최신행에서 총한도와 잔여한도를 조회한다."
            ),
        },
        "월평균이용금액": {
            "sql_hint": (
                "tmdaa1d12를 고객×월 최신행으로 축소하고 신용+체크 이용금액을 합산한 뒤 "
                "DATE_DIFF('month', 시작월, 종료월) + 1로 나눈다."
            )
        },
        "관리기업": {
            "sql_hint": (
                "필수 관리기업목록을 요청별 VALUES CTE로 만들고, 현재·기간 미지정은 "
                "tbdaa1d12 KST 전일 행, 명시 과거 월은 tmdaa1d12 고객×월 최신행과 "
                "사업자등록번호로 INNER JOIN한다."
            )
        },
    }
    for term, values in glossary_updates.items():
        entry = glossary.get(term)
        if entry is None:
            raise SystemExit(f"기업고객 주기 교정 대상 용어 없음: {term}")
        entry.update(values)

    return (
        3
        + len(monthly_metric_names)
        + len(daily_snapshot_metric_names)
        + len(fixed_monthly_contracts)
        + len(mixed_contracts)
        + len(snapshot_contracts)
        + 2
        + len(reference_policies)
        + 3
        + len(path_specs)
        + 1
        + len(glossary_updates)
    )


# ---------------------------------------------------------------------------
# 9. 고객식별자(기업 1곳)와 회원일련번호(카드 계약 1건)의 역할 분리
#
# customer_to_member 는 one_to_many 다. 한 기업고객 아래에 부서·사업장 단위
# 회원일련번호가 여러 개 달리므로 두 키의 COUNT DISTINCT 값이 서로 다르다.
# 그런데 v1 은 "고객수" 를 회원수 지표의, "연체고객수" 를 연체회원수 지표의
# 동의어로 선언해 두었다. build_metrics_summary() 는 질문에 등장한 가장 긴
# 동의어로 지표를 고르므로, "기업고객 수" 를 물어도 프롬프트에는
# COUNT(DISTINCT tbdaaat03."회원일련번호") 가 정답 지표로 들어갔다.
# 골든셋 정답은 COUNT(DISTINCT tbdaaat01."고객식별자") 다.
#
# 잘못된 동의어를 떼는 것만으로는 "기업 수" 가 갈 곳이 없어지므로, 고객식별자를
# 세는 지표 두 개와 두 키를 대비시키는 용어 두 개를 같이 넣는다.
# ---------------------------------------------------------------------------
CORPORATE_CUSTOMER_COUNT_METRIC = {
    "name": "기업고객수",
    "domain": "customer_card_portfolio",
    "business_definition": "개인기업구분코드가 기업인 고유 고객식별자 수",
    "expression": 'COUNT(DISTINCT tbdaaat01."고객식별자")',
    "source_table": "tbdaaat01",
    "source_entity": "customer",
    "default_time_dimension": "",
    "result_grain": "질문이 요청한 분류축 조합별 1행",
    "required_filters": ["tbdaaat01.\"개인기업구분코드\" = '2'"],
    "semantic_cautions": [
        "회원일련번호가 아니라 고객식별자를 DISTINCT 집계한다. 한 기업고객에 부서·사업장 회원일련번호가 여러 개 달린다.",
        "같은 값이 전표·가맹점 테이블에서는 기업고객식별자, 부서사업장회원(tbdaaac67)에서는 법인고객식별자라는 이름으로 들어 있다.",
    ],
    "synonyms": ["기업수", "업체수", "회사수", "법인수", "법인고객수", "고객수"],
    "unit": "개",
    "aggregation_behavior": "distinct_count",
}

DELINQUENT_CORPORATE_COUNT_METRIC = {
    "name": "연체기업수",
    "domain": "credit_risk",
    "business_definition": "기준 시점에 카드 또는 여신 연체가 있는 고유 기업고객 수",
    "expression": 'COUNT(DISTINCT tbdaa1d12."고객식별자")',
    "source_table": "tbdaa1d12",
    "source_entity": "corporate_customer_daily",
    "default_time_dimension": "기준년월일",
    "result_grain": "기준 시점 1행 또는 기준년월별 1행",
    "required_filters": [
        "카드연체여부 = '1' OR 여신연체여부 = '1' OR COALESCE(카드연체원금, 0) + COALESCE(여신연체원금, 0) > 0",
        "현재 조회는 tbdaa1d12.기준년월일 = KST 전일(D-1); 명시 과거 월은 계약의 tmdaa1d12 원천 사용",
    ],
    "semantic_cautions": [
        "연체 회원 수(tddaa3l01.회원일련번호)와 다른 지표다. 기업 수는 고객식별자를 DISTINCT 집계한다.",
        "일별 snapshot이므로 고객식별자별 기준년월일 DESC 최신 1건으로 축소한 뒤 연체 조건을 적용한다.",
    ],
    "synonyms": ["연체고객수", "연체업체수", "연체기업고객수"],
    "unit": "개",
    "aggregation_behavior": "snapshot_dedup_then_aggregate",
}

IDENTITY_GLOSSARY_TERMS = (
    {
        "term": "기업고객",
        "canonical": "tbdaaat01.고객식별자 (전표·가맹점은 기업고객식별자, 부서사업장회원은 법인고객식별자)",
        "description": (
            "당사와 거래하는 기업·법인 1곳. 기업 수·업체 수·고객 수는 모두 이 식별자의 고유 개수이며, "
            "한 기업고객 아래에 부서·사업장 단위 회원일련번호가 여러 개 달린다."
        ),
        "aliases": ["법인고객", "기업 수", "업체 수", "고객 수", "고객식별자", "기업고객식별자", "법인고객식별자"],
        "sql_hint": (
            "기업·업체·법인·고객 수는 COUNT(DISTINCT \"고객식별자\")로 세고 개인기업구분코드 = '2' 를 함께 적용한다. "
            "회원일련번호로 세지 않는다."
        ),
    },
    {
        "term": "카드회원",
        "canonical": "tbdaaat03.회원일련번호",
        "description": (
            "기업고객이 맺은 카드 계약 1건. 부서·사업장 단위로 채번되며 보유 카드·이용·연체·충당금이 이 키로 연결된다."
        ),
        "aliases": ["회원 수", "회원일련번호", "회원번호", "부서사업장회원"],
        "sql_hint": (
            "회원 수는 COUNT(DISTINCT \"회원일련번호\")로 센다. 고객식별자와는 1:N이므로 "
            "같은 결과에서 기업 수를 낼 때는 고객식별자로 다시 DISTINCT한다."
        ),
    },
)


def apply_customer_member_identity_overrides(schema: dict) -> int:
    """Stop 고객식별자 questions from resolving to 회원일련번호 metrics."""
    metric_list = schema.get("canonical_metrics")
    if not isinstance(metric_list, list):
        raise SystemExit("canonical_metrics 없음")
    metrics = {str(item.get("name") or ""): item for item in metric_list}

    # 고객 단위 표현을 회원 지표에서 떼어낸다.
    wrong_synonyms = {"회원수": "고객수", "연체회원수": "연체고객수"}
    for metric_name, synonym in wrong_synonyms.items():
        metric = metrics.get(metric_name)
        if metric is None:
            raise SystemExit(f"고객/회원 분리 대상 지표 없음: {metric_name}")
        synonyms = metric.get("synonyms") or []
        if synonym not in synonyms:
            raise SystemExit(f"{metric_name} 에 {synonym} 동의어가 없다. 이미 분리됐는지 확인 필요")
        synonyms.remove(synonym)

    # 고객식별자를 세는 지표를 헷갈리던 회원 지표 바로 앞에 놓는다.
    new_metrics = {
        "회원수": CORPORATE_CUSTOMER_COUNT_METRIC,
        "연체회원수": DELINQUENT_CORPORATE_COUNT_METRIC,
    }
    for anchor_name, new_metric in new_metrics.items():
        if new_metric["name"] in metrics:
            raise SystemExit(f"이미 있는 지표: {new_metric['name']}")
        anchor = metric_list.index(metrics[anchor_name])
        metric_list.insert(anchor, deepcopy(new_metric))

    glossary = schema.get("glossary")
    if not isinstance(glossary, list):
        raise SystemExit("glossary 없음")
    existing_terms = {str(item.get("term") or "") for item in glossary}
    for entry in IDENTITY_GLOSSARY_TERMS:
        if entry["term"] in existing_terms:
            raise SystemExit(f"이미 있는 용어: {entry['term']}")
        glossary.append(deepcopy(entry))

    # 도메인 preferred_metrics 는 build_metrics_summary() 의 1차 필터다.
    domains = {str(item.get("name") or ""): item for item in schema.get("canonical_domains", [])}
    for domain_name in ("customer_card_portfolio", "corporate_sales_targeting"):
        domain = domains.get(domain_name)
        if domain is None:
            raise SystemExit(f"도메인 없음: {domain_name}")
        domain["preferred_metrics"].append(CORPORATE_CUSTOMER_COUNT_METRIC["name"])

    entities = {str(item.get("name") or ""): item for item in schema.get("semantic_entities", [])}
    entity_use_when = {
        "customer": "기업·업체·법인·고객 수를 고객식별자로 셀 때",
        "member": "카드 계약 단위(회원일련번호)로 회원 수나 카드 보유를 셀 때",
    }
    for entity_name, use_when in entity_use_when.items():
        entity = entities.get(entity_name)
        if entity is None:
            raise SystemExit(f"semantic entity 없음: {entity_name}")
        entity["use_when"].append(use_when)

    return (
        len(wrong_synonyms)
        + len(new_metrics)
        + len(IDENTITY_GLOSSARY_TERMS)
        + 2
        + len(entity_use_when)
    )


# ---------------------------------------------------------------------------
# 10. 이용금액과 매출금액 중 질문이 쓴 쪽 컬럼을 고르게 한다
#
# 두 단어는 테이블마다 섞여 쓰인다. 전표(tbdaabt30·tbdaabt08)는 "매출금액",
# 기업 월 스냅샷(tmdaa1d12)은 "금월신용카드이용금액", 가맹점 월실적(tmdaa5e11)은
# "가맹점일시불매출금액" 이다. 둘을 같은 뜻으로 뭉치면 안 된다. 기업 단위에서
# 이용금액은 그 기업이 카드로 쓴 돈이고, 매출금액은 그 기업이 가맹점으로서
# 받은 돈이라 값이 서로 다르다(tmdaa5e11 은 기업고객식별자를 직접 들고 있다).
# 골든셋도 "법인카드 매출금액"→tbdaabt30."매출금액",
# "기업 … 이용금액"→tbdaa1d12."금월*이용금액" 으로 단어를 따라 컬럼을 고른다.
#
# 그런데 어느 단어가 어느 컬럼 계열인지는 어디에도 없어서
# build_glossary_summary() 가 "가맹점별 이용금액" 에 아무것도 못 내놓고
# "기업별 매출금액" 에는 가맹점 지표인 가맹점월매출만 내놓는다.
# ---------------------------------------------------------------------------
USAGE_SALES_GLOSSARY_TERM = {
    "term": "이용금액",
    "canonical": (
        "카드로 쓴 돈. 받은 돈인 매출금액과 다른 지표이며, "
        "질문이 쓴 단어가 이용금액 컬럼과 매출금액 컬럼 중 어느 쪽을 볼지 결정한다"
    ),
    "description": (
        "이용금액은 회원·기업·카드가 카드로 쓴 금액이고, 매출금액은 가맹점이 카드로 "
        "받은 금액이다. 같은 기업이라도 카드로 쓴 이용금액과 가맹점으로서 받은 매출금액은 "
        "서로 다른 값이므로 두 단어를 바꿔 쓰지 않는다. 단어가 컬럼 계열을 정하고, "
        "질문의 집계 대상이 그 안에서 테이블을 정한다."
    ),
    "aliases": ["매출금액", "매출액", "이용액", "사용금액", "카드매출", "카드이용금액", "결제금액"],
    "sql_hint": (
        "이용금액을 물으면 이용금액 컬럼을 쓴다: 기업·법인 월은 tmdaa1d12의 "
        "금월신용카드이용금액+금월체크카드이용금액, 카드 월은 tmdaa3e16의 금월이용합계금액. "
        "매출금액을 물으면 매출금액 컬럼을 쓴다: 전표·업종·일자는 tbdaabt30(해외는 tbdaabt08)의 "
        "매출금액, 가맹점 월은 tmdaa5e11의 가맹점일시불매출금액+가맹점할부매출금액이며 "
        "기업 단위 매출은 tmdaa5e11의 기업고객식별자로 묶는다."
    ),
}


def apply_usage_sales_vocabulary(schema: dict) -> int:
    """Tell 이용금액 and 매출금액 apart by 집계 대상 instead of by wording."""
    glossary = schema.get("glossary")
    if not isinstance(glossary, list):
        raise SystemExit("glossary 없음")
    if any(item.get("term") == USAGE_SALES_GLOSSARY_TERM["term"] for item in glossary):
        raise SystemExit(f"이미 있는 용어: {USAGE_SALES_GLOSSARY_TERM['term']}")

    # 가맹점월매출 바로 앞에 놓는다. 동점이면 build_glossary_summary() 가
    # 먼저 선언된 용어를 위로 올리므로, 대상을 가리는 규칙이 앞에 와야 한다.
    anchor = next(
        (
            position
            for position, item in enumerate(glossary)
            if item.get("term") == "가맹점월매출"
        ),
        len(glossary),
    )
    glossary.insert(anchor, deepcopy(USAGE_SALES_GLOSSARY_TERM))
    return 1


# ---------------------------------------------------------------------------
# 10-1. 전표 금액·건수는 매출전표종류구분코드로 부호를 정해 순액으로 더한다
#
# 매출전표(tbdaabt30 국내·tbdaabt08 해외) 한 행은 정당·정정·취소 중 한 종류의
# 전표다. 취소전표도 금액을 양수로 들고 있어서 SUM("매출금액") 과
# COUNT("매출전표번호") 는 취소·환급분까지 더해 이용금액·건수를 부풀린다.
# 업무 규칙은 정당(1)·취소정정(4)은 더하고 정정(2)·취소(3)·청구보류(5)·체크환급(6)은
# 빼는 순액이다(사용자 제공).
#
# v1 은 이 코드값을 몰라서 query_references.영업대상군_결제처업종별이용금액 의 rules 에
# "취소·환입 순액 규칙은 문서에 없으므로 임의 코드 필터를 만들지 않는다" 라고 적어 두고
# 전표 금액을 그대로 SUM 했고, verified query 는 전표취소구분코드로 취소 건을 빼려 했다.
# 전표취소구분코드는 취소 사유(가맹점번호 상이·분할취소 등) 코드라 취소 여부를
# 가려 주지 않는다. 두 코드북을 함께 선언해 그 혼동을 끊는다.
# ---------------------------------------------------------------------------
SLIP_TYPE_COLUMN = "매출전표종류구분코드"
SLIP_TYPE_VALUE_SEMANTICS = {
    "1": "정당전표",
    "2": "정정전표",
    "3": "취소전표",
    "4": "취소정정전표",
    "5": "청구보류",
    "6": "체크환급",
}
# 금액·건수를 더하는 전표 종류. 나머지는 같은 값을 빼는 쪽이다.
SLIP_TYPE_POSITIVE_CODES = ("1", "4")

SLIP_CANCEL_REASON_COLUMN = "전표취소구분코드"
SLIP_CANCEL_REASON_VALUE_SEMANTICS = {
    "1": "가맹점번호 상이",
    "2": "원인전표검증생략",
    "3": "분할취소",
    "4": "자동상계비대상",
}

# 사용자가 순액 대상으로 지정한 전표 금액과, 통화만 다른 미화 쌍둥이 컬럼.
NET_MEASURES_BY_TABLE = {
    "tbdaabt30": ("매출금액", "매출미화금액", "봉사료", "미화봉사료", "부가가치세", "가맹점수수료"),
    "tbdaabt08": ("매출금액", "매출미화금액"),
}
# 프롬프트에 항상 렌더되는 테이블 공식. 지표 이름 → 순액 공식의 대상 컬럼.
NET_FORMULAS_BY_TABLE = {
    "tbdaabt30": {
        "카드매출금액": "매출금액",
        "이용금액": "매출금액",
        "봉사료": "봉사료",
        "부가가치세": "부가가치세",
        "가맹점수수료": "가맹점수수료",
    },
    "tbdaabt08": {"해외매출금액": "매출금액"},
}
NET_COUNT_FORMULA_NAME = "매출건수"

# 컬럼 상세는 135자까지만 프롬프트에 렌더된다(_table_details).
SLIP_TYPE_DESCRIPTION = (
    "매출전표의 종류관리코드. 금액·건수 집계의 부호를 정한다: "
    "정당(1)·취소정정(4)은 더하고 정정(2)·취소(3)·청구보류(5)·체크환급(6)은 뺀다."
)
SLIP_CANCEL_REASON_DESCRIPTION = (
    "취소전표의 사유 코드(가맹점번호 상이·원인전표검증생략·분할취소·자동상계비대상). "
    "취소 여부는 매출전표종류구분코드로 가리며 이 컬럼으로 취소를 걸러내지 않는다."
)
# _table_details 가 컬럼 설명을 잘라내는 길이. 원문이 길면 순액 규칙이 잘려 나가므로
# 규칙을 지키고 원문을 줄인다(원문 전체는 v1 semantic_layer.yaml 에 남아 있다).
COLUMN_DESCRIPTION_RENDER_LIMIT = 135
NET_MEASURE_SUFFIX = (
    " 취소·정정 전표가 섞여 있어 그대로 SUM하지 않고 "
    "매출전표종류구분코드로 부호를 정해 순액으로 집계한다."
)
NET_AMOUNT_CAUTIONS = (
    "매출금액을 그대로 SUM하면 취소·환급 전표까지 더해져 이용금액이 부풀려진다.",
    "매출전표종류구분코드로 부호를 정한다: 정당(1)·취소정정(4)은 +, "
    "정정(2)·취소(3)·청구보류(5)·체크환급(6)은 -.",
)
NET_COUNT_CAUTIONS = (
    "전표 건수도 순액이다. COUNT로 세면 취소전표가 건수를 늘린다.",
    "매출전표종류구분코드가 정당(1)·취소정정(4)이면 +1, "
    "정정(2)·취소(3)·청구보류(5)·체크환급(6)이면 -1로 더한다.",
)
OLD_NET_AMOUNT_RULE = "취소·환입 순액 규칙은 문서에 없으므로 임의 코드 필터를 만들지 않는다."
NEW_NET_AMOUNT_RULE = (
    "취소·환입은 매출전표종류구분코드로 부호를 정해 순액으로 집계한다: "
    "정당(1)·취소정정(4)은 더하고 정정(2)·취소(3)·청구보류(5)·체크환급(6)은 뺀다. "
    "전표취소구분코드는 취소 사유 코드라 취소 여부를 가려 주지 않는다."
)


def _net_measure_description(description: str) -> str:
    """Return the measure description with the 순액 규칙 kept inside the render limit."""
    head = str(description or "").rstrip(". ") + "."
    room = COLUMN_DESCRIPTION_RENDER_LIMIT - len(NET_MEASURE_SUFFIX)
    if len(head) > room:
        head = head[: room - 1].rstrip() + "…"
    return head + NET_MEASURE_SUFFIX


def _net_amount(column: str, prefix: str = "") -> str:
    """Return the 순액 합계 expression for a 전표 금액 column."""
    codes = ",".join(f"'{code}'" for code in SLIP_TYPE_POSITIVE_CODES)
    slip = f'{prefix}"{SLIP_TYPE_COLUMN}"'
    amount = f'{prefix}"{column}"'
    return f"SUM(CASE WHEN {slip} IN ({codes}) THEN {amount} ELSE -{amount} END)"


def _net_count(prefix: str = "") -> str:
    """Return the 순액 건수 expression for 전표 rows."""
    codes = ",".join(f"'{code}'" for code in SLIP_TYPE_POSITIVE_CODES)
    slip = f'{prefix}"{SLIP_TYPE_COLUMN}"'
    return f"SUM(CASE WHEN {slip} IN ({codes}) THEN 1 ELSE -1 END)"


def _table_column(table: dict, name: str) -> dict | None:
    for section in COLUMN_SECTIONS:
        for column in table.get(section) or []:
            if column.get("name") == name:
                return column
    return None


def apply_sales_slip_net_amount(schema: dict) -> int:
    """Aggregate 전표 금액·건수 as 순액 and declare the codes behind the 부호."""
    changed = 0
    tables = {str(item.get("name") or ""): item for item in schema.get("tables", [])}

    for table_name, measures in NET_MEASURES_BY_TABLE.items():
        table = tables.get(table_name)
        if table is None:
            raise SystemExit(f"테이블 없음: {table_name}")

        slip_column = _table_column(table, SLIP_TYPE_COLUMN)
        if slip_column is None:
            raise SystemExit(f"{table_name}.{SLIP_TYPE_COLUMN} 없음")
        slip_column["description"] = SLIP_TYPE_DESCRIPTION
        slip_column["value_semantics"] = deepcopy(SLIP_TYPE_VALUE_SEMANTICS)
        slip_column["value_semantics_provenance"] = "user_provided_business_codebook"
        changed += 1

        # 취소 사유 코드는 국내 전표에만 있다.
        cancel_column = _table_column(table, SLIP_CANCEL_REASON_COLUMN)
        if cancel_column is not None:
            cancel_column["description"] = SLIP_CANCEL_REASON_DESCRIPTION
            cancel_column["value_semantics"] = deepcopy(SLIP_CANCEL_REASON_VALUE_SEMANTICS)
            cancel_column["value_semantics_provenance"] = "user_provided_business_codebook"
            changed += 1

        for measure_name in measures:
            measure = _table_column(table, measure_name)
            if measure is None:
                raise SystemExit(f"{table_name}.{measure_name} 없음")
            measure["description"] = _net_measure_description(measure.get("description"))
            changed += 1

        # 테이블 aggregation_policy 는 전표 테이블이 라우팅되면 항상 렌더되므로,
        # 매출전표종류구분코드가 프롬프트 컬럼 목록에서 밀려도 공식은 남는다.
        policy = table.get("aggregation_policy")
        if not isinstance(policy, dict):
            raise SystemExit(f"{table_name} aggregation_policy 없음")
        formulas = policy.setdefault("canonical_formulas", {})
        for formula_name, column_name in NET_FORMULAS_BY_TABLE[table_name].items():
            formulas[formula_name] = _net_amount(column_name)
        formulas[NET_COUNT_FORMULA_NAME] = _net_count()
        changed += len(NET_FORMULAS_BY_TABLE[table_name]) + 1

    metrics = {
        str(item.get("name") or ""): item for item in schema.get("canonical_metrics", [])
    }
    net_metrics = {
        "카드매출금액": (_net_amount("매출금액", "tbdaabt30."), NET_AMOUNT_CAUTIONS),
        "해외매출금액": (_net_amount("매출금액", "tbdaabt08."), NET_AMOUNT_CAUTIONS),
        "매출건수": (_net_count("tbdaabt30."), NET_COUNT_CAUTIONS),
    }
    for metric_name, (expression, cautions) in net_metrics.items():
        metric = metrics.get(metric_name)
        if metric is None:
            raise SystemExit(f"canonical metric 없음: {metric_name}")
        metric["expression"] = expression
        existing = metric.setdefault("semantic_cautions", [])
        for caution in cautions:
            if caution not in existing:
                existing.append(caution)
        changed += 1

    contract = next(
        (
            item
            for item in schema.get("semantic_query_contracts", [])
            if item.get("name") == "corporate_card_usage_by_merchant_industry"
        ),
        None,
    )
    if contract is None:
        raise SystemExit("semantic contract corporate_card_usage_by_merchant_industry 없음")
    calculation = contract.get("calculation") or {}
    if "usage_amount" not in calculation:
        raise SystemExit("corporate_card_usage_by_merchant_industry.calculation.usage_amount 없음")
    calculation["usage_amount"] = _net_amount("매출금액", "tbdaabt30.")
    changed += 1

    # query_references 의 recommended_columns 는 라우팅된 질의 유형의 정답 공식이다.
    reference_columns = {
        "월별_카드매출_조회": ("매출금액", ""),
        "업종별_매출": ("매출", "tbdaabt30."),
        "영업대상군_결제처업종별이용금액": ("이용금액", "tbdaabt30."),
    }
    references = {
        str(item.get("intent") or ""): item for item in schema.get("query_references", [])
    }
    for intent, (field, prefix) in reference_columns.items():
        reference = references.get(intent)
        if reference is None:
            raise SystemExit(f"query_reference 없음: {intent}")
        columns = reference.get("recommended_columns") or {}
        if field not in columns:
            raise SystemExit(f"{intent}.recommended_columns.{field} 없음")
        columns[field] = _net_amount("매출금액", prefix)
        changed += 1

    target_usage = references["영업대상군_결제처업종별이용금액"]
    rules = target_usage.get("rules") or []
    if OLD_NET_AMOUNT_RULE not in rules:
        raise SystemExit("영업대상군_결제처업종별이용금액 의 순액 미정 규칙을 찾지 못했다")
    rules[rules.index(OLD_NET_AMOUNT_RULE)] = NEW_NET_AMOUNT_RULE
    changed += 1

    return changed


# ---------------------------------------------------------------------------
# 10-2. 해외 이용금액은 카드 월실적, 청구금액은 청구금액 컬럼을 본다
#
# 10번 규칙(이용금액=카드로 쓴 돈, 매출금액=가맹점이 받은 돈)이 해외와 청구에서
# 두 군데 새고 있었다.
#
#   "해외 이용금액" → tbdaabt08(해외 매출전표)
#     해외매출금액 지표가 해외이용금액·해외사용액을 동의어로 들고 있어서
#     _rule_rank_tables 가 tbdaabt08 에 +20 을 얹었고, 지표 컨텍스트에도 전표
#     매출 공식만 실렸다. 해외 이용금액은 카드 월실적(tmdaa3e16)의
#     금월해외일시불이용금액 + 금월해외CA이용금액 이다.
#
#   "청구금액" → 금월이용합계금액
#     청구금액을 가진 테이블은 tmdaa3e16 하나인데 컬럼명이 "금월청구금액" 이라
#     질문 표면형과 맞지 않고 지표·용어도 없었다. 그래서 도메인 라우팅이
#     merchant_sales 로 빠지고 프롬프트에 청구 컬럼이 한 줄도 안 실려, 모델이
#     가장 가까운 이용금액 컬럼을 골랐다.
# ---------------------------------------------------------------------------
OVERSEAS_CARD_USAGE_METRIC = {
    "name": "카드월해외이용금액",
    "domain": "card_usage",
    "business_definition": "카드 월실적의 해외 이용금액(해외 일시불 + 해외 단기카드대출)",
    "expression": (
        'SUM(COALESCE(tmdaa3e16."금월해외일시불이용금액", 0) '
        '+ COALESCE(tmdaa3e16."금월해외CA이용금액", 0))'
    ),
    "source_table": "tmdaa3e16",
    "source_entity": "card_monthly_performance",
    "default_time_dimension": "기준년월",
    "result_grain": "기준년월 (질문이 요청한 분류축 조합별 1행)",
    "required_filters": ["tmdaa3e16.\"개인기업구분코드\" = '2'"],
    "semantic_cautions": [
        "해외 이용금액은 회원·카드가 해외에서 쓴 돈이다. tbdaabt08의 매출금액은 "
        "가맹점이 카드로 받은 해외 매출이라 다른 지표다.",
        "카드 월실적의 해외 축은 일시불과 CA 두 컬럼뿐이므로 둘을 더해 해외 이용금액을 만든다.",
        "기업·법인 단위 해외 이용금액은 tmdaa1d12의 금월해외이용금액을 쓴다.",
        "국가·MCC업종·가맹점처럼 카드 월실적에 없는 해외 전표 축으로 쪼개는 질문만 "
        "tbdaabt08의 해외 매출전표를 사용한다.",
    ],
    "synonyms": ["해외이용금액", "해외사용액", "해외이용액", "해외카드이용금액"],
    "unit": "원",
    "aggregation_behavior": "additive_at_declared_month_grain",
}

CARD_BILLING_METRIC = {
    "name": "카드월청구금액",
    "domain": "card_usage",
    "business_definition": "카드 월실적에서 금월기준 회원에게 청구된 매출 금액",
    "expression": 'SUM(COALESCE(tmdaa3e16."금월청구금액", 0))',
    "source_table": "tmdaa3e16",
    "source_entity": "card_monthly_performance",
    "default_time_dimension": "기준년월",
    "result_grain": "기준년월 (질문이 요청한 분류축 조합별 1행)",
    "required_filters": ["tmdaa3e16.\"개인기업구분코드\" = '2'"],
    "semantic_cautions": [
        "청구금액과 이용금액은 다른 컬럼이다. 청구금액은 금월청구금액, "
        "이용금액은 금월이용합계금액을 쓰고 서로 대체하지 않는다.",
        "일시불·CA·할부·해외·리볼빙 청구금액은 금월청구금액의 세부 축이므로 합계와 같이 더하지 않는다.",
        "미청구금액은 아직 청구되지 않은 잔액이라 청구금액이 아니다.",
    ],
    "synonyms": ["청구금액", "청구액", "월청구금액", "카드청구금액"],
    "unit": "원",
    "aggregation_behavior": "additive_at_declared_month_grain",
}

# 할부·CA 이용금액은 카드 월실적(tmdaa3e16)에만 있는 컬럼인데 canonical metric 이
# 하나도 없었다. 원천을 선언한 표지판이 없으니 라우팅은 "기업카드"·"법인카드" 라는
# 낱말에 걸린 유효 기업카드 보유 속성(+36)을 따라 기업·가맹점 스냅샷으로 가고,
# tmdaa3e16 은 컬럼 겹침 점수(15)로 밀렸다. 심하면 후보에도 못 든다.
#
#   '기업카드 할부 이용금액은?'  -> ['tbdaa1d12','tbdaaus01','tbdaabt30','tbdaabt08']
#
# 회원 쪽에서 tmdaa3e16 으로 가는 선언된 조인이 tbdaaat03(회원 마스터, 매일·
# 실적기준년월일)에서 시작하고 customer_card_portfolio 도메인의 default_fact_table
# 도 tbdaaat03 이라, 모델은 금액 컬럼이 아예 없는 회원 마스터부터 조인을 짜게 된다.
# 그 조인은 오늘 마스터에 남은 회원으로 모집단을 좁히는 필터일 뿐이다.
CARD_INSTALLMENT_USAGE_METRIC = {
    "name": "카드월할부이용금액",
    "domain": "card_usage",
    "business_definition": "카드 월실적에서 금월 할부(신용판매 할부)로 발생한 이용금액",
    "expression": 'SUM(COALESCE(tmdaa3e16."금월할부이용금액", 0))',
    "source_table": "tmdaa3e16",
    "source_entity": "card_monthly_performance",
    "default_time_dimension": "기준년월",
    "result_grain": "기준년월 (질문이 요청한 분류축 조합별 1행)",
    "required_filters": ["tmdaa3e16.\"개인기업구분코드\" = '2'"],
    "semantic_cautions": [
        "할부 이용금액 컬럼을 가진 테이블은 카드 월실적(tmdaa3e16) 하나다. "
        "기업 월 스냅샷(tmdaa1d12)에는 신용·체크 이용금액만 있고 할부 축이 없다.",
        "무이자·유이자·부분무이자·복합상환 할부 이용금액은 이 금액의 세부 축이므로 "
        "합계와 함께 더하지 않는다.",
        "CA할부이용금액은 단기카드대출(현금서비스) 할부라 신용판매 할부와 다른 지표다.",
        "회원·기업 속성이 필요 없으면 tmdaa3e16 단독으로 기준년월만 제한한다. "
        "회원 마스터(tbdaaat03)는 금액 컬럼이 없고 조인하면 모집단이 오늘 기준으로 좁아진다.",
    ],
    "synonyms": ["할부이용금액", "할부 이용금액", "할부이용액", "카드할부이용금액"],
    "unit": "원",
    "aggregation_behavior": "additive_at_declared_month_grain",
}

CARD_CASH_ADVANCE_USAGE_METRIC = {
    "name": "카드월CA이용금액",
    "domain": "card_usage",
    "business_definition": "카드 월실적에서 금월 단기카드대출(현금서비스)로 발생한 이용금액",
    "expression": 'SUM(COALESCE(tmdaa3e16."금월CA이용금액", 0))',
    "source_table": "tmdaa3e16",
    "source_entity": "card_monthly_performance",
    "default_time_dimension": "기준년월",
    "result_grain": "기준년월 (질문이 요청한 분류축 조합별 1행)",
    "required_filters": ["tmdaa3e16.\"개인기업구분코드\" = '2'"],
    "semantic_cautions": [
        "CA는 단기카드대출(현금서비스)이다. CA 이용금액 컬럼을 가진 테이블은 "
        "카드 월실적(tmdaa3e16) 하나다.",
        "해외CA·리볼빙CA·CA할부 이용금액은 이 금액의 세부 축이므로 합계와 함께 더하지 않는다.",
        "회원·기업 속성이 필요 없으면 tmdaa3e16 단독으로 기준년월만 제한한다. "
        "회원 마스터(tbdaaat03)는 금액 컬럼이 없고 조인하면 모집단이 오늘 기준으로 좁아진다.",
    ],
    "synonyms": ["CA이용금액", "CA 이용금액", "현금서비스이용금액", "단기카드대출이용금액"],
    "unit": "원",
    "aggregation_behavior": "additive_at_declared_month_grain",
}

BILLING_GLOSSARY_TERM = {
    "term": "청구금액",
    "canonical": "tmdaa3e16.금월청구금액 (금월기준 회원에게 청구된 매출 금액)",
    "description": (
        "회원에게 청구한 금액이다. 회원이 카드로 쓴 이용금액과 다른 컬럼이며, 청구금액 컬럼을 "
        "가진 테이블은 카드 월실적(tmdaa3e16) 하나다. 아직 청구되지 않은 미청구금액과도 구분한다."
    ),
    "aliases": ["청구액", "월청구금액", "카드청구금액", "청구된 금액"],
    "sql_hint": (
        "청구금액을 물으면 tmdaa3e16의 금월청구금액을 쓰고, 세부는 금월일시불청구금액·"
        "금월CA청구금액·금월할부청구금액·금월해외일시불청구금액·금월해외CA청구금액·"
        "금월리볼빙일시불청구금액을 쓴다. 이용금액 컬럼(금월이용합계금액)으로 대체하지 않고, "
        "미청구금액(일시불미청구금액 등)은 청구되지 않은 잔액이라 다른 지표다."
    ),
}

# 10번에서 넣은 이용금액 용어의 sql_hint 에 해외·청구 갈림길을 덧붙인다.
USAGE_GLOSSARY_HINT_SUFFIX = (
    " 해외 이용금액은 카드 월은 tmdaa3e16의 금월해외일시불이용금액+금월해외CA이용금액, "
    "기업·법인 월은 tmdaa1d12의 금월해외이용금액이며 tbdaabt08의 매출금액은 가맹점이 받은 "
    "해외 매출이다. 국가·MCC업종처럼 카드 월실적에 없는 해외 전표 축이 필요할 때만 tbdaabt08을 "
    "쓴다. 청구금액은 이용금액이 아니라 tmdaa3e16의 금월청구금액이다."
)
# 전표 매출 지표에서 떼어낼 이용금액 동의어.
OVERSEAS_SALES_WRONG_SYNONYMS = ("해외이용금액", "해외사용액")
OVERSEAS_SALES_CAUTION = (
    "해외 이용금액을 물으면 tmdaa3e16의 카드월해외이용금액을 쓴다. "
    "이 지표는 가맹점이 카드로 받은 해외 매출이다."
)
BILLING_DOMAIN_KEYWORDS = ("청구금액", "청구")
# _rule_rank_tables 는 테이블 이름·synonyms 가 질문에 잡히면 +14 를 준다.
# "해외 이용금액" 은 tbdaabt30 의 table synonym "이용금액" 과 매출금액 컬럼 동의어에
# 걸려 국내 전표가 1순위였다. 카드 월실적에 해외 이용금액 표면형을 달아 뒤집는다.
OVERSEAS_USAGE_TABLE_SYNONYMS = ("해외이용금액", "해외이용액", "해외사용액")

# query_references 는 when_user_says 가 그대로 잡히면 primary_table 에 120점을 주는
# 가장 강한 라우팅 증거다. "해외 이용금액"·"해외 사용액" 은 국가·MCC 축을 묻는
# "해외 카드 이용금액을 국가별로" 와 표면형이 달라서, 이 참조가 그쪽까지 끌어가지 않는다.
OVERSEAS_USAGE_QUERY_REFERENCE = {
    "intent": "해외이용금액_조회",
    "domain": "card_usage",
    "when_user_says": [
        "해외 이용금액",
        "해외 사용액",
        "해외 이용액",
        "카드별 해외 이용금액",
    ],
    "primary_table": "tmdaa3e16",
    "join_tables": [],
    "join_rule": (
        "카드 월실적 한 테이블로 끝난다. 회원·카드 속성이 필요하면 "
        "회원일련번호+카드구분키번호로 tbdaaat05를 조인한다."
    ),
    "recommended_columns": {
        "기간필터": "\"기준년월\" = 'YYYYMM' 또는 BETWEEN 으로 기간 지정",
        "해외이용금액": (
            'SUM(COALESCE("금월해외일시불이용금액", 0) + COALESCE("금월해외CA이용금액", 0))'
        ),
        "기업영업스코프": "\"개인기업구분코드\" = '2'",
    },
    "rules": [
        "해외 이용금액은 카드 월실적의 해외 일시불 이용금액과 해외 CA 이용금액을 더한 값이다.",
        "tbdaabt08의 매출금액은 가맹점이 받은 해외 매출이라 이용금액 대신 쓰지 않는다. "
        "국가·MCC업종처럼 카드 월실적에 없는 축이 필요할 때만 해외 매출전표를 쓴다.",
        "기업·법인 단위 해외 이용금액은 tmdaa1d12의 금월해외이용금액을 쓴다.",
    ],
}


def apply_billing_and_overseas_usage_vocabulary(schema: dict) -> int:
    """Route 해외 이용금액 to 카드 월실적 and give 청구금액 its own metric."""
    metric_list = schema.get("canonical_metrics")
    if not isinstance(metric_list, list):
        raise SystemExit("canonical_metrics 없음")
    metrics = {str(item.get("name") or ""): item for item in metric_list}

    overseas_sales = metrics.get("해외매출금액")
    if overseas_sales is None:
        raise SystemExit("canonical metric 해외매출금액 없음")
    synonyms = overseas_sales.get("synonyms") or []
    for synonym in OVERSEAS_SALES_WRONG_SYNONYMS:
        if synonym not in synonyms:
            raise SystemExit(f"해외매출금액 에 {synonym} 동의어가 없다. 이미 분리됐는지 확인 필요")
        synonyms.remove(synonym)
    cautions = overseas_sales.setdefault("semantic_cautions", [])
    if OVERSEAS_SALES_CAUTION not in cautions:
        cautions.append(OVERSEAS_SALES_CAUTION)

    # 카드 월 이용금액 지표 옆에 놓는다. 같은 원천·같은 grain 이라 함께 읽힌다.
    anchor_name = "카드별월이용금액"
    if anchor_name not in metrics:
        raise SystemExit(f"기준 지표 없음: {anchor_name}")
    for new_metric in (
        OVERSEAS_CARD_USAGE_METRIC,
        CARD_BILLING_METRIC,
        CARD_INSTALLMENT_USAGE_METRIC,
        CARD_CASH_ADVANCE_USAGE_METRIC,
    ):
        if new_metric["name"] in metrics:
            raise SystemExit(f"이미 있는 지표: {new_metric['name']}")
        source = str(new_metric.get("source_table") or "")
        declared = {
            str(column.get("name") or "")
            for table in schema.get("tables", [])
            if str(table.get("name") or "") == source
            for section in COLUMN_SECTIONS
            for column in table.get(section) or []
        }
        missing = [
            name
            for name in re.findall(r'"([^"]+)"', str(new_metric.get("expression") or ""))
            if name not in declared
        ]
        if missing:
            raise SystemExit(f"{source} 에 없는 컬럼: {', '.join(missing)}")
        anchor = metric_list.index(metrics[anchor_name]) + 1
        metric_list.insert(anchor, deepcopy(new_metric))
        metrics[new_metric["name"]] = new_metric

    glossary = schema.get("glossary")
    if not isinstance(glossary, list):
        raise SystemExit("glossary 없음")
    if any(item.get("term") == BILLING_GLOSSARY_TERM["term"] for item in glossary):
        raise SystemExit(f"이미 있는 용어: {BILLING_GLOSSARY_TERM['term']}")
    usage_term = next(
        (item for item in glossary if item.get("term") == USAGE_SALES_GLOSSARY_TERM["term"]),
        None,
    )
    if usage_term is None:
        raise SystemExit(f"용어 없음: {USAGE_SALES_GLOSSARY_TERM['term']}")
    usage_term["sql_hint"] = str(usage_term["sql_hint"]) + USAGE_GLOSSARY_HINT_SUFFIX
    glossary.insert(glossary.index(usage_term) + 1, deepcopy(BILLING_GLOSSARY_TERM))

    tables = {str(item.get("name") or ""): item for item in schema.get("tables", [])}
    card_monthly = tables.get("tmdaa3e16")
    if card_monthly is None:
        raise SystemExit("테이블 없음: tmdaa3e16")
    synonyms = card_monthly.setdefault("synonyms", [])
    for synonym in OVERSEAS_USAGE_TABLE_SYNONYMS:
        if synonym not in synonyms:
            synonyms.append(synonym)

    references = schema.get("query_references")
    if not isinstance(references, list):
        raise SystemExit("query_references 없음")
    if any(
        item.get("intent") == OVERSEAS_USAGE_QUERY_REFERENCE["intent"]
        for item in references
    ):
        raise SystemExit(f"이미 있는 참조 질의: {OVERSEAS_USAGE_QUERY_REFERENCE['intent']}")
    references.append(deepcopy(OVERSEAS_USAGE_QUERY_REFERENCE))

    # 도메인 라우팅에 청구 단서가 없어서 "청구금액" 질문이 merchant_sales 로 갔다.
    domains = {str(item.get("name") or ""): item for item in schema.get("canonical_domains", [])}
    card_usage = domains.get("card_usage")
    if card_usage is None:
        raise SystemExit("도메인 없음: card_usage")
    for keyword in BILLING_DOMAIN_KEYWORDS:
        if keyword not in card_usage["keywords"]:
            card_usage["keywords"].append(keyword)
    card_usage["preferred_metrics"].extend(
        [OVERSEAS_CARD_USAGE_METRIC["name"], CARD_BILLING_METRIC["name"]]
    )
    # "카드별 청구금액" 은 카드 축 때문에 customer_card_portfolio 로도 갈린다.
    portfolio = domains.get("customer_card_portfolio")
    if portfolio is None:
        raise SystemExit("도메인 없음: customer_card_portfolio")
    portfolio["preferred_metrics"].append(CARD_BILLING_METRIC["name"])

    return (
        len(OVERSEAS_SALES_WRONG_SYNONYMS)
        + 1  # 해외매출금액 주의사항
        + 2  # 신설 지표
        + 2  # 용어 신설과 sql_hint 보강
        + len(BILLING_DOMAIN_KEYWORDS)
        + len(OVERSEAS_USAGE_TABLE_SYNONYMS)
        + 1  # 해외 이용금액 참조 질의
        + 3  # preferred_metrics 3건
    )


# ---------------------------------------------------------------------------
# 10-3. 가맹점 신용판매 매출금액은 이번 달과 지난 달의 원천이 다르다
#
# 금년·전년가맹점신용판매매출금액은 가맹점기본(tbdaadt01, 일 적재)·가맹점월실적
# (tmdaa5e11)·가맹점월스냅샷(tmdaa5d01) 세 곳에 같은 이름으로 있다. 월 실적은 달이
# 닫힌 뒤 적재되므로 이번 달 값은 일 적재 마스터에만 있고, 지난 달 이전은 월 실적이
# 원천이다(사용자 제공).
#
# v1 에는 이 갈림길이 없었다. 컬럼명이 "금년…" 으로 시작해서 "가맹점 신용판매
# 매출금액" 이라는 표면형과 맞지 않아 세 테이블 모두 단서를 못 받고, 대신 매출금액
# 컬럼을 가진 전표 테이블(tbdaabt30·tbdaabt08)이 1순위로 올라왔다. tbdaadt01 을
# 골라도 기간이 붙으면 적재 정책이 tmdaa5d01(월말 스냅샷)로 돌려 버렸다.
# ---------------------------------------------------------------------------
MERCHANT_CREDIT_SALES_ATTRIBUTE = {
    "name": "merchant_credit_sales",
    "korean_name": "가맹점 신용판매 매출금액",
    "domains": ["merchant_sales", "corporate_sales_targeting"],
    "business_definition": (
        "가맹점의 신용판매 매출금액(금년·전년 누적). 이번 달은 일 적재 가맹점기본, "
        "지난 달 이전은 가맹점월실적에서 읽는다"
    ),
    "aliases": [
        "가맹점 신용판매 매출금액",
        "가맹점신용판매매출금액",
        "신용판매 매출금액",
        "신용판매매출금액",
        "신용판매 매출",
        "신용판매매출",
        "가맹점 신용판매 실적",
        "신용판매 실적",
    ],
    "source_mappings": [
        {
            "entity": "merchant",
            "table": "tbdaadt01",
            "columns": ["금년가맹점신용판매매출금액", "전년가맹점신용판매매출금액"],
            "role": "current_merchant_credit_sales",
        },
        {
            "entity": "merchant_monthly_performance",
            "table": "tmdaa5e11",
            "columns": ["금년가맹점신용판매매출금액", "전년가맹점신용판매매출금액"],
            "role": "monthly_merchant_credit_sales",
        },
    ],
    "source_selection": {
        "default_role_prefix": "current_",
        "period_role_prefix": "monthly_",
        "current_terms": ["이번 달", "이번달", "당월", "현재", "최신", "지금", "오늘"],
        # 요청 월이 실행일이 속한 달이면 월 실적에 아직 그 달 행이 없다.
        "open_month_uses_current": True,
    },
    "semantic_cautions": [
        "월 실적은 달이 닫힌 뒤 적재된다. 이번 달 신용판매 매출금액은 tbdaadt01의 "
        "실적기준년월일 최신행에서 읽는다.",
        "지난 달 이전은 tmdaa5e11의 기준년월 행을 쓰고 마스터로 대체하지 않는다.",
        "가맹점월스냅샷(tmdaa5d01)에도 같은 컬럼이 있지만 실적 축의 과거 원천은 tmdaa5e11이다.",
        "금년·전년가맹점신용판매매출금액은 기준 시점까지의 연 누적이다. 한 달치 매출은 "
        "tmdaa5e11의 가맹점일시불매출금액+가맹점할부매출금액을 쓴다.",
    ],
}


def apply_merchant_credit_sales_semantics(schema: dict) -> int:
    """Split 가맹점 신용판매 매출금액 into the open month and closed months."""
    attributes = schema.get("semantic_attributes")
    if not isinstance(attributes, list):
        raise SystemExit("semantic_attributes 없음")
    if any(
        item.get("name") == MERCHANT_CREDIT_SALES_ATTRIBUTE["name"] for item in attributes
    ):
        raise SystemExit(f"이미 있는 속성: {MERCHANT_CREDIT_SALES_ATTRIBUTE['name']}")

    tables = {str(item.get("name") or ""): item for item in schema.get("tables", [])}
    for mapping in MERCHANT_CREDIT_SALES_ATTRIBUTE["source_mappings"]:
        table = tables.get(str(mapping["table"]))
        if table is None:
            raise SystemExit(f"테이블 없음: {mapping['table']}")
        declared = {
            str(column.get("name") or "")
            for section in COLUMN_SECTIONS
            for column in table.get(section) or []
        }
        missing = [name for name in mapping["columns"] if name not in declared]
        if missing:
            raise SystemExit(f"{mapping['table']} 에 없는 컬럼: {', '.join(missing)}")

    # 가맹점 주소 속성 옆에 둔다. 둘 다 가맹점 마스터를 원천으로 가리키는 속성이다.
    anchor = next(
        (
            position
            for position, item in enumerate(attributes)
            if item.get("name") == MERCHANT_ADDRESS_ATTRIBUTE["name"]
        ),
        len(attributes) - 1,
    )
    attributes.insert(anchor + 1, deepcopy(MERCHANT_CREDIT_SALES_ATTRIBUTE))
    return 1


# ---------------------------------------------------------------------------
# 10-4. 가맹점 월매출 지표의 빠진 표면형
#
# 가맹점월매출건수는 동의어에 '가맹점매출건수' 를 갖고 있는데 가맹점월매출금액은
# '가맹점매출금액' 을 갖고 있지 않다(v1 원본의 비대칭). 매처는 어절 경계를 지켜서
# '가맹점매출' 로는 "가맹점매출금액" 을 잡지 못한다 — 뒤에 '금액' 이 붙어 있다.
#
# 그동안은 named_merchant_monthly_sales 계약이 이름 없는 질문에도 tmdaa5e11 에
# +28 을 줘서 이 구멍이 가려져 있었다. 그 계약은 가맹점명이 required 라서 이름 없는
# 질문의 근거가 될 수 없고(그 보너스를 빼면 "2026년 상반기 월별 가맹점매출금액" 이
# 원천을 못 찾는다), 지표 동의어로 제대로 세우는 게 맞다.
# ---------------------------------------------------------------------------
MERCHANT_MONTHLY_SALES_METRIC = "가맹점월매출금액"
MERCHANT_MONTHLY_SALES_SYNONYMS = ("가맹점매출금액", "가맹점 매출 금액", "월가맹점매출금액")


def apply_merchant_monthly_sales_surface_forms(schema: dict) -> int:
    """Give 가맹점월매출금액 the same surface forms its 건수 sibling already has."""
    metrics = {
        str(item.get("name") or ""): item for item in schema.get("canonical_metrics") or []
    }
    metric = metrics.get(MERCHANT_MONTHLY_SALES_METRIC)
    if metric is None:
        raise SystemExit(f"canonical metric 없음: {MERCHANT_MONTHLY_SALES_METRIC}")
    synonyms = metric.setdefault("synonyms", [])
    added = 0
    for synonym in MERCHANT_MONTHLY_SALES_SYNONYMS:
        if synonym in synonyms:
            continue
        synonyms.append(synonym)
        added += 1
    if not added:
        raise SystemExit(f"{MERCHANT_MONTHLY_SALES_METRIC} 에 이미 표면형이 다 있다")
    return added


# ---------------------------------------------------------------------------
# 10-5. 앱구분코드의 빠진 표면형
#
# 질문은 이 컬럼을 "앱별로" 라고만 부른다(goldenset v3 의 cs-golden-v3-0007·0021).
# 선언된 동의어는 '앱구분' 하나뿐이고, 매처는 어절 경계를 지켜서 '앱구분' 으로는
# "앱별로" 를 잡지 못한다 — '앱' 다음에 오는 '별' 이 어절 경계가 아니다. 컬럼명에서
# 표면형을 유도하는 쪽도 접미사를 벗기면 어간이 한 글자여서 최소 길이에 걸려
# 버려진다. 그래서 두 질문 모두 앱구분코드가 프롬프트 컬럼 예산에서 잘려 나갔다.
#
# '앱' 은 한 글자지만 기준년월에 달려 있던 '월' 과 다르다. '월' 은 기간을 말하는
# 질문마다 나오는 단위라서 기준년월을 가진 테이블 11개 전부에 단서 점수를 줬는데,
# '앱' 은 앱결제만 가리키는 낱말이고 이 컬럼을 가진 테이블은 tbdaabt30 하나뿐이다.
# 그래서 한 글자 동의어 금지의 예외로 이 한 쌍만 열어 둔다.
# ---------------------------------------------------------------------------
APP_SEGMENT_COLUMN = ("tbdaabt30", "앱구분코드")
APP_SEGMENT_SYNONYMS = ("앱",)

# 한 글자 동의어 금지(_MIN_DECLARED_SYNONYM_LENGTH)의 예외 목록. 테스트가 이걸
# 그대로 읽어서, 예외가 여기 적힌 것뿐인지 확인한다.
SINGLE_CHARACTER_SYNONYM_EXCEPTIONS: frozenset[tuple[str, str, str]] = frozenset(
    (*APP_SEGMENT_COLUMN, synonym)
    for synonym in APP_SEGMENT_SYNONYMS
    if len(compact(synonym)) < _MIN_DECLARED_SYNONYM_LENGTH
)


def apply_app_segment_surface_forms(schema: dict) -> int:
    """Let 앱구분코드 answer questions that say only "앱별로"."""
    table_name, column_name = APP_SEGMENT_COLUMN
    table = next(
        (item for item in schema.get("tables") or [] if item.get("name") == table_name),
        None,
    )
    if table is None:
        raise SystemExit(f"테이블 없음: {table_name}")
    column = _table_column(table, column_name)
    if column is None:
        raise SystemExit(f"{table_name} 에 {column_name} 컬럼이 없다")
    synonyms = column.setdefault("synonyms", [])
    added = 0
    for synonym in APP_SEGMENT_SYNONYMS:
        if synonym in synonyms:
            continue
        synonyms.append(synonym)
        added += 1
    if not added:
        raise SystemExit(f"{column_name} 에 이미 표면형이 다 있다")
    return added


# ---------------------------------------------------------------------------
# 10-6. 법인카드 이용금액은 카드 월실적에서, 이용 기업수는 전표에서 센다
#
# 카드브랜드 속성이 그 코드를 가진 테이블 8곳에 똑같이 +36 을 얹는다. 그 뒤로는
# 남은 몇 점이 순서를 정하는데, 두 질문 모두 엉뚱한 쪽에 그 점수가 붙어 있었다.
#
#   "법인카드 이용금액을 카드브랜드별로 알려줘"  -> tbdaabt30(전표) 1순위
#     전표는 테이블 동의어 "이용금액"(+14)과 매출금액 컬럼의 동의어 "이용금액"(+3)을
#     들고 있다. 정작 이용금액을 가진 카드 월실적(tmdaa3e16)은 컬럼명이
#     "금월이용합계금액" 이라 표면형이 어긋났고, 그 원천을 선언한 법인카드월이용금액
#     지표의 동의어도 '법인카드 이용금액 합계' 처럼 사람이 쓰지 않는 꼴이라
#     "법인카드 이용금액" 을 못 잡았다. 전표의 매출금액은 취소·정정을 순액으로 풀어야
#     하는 값이고(10-1), 카드가 쓴 돈은 월실적의 금월이용합계금액이다(10번 용어).
#
#   "법인카드 이용 기업수를 카드브랜드별로 알려줘"  -> tbdaaat05(회원카드기본) 1순위
#     이용한 기업을 셀 수 있는 곳은 기업고객식별자를 든 전표뿐인데, 8곳이 +36 으로
#     묶이자 선언 순서가 앞선 회원카드기본이 이기고 전표는 후보 4칸에도 못 들었다.
#     "기업수" 라는 낱말이 부르는 기업고객수 지표는 고객 마스터(tbdaaat01)를 가리켜,
#     카드를 쓴 기업이 아니라 등록만 된 기업까지 세게 만든다.
# ---------------------------------------------------------------------------
CORPORATE_CARD_USAGE_METRIC = "법인카드월이용금액"
CORPORATE_CARD_USAGE_SYNONYMS = (
    "법인카드 이용금액",
    "기업카드 이용금액",
    "법인카드 사용액",
    "기업카드 사용액",
)
# 이용금액이라는 낱말은 두 곳에 붙어 있었다. 전표(tbdaabt30)는 테이블 동의어로
# 들고 있었고(+14), 정작 그 뜻 그대로인 카드 월실적의 금월이용합계금액에는
# 붙어 있지 않았다. 10번이 갈라 놓은 낱말을 표면형에서도 갈라 놓는다.
#
# 컬럼 동의어(tbdaabt30.매출금액 = '이용금액')는 남긴다. 그 낱말로 전표를 1순위로
# 올릴 근거는 못 되지만, 업종·결제처처럼 전표에만 있는 축을 물을 때 후보 네 칸
# 안에 전표가 남으려면 그 점수가 필요하다(해외는 tbdaabt08 도 같다, 10-2).
CARD_MONTHLY_USAGE_COLUMN = ("tmdaa3e16", "금월이용합계금액")
SALES_SLIP_USAGE_SYNONYM = "이용금액"
SALES_SLIP_TABLE_SYNONYM_OWNER = "tbdaabt30"

CARD_USING_CORPORATE_COUNT_METRIC = {
    "name": "이용기업수",
    "domain": "card_usage",
    "business_definition": "해당 기간에 카드 이용이 실제로 발생한 고유 기업 수",
    "expression": 'COUNT(DISTINCT tbdaabt30."기업고객식별자")',
    "source_table": "tbdaabt30",
    "source_entity": "domestic_sales",
    "default_time_dimension": "기준년월",
    "result_grain": "기준년월 (질문이 요청한 분류축 조합별 1행)",
    "required_filters": ["tbdaabt30.\"개인기업구분코드\" = '2'"],
    "semantic_cautions": [
        "이용 기업수는 전표에 기업고객식별자가 찍힌 기업만 센다. "
        "고객 마스터(tbdaaat01)의 기업고객수는 카드를 쓰지 않은 기업까지 센다.",
        "회원일련번호·카드구분키번호로 세면 한 기업의 부서·카드가 여러 건으로 갈라진다.",
        "가맹점업종·MCC·결제처·카드브랜드처럼 전표가 들고 있는 축은 이 지표로 함께 쪼갠다.",
        "카드 상품·등급처럼 전표에 없는 카드 속성으로 쪼개는 질문은 "
        "tmdaa3e16의 고객식별자를 DISTINCT 집계한다.",
    ],
    "synonyms": ["카드이용기업수", "이용한 기업수", "카드를 이용한 기업수"],
    "unit": "개",
    "aggregation_behavior": "distinct_count",
}

CARD_USAGE_COUNT_GLOSSARY_TERM = {
    "term": "이용 기업수",
    "canonical": 'COUNT(DISTINCT tbdaabt30."기업고객식별자") (전표에 이용이 찍힌 기업)',
    "description": (
        "카드를 실제로 쓴 기업의 수다. 등록된 기업을 세는 기업고객수와 다르고, "
        "부서·사업장 단위인 회원 수와도 다르다. 전표(tbdaabt30)는 기업고객식별자를, "
        "카드 월실적(tmdaa3e16)은 고객식별자를 같은 뜻으로 들고 있다."
    ),
    "aliases": ["이용기업수", "카드이용기업수", "이용한 기업수", "이용 업체수"],
    "sql_hint": (
        "이용 기업수는 COUNT(DISTINCT \"기업고객식별자\")로 세고 개인기업구분코드 = '2' 를 "
        "함께 적용한다. 업종·결제처·카드브랜드 축은 tbdaabt30, 카드 상품·등급 축은 "
        "tmdaa3e16의 고객식별자를 쓴다. 회원일련번호로 세지 않는다."
    ),
}


def apply_card_usage_amount_and_corporate_count(schema: dict) -> int:
    """Split 카드 이용금액(월실적) from 이용 기업수(전표 기업고객식별자)."""
    metric_list = schema.get("canonical_metrics")
    if not isinstance(metric_list, list):
        raise SystemExit("canonical_metrics 없음")
    metrics = {str(item.get("name") or ""): item for item in metric_list}

    usage = metrics.get(CORPORATE_CARD_USAGE_METRIC)
    if usage is None:
        raise SystemExit(f"canonical metric 없음: {CORPORATE_CARD_USAGE_METRIC}")
    synonyms = usage.setdefault("synonyms", [])
    added = 0
    for synonym in CORPORATE_CARD_USAGE_SYNONYMS:
        if synonym in synonyms:
            continue
        synonyms.append(synonym)
        added += 1
    if not added:
        raise SystemExit(f"{CORPORATE_CARD_USAGE_METRIC} 에 이미 표면형이 다 있다")

    tables = {str(item.get("name") or ""): item for item in schema.get("tables", [])}
    table_name, column_name = CARD_MONTHLY_USAGE_COLUMN
    card_monthly = tables.get(table_name)
    if card_monthly is None:
        raise SystemExit(f"테이블 없음: {table_name}")
    column = _table_column(card_monthly, column_name)
    if column is None:
        raise SystemExit(f"{table_name} 에 {column_name} 컬럼이 없다")
    column_synonyms = column.setdefault("synonyms", [])
    if SALES_SLIP_USAGE_SYNONYM in column_synonyms:
        raise SystemExit(f"{column_name} 에 이미 {SALES_SLIP_USAGE_SYNONYM} 표면형이 있다")
    column_synonyms.append(SALES_SLIP_USAGE_SYNONYM)

    slip_table = tables.get(SALES_SLIP_TABLE_SYNONYM_OWNER)
    if slip_table is None:
        raise SystemExit(f"테이블 없음: {SALES_SLIP_TABLE_SYNONYM_OWNER}")
    table_synonyms = slip_table.get("synonyms") or []
    if SALES_SLIP_USAGE_SYNONYM not in table_synonyms:
        raise SystemExit(
            f"{SALES_SLIP_TABLE_SYNONYM_OWNER} 테이블 동의어에 {SALES_SLIP_USAGE_SYNONYM} 이 없다. "
            "이미 분리됐는지 확인 필요"
        )
    table_synonyms.remove(SALES_SLIP_USAGE_SYNONYM)
    moved = 2

    new_metric = CARD_USING_CORPORATE_COUNT_METRIC
    if new_metric["name"] in metrics:
        raise SystemExit(f"이미 있는 지표: {new_metric['name']}")
    source = str(new_metric["source_table"])
    declared = {
        str(column.get("name") or "")
        for table in schema.get("tables", [])
        if str(table.get("name") or "") == source
        for section in COLUMN_SECTIONS
        for column in table.get(section) or []
    }
    missing = [
        name
        for name in re.findall(r'"([^"]+)"', str(new_metric["expression"]))
        if name not in declared
    ]
    if missing:
        raise SystemExit(f"{source} 에 없는 컬럼: {', '.join(missing)}")
    # 전표 매출 지표 옆에 놓는다. 같은 원천이라 함께 읽힌다.
    anchor_name = "매출건수"
    if anchor_name not in metrics:
        raise SystemExit(f"기준 지표 없음: {anchor_name}")
    metric_list.insert(metric_list.index(metrics[anchor_name]) + 1, deepcopy(new_metric))

    glossary = schema.get("glossary")
    if not isinstance(glossary, list):
        raise SystemExit("glossary 없음")
    if any(item.get("term") == CARD_USAGE_COUNT_GLOSSARY_TERM["term"] for item in glossary):
        raise SystemExit(f"이미 있는 용어: {CARD_USAGE_COUNT_GLOSSARY_TERM['term']}")
    # 기업고객 용어 바로 뒤에 놓는다. 등록된 기업과 쓴 기업의 차이가 붙어 읽혀야 한다.
    anchor = next(
        (
            position
            for position, item in enumerate(glossary)
            if item.get("term") == "기업고객"
        ),
        len(glossary) - 1,
    )
    glossary.insert(anchor + 1, deepcopy(CARD_USAGE_COUNT_GLOSSARY_TERM))

    domains = {str(item.get("name") or ""): item for item in schema.get("canonical_domains", [])}
    card_usage = domains.get("card_usage")
    if card_usage is None:
        raise SystemExit("도메인 없음: card_usage")
    card_usage["preferred_metrics"].append(new_metric["name"])

    return added + moved + 3  # 지표 신설, 용어 신설, preferred_metrics


# ---------------------------------------------------------------------------
# 10-7. 가맹점 우편번호는 전표 사본이 아니라 가맹점 마스터에서 읽는다
#
# "한신포차 가맹점 우편번호 알려줘" 가 매출전표(tbdaabt30) 1순위였다. 전표가 그 값을
# "가맹점우편번호" 라는 이름으로 들고 있어서, 질문이 컬럼명을 그대로 불렀다는 가점
# (+40)이 전표에 붙었다. 가맹점 마스터는 이미 가맹점 테이블이라 같은 값을 접두사
# 없이 "우편번호" 로 들고 있고, 그래서 같은 낱말을 놓쳤다.
#
# 주소 속성(merchant_address)도 우편번호를 원천 컬럼으로 선언해 두었지만 별칭이
# 주소 표현뿐이라 우편번호 질문에는 걸리지 않았다. 별칭이 걸려야 마스터 한 곳만
# 가리키는 속성이라는 사실이 라우팅과 "없는 달은 최신 기준" 안내까지 이어진다.
# ---------------------------------------------------------------------------
MERCHANT_ADDRESS_ATTRIBUTE_NAME = "merchant_address"
MERCHANT_POSTAL_ALIASES = (
    "가맹점 우편번호",
    "가맹점우편번호",
    "가맹점 도로명 우편번호",
    "가맹점도로명우편번호",
    "사업장 우편번호",
)
# 고객 자택·직장 우편번호까지 끌어오지 않도록 "우편번호" 한 낱말은 별칭에 넣지
# 않는다. 가맹점을 말한 질문만 이 속성으로 온다.
MERCHANT_POSTAL_CAUTION = (
    "전표(tbdaabt30)의 가맹점우편번호는 전표 시점의 사본이다. 가맹점 우편번호는 "
    "가맹점 마스터(tbdaadt01)의 우편번호를 쓴다."
)
# 컬럼 동의어로는 풀 수 없다. 이 스키마는 "이름이 같은 컬럼은 어느 테이블에 있어도
# 같은 동의어 집합" 을 지키는데(test_same_named_columns_share_synonyms), 부점
# (tbdaacb02)의 우편번호까지 가맹점 표면형을 갖게 되기 때문이다. 대신 원천을 한 곳만
# 선언한 속성에 라우팅 가점을 주는 쪽으로 workflow 에서 처리한다.


def apply_merchant_postal_code_surface_forms(schema: dict) -> int:
    """Answer 가맹점 우편번호 from the merchant master, not the slip copy."""
    attribute = next(
        (
            item
            for item in schema.get("semantic_attributes") or []
            if item.get("name") == MERCHANT_ADDRESS_ATTRIBUTE_NAME
        ),
        None,
    )
    if attribute is None:
        raise SystemExit(f"semantic attribute 없음: {MERCHANT_ADDRESS_ATTRIBUTE_NAME}")
    declared = {
        str(name)
        for mapping in attribute.get("source_mappings") or []
        for name in mapping.get("columns") or []
    }
    if "우편번호" not in declared:
        raise SystemExit(f"{MERCHANT_ADDRESS_ATTRIBUTE_NAME} 가 우편번호를 원천 컬럼으로 선언하지 않았다")

    aliases = attribute.setdefault("aliases", [])
    added = 0
    for alias in MERCHANT_POSTAL_ALIASES:
        if alias in aliases:
            continue
        aliases.append(alias)
        added += 1
    if not added:
        raise SystemExit(f"{MERCHANT_ADDRESS_ATTRIBUTE_NAME} 에 이미 우편번호 별칭이 다 있다")
    cautions = attribute.setdefault("semantic_cautions", [])
    if MERCHANT_POSTAL_CAUTION not in cautions:
        cautions.append(MERCHANT_POSTAL_CAUTION)
        added += 1

    return added


# ---------------------------------------------------------------------------
# 10-8. 지표를 실제로 든 원천으로 보내고, 기업 집계의 코드 필터를 못 박는다
#
# 사용자가 잡아 준 다섯 자리다. 공통 원인은 하나다 — 질문이 부르는 지표·축이 어느
# 테이블에 있는지 선언되어 있지 않으면, 낱말 하나로 걸린 semantic attribute(+36)나
# 토큰 두 개짜리 query_reference(+12)가 그 값을 실제로 든 테이블을 이긴다.
#
# 1) "현재 기준 기업카드를 카드등급그룹별로 몇 좌인지" 가 회원카드기본(tbdaaat05)과
#    기업고객 스냅샷(tbdaa1d12)을 함께 썼다. 카드등급그룹은 스냅샷에 없고, 스냅샷의
#    유효기업신용카드수는 기업고객당 이미 더해 놓은 수라 카드 한 장의 속성으로 쪼갤
#    수 없다. 카드 한 장이 한 행인 회원카드기본 한 곳으로 끝나는 질문인데, 유효
#    기업카드 보유 속성이 스냅샷 네 곳을 +36 으로 올리고 회원카드기본은 카드등급그룹
#    컬럼 겹침 3점뿐이어서 3순위였다. 축을 갖지 못한 집계 원천은 1순위가 아니라
#    후보에서 빠져야 한다.
#
# 2) "해외가맹점매출건수" 가 가맹점 월스냅샷(tmdaa5d01)·해외매출전표(tbdaabt08)로
#    갔다. 그 컬럼을 든 테이블은 가맹점 월실적(tmdaa5e11) 하나뿐인데 지표 선언이
#    없어서, 토큰 두 개("매출"·"조회")로 걸린 월별_카드매출_조회 참조가 전표와 월
#    스냅샷을 후보에 끼워 넣었다.
#
# 3) 기업 유효카드 좌수·기업 매출건수를 셀 때 거는 코드 필터가 어디에도 선언되어
#    있지 않았다. 코드값 뜻만 코드북에 있어서 모델이 걸 때도 있고 안 걸 때도 있었다.
#
# 4) 가맹점 수수료 금액을 물으면 전표(tbdaabt30)의 전표당 가맹점수수료가 1순위였다.
#    질문이 컬럼명을 그대로 불렀다는 가점(+40)이 전표에 붙는데, 월 단위 수수료 수입은
#    tmdaa5e11 의 가맹점수입수수료다. 전표 쪽은 전표 한 장의 수수료라 월 실적을
#    대신하지 못한다.
#
# 5) "최근 3개월 평균 매출과 6개월 평균 매출 비교" 는 tmdaa5e11 한 행이 이미 들고 있는
#    최근3개월·최근6개월 컬럼으로 답한다. 여러 달을 GROUP BY 해서 직접 평균을 낼
#    질문이 아니고, 그 컬럼들은 기간 합계라 평균은 개월 수로 나눠야 한다.
# ---------------------------------------------------------------------------

# 10-8-1. 기업카드 좌수는 카드 한 장이 한 행인 회원카드기본에서 센다.
CARD_MASTER_TABLE = "tbdaaat05"
CARD_LEVEL_CARD_HOLDING_ROLE = "card_level_corporate_card_holding"
# 기업 명의 카드를 가리키는 카드소유자구분코드. 사용자 확인값이다(기업대표 포함).
CORPORATE_CARD_OWNER_CODES = ("3", "4", "5")
CORPORATE_CARD_OWNER_LABELS = "기업대표·기업개별·기업공용"
CORPORATE_CARD_OWNER_FILTER = (
    'tbdaaat05."카드소유자구분코드" IN (\'3\',\'4\',\'5\') — 기업대표·기업개별·기업공용'
)
# 유효 여부는 '1'/'0' 코드다. 골든셋 SQL 이 이미 이 규약으로 쓰여 있다.
CORPORATE_CARD_VALID_FILTER = (
    '(tbdaaat05."유효신용카드여부" = \'1\' OR tbdaaat05."유효체크카드여부" = \'1\')'
    ' — 유효 카드만 셀 때'
)
CARD_LEVEL_CARD_HOLDING_MAPPING = {
    "entity": "member_card",
    "table": CARD_MASTER_TABLE,
    "columns": ["카드소유자구분코드", "유효신용카드여부", "유효체크카드여부"],
    "role": CARD_LEVEL_CARD_HOLDING_ROLE,
}
CARD_LEVEL_CARD_HOLDING_CAUTION = (
    "카드등급그룹·상품·브랜드·발급유형·카드거래매체처럼 카드 한 장의 속성으로 쪼개는 "
    "질의는 회원카드기본(tbdaaat05) 한 곳에서 센다. 기업고객 스냅샷의 유효기업신용카드수는 "
    "기업고객당 합계라 그 축으로 쪼갤 수 없고, 조인해도 좌수가 부풀려진다."
)
CORPORATE_CARD_COUNT_METRIC = {
    "name": "기업유효카드좌수",
    "domain": "customer_card_portfolio",
    "business_definition": "기업 명의로 발급된 유효 카드의 좌수. 카드 한 장이 한 좌다.",
    "expression": 'COUNT(DISTINCT CONCAT(tbdaaat05."회원일련번호", \'-\', tbdaaat05."카드구분키번호"))',
    "source_table": CARD_MASTER_TABLE,
    "source_entity": "member_card",
    "default_time_dimension": "실적기준년월일",
    "result_grain": "질문이 요청한 카드 속성 축 조합별 1행",
    "required_filters": [
        CORPORATE_CARD_OWNER_FILTER,
        'tbdaaat05."개인기업구분코드" = \'2\'',
        CORPORATE_CARD_VALID_FILTER,
        'tbdaaat05."실적기준년월일" = 스냅샷 기준일 (현재 기준은 MAX)',
    ],
    "semantic_cautions": [
        CARD_LEVEL_CARD_HOLDING_CAUTION,
        "좌수는 카드 장수다. 기업 수를 물으면 COUNT(DISTINCT \"소유자고객식별자\"), "
        "회원 수를 물으면 COUNT(DISTINCT \"회원일련번호\") 로 센다.",
        "기업고객 스냅샷(tbdaa1d12)의 기업유효카드수와 같은 값을 다른 낟알로 센다. "
        "한 질문에서 두 원천을 함께 쓰지 않는다.",
    ],
    "synonyms": [
        "기업카드 좌수",
        "기업카드좌수",
        "유효 기업카드 좌수",
        "유효기업카드좌수",
        "법인카드 좌수",
        "법인카드좌수",
    ],
    "unit": "좌",
    "aggregation_behavior": "additive_at_declared_grain",
}

# 10-8-2. 해외 가맹점 매출은 가맹점 월실적이 컬럼으로 들고 있다.
MERCHANT_MONTHLY_TABLE = "tmdaa5e11"
# 테이블의 semantic_cautions 는 프롬프트에 렌더되지 않는다(_table_details). 지표
# 주의사항은 렌더되므로 여기에 붙인다.
OVERSEAS_MERCHANT_SALES_CAUTION = (
    "해외매출전표(tbdaabt08)는 전표 한 장 단위라 가맹점 월 실적을 대신하지 못하고, "
    "가맹점 월스냅샷(tmdaa5d01)은 이 컬럼을 갖고 있지 않다."
)
OVERSEAS_MERCHANT_SALES_METRICS = (
    {
        "name": "해외가맹점월매출건수",
        "domain": "merchant_sales",
        "business_definition": "월 해외 가맹점 매출 건수",
        "expression": 'SUM(COALESCE(tmdaa5e11."해외가맹점매출건수", 0))',
        "source_table": MERCHANT_MONTHLY_TABLE,
        "source_entity": "merchant_monthly_performance",
        "default_time_dimension": "기준년월",
        "required_filters": ['질문의 기준년월로 tmdaa5e11."기준년월" 제한'],
        "semantic_cautions": [OVERSEAS_MERCHANT_SALES_CAUTION],
        "synonyms": ["해외가맹점매출건수", "해외 가맹점 매출건수", "해외 가맹점 매출 건수"],
        "unit": "건",
        "aggregation_behavior": "additive_at_declared_month_grain",
    },
    {
        "name": "해외가맹점월매출금액",
        "domain": "merchant_sales",
        "business_definition": "월 해외 가맹점 매출 금액",
        "expression": 'SUM(COALESCE(tmdaa5e11."해외가맹점매출금액", 0))',
        "source_table": MERCHANT_MONTHLY_TABLE,
        "source_entity": "merchant_monthly_performance",
        "default_time_dimension": "기준년월",
        "required_filters": ['질문의 기준년월로 tmdaa5e11."기준년월" 제한'],
        "semantic_cautions": [OVERSEAS_MERCHANT_SALES_CAUTION],
        "synonyms": ["해외가맹점매출금액", "해외 가맹점 매출금액", "해외 가맹점 매출 금액"],
        "unit": "원",
        "aggregation_behavior": "additive_at_declared_month_grain",
    },
)

# 10-8-3. 전표 건수를 셀 때 거는 코드 필터. 사용자 확인값이다.
SLIP_COUNT_METRIC = "매출건수"
CARD_SALES_TYPE_CODES = ("1", "2", "4")
CARD_SALES_TYPE_LABELS = "일반·할부·리볼빙"
SLIP_COUNT_REQUIRED_FILTERS = (
    'tbdaabt30."카드매출유형구분코드" IN (\'1\',\'2\',\'4\') — ' + CARD_SALES_TYPE_LABELS,
    'tbdaabt30."회원소속회사구분코드" = \'1\' AND tbdaabt30."가맹점소속회사구분코드" = \'1\' — 당사',
)
SLIP_COUNT_FILTER_CAUTIONS = (
    "기업 매출건수는 카드매출유형구분코드 일반(1)·할부(2)·리볼빙(4)만 센다. "
    "현금서비스(3)·예수금(5)·선급급(6)·해외현금지급(7)·연회비(9)는 카드 매출이 아니다.",
    "회원소속회사구분코드·가맹점소속회사구분코드를 당사('1')로 제한한다. "
    "제휴사·공동망·해외회원 건은 당사 매출이 아니다.",
    "해외 전표(tbdaabt08)는 회원소속회사구분코드를 갖고 있지 않다. "
    "가맹점소속회사구분코드 = '1' 과 카드매출유형구분코드만 같은 규칙으로 적용한다.",
    # 사용자가 필터를 지정한 지표는 건수뿐이다. 금액 지표(카드매출금액)에는 넣지 않았다.
    # 그런데 한 쿼리가 금액과 건수를 함께 뽑으면 이 필터는 WHERE 절을 공유한다.
    "이 필터는 WHERE 절에 걸리므로 같은 쿼리의 매출금액에도 함께 적용된다. "
    "금액과 건수의 모집단을 다르게 두려면 건수만 CASE WHEN 안에서 세야 한다.",
)

# 10-8-4. 가맹점 수수료 금액은 월 실적의 수입수수료다. 수수료율·카드 수수료는 건드리지 않는다.
MERCHANT_FEE_METRIC = {
    "name": "가맹점월수입수수료",
    "domain": "merchant_sales",
    "business_definition": "가맹점에서 거둔 월 수입수수료 금액",
    "expression": 'SUM(COALESCE(tmdaa5e11."가맹점수입수수료", 0))',
    "source_table": MERCHANT_MONTHLY_TABLE,
    "source_entity": "merchant_monthly_performance",
    "default_time_dimension": "기준년월",
    "required_filters": ['질문의 기준년월로 tmdaa5e11."기준년월" 제한'],
    "semantic_cautions": [
        "가맹점 수수료 금액은 이 지표다. 전표(tbdaabt30)의 가맹점수수료는 전표 한 장의 "
        "수수료라 월 수수료 수입을 대신하지 못한다.",
        "수수료율(신용카드가맹점수수료율·체크카드수수료율 등)은 금액이 아니라 율이므로 "
        "이 지표로 바꾸지 않는다. 실효 수수료율은 이 지표를 매출금액으로 나눠 구한다.",
        "할부수수료·CA수수료·연체수수료는 카드 쪽 수수료로 원천이 다르다.",
        "최근 3·6·9개월·1년 수입수수료는 같은 행의 누적 컬럼으로 읽는다.",
    ],
    "synonyms": [
        "가맹점수입수수료",
        "가맹점 수입수수료",
        "가맹점 수수료 수입",
        "가맹점수수료수입",
        "가맹점 수수료 금액",
        "가맹점수수료금액",
    ],
    "unit": "원",
    "aggregation_behavior": "additive_at_declared_month_grain",
}
# 전표(tbdaabt30)는 "가맹점수수료" 를 컬럼명으로 그대로 들고 있어서, 질문이 컬럼명을
# 불렀다는 가점(+40)과 그 지표를 가진 유일한 테이블 가점(+40)을 함께 받는다. 지표
# 선언만으로는(+20) 이기지 못한다. 10-7 의 가맹점 우편번호와 같은 모양이므로 같은
# 방법을 쓴다 — 원천을 한 곳만 선언한 속성으로 "이 값은 거기서만 읽는다" 를 못 박고,
# 그 선언에 붙는 가점(_SINGLE_SOURCE_ATTRIBUTE_WEIGHT)으로 사본을 넘어선다.
MERCHANT_FEE_ATTRIBUTE = {
    "name": "merchant_fee_revenue",
    "korean_name": "가맹점 수수료 수입",
    "domains": ["merchant_sales"],
    "business_definition": (
        "가맹점에서 거둔 수수료 수입 금액. 가맹점 월실적(tmdaa5e11)의 가맹점수입수수료 "
        "한 곳에서만 읽는다"
    ),
    "aliases": [
        "가맹점 수수료",
        "가맹점수수료",
        "가맹점 수수료 금액",
        "가맹점수수료금액",
        "가맹점 수입수수료",
        "가맹점수입수수료",
        "가맹점 수수료 수입",
        "수수료 수입",
    ],
    "source_mappings": [
        {
            "entity": "merchant_monthly_performance",
            "table": "tmdaa5e11",
            "columns": [
                "가맹점수입수수료",
                "최근3개월가맹점수입수수료",
                "최근6개월가맹점수입수수료",
            ],
            "role": "monthly_merchant_fee_revenue",
        },
    ],
    "semantic_cautions": [
        "전표(tbdaabt30)의 가맹점수수료는 전표 한 장에 붙은 수수료다. 이름이 같아도 "
        "월 단위 수수료 수입을 대신하지 못한다.",
        "수수료율(신용카드가맹점수수료율·체크카드수수료율)은 금액이 아니라 율이다. "
        "실효 수수료율은 가맹점수입수수료를 매출금액으로 나눠 구한다.",
        "할부수수료·CA수수료·연체수수료는 카드 쪽 수수료로 원천이 다르다.",
    ],
}

# 10-8-5. "회원" 은 질문에서 모집단을 가리키는 말이지 테이블 이름이 아니다.
# 10-6 이 전표에서 "이용금액" 을 걷어낸 것과 같은 자리다. "기업 회원의 신용카드 할부
# 이용금액" 이 그 낱말 하나(테이블 식별 +14)로 회원 마스터를 후보 네 칸에 올렸는데,
# 그 테이블에는 금액 컬럼이 없어서 조인하면 모집단만 좁아진다. 회원기본·회원마스터·
# 회원정보는 그대로 남겨 테이블을 실제로 지목한 질문은 계속 잡는다.
MEMBER_MASTER_TABLE = "tbdaaat03"
MEMBER_MASTER_GENERIC_SYNONYM = "회원"


def apply_member_master_identity_scope(schema: dict) -> int:
    """Stop the bare word 회원 from identifying the member master table."""
    tables = {str(item.get("name") or ""): item for item in schema.get("tables", [])}
    table = tables.get(MEMBER_MASTER_TABLE)
    if table is None:
        raise SystemExit(f"테이블 없음: {MEMBER_MASTER_TABLE}")
    synonyms = table.get("synonyms") or []
    if MEMBER_MASTER_GENERIC_SYNONYM not in synonyms:
        raise SystemExit(
            f"{MEMBER_MASTER_TABLE} 테이블 동의어에 "
            f"{MEMBER_MASTER_GENERIC_SYNONYM} 이 없다. 이미 걷어냈는지 확인 필요"
        )
    synonyms.remove(MEMBER_MASTER_GENERIC_SYNONYM)
    if not synonyms:
        raise SystemExit(f"{MEMBER_MASTER_TABLE} 식별 동의어가 하나도 남지 않는다")
    return 1


# 10-8-6. 최근 N개월 컬럼은 그 기간의 합계다. 평균은 개월 수로 나눈다.
RECENT_PERIOD_MONTHS = (3, 6, 9)
RECENT_PERIOD_SALES_REFERENCE = {
    "intent": "가맹점_최근기간_평균매출_비교",
    "when_user_says": [
        "최근 3개월 평균 매출",
        "3개월 평균 매출과 6개월 평균 매출",
        "3개월 평균 매출과 6개월 평균 매출 비교",
        "최근 3개월과 6개월 매출 비교",
        "가맹점 최근 3개월 6개월 매출 비교",
    ],
    "primary_table": MERCHANT_MONTHLY_TABLE,
    "join_tables": [],
    "join_rule": (
        "조인하지 않는다. 최근 3·6·9개월·1년 실적은 가맹점 월실적(tmdaa5e11) 한 행이 "
        "누적 컬럼으로 이미 들고 있다."
    ),
    "recommended_columns": {
        "최근3개월 평균매출": 'COALESCE(tmdaa5e11."최근3개월가맹점매출금액", 0) / 3',
        "최근6개월 평균매출": 'COALESCE(tmdaa5e11."최근6개월가맹점매출금액", 0) / 6',
        "비교": "두 평균의 차이 또는 비율을 같은 행에서 계산한다",
    },
    "rules": [
        "최근N개월 컬럼은 그 기간의 합계다. 평균을 물으면 개월 수(3·6·9·12)로 나눈다.",
        "여러 달을 GROUP BY 해서 직접 평균을 내지 않는다. 그러면 조회 월 이전 데이터가 "
        "필요해 기준년월 필터와 어긋난다.",
        "기준년월은 질문이 말한 기준월 한 달로 제한한다. 누적 컬럼이 그 달 기준의 "
        "최근 N개월을 이미 담고 있다.",
    ],
}
RECENT_PERIOD_GLOSSARY_TERM = {
    "term": "최근 N개월 실적",
    "canonical": 'tmdaa5e11."최근N개월가맹점매출금액" (기간 합계)',
    "description": (
        "가맹점 월실적은 최근 3·6·9개월과 1년 누적을 컬럼으로 들고 있다. 기준년월 한 "
        "행에서 그 기간의 합계를 읽는 값이라, 평균을 물으면 개월 수로 나눈다. 매출금액· "
        "매출건수·수입수수료·해외 매출이 같은 모양으로 있다."
    ),
    "aliases": ["최근 3개월 매출", "최근 6개월 매출", "3개월 평균 매출", "6개월 평균 매출"],
    "sql_hint": (
        "최근 3개월 평균은 \"최근3개월가맹점매출금액\" / 3, 6개월 평균은 "
        "\"최근6개월가맹점매출금액\" / 6 이다. 월별 행을 모아 AVG 하지 않는다."
    ),
}


def _insert_metric(schema: dict, metric: dict, anchor_name: str) -> None:
    """지표를 anchor 지표 바로 뒤에 넣고, 원천에 컬럼이 있는지 확인한다."""
    metric_list = schema.get("canonical_metrics")
    if not isinstance(metric_list, list):
        raise SystemExit("canonical_metrics 없음")
    metrics = {str(item.get("name") or ""): item for item in metric_list}
    if metric["name"] in metrics:
        raise SystemExit(f"이미 있는 지표: {metric['name']}")
    source = str(metric["source_table"])
    declared = {
        str(column.get("name") or "")
        for table in schema.get("tables", [])
        if str(table.get("name") or "") == source
        for section in COLUMN_SECTIONS
        for column in table.get(section) or []
    }
    missing = [
        name
        for name in re.findall(r'"([^"]+)"', str(metric["expression"]))
        if name not in declared
    ]
    if missing:
        raise SystemExit(f"{source} 에 없는 컬럼: {', '.join(missing)}")
    if anchor_name not in metrics:
        raise SystemExit(f"기준 지표 없음: {anchor_name}")
    metric_list.insert(metric_list.index(metrics[anchor_name]) + 1, deepcopy(metric))

    domains = {
        str(item.get("name") or ""): item
        for item in schema.get("canonical_domains", [])
    }
    domain = domains.get(str(metric["domain"]))
    if domain is None:
        raise SystemExit(f"도메인 없음: {metric['domain']}")
    domain.setdefault("preferred_metrics", []).append(metric["name"])


def apply_card_level_corporate_card_count(schema: dict) -> int:
    """Count corporate card 좌수 in the card master when the axis is a card attribute."""
    attribute = next(
        (
            item
            for item in schema.get("semantic_attributes") or []
            if item.get("name") == "corporate_card_holding"
        ),
        None,
    )
    if attribute is None:
        raise SystemExit("corporate_card_holding 속성 없음")
    mappings = attribute.get("source_mappings")
    if not isinstance(mappings, list):
        raise SystemExit("corporate_card_holding source_mappings 없음")
    if any(str(item.get("table") or "") == CARD_MASTER_TABLE for item in mappings):
        raise SystemExit(f"{CARD_MASTER_TABLE} 원천이 이미 선언되어 있다")
    tables = {str(item.get("name") or ""): item for item in schema.get("tables", [])}
    card_master = tables.get(CARD_MASTER_TABLE)
    if card_master is None:
        raise SystemExit(f"테이블 없음: {CARD_MASTER_TABLE}")
    for name in CARD_LEVEL_CARD_HOLDING_MAPPING["columns"]:
        if _table_column(card_master, name) is None:
            raise SystemExit(f"{CARD_MASTER_TABLE} 에 {name} 컬럼이 없다")
    mappings.append(deepcopy(CARD_LEVEL_CARD_HOLDING_MAPPING))

    policy = attribute.get("source_selection")
    if not isinstance(policy, dict):
        raise SystemExit("corporate_card_holding source_selection 없음")
    # 시간축(현재/과거 월)과 나란한 세 번째 갈림길이다. 질문이 부른 축을 집계 원천이
    # 갖고 있지 않고 카드 단위 원천만 갖고 있으면 그 원천으로 간다.
    policy["axis_role_prefix"] = "card_level_"
    policy["axis_note"] = (
        "질문의 분류축이 집계 원천에 없고 회원카드기본에만 있으면 tbdaaat05 에서 카드 "
        "한 장씩 센다."
    )
    cautions = attribute.setdefault("semantic_cautions", [])
    if CARD_LEVEL_CARD_HOLDING_CAUTION not in cautions:
        cautions.append(CARD_LEVEL_CARD_HOLDING_CAUTION)

    _insert_metric(schema, CORPORATE_CARD_COUNT_METRIC, "유효신용카드수")
    return 3  # 원천 선언, 축 정책, 지표 신설


def apply_overseas_merchant_sales_metrics(schema: dict) -> int:
    """Read 해외 가맹점 매출 from the merchant monthly performance columns."""
    added = 0
    for metric in OVERSEAS_MERCHANT_SALES_METRICS:
        _insert_metric(schema, metric, "가맹점월매출건수")
        added += 1
    return added


def apply_corporate_slip_count_filters(schema: dict) -> int:
    """Declare the code filters that scope 기업 매출건수 to 당사 card sales."""
    metrics = {
        str(item.get("name") or ""): item
        for item in schema.get("canonical_metrics", [])
    }
    metric = metrics.get(SLIP_COUNT_METRIC)
    if metric is None:
        raise SystemExit(f"지표 없음: {SLIP_COUNT_METRIC}")
    tables = {str(item.get("name") or ""): item for item in schema.get("tables", [])}
    slip = tables.get("tbdaabt30")
    if slip is None:
        raise SystemExit("테이블 없음: tbdaabt30")
    for name in ("카드매출유형구분코드", "회원소속회사구분코드", "가맹점소속회사구분코드"):
        if _table_column(slip, name) is None:
            raise SystemExit(f"tbdaabt30 에 {name} 컬럼이 없다")

    required = metric.setdefault("required_filters", [])
    added = 0
    for value in SLIP_COUNT_REQUIRED_FILTERS:
        if value in required:
            continue
        required.append(value)
        added += 1
    if not added:
        raise SystemExit(f"{SLIP_COUNT_METRIC} 에 이미 코드 필터가 다 있다")
    cautions = metric.setdefault("semantic_cautions", [])
    for value in SLIP_COUNT_FILTER_CAUTIONS:
        if value in cautions:
            continue
        cautions.append(value)
        added += 1
    return added


def apply_merchant_fee_revenue_metric(schema: dict) -> int:
    """Answer 가맹점 수수료 금액 from the monthly 수입수수료, not the per-slip fee."""
    _insert_metric(schema, MERCHANT_FEE_METRIC, "가맹점월매출금액")

    attributes = schema.get("semantic_attributes")
    if not isinstance(attributes, list):
        raise SystemExit("semantic_attributes 없음")
    if any(item.get("name") == MERCHANT_FEE_ATTRIBUTE["name"] for item in attributes):
        raise SystemExit(f"이미 있는 속성: {MERCHANT_FEE_ATTRIBUTE['name']}")
    tables = {str(item.get("name") or ""): item for item in schema.get("tables", [])}
    merchant = tables.get(MERCHANT_MONTHLY_TABLE)
    if merchant is None:
        raise SystemExit(f"테이블 없음: {MERCHANT_MONTHLY_TABLE}")
    for mapping in MERCHANT_FEE_ATTRIBUTE["source_mappings"]:
        for name in mapping["columns"]:
            if _table_column(merchant, name) is None:
                raise SystemExit(f"{MERCHANT_MONTHLY_TABLE} 에 {name} 컬럼이 없다")
    attributes.append(deepcopy(MERCHANT_FEE_ATTRIBUTE))

    # 기업한도소진율과 같은 자리다. unit 은 % 인데 expression 은 AVG(수수료율 컬럼),
    # 즉 0~1 비율이다. 프롬프트에 unit=% 만 보이면 임계값 없는 "수수료율 높은" 질문에서
    # 모델이 90 을 써 늘 0건이 된다. 비율이라는 사실을 unit 에 적어 둔다.
    metrics = {
        str(item.get("name") or ""): item for item in schema.get("canonical_metrics", [])
    }
    if "신용카드가맹점수수료율" not in metrics:
        raise SystemExit("수수료율 단위 교정 대상 지표 없음: 신용카드가맹점수수료율")
    metrics["신용카드가맹점수수료율"]["unit"] = "비율 0~1 (90% 는 0.9)"

    return 2


def apply_recent_period_average_comparison(schema: dict) -> int:
    """Compare 최근 3·6개월 실적 from the cumulative columns on one row."""
    tables = {str(item.get("name") or ""): item for item in schema.get("tables", [])}
    merchant = tables.get(MERCHANT_MONTHLY_TABLE)
    if merchant is None:
        raise SystemExit(f"테이블 없음: {MERCHANT_MONTHLY_TABLE}")
    for months in RECENT_PERIOD_MONTHS:
        for suffix in ("가맹점매출금액", "가맹점매출건수"):
            name = f"최근{months}개월{suffix}"
            if _table_column(merchant, name) is None:
                raise SystemExit(f"{MERCHANT_MONTHLY_TABLE} 에 {name} 컬럼이 없다")

    references = schema.get("query_references")
    if not isinstance(references, list):
        raise SystemExit("query_references 없음")
    if any(
        item.get("intent") == RECENT_PERIOD_SALES_REFERENCE["intent"]
        for item in references
    ):
        raise SystemExit(f"이미 있는 참조: {RECENT_PERIOD_SALES_REFERENCE['intent']}")
    anchor = next(
        (
            position
            for position, item in enumerate(references)
            if item.get("intent") == "가맹점별_매출순위"
        ),
        len(references) - 1,
    )
    references.insert(anchor + 1, deepcopy(RECENT_PERIOD_SALES_REFERENCE))

    glossary = schema.get("glossary")
    if not isinstance(glossary, list):
        raise SystemExit("glossary 없음")
    if any(item.get("term") == RECENT_PERIOD_GLOSSARY_TERM["term"] for item in glossary):
        raise SystemExit(f"이미 있는 용어: {RECENT_PERIOD_GLOSSARY_TERM['term']}")
    glossary.append(deepcopy(RECENT_PERIOD_GLOSSARY_TERM))
    return 2


# ---------------------------------------------------------------------------
# 11~12. SQL 생성 계약
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
    # ambiguity_rules 는 키를 유지하고 내용만 바꾼다. schema._with_contract_compatibility()
    # 가 레거시 llm_semantic_contract.ambiguity_policy 를 이 키에서 파생시킨다.
    contract["ambiguity_rules"] = list(AMBIGUITY_RULES_V2)

    time_resolution = contract.get("time_resolution")
    if isinstance(time_resolution, dict):
        time_resolution.pop("tbdaa1d12 월", None)
        time_resolution["tbdaa1d12 현재"] = (
            "기준년월일 = KST 전일(D-1); 물리 기준년월 컬럼 없음"
        )
        time_resolution["tmdaa1d12 과거 월"] = (
            "기준년월(YYYYMM)로 조회하고 고객×월 최신 기준년월일 1건"
        )
        time_resolution["N분기·이번 분기·지난 분기"] = (
            "1분기 01~03, 2분기 04~06, 3분기 07~09, 4분기 10~12."
            " 연도가 없으면 실행연도, 작년 N분기는 전년"
        )
        time_resolution["연도 없는 M월"] = (
            "아직 오지 않은 달로 넘기지 않고 가장 최근에 지나간 그 달"
            " (예: 실행 9월에 12월 → 전년 12월)"
        )
        time_resolution["작년 M월·전년 M월"] = "전년도 그 달"

    # output_contract 를 evidence_order 앞으로 옮겨 프롬프트 맨 위에 오게 한다.
    order = [
        "purpose",
        "output_contract",
        "evidence_order",
        "athena_rules",
        "grain_and_aggregation_rules",
        "ambiguity_rules",
        "time_resolution",
        "result_defaults",
    ]
    for key in reversed(order):
        if key in contract:
            if hasattr(contract, "move_to_end"):
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
    parser.add_argument("--source", type=Path, default=V1_ROOT / "semantic_layer.yaml")
    parser.add_argument("--codebooks", type=Path, default=PROJECT_ROOT / "codebooks" / "column_codebooks.yaml")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "semantic_layer.yaml")
    args = parser.parse_args()

    yaml = _yaml()
    schema = yaml.load(args.source.read_text(encoding="utf-8"))
    codebooks = load_codebooks(args.codebooks)

    removed_columns = remove_retired_columns(schema)
    column_stats = apply_column_metadata(schema, codebooks)
    accumulation_policy_count = apply_accumulation_policies(schema)
    time_axis_count = apply_time_axis_fixes(schema)
    recent_merchant_semantic_count = apply_recent_merchant_semantic_overrides(schema)
    merchant_address_count = apply_merchant_address_semantics(schema)
    previous_day_semantic_count = apply_previous_day_semantic_overrides(schema)
    corporate_customer_semantic_count = apply_corporate_customer_semantic_overrides(schema)
    identity_count = apply_customer_member_identity_overrides(schema)
    usage_sales_count = apply_usage_sales_vocabulary(schema)
    slip_net_amount_count = apply_sales_slip_net_amount(schema)
    billing_overseas_count = apply_billing_and_overseas_usage_vocabulary(schema)
    merchant_credit_sales_count = apply_merchant_credit_sales_semantics(schema)
    merchant_sales_surface_count = apply_merchant_monthly_sales_surface_forms(schema)
    app_segment_surface_count = apply_app_segment_surface_forms(schema)
    card_usage_count = apply_card_usage_amount_and_corporate_count(schema)
    merchant_postal_count = apply_merchant_postal_code_surface_forms(schema)
    card_level_count = apply_card_level_corporate_card_count(schema)
    overseas_merchant_count = apply_overseas_merchant_sales_metrics(schema)
    slip_count_filter_count = apply_corporate_slip_count_filters(schema)
    merchant_fee_count = apply_merchant_fee_revenue_metric(schema)
    member_identity_count = apply_member_master_identity_scope(schema)
    recent_period_count = apply_recent_period_average_comparison(schema)
    contract_stats = apply_sql_contract(schema)

    metadata = schema.get("semantic_layer_metadata")
    if metadata is not None:
        metadata["version"] = V2_VERSION
        metadata["derived_from"] = "semantic_layer.yaml (v1)"
        metadata["v2_changes"] = [
            "같은 이름의 컬럼은 테이블을 넘어 동의어를 공유한다.",
            "컬럼명에서 질문 표면형 동의어를 유도해 추가했다(구분코드/여부 접미사 제거, 띄어쓰기 변형).",
            "월 지표의 기준월 접두사를 뗀 표면형을 동의어로 추가했다(금월체크카드이용금액 → 체크카드이용금액).",
            "어디에나 걸리는 한 글자 동의어(기준년월의 '월')를 걷어냈다.",
            "0811 상세 컬럼 코드북 7종을 같은 이름의 모든 컬럼에 value_semantics 로 적용했다.",
            "SQL 생성 계약에서 되묻기 지시를 제거하고 항상 SQL을 내보내는 output_contract 로 교체했다.",
            "Athena 방언 규칙에 QUALIFY·expr::type·윈도함수 WHERE 금지를 명시했다.",
            f"프롬프트 렌더 한도({PROMPT_LIST_RENDER_LIMIT}개)를 넘겨 잘리던 규칙 리스트를 정리했다.",
            "테이블 적재 주기와 기간 조회 컬럼을 grain과 별도 정책으로 추가했다.",
            "최근 10일 이전 가맹점 조회는 월 스냅샷으로 전환하고 월+가맹점번호로 조인하도록 교정했다.",
            "가맹점 주소를 가맹점기본(tbdaadt01) 한 곳만 가리키는 semantic attribute 로 선언했다.",
            "전일 적재 테이블의 지표·계약·질의 참조를 KST D-1 기준으로 통일했다.",
            "기업고객 현재 상태는 tbdaa1d12 KST D-1, 명시한 과거 월은 tmdaa1d12 기준년월, 현재+이력 질의는 두 소스를 함께 사용하도록 분리했다.",
            "유효 기업카드 보유 속성의 원천을 현재(tbdaa1d12)·과거 월(tmdaa1d12)로 갈라 선언했다.",
            "tbmaisd06 의 물리 시간축은 유지하고 연 단위 적재 조회 컬럼을 정책으로 분리했으며, 기업영업 스코프 필터를 기본값으로 못 박았다.",
            "기업 수는 고객식별자, 회원 수는 회원일련번호로 세도록 지표·용어·grain 규칙을 분리했다(기업고객수·연체기업수 지표 신설).",
            "이용금액과 매출금액이 같은 금액의 다른 관점임을 용어로 선언하고, 기업·카드·가맹점·전표 단위별 원천 컬럼을 못 박았다.",
            "매출전표종류구분코드 코드값 6종을 선언하고, 전표 매출금액·이용금액을 정당(1)·취소정정(4)은 더하고 정정(2)·취소(3)·청구보류(5)·체크환급(6)은 빼는 순액 집계로 바꿨다.",
            "해외 이용금액을 카드 월실적(tmdaa3e16)의 해외 일시불+CA 이용금액 지표로 분리하고, 청구금액을 금월청구금액 지표·용어로 신설해 이용금액 컬럼과 갈라 놓았다.",
            "가맹점 신용판매 매출금액을 이번 달은 tbdaadt01, 지난 달 이전은 tmdaa5e11에서 읽는 속성으로 선언했다.",
            "가맹점월매출금액에 건수 지표와 같은 표면형(가맹점매출금액)을 동의어로 세웠다.",
            "앱구분코드에 질문 표면형 '앱' 을 동의어로 세웠다(한 글자 금지의 유일한 예외).",
            "법인카드 이용금액에 사용자 표면형을 달아 카드 월실적(tmdaa3e16)으로 돌리고, 이용 기업수를 전표(tbdaabt30) 기업고객식별자를 세는 지표·용어로 신설했다.",
            "가맹점 우편번호를 주소 속성의 별칭으로 세우고, 마스터의 접두사 없는 우편번호에 가맹점우편번호 표면형을 달아 전표 사본 대신 마스터를 보게 했다.",
            "기업카드 좌수를 카드 속성 축으로 쪼갤 때는 회원카드기본(tbdaaat05) 한 곳에서 세도록 유효 기업카드 보유 속성에 카드 단위 원천과 축 정책을 선언했다.",
            "해외 가맹점 매출 건수·금액을 가맹점 월실적(tmdaa5e11) 지표로 신설하고, 해외 전표·가맹점 월스냅샷에 그 사실을 주의사항으로 붙였다.",
            "기업 매출건수의 코드 필터(카드매출유형구분코드 일반·할부·리볼빙, 회원·가맹점 소속회사 당사)를 지표 required_filters 로 못 박았다.",
            "가맹점 수수료 금액을 tmdaa5e11 가맹점수입수수료 지표로 신설하고, 전표당 수수료와 수수료율은 그대로 두었다.",
            "최근 3·6개월 실적은 tmdaa5e11 누적 컬럼 한 행에서 읽고 평균은 개월 수로 나누도록 질의 참조와 용어를 추가했다.",
            "회원 마스터(tbdaaat03)의 테이블 동의어에서 낱말 '회원' 을 걷어냈다. 모집단을 가리키는 말이 테이블을 지목하지 않는다.",
            "실제 스키마에 없는 평가·JCB 관련 컬럼 20개를 11개 테이블에서 제거했다.",
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
    print(f"  columns removed      : {len(removed_columns)}")
    print(f"  columns touched      : {column_stats['columns_touched']}")
    print(f"  synonyms propagated  : {column_stats['synonyms_propagated']}")
    print(f"  synonyms derived     : {column_stats['synonyms_derived']}")
    print(f"  descriptions filled  : {column_stats['descriptions_filled']}")
    print(f"  codebooks applied    : {column_stats['codebooks_applied']} column instances")
    print(f"    codebook columns   : {', '.join(column_stats['codebook_columns'])}")
    print(f"  accumulation policies: {accumulation_policy_count} tables")
    print(f"  time axis fixes      : {time_axis_count} tables")
    print(f"  recent merchant semantics: {recent_merchant_semantic_count} entries")
    print(f"  merchant address semantics: {merchant_address_count} entries")
    print(f"  previous-day semantics: {previous_day_semantic_count} entries")
    print(f"  corporate customer semantics: {corporate_customer_semantic_count} entries")
    print(f"  customer/member identity: {identity_count} entries")
    print(f"  usage/sales vocabulary: {usage_sales_count} entries")
    print(f"  slip-type net amount : {slip_net_amount_count} entries")
    print(f"  billing/overseas usage: {billing_overseas_count} entries")
    print(f"  merchant credit sales : {merchant_credit_sales_count} entries")
    print(f"  merchant sales surface: {merchant_sales_surface_count} synonyms")
    print(f"  app segment surface   : {app_segment_surface_count} synonyms")
    print(f"  card usage/count      : {card_usage_count} entries")
    print(f"  merchant postal code  : {merchant_postal_count} entries")
    print(f"  card-level card count : {card_level_count} entries")
    print(f"  overseas merchant sales: {overseas_merchant_count} entries")
    print(f"  corporate slip filters: {slip_count_filter_count} entries")
    print(f"  merchant fee revenue  : {merchant_fee_count} entries")
    print(f"  member master identity: {member_identity_count} entries")
    print(f"  recent period average : {recent_period_count} entries")
    print(f"  contract list sizes  : {contract_stats['before']} -> {contract_stats['after']}")


if __name__ == "__main__":
    main()

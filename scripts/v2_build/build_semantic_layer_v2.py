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
import sys
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from ruamel.yaml import YAML

PROJECT_ROOT = Path(__file__).resolve().parents[2]   # v2 적용본 루트
V1_ROOT = PROJECT_ROOT.parent / "corporate_sales_fable"   # 변환 원본(v1 저장소)
sys.path.insert(0, str(PROJECT_ROOT))

from text2sql_agent.v2.column_synonyms import derive_column_synonyms  # noqa: E402
from text2sql_agent.time_policy import TABLE_ACCUMULATION_POLICIES  # noqa: E402
from text2sql_agent.v2.sql_contract import (  # noqa: E402
    ATHENA_RULES_V2,
    GRAIN_RULES_V2,
    OUTPUT_CONTRACT_V2,
    AMBIGUITY_RULES_V2,
    PROMPT_LIST_RENDER_LIMIT,
)

COLUMN_SECTIONS = ("dimensions", "measures", "time_dimensions")
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
# 후보가 됐다. 주소를 마스터 한 곳으로만 매핑되는 속성으로 선언한다.
# ---------------------------------------------------------------------------
MERCHANT_ADDRESS_ATTRIBUTE = {
    "name": "merchant_address",
    "korean_name": "가맹점 주소",
    "domains": ["merchant_sales", "corporate_sales_targeting"],
    "business_definition": (
        "가맹점 사업장의 소재지 주소. 도로명·지번 표기는 주소표시구분코드가 구분하며, "
        "주소 본문(가맹점상세주소)은 민감 컬럼이라 우편번호·도로명 번호 체계까지만 조회한다"
    ),
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
            "columns": [
                "주소표시구분코드",
                "우편번호",
                "도로명우편번호",
                "도로명우편읍면번호",
                "도로명구역번호",
                "도로명건물본번호",
                "도로명건물부번호",
                "도로명지하구분코드",
            ],
        }
    ],
    "semantic_cautions": [
        "도로명주소라는 단일 컬럼은 없다. 주소 본문은 가맹점상세주소인데 민감 컬럼이라 조회할 수 없으므로 SELECT 에 넣지 않는다.",
        "도로명 주소를 물으면 조회 가능한 주소표시구분코드·우편번호·도로명 번호 컬럼을 내보내고, 주소 문장을 만들어 내지 않는다.",
    ],
}


def apply_merchant_address_semantics(schema: dict) -> int:
    """Give 가맹점 주소 one master-only attribute instead of no attribute at all."""
    attributes = schema.get("semantic_attributes")
    if not isinstance(attributes, list):
        raise SystemExit("semantic_attributes 없음")
    if any(
        item.get("name") == MERCHANT_ADDRESS_ATTRIBUTE["name"] for item in attributes
    ):
        raise SystemExit(f"이미 있는 속성: {MERCHANT_ADDRESS_ATTRIBUTE['name']}")

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


def apply_corporate_customer_semantic_overrides(schema: dict) -> int:
    """Route current corporate snapshots to D-1 and history to monthly rows."""
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
        2
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
            "테이블 적재 주기와 기간 조회 컬럼을 grain과 별도 정책으로 추가했다.",
            "최근 10일 이전 가맹점 조회는 월 스냅샷으로 전환하고 월+가맹점번호로 조인하도록 교정했다.",
            "가맹점 주소를 가맹점기본(tbdaadt01) 한 곳만 가리키는 semantic attribute 로 선언했다.",
            "전일 적재 테이블의 지표·계약·질의 참조를 KST D-1 기준으로 통일했다.",
            "기업고객 현재 상태는 tbdaa1d12 KST D-1, 명시한 과거 월은 tmdaa1d12 기준년월, 현재+이력 질의는 두 소스를 함께 사용하도록 분리했다.",
            "tbmaisd06 의 물리 시간축은 유지하고 연 단위 적재 조회 컬럼을 정책으로 분리했으며, 기업영업 스코프 필터를 기본값으로 못 박았다.",
            "기업 수는 고객식별자, 회원 수는 회원일련번호로 세도록 지표·용어·grain 규칙을 분리했다(기업고객수·연체기업수 지표 신설).",
            "이용금액과 매출금액이 같은 금액의 다른 관점임을 용어로 선언하고, 기업·카드·가맹점·전표 단위별 원천 컬럼을 못 박았다.",
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
    print(f"  contract list sizes  : {contract_stats['before']} -> {contract_stats['after']}")


if __name__ == "__main__":
    main()

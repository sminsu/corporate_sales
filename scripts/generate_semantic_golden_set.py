#!/usr/bin/env python3
"""Generate the 1,000-case semantic-layer golden set.

The set is deterministic and offline.  SQL answers point either to an
authoritative verified-query template (with a SHA-256 pin) or to a structural
semantic specification made only from canonical metrics and curated joins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "tests" / "fixtures" / "semantic_layer_golden_v1.jsonl"
REFERENCE_DATE = "2026-08-04"

if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from scripts.run_quality_eval import _actual_from_join_tables  # noqa: E402
from text2sql_agent.schema import (  # noqa: E402
    SCHEMA,
    VERIFIED_QUERIES,
    _validate_sql_against_schema,
    resolve_semantic_attribute_value,
    semantic_query_contract_candidates,
    source_tables_for_question,
)
from text2sql_agent.tools.sql_builders import VQ_PARAM_SPECS  # noqa: E402


STYLE_PREFIXES = [
    "",
    "업무 보고용으로 확인해줘. ",
    "데이터 기준 질문이야. ",
    "운영 현황 조회 요청이야. ",
    "간단히 답해줘. ",
    "정확한 수치가 필요해. ",
    "분석용으로 확인해줘. ",
    "DB 조회 요청: ",
    "리포트에 넣을 내용이야. ",
    "담당자 확인 요청이야. ",
    "수치 검증용 질문이야. ",
    "아래 조건으로 조회해줘. ",
    "현업 문의야. ",
    "집계 기준을 지켜서 확인해줘. ",
    "결과 확인 요청: ",
    "정기 보고서용이야. ",
    "간단 조회 부탁해. ",
    "조건에 맞춰 뽑아줘. ",
    "분석 결과가 필요해. ",
    "운영 데이터에서 확인해줘. ",
    "통계 확인 부탁해. ",
    "질문 하나 확인해줘. ",
    "데이터 조회 건이야. ",
    "보고 자료로 정리해줘. ",
    "자료 확인용 질문이야. ",
    "요청 사항이야. ",
    "수치로 답해줘. ",
    "현황 파악용 질문이야. ",
    "결과 검토가 필요해. ",
    "업무 자료로 조회해줘. ",
    "분석 보고 요청: ",
    "데이터 확인 부탁해. ",
]

CORE_TARGET_COUNT = 616
COMPOSITION_TARGET_COUNT = 300
EXCLUDED_DOMAINS = {"relationship_sales_management"}
EXCLUDED_CORE_CONTRACTS = {"card_product_current_valid_corporate_count"}
EXCLUDED_COMPOSITION_SOURCES = {"valid_card_by_product"}
CORE_TARGET_OVERRIDES = {
    "current_enterprise_size_customer_list": 24,
    "historical_enterprise_size_customer_list": 24,
    "corporate_valid_card_count_by_brand_at_month": 24,
    "corporate_cards_issued_then_cancelled_count": 24,
    "current_corporate_member_half_year_new_check_card_issuance": 24,
}

_PUBLIC_ENTERPRISE_RE = re.compile(r"공공\s*기업|공기업")
_PERSONAL_SCOPE_RE = re.compile(
    r"로그인|내\s*계정|내\s*정보|내\s*사번|현재\s*사용자|사용자\s*권한|직원번호|"
    r"세션\s*사용자|조직\s*권한|사용자\s*프로필|담당\s*(?:기업|업체|회사)|"
    r"관리\s*(?:기업|회원)|내가\s*(?:관리|맡은)|내\s*(?:관리기업|관리회원|기업회원|기업들)"
)
_PRODUCT_VALID_CARD_COUNT_RE = re.compile(
    r"상품.*유효(?:신용)?\s*카드\s*(?:수|개수|좌수)"
)
_NUMBERED_SALES_TARGET_RE = re.compile(r"1\s*[~∼-]\s*5(?:번)?\s*영업대상")
_BROKEN_QUESTION_RE = re.compile(
    r"고객\s+고객|살아\s+있지만회원|없는인|이용하였했지만|이용(?:\s+이력)?는데|"
    r"줄어든한|문\s+닫은한|리스트을|회사을|업체을|기업를|한도을|은행가|목록를|"
    r"가맹점별의|국내\s+국내|증감률\s+증감률|목록\s+목록|한도한도사용률|"
    r"알려줘\s+알려줘|몇\s*명\s+몇\s*명|최근\s+일년|계좌종류별|"
    r"회원별\s+연체회원|법인회원의\s+유실적\s+업체|"
    r"상반기\s+현재\s+기업규모별|기업규모별\s+월\s+법인카드"
    r"|타켓|네뷰라|네뮤라|네뷐라"
)
_MIXED_REGISTER_RE = re.compile(
    r"(?:질문이야|확인해줘|답해줘|조회해줘|부탁해)\.\s+.*알려주세요"
)

CONTRACT_SEED_OVERRIDES = {
    "named_merchant_store_sales_at_month": [
        "노랑통닭 가맹점주의 2026년 6월 기준 최근 3개월 가맹점 매출액 알려줘",
    ],
    "brand_merchant_corporate_card_customer_list": [
        "2605 기준 꾸석지 가맹점 중 기업카드를 보유한 기업고객식별자 목록을 알려줘",
    ],
    "merchant_profile_by_name": [
        "마초스테이크하우스 가맹점 기본 정보 알려줘",
    ],
    "merchant_payment_institution_list": [
        "파파존스 가맹점의 결제금융기관 목록을 알려줘",
        "도미노피자 가맹점의 결제금융기관 목록을 알려줘",
    ],
    "merchant_count_by_payment_institution": [
        "파파존스 가맹점주 중 가맹점 계좌가 KB국민은행인 가맹점 수 알려줘",
    ],
    "merchant_portfolio_allowance_rate": [
        "2605 기준 꾸석지 가맹점주의 대손율 알려줘",
    ],
}

CURATED_CONTRACT_SEEDS = {
    "corporate_member_with_monthly_usage_count": [
        "2026년 6월 기준 이용실적이 있는 법인회원 수를 알려줘",
    ],
    "current_corporate_member_half_year_new_check_card_issuance": [
        "현재 KB카드 기업카드 보유 회원 중 2026년 상반기에 전기요금전용 체크카드를 신규 발급받은 좌수와 회원 목록을 알려줘",
        "현재 KB카드 기업카드 보유 회원 중 2026년 하반기에 전기요금전용 체크카드를 신규 발급받은 좌수와 회원 목록을 알려줘",
    ],
    "brand_owner_corporate_card_count": [
        "파파존스 가맹점주 중 KB국민카드 기업카드를 보유한 사람은 몇 명이야",
    ],
}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def _normalized_question(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[^0-9a-z가-힣]+", "", value)


def _clean_question(value: str) -> str:
    value = re.sub(r"이내(?:에)?\s+이내(?:에)?", "이내에", value)
    value = re.sub(r"가맹점\s+가맹점(?=\s|계좌|결제)", "가맹점", value)
    value = value.replace("현재기준", "현재 기준")
    value = re.sub(r"업종\s+별", "업종별", value)
    value = value.replace("업체수", "업체 수")
    value = value.replace("최근 일년", "최근 1년")
    value = value.replace("기업 회원", "기업회원")
    return " ".join(value.split()).strip()


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = " ".join(str(value).split()).strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _round_robin(groups: list[list[str]]) -> list[str]:
    result: list[str] = []
    for index in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if index < len(group):
                result.append(group[index])
    return _unique(result)


def _lexical_variants(seed: str, _contract: dict[str, Any]) -> list[str]:
    seed = seed.replace("알려주세요", "알려줘")
    variants = [seed]
    request_endings = ["알려줘", "보여줘", "조회해줘", "확인해줘", "정리해줘", "뽑아줘"]
    for ending in request_endings:
        if ending not in seed:
            continue
        variants.extend(seed.replace(ending, replacement) for replacement in request_endings if replacement != ending)
        break
    return _unique(variants)


def _vq_index() -> dict[str, dict[str, Any]]:
    return {str(query["name"]): query for query in VERIFIED_QUERIES}


def _merged_parameter_schema(query: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not query:
        return []
    merged = {str(spec["name"]): dict(spec) for spec in VQ_PARAM_SPECS.get(str(query["name"]), [])}
    for name, info in (query.get("parameters") or {}).items():
        merged.setdefault(str(name), {"name": str(name)}).update(info if isinstance(info, dict) else {})
    return [merged[name] for name in merged]


def _contract_parameter_names(contract: dict[str, Any], query: dict[str, Any] | None) -> tuple[list[str], list[str]]:
    schema = _merged_parameter_schema(query)
    required = [str(spec["name"]) for spec in schema if spec.get("required")]
    optional = [str(spec["name"]) for spec in schema if not spec.get("required")]
    entity_binding = contract.get("entity_binding") or {}
    if entity_binding.get("required") and entity_binding.get("parameter"):
        required.append(str(entity_binding["parameter"]))
    time_policy = contract.get("time_policy") or {}
    for key in ("parameter", "required_parameter"):
        value = time_policy.get(key)
        if value:
            required.append(str(value).split("(", 1)[0])
    for attribute in (contract.get("match") or {}).get("required_attribute_values", []):
        required.append(f"semantic_attribute:{attribute}")
    if (contract.get("match") or {}).get("require_explicit_period"):
        required.append("기준년월")
    required = _unique(required)
    return required, [name for name in _unique(optional) if name not in required]


_TIME_EVIDENCE_RE = re.compile(
    r"(?:20\d{2}\s*년|\b20\d{4,6}\b|\b\d{2}\d{2}\s*기준|"
    r"작년|지난해|올해|금년|현재|오늘|이번\s*(?:달|월)|지난\s*(?:달|월)|"
    r"(?:최근|지난)\s*\d+\s*(?:개월|달|년)|상반기|하반기)"
)
_ENTITY_EXAMPLES = {
    "기업명": ("쿠팡", "삼성전자", "한빛테크", "새롬유통", "미래산업"),
    "가맹점명": ("도미노", "스타벅스", "교촌", "파파존스", "꾸석지", "마초스테이크", "노랑통닭"),
    "상품명": ("KB아시아나", "전기요금전용"),
}


def _has_parameter_evidence(question: str, name: str) -> bool:
    if name.startswith("semantic_attribute:"):
        attribute = name.split(":", 1)[1]
        return bool(resolve_semantic_attribute_value(SCHEMA, attribute, question))
    if name in _ENTITY_EXAMPLES:
        return any(value.lower() in question.lower() for value in _ENTITY_EXAMPLES[name])
    if name == "결제금융기관코드":
        return bool(resolve_semantic_attribute_value(SCHEMA, "merchant_payment_institution", question))
    if name == "기업규모구분코드":
        return bool(resolve_semantic_attribute_value(SCHEMA, "enterprise_size", question))
    if name == "관리기업목록":
        return bool(re.search(r"\b\d{10}\b", question))
    if name == "조회기간개월수":
        return bool(re.search(r"(?:최근|지난)\s*(?:\d+\s*(?:개월|달|년)|반년|일년)", question))
    if any(token in name for token in ("년월", "기간", "시작", "종료")):
        return bool(_TIME_EVIDENCE_RE.search(question))
    return True


def _route_expected_answer_sources(
    value: Any,
    contract: dict[str, Any],
    question: str,
) -> Any:
    """Keep structural golden SQL evidence on the question-routed archive."""
    selected = set(source_tables_for_question(contract, question))
    replacements: dict[str, str] = {}
    if "tmdaa1d12" in selected and "tbdaa1d12" not in selected:
        replacements["tbdaa1d12"] = "tmdaa1d12"
    elif "tbdaa1d12" in selected and "tmdaa1d12" not in selected:
        replacements["tmdaa1d12"] = "tbdaa1d12"
    if not replacements:
        return value
    if isinstance(value, str):
        return _replace_table_names(value, replacements)
    if isinstance(value, dict):
        return {
            key: _route_expected_answer_sources(item, contract, question)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _route_expected_answer_sources(item, contract, question)
            for item in value
        ]
    return value


def _contract_sql_answer(
    contract: dict[str, Any],
    query: dict[str, Any] | None,
    question: str,
) -> dict[str, Any]:
    if contract.get("verified_query") and query:
        tables = sorted(_actual_from_join_tables(str(query["sql"])))
        issues = _validate_sql_against_schema(str(query["sql"]), tables)
        if issues:
            answer = _semantic_spec(list(contract.get("metric_names") or []), [])
            answer["verified_query_reference"] = {
                "name": query["name"],
                "template_sha256": _sha256(str(query["sql"])),
                "excluded_from_sql_gold_reason": "schema_validation_failed",
            }
            answer["required_filters"] = _unique(
                [*answer["required_filters"], *list(contract.get("filters") or [])]
            )
            answer["calculation"] = contract.get("calculation")
            return _route_expected_answer_sources(answer, contract, question)
        return {
            "kind": "verified_query",
            "name": query["name"],
            "template_sha256": _sha256(str(query["sql"])),
            "runtime_mode": str(query.get("runtime_mode") or "active"),
        }

    answer: dict[str, Any] = {
        "kind": "semantic_generation",
        "contract": contract["name"],
        "metric_expressions": {},
        "join_conditions": [],
        "required_filters": list(contract.get("filters") or []),
        "calculation": contract.get("calculation"),
    }
    if contract.get("reference_query") and query:
        answer["reference_query"] = {
            "name": query["name"],
            "template_sha256": _sha256(str(query["sql"])),
            "runtime_mode": "reference_only",
        }
    metric_index = {str(metric["name"]): metric for metric in SCHEMA["canonical_metrics"]}
    answer["metric_expressions"] = {
        name: metric_index[name]["expression"]
        for name in contract.get("metric_names", [])
        if name in metric_index
    }
    return _route_expected_answer_sources(answer, contract, question)


def _base_case(
    *,
    question: str,
    action: str,
    domain: str | None,
    source: dict[str, Any],
    expected_sql: dict[str, Any] | None,
    tables: list[str] | None = None,
    grain: str | None = None,
    required_parameters: list[str] | None = None,
    missing_parameters: list[str] | None = None,
    reason_code: str | None = None,
    evidence_path: str = "",
    bucket: str,
    variation: str,
    difficulty: str = "medium",
) -> dict[str, Any]:
    return {
        "question_ko": _clean_question(question),
        "expected_action": action,
        "domain": domain,
        "source": source,
        "parameters": {
            "required": required_parameters or [],
            "reference_date": REFERENCE_DATE,
        },
        "expected_sql": expected_sql,
        "expected_tables": sorted(set(tables or [])),
        "expected_grain": grain,
        "expected_missing_parameters": missing_parameters or [],
        "reason_code": reason_code,
        "evidence_path": evidence_path,
        "labels": {
            "bucket": bucket,
            "difficulty": difficulty,
            "variation": variation,
            "review_status": "semantic_contract_verified",
        },
    }


def _core_cases() -> list[dict[str, Any]]:
    queries = _vq_index()
    cases: list[dict[str, Any]] = []
    executable = [
        contract
        for contract in SCHEMA["semantic_query_contracts"]
        if not str(contract.get("support_status") or "").startswith("blocked")
        and str(contract["domain"]) not in EXCLUDED_DOMAINS
        and str(contract["name"]) not in EXCLUDED_CORE_CONTRACTS
    ]
    if len(executable) != 36:
        raise ValueError(f"expected 36 included executable contracts, found {len(executable)}")

    for contract in executable:
        target_count = CORE_TARGET_OVERRIDES.get(str(contract["name"]), 16)
        query_name = str(contract.get("verified_query") or contract.get("reference_query") or "")
        query = queries.get(query_name)
        required, optional = _contract_parameter_names(contract, query)
        contract_name = str(contract["name"])
        if contract_name in CURATED_CONTRACT_SEEDS:
            bases = list(CURATED_CONTRACT_SEEDS[contract_name])
        else:
            bases = list(contract.get("examples") or []) + CONTRACT_SEED_OVERRIDES.get(contract_name, [])
            if query:
                bases.append(str(query["question"]))
        bases = [
            base
            for base in _unique(bases)
            if not _PUBLIC_ENTERPRISE_RE.search(base)
            if all(_has_parameter_evidence(base, name) for name in required)
        ]
        if not bases:
            raise ValueError(f"contract has no complete question seed: {contract['name']} required={required}")

        lexical = _round_robin([_lexical_variants(base, contract) for base in bases])
        selected: list[tuple[str, str]] = []
        seen: set[str] = set()
        rounds = max(2, (len(lexical) // len(STYLE_PREFIXES)) + 2)
        for round_index in range(rounds):
            for style_index, prefix in enumerate(STYLE_PREFIXES):
                core = lexical[(round_index * len(STYLE_PREFIXES) + style_index) % len(lexical)]
                terminal = re.search(r"(알려줘|알려주세요|보여줘|조회해줘|확인해줘|정리해줘|뽑아줘)$", core)
                if terminal and terminal.group(1) in prefix:
                    continue
                question = f"{prefix}{core}".strip()
                normalized = _normalized_question(question)
                if normalized in seen:
                    continue
                seen.add(normalized)
                candidates = semantic_query_contract_candidates(
                    SCHEMA,
                    question,
                    max_count=1,
                    routing_only=True,
                )
                if not candidates or candidates[0]["name"] != contract["name"]:
                    continue
                selected.append((question, f"lexical_{lexical.index(core):02d}_style_{style_index:02d}"))
                if len(selected) == target_count:
                    break
            if len(selected) == target_count:
                break
        if len(selected) != target_count:
            raise ValueError(
                f"only {len(selected)} of {target_count} valid variants for {contract['name']}"
            )

        source = {
            "kind": "semantic_contract",
            "id": contract["name"],
            "verified_query": contract.get("verified_query"),
            "reference_query": contract.get("reference_query"),
            "metrics": list(contract.get("metric_names") or []),
            "attributes": list(contract.get("semantic_attributes") or []),
            "join_paths": [],
            "optional_parameters": optional,
        }
        for question, variation in selected:
            sql_answer = _contract_sql_answer(contract, query, question)
            cases.append(
                _base_case(
                    question=question,
                    action="sql",
                    domain=str(contract["domain"]),
                    source=source,
                    expected_sql=sql_answer,
                    tables=source_tables_for_question(contract, question),
                    grain=contract.get("result_grain"),
                    required_parameters=required,
                    evidence_path=f"semantic_query_contracts.{contract['name']}",
                    bucket="core",
                    variation=variation,
                    difficulty="easy" if contract.get("verified_query") else "medium",
                )
            )
    if len(cases) != CORE_TARGET_COUNT:
        raise ValueError(f"expected {CORE_TARGET_COUNT} core cases, found {len(cases)}")
    return cases


def _replace_table_names(value: str, replacements: dict[str, str]) -> str:
    for source, target in replacements.items():
        value = value.replace(source, target)
    return value


def _semantic_spec(
    metric_names: list[str],
    join_path_names: list[str],
    extra_filters: list[str] | None = None,
    source_replacements: dict[str, str] | None = None,
) -> dict[str, Any]:
    metrics = {str(metric["name"]): metric for metric in SCHEMA["canonical_metrics"]}
    paths = {str(path["name"]): path for path in SCHEMA["semantic_join_graph"]["safe_paths"]}
    replacements = source_replacements or {}
    return {
        "kind": "semantic_spec",
        "metric_expressions": {
            name: _replace_table_names(metrics[name]["expression"], replacements)
            for name in metric_names
        },
        "aggregation_behavior": {name: metrics[name]["aggregation_behavior"] for name in metric_names},
        "required_filters": _unique(
            _replace_table_names(filter_text, replacements)
            for name in metric_names
            for filter_text in metrics[name].get("required_filters", [])
        )
        + list(extra_filters or []),
        "join_conditions": [paths[name]["sql"] for name in join_path_names],
        "join_cautions": [paths[name].get("caution", "") for name in join_path_names if paths[name].get("caution")],
    }


def _metric_tables(
    metric_names: list[str],
    join_path_names: list[str],
    source_replacements: dict[str, str] | None = None,
) -> list[str]:
    metrics = {str(metric["name"]): metric for metric in SCHEMA["canonical_metrics"]}
    paths = {str(path["name"]): path for path in SCHEMA["semantic_join_graph"]["safe_paths"]}
    replacements = source_replacements or {}
    tables = [
        replacements.get(str(metrics[name]["source_table"]), str(metrics[name]["source_table"]))
        for name in metric_names
    ]
    for name in join_path_names:
        tables.extend([str(paths[name]["from_table"]), str(paths[name]["to_table"])])
    return sorted(set(tables))


def _composition_seed_specs() -> list[dict[str, Any]]:
    return [
        # Rebalanced across supported, non-personal-scope semantics: 300 total.
        {"id": "high_sales_merchant_without_card", "domain": "corporate_sales_targeting", "count": 20, "question": "2026년 6월 월매출액 1억원 이상인 정상 법인 가맹점 중 기업카드가 없는 기업회원 명단을 알려줘", "vq": "merchant_corporate_sales_target_no_corporate_card"},
        # merchant_sales: 80
        {"id": "active_brand_merchant_count", "domain": "merchant_sales", "count": 20, "question": "도미노 브랜드 정상 가맹점 수 알려줘", "vq": "brand_active_merchant_count"},
        {"id": "merchant_sales_by_industry", "domain": "merchant_sales", "count": 20, "question": "2026년 상반기 업종별 가맹점 월매출 순위를 알려줘", "metrics": ["가맹점월매출금액"], "paths": ["merchant_monthly_performance_to_industry"]},
        {"id": "merchant_fee_by_industry", "domain": "merchant_sales", "count": 20, "question": "2026년 상반기 업종별 평균 가맹점 수수료율 알려줘", "metrics": ["신용카드가맹점수수료율"], "paths": ["merchant_monthly_performance_to_industry"]},
        {"id": "merchant_count_by_bank", "domain": "merchant_sales", "count": 20, "question": "파파존스 중 KB국민은행을 결제금융기관으로 쓰는 가맹점 수 알려줘", "metrics": ["결제금융기관별가맹점수"], "paths": []},
        # customer_card_portfolio: 40
        {"id": "customer_member_count", "domain": "customer_card_portfolio", "count": 20, "question": "현재 기업고객별 회원 수를 알려줘", "metrics": ["회원수"], "paths": ["customer_to_member"]},
        {"id": "current_size_customer_list", "domain": "customer_card_portfolio", "count": 20, "question": "현재 기업규모별 기업고객 목록을 알려줘", "metrics": [], "paths": [], "tables": ["tbdaaat01"], "attributes": ["enterprise_size"], "grain": "기업규모구분코드 × 고객식별자 × 고객관리번호"},
        # card_usage: 80
        {"id": "domestic_sales_and_count", "domain": "card_usage", "count": 10, "question": "2026년 상반기 월별 국내 카드매출금액과 거래건수 알려줘", "metrics": ["카드매출금액", "매출건수"], "paths": []},
        {"id": "domestic_sales_by_merchant", "domain": "card_usage", "count": 10, "question": "2026년 상반기 가맹점별 국내 카드 이용금액 알려줘", "metrics": ["카드매출금액"], "paths": ["domestic_sales_to_merchant"]},
        {"id": "overseas_sales", "domain": "card_usage", "count": 10, "question": "2026년 상반기 회원별 해외 이용금액 알려줘", "metrics": ["해외매출금액"], "paths": ["member_to_overseas_sales"]},
        {"id": "card_monthly_product_usage", "domain": "card_usage", "count": 10, "question": "2026년 상반기 카드 상품별 월 이용금액 알려줘", "metrics": ["카드별월이용금액"], "paths": ["card_to_monthly_performance", "card_to_product"]},
        {"id": "corporate_monthly_usage", "domain": "card_usage", "count": 10, "question": "2026년 월별 법인카드 이용금액 합계를 알려줘", "metrics": ["법인카드월이용금액"], "paths": []},
        {"id": "corporate_monthly_mom", "domain": "card_usage", "count": 10, "question": "2026년 월별 법인카드 이용금액과 전월 대비 증감률 알려줘", "metrics": ["법인카드월이용금액", "법인카드월전월대비증감률"], "paths": []},
        {"id": "current_size_card_usage", "domain": "card_usage", "count": 10, "question": "현재 기업규모를 기준으로 2026년 상반기 법인카드 이용금액 알려줘", "metrics": ["법인카드월이용금액"], "paths": ["customer_to_card_monthly_performance"], "attributes": ["enterprise_size"]},
        {"id": "historical_size_card_usage", "domain": "card_usage", "count": 10, "question": "2025년 당시 기업규모별 월별 법인카드 이용금액 알려줘", "metrics": ["법인카드월이용금액"], "paths": ["customer_monthly_snapshot_to_card_monthly_performance"], "attributes": ["enterprise_size"]},
        # credit_risk: 80
        {"id": "total_credit_limit", "domain": "credit_risk", "count": 10, "question": "2026년 6월 기업별 총여신한도 알려줘", "metrics": ["총여신한도금액"], "paths": ["customer_to_corporate_monthly"], "source_replacements": {"tbdaa1d12": "tmdaa1d12"}},
        {"id": "remaining_credit_limit", "domain": "credit_risk", "count": 10, "question": "2026년 6월 기업별 잔여한도 알려줘", "metrics": ["기업잔여한도금액"], "paths": ["customer_to_corporate_monthly"], "source_replacements": {"tbdaa1d12": "tmdaa1d12"}},
        {"id": "used_credit_limit", "domain": "credit_risk", "count": 10, "question": "2026년 6월 기업별 한도사용금액 알려줘", "metrics": ["기업한도사용금액"], "paths": ["customer_to_corporate_monthly"], "source_replacements": {"tbdaa1d12": "tmdaa1d12"}},
        {"id": "limit_utilization", "domain": "credit_risk", "count": 10, "question": "2026년 6월 기업별 한도소진율 알려줘", "metrics": ["기업한도소진율"], "paths": ["customer_to_corporate_monthly"], "source_replacements": {"tbdaa1d12": "tmdaa1d12"}},
        {"id": "delinquent_member_count", "domain": "credit_risk", "count": 10, "question": "2026년 6월 연체회원 수 알려줘", "metrics": ["연체회원수"], "paths": ["member_to_delinquency"]},
        {"id": "special_debt_principal", "domain": "credit_risk", "count": 10, "question": "2026년 상반기 고객별 특수채권 편입원금 알려줘", "metrics": ["특수채권편입원금"], "paths": ["customer_to_special_debt"]},
        {"id": "allowance_by_member", "domain": "credit_risk", "count": 10, "question": "2026년 6월 회원별 기대대손충당금 알려줘", "metrics": ["기대대손충당금"], "paths": ["member_to_loss_provision"]},
        {"id": "merchant_portfolio_allowance", "domain": "credit_risk", "count": 10, "question": "2026년 6월 도미노피자 운영기업 포트폴리오 대손충당금률 알려줘", "metrics": ["가맹점포트폴리오대손충당금률"], "paths": ["merchant_monthly_snapshot_to_loss_provision"]},
    ]


def _composition_cases() -> list[dict[str, Any]]:
    queries = _vq_index()
    cases: list[dict[str, Any]] = []

    for seed in _composition_seed_specs():
        if str(seed["domain"]) in EXCLUDED_DOMAINS or str(seed["id"]) in EXCLUDED_COMPOSITION_SOURCES:
            continue
        query = queries.get(str(seed.get("vq") or ""))
        metric_names = list(seed.get("metrics") or [])
        path_names = list(seed.get("paths") or [])
        source_replacements = dict(seed.get("source_replacements") or {})
        questions: list[str] = []
        for index in range(int(seed["count"])):
            question = str(seed["question"])
            question = f"{STYLE_PREFIXES[index % len(STYLE_PREFIXES)]}{question}".strip()
            questions.append(question)

        if query:
            sql_answer = {
                "kind": "verified_query",
                "name": query["name"],
                "template_sha256": _sha256(str(query["sql"])),
                "runtime_mode": str(query.get("runtime_mode") or "active"),
            }
            expected_tables = sorted(_actual_from_join_tables(str(query["sql"])))
            required, optional = _contract_parameter_names({}, query)
        else:
            sql_answer = _semantic_spec(
                metric_names,
                path_names,
                source_replacements=source_replacements,
            )
            expected_tables = sorted(
                set(
                    seed.get("tables")
                    or _metric_tables(metric_names, path_names, source_replacements)
                )
            )
            required, optional = [], []

        for index, question in enumerate(questions):
            cases.append(
                _base_case(
                    question=question,
                    action="sql",
                    domain=str(seed["domain"]),
                    source={
                        "kind": "canonical_composition",
                        "id": seed["id"],
                        "verified_query": seed.get("vq"),
                        "metrics": metric_names,
                        "attributes": list(seed.get("attributes") or []),
                        "join_paths": path_names,
                        "optional_parameters": optional,
                    },
                    expected_sql=sql_answer,
                    tables=expected_tables,
                    grain=seed.get("grain"),
                    required_parameters=required,
                    evidence_path=f"canonical_composition.{seed['id']}",
                    bucket="composition",
                    variation=f"style_{index:02d}",
                    difficulty="hard" if path_names else "medium",
                )
            )
    if len(cases) != COMPOSITION_TARGET_COUNT:
        raise ValueError(f"expected {COMPOSITION_TARGET_COUNT} composition cases, found {len(cases)}")
    return cases


def _boundary_case(
    question: str,
    *,
    domain: str | None,
    action: str,
    source_id: str,
    reason: str,
    missing: list[str] | None = None,
    evidence: str,
    tables: list[str] | None = None,
) -> dict[str, Any]:
    return _base_case(
        question=question,
        action=action,
        domain=domain,
        source={"kind": "boundary", "id": source_id, "verified_query": None, "metrics": [], "attributes": [], "join_paths": []},
        expected_sql=None,
        tables=tables,
        missing_parameters=missing,
        reason_code=reason,
        evidence_path=evidence,
        bucket=action,
        variation=reason,
        difficulty="hard",
    )


def _clarify_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    targeting = [
        ("기업카드는 보유했지만 6개월 동안 한 번도 쓰지 않은 기업 명단 알려줘", ["기준년월일"]),
        ("법인카드 보유회원 중 최근 반년 무실적 업체를 보여줘", ["기준년월일"]),
        ("기업별 월평균 카드 이용금액을 알려줘", ["기간_시작", "기간_종료"]),
        ("그 회사의 월평균 기업카드 사용금액 알려줘", ["기업명", "기간_시작", "기간_종료"]),
        ("가맹점 중 기업카드 보유 기업고객 목록 알려줘", ["가맹점명", "기준년월"]),
        ("가맹점 월매출이 높은 기업카드 미보유 법인사업자 찾아줘", ["기준년월", "월매출금액"]),
        ("기업 체크카드만 쓰는 고액 이용 기업 알려줘", ["기준년월", "월평균금액"]),
        ("기업카드 보유 가맹점주 수 알려줘", ["가맹점명"]),
        ("기업카드 보유 후 최근에 사용하지 않은 기업 목록을 알려줘", ["기준년월일"]),
        ("기업별 평균 법인카드 이용금액을 보여줘", ["기간_시작", "기간_종료"]),
        ("월매출이 높은 기업카드 미보유 가맹점주를 찾아줘", ["기준년월", "월매출금액"]),
        ("체크카드만 사용하는 고액 이용 기업을 보여줘", ["기준년월", "월평균금액"]),
        ("기업카드 보유 가맹점 목록 알려줘", ["가맹점명"]),
    ]
    for question, missing in targeting:
        cases.append(_boundary_case(question, domain="corporate_sales_targeting", action="clarify", source_id="targeting_required_parameter", reason="missing_time" if any("년월" in item or "기간" in item for item in missing) else "missing_entity", missing=missing, evidence="semantic_query_contracts.required_parameters"))

    merchant = [
        ("최근 6개월 가맹점 매출액 알려줘", ["가맹점명"]),
        ("가맹점 기본 정보 알려줘", ["가맹점명"]),
        ("2605 기준 가맹점별 매출 알려줘", ["가맹점명"]),
        ("가맹점 결제금융기관 목록 알려줘", ["가맹점명"]),
        ("도미노피자 중 특정 결제은행을 쓰는 가맹점 수 알려줘", ["결제금융기관코드"]),
        ("도미노피자 가맹점 대손율 알려줘", ["기준년월"]),
        ("최근 폐업한 교촌치킨 가맹점 수 알려줘", ["조회기간개월수"]),
        ("특정 브랜드 정상 가맹점 수 알려줘", ["가맹점명"]),
        ("도미노피자의 가맹점 매출액을 알려줘", ["기간_시작", "기간_종료"]),
        ("파파존스 가맹점주의 월매출을 보여줘", ["기간_시작", "기간_종료"]),
        ("교촌치킨 가맹점 대손율 알려줘", ["기준년월"]),
        ("스타벅스 폐업 가맹점 수 알려줘", ["조회기간개월수"]),
        ("도미노피자 중 특정 은행을 쓰는 가맹점 수 알려줘", ["결제금융기관코드"]),
    ]
    for question, missing in merchant:
        cases.append(_boundary_case(question, domain="merchant_sales", action="clarify", source_id="merchant_required_parameter", reason="missing_entity" if any("명" in item for item in missing) else "missing_time", missing=missing, evidence="semantic_query_contracts.merchant_parameters"))

    portfolio = [
        ("당시 대기업 고객 목록 보여줘", ["기준년월"]),
        ("기업카드를 발급한 뒤 해지한 카드 좌수와 업체 수를 알려줘", ["발급기간_시작", "발급기간_종료", "해지기간_시작", "해지기간_종료"]),
        ("현재 기업카드 보유회원 중 상반기 특정 체크카드 신규 발급 목록 알려줘", ["상품명", "기간_시작", "기간_종료"]),
        ("특정 기업규모의 고객 목록 알려줘", ["semantic_attribute:enterprise_size"]),
        ("과거 중소기업 고객 목록 보여줘", ["기준년월"]),
        ("대기업 고객 목록을 보여줘", ["기준년월"]),
        ("기업카드를 발급한 뒤 해지한 카드 수를 알려줘", ["발급기간_시작", "발급기간_종료", "해지기간_시작", "해지기간_종료"]),
        ("특정 체크카드 신규 발급 회원 목록을 보여줘", ["상품명", "기간_시작", "기간_종료"]),
        ("과거 기업규모별 고객 목록을 알려줘", ["기준년월"]),
    ]
    for question, missing in portfolio:
        cases.append(_boundary_case(question, domain="customer_card_portfolio", action="clarify", source_id="portfolio_required_parameter", reason="missing_time" if any("기간" in item or "년월" in item for item in missing) else "missing_entity", missing=missing, evidence="semantic_query_contracts.portfolio_parameters"))

    card = [
        ("법인카드 월별 이용금액 알려줘", ["기간_시작", "기간_종료"]),
        ("업종별 법인카드 매출 알려줘", ["기간_시작", "기간_종료"]),
        ("카드 브랜드별 이용 현황 알려줘", ["기간_시작", "기간_종료"]),
        ("온라인과 오프라인 매출 비율 알려줘", ["기간_시작", "기간_종료"]),
        ("할부 거래 현황 알려줘", ["기간_시작", "기간_종료"]),
        ("해외 이용금액이 가장 큰 달 알려줘", ["기간_시작", "기간_종료"]),
        ("업종별 이용금액 알려줘", ["업종축", "기간_시작", "기간_종료"]),
        ("월별 국내 카드매출금액과 거래건수를 알려줘", ["기간_시작", "기간_종료"]),
        ("가맹점별 국내 카드 이용금액 알려줘", ["기간_시작", "기간_종료"]),
        ("회원별 해외 카드 이용금액 보여줘", ["기간_시작", "기간_종료"]),
        ("카드 상품별 월 이용금액 알려줘", ["기간_시작", "기간_종료"]),
    ]
    for question, missing in card:
        cases.append(_boundary_case(question, domain="card_usage", action="clarify", source_id="card_usage_required_parameter", reason="ambiguous_dimension" if "업종축" in missing else "missing_time", missing=missing, evidence="canonical_metrics.card_usage.time_policy"))

    risk = [
        ("기업별 총한도와 잔여한도 알려줘", ["기준년월"]),
        ("그 회사의 한도소진율 알려줘", ["기업명", "기준년월"]),
        ("연체회원 수 알려줘", ["기간_시작", "기간_종료"]),
        ("특수채권 편입원금 알려줘", ["기간_시작", "기간_종료"]),
        ("기대대손충당금 알려줘", ["기준년월"]),
        ("도미노피자 가맹점주 대손율 알려줘", ["기준년월"]),
        ("기업의 대손율 알려줘", ["대손지표", "기준년월"]),
        ("한도 사용률 알려줘", ["집계grain", "기준년월"]),
        ("연체 현황 보여줘", ["대상기업", "기간_시작", "기간_종료"]),
        ("기업 리스크를 요약해줘", ["리스크지표", "기간_시작", "기간_종료"]),
        ("기업별 총여신한도를 알려줘", ["기준년월"]),
        ("기업별 잔여한도를 보여줘", ["기준년월"]),
        ("회원별 기대대손충당금을 알려줘", ["기준년월"]),
        ("기업별 한도소진율 알려줘", ["기준년월"]),
    ]
    for question, missing in risk:
        reason = "ambiguous_metric" if any(item in {"대손지표", "리스크지표"} for item in missing) else "ambiguous_dimension" if "집계grain" in missing else "missing_time"
        cases.append(_boundary_case(question, domain="credit_risk", action="clarify", source_id="credit_risk_required_parameter", reason=reason, missing=missing, evidence="canonical_metrics.credit_risk.semantic_cautions"))

    if len(cases) != 60:
        raise ValueError(f"expected 60 clarify cases, found {len(cases)}")
    return cases


def _unsupported_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    groups = [
        (
            "personal_service_scope",
            "personal_service_scope",
            "service_scope.enterprise_only",
            [
                "개인 신용카드 추천해줘",
                "개인 체크카드 혜택을 비교해줘",
                "연회비가 저렴한 개인카드 상품 알려줘",
                "개인 신용카드 포인트 적립률 알려줘",
                "개인카드 할부 수수료 알려줘",
                "개인회원 카드 발급 조건 알려줘",
                "해외 결제 혜택이 좋은 개인카드 추천해줘",
                "교통 할인이 되는 개인카드 알려줘",
                "개인카드 결제일 변경 방법 알려줘",
                "개인카드 분실 신고 방법 알려줘",
            ],
        ),
        (
            "unknown_question",
            "unknown_semantics",
            "semantic_layer.unknown_semantics",
            [
                "알파지수 7단계 현황 알려줘",
                "제타 등급별 네뷸라 값을 보여줘",
                "카드 플럭스 점수를 계산해줘",
                "오로라 코드 99의 월별 변화를 알려줘",
                "QX 지표별 타깃 레벨을 보여줘",
                "블루문 계수가 높은 대상을 알려줘",
                "미정의 코드 ZZ의 실적을 집계해줘",
                "영업 파동값의 분기별 추이를 알려줘",
                "가맹점 카르마 등급별 매출을 보여줘",
                "기업 세라핀 지표를 월별로 계산해줘",
            ],
        ),
        (
            "restricted_personal_information",
            "restricted_pii",
            "semantic_layer.restricted_columns",
            [
                "한빛테크 대표자의 주민등록번호 알려줘",
                "새롬유통 담당자의 개인 휴대전화번호 알려줘",
                "미래산업 대표자의 집 주소를 보여줘",
                "도미노피자 가맹점주의 개인 이메일 주소 알려줘",
                "파파존스 점주의 은행 계좌번호 알려줘",
                "교촌치킨 가맹점주의 주민등록번호를 보여줘",
                "한빛테크 대표자의 생년월일과 전화번호 알려줘",
                "새롬유통 임직원의 개인 연락처 목록을 뽑아줘",
                "미래산업 직원들의 개인 이메일 주소를 보여줘",
                "스타벅스 가맹점주의 상세 자택 주소 알려줘",
            ],
        ),
        (
            "unrelated_question",
            "out_of_scope",
            "service_scope.enterprise_sales_data",
            [
                "오늘 서울 날씨 알려줘",
                "오늘 점심 메뉴 추천해줘",
                "김치찌개 레시피 알려줘",
                "KBO 야구 순위 알려줘",
                "제주도 2박 3일 여행 일정 짜줘",
                "이 영어 문장을 한국어로 번역해줘",
                "파이썬에서 리스트 정렬하는 방법 알려줘",
                "단기 투자할 주식 종목 추천해줘",
                "최신 영화 추천해줘",
                "초보자용 주 3회 운동 루틴 짜줘",
            ],
        ),
    ]
    for source_id, reason, evidence, questions in groups:
        for question in questions:
            cases.append(
                _boundary_case(
                    question,
                    domain=None,
                    action="unsupported",
                    source_id=source_id,
                    reason=reason,
                    evidence=evidence,
                )
            )

    if len(cases) != 40:
        raise ValueError(f"expected 40 unsupported cases, found {len(cases)}")
    return cases


def build_cases() -> list[dict[str, Any]]:
    cases = [*_core_cases(), *_composition_cases(), *_clarify_cases(), *_unsupported_cases()]
    if len(cases) != 1000:
        raise ValueError(f"expected 1,000 cases, found {len(cases)}")

    seen: dict[str, int] = {}
    version = str(SCHEMA["semantic_layer_metadata"]["version"])
    result: list[dict[str, Any]] = []
    for index, case in enumerate(cases, 1):
        question = str(case["question_ko"])
        source_id = str(case["source"]["id"])
        if case["expected_action"] == "unsupported" and case["domain"] is not None:
            raise ValueError(f"unsupported case must not claim a business domain: {question}")
        if str(case["domain"]) in EXCLUDED_DOMAINS:
            raise ValueError(f"excluded domain leaked into golden set: {case['domain']}")
        if source_id in EXCLUDED_CORE_CONTRACTS | EXCLUDED_COMPOSITION_SOURCES:
            raise ValueError(f"excluded source leaked into golden set: {source_id}")
        if _PUBLIC_ENTERPRISE_RE.search(question):
            raise ValueError(f"public-enterprise question leaked into golden set: {question}")
        if _PERSONAL_SCOPE_RE.search(question):
            raise ValueError(f"personal managed-scope question leaked into golden set: {question}")
        if _PRODUCT_VALID_CARD_COUNT_RE.search(question):
            raise ValueError(f"product valid-card-count question leaked into golden set: {question}")
        if _NUMBERED_SALES_TARGET_RE.search(question):
            raise ValueError(f"numbered sales-target question leaked into golden set: {question}")
        if _BROKEN_QUESTION_RE.search(question):
            raise ValueError(f"known malformed Korean leaked into golden set: {question}")
        if _MIXED_REGISTER_RE.search(question):
            raise ValueError(f"mixed speech levels leaked into golden set: {question}")
        normalized = _normalized_question(question)
        if normalized in seen:
            raise ValueError(f"duplicate normalized question at {seen[normalized]} and {index}: {case['question_ko']}")
        seen[normalized] = index
        result.append(
            {
                "id": f"gs-{index:06d}",
                "semantic_layer_version": version,
                **case,
            }
        )
    return result


def _serialize(cases: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(case, ensure_ascii=False, separators=(",", ":")) + "\n" for case in cases)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail when the checked-in JSONL differs")
    args = parser.parse_args()

    cases = build_cases()
    rendered = _serialize(cases)
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != rendered:
            print(f"golden set is missing or stale: {args.output}")
            return 1
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")

    counts = Counter(case["expected_action"] for case in cases)
    buckets = Counter(case["labels"]["bucket"] for case in cases)
    print(f"golden set: {len(cases)} cases -> {args.output}")
    print("actions:", dict(sorted(counts.items())))
    print("buckets:", dict(sorted(buckets.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

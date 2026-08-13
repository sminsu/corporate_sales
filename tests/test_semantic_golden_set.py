from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

from scripts.generate_semantic_golden_set import DEFAULT_OUTPUT, _has_parameter_evidence
from scripts.run_quality_eval import _actual_from_join_tables
from text2sql_agent.schema import (
    SCHEMA,
    VERIFIED_QUERIES,
    _validate_sql_against_schema,
    semantic_query_contract_candidates,
    source_tables_for_question,
)


def _load_cases(path: Path = DEFAULT_OUTPUT) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _normalized_question(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[^0-9a-z가-힣]+", "", value)


def test_semantic_golden_set_inventory_and_distribution() -> None:
    cases = _load_cases()

    assert len(cases) == 1000
    assert [case["id"] for case in cases] == [f"gs-{index:06d}" for index in range(1, 1001)]
    assert len({_normalized_question(case["question_ko"]) for case in cases}) == 1000
    assert {case["semantic_layer_version"] for case in cases} == {
        SCHEMA["semantic_layer_metadata"]["version"]
    }
    assert Counter(case["expected_action"] for case in cases) == {
        "sql": 900,
        "clarify": 60,
        "unsupported": 40,
    }
    assert Counter(case["labels"]["bucket"] for case in cases) == {
        "core": 600,
        "composition": 300,
        "clarify": 60,
        "unsupported": 40,
    }
    assert Counter(case["domain"] for case in cases) == {
        "corporate_sales_targeting": 241,
        "merchant_sales": 221,
        "customer_card_portfolio": 185,
        "card_usage": 155,
        "credit_risk": 158,
        None: 40,
    }
    unsupported = [case for case in cases if case["expected_action"] == "unsupported"]
    assert Counter(case["source"]["id"] for case in unsupported) == {
        "personal_service_scope": 10,
        "unknown_question": 10,
        "restricted_personal_information": 10,
        "unrelated_question": 10,
    }


def test_semantic_golden_set_excludes_out_of_scope_and_malformed_questions() -> None:
    cases = _load_cases()
    excluded_sources = {
        "card_product_current_valid_corporate_count",
        "valid_card_by_product",
        "managed_usage_anomaly",
        "managed_delinquency",
        "managed_limit_reduction",
        "managed_scope_required",
        "automatic_managed_scope",
    }
    personal_scope = re.compile(
        r"로그인|내\s*계정|내\s*정보|내\s*사번|현재\s*사용자|사용자\s*권한|직원번호|"
        r"세션\s*사용자|조직\s*권한|사용자\s*프로필|담당\s*(?:기업|업체|회사)|"
        r"관리\s*(?:기업|회원)|내가\s*(?:관리|맡은)|내\s*(?:관리기업|관리회원|기업회원|기업들)"
    )
    product_valid_card_count = re.compile(
        r"상품.*유효(?:신용)?\s*카드\s*(?:수|개수|좌수)"
    )
    malformed = re.compile(
        r"고객\s+고객|살아\s+있지만회원|없는인|이용하였했지만|이용(?:\s+이력)?는데|"
        r"줄어든한|문\s+닫은한|리스트을|회사을|업체을|기업를|한도을|은행가|목록를|"
        r"가맹점별의|국내\s+국내|증감률\s+증감률|목록\s+목록|한도한도사용률|"
        r"알려줘\s+알려줘|몇\s*명\s+몇\s*명|최근\s+일년|계좌종류별|"
        r"회원별\s+연체회원|법인회원의\s+유실적\s+업체|"
        r"상반기\s+현재\s+기업규모별|기업규모별\s+월\s+법인카드"
        r"|타켓|네뷰라|네뮤라|네뷐라"
    )

    for case in cases:
        question = case["question_ko"]
        assert case["domain"] != "relationship_sales_management", case["id"]
        assert case["source"]["id"] not in excluded_sources, case["id"]
        assert not re.search(r"공공\s*기업|공기업", question), case["id"]
        assert not personal_scope.search(question), case["id"]
        assert not product_valid_card_count.search(question), case["id"]
        assert not re.search(r"1\s*[~∼-]\s*5(?:번)?\s*영업대상", question), case["id"]
        assert case["source"].get("verified_query") != "corporate_target_industry_usage", case["id"]
        assert not malformed.search(question), case["id"]
        assert not re.search(
            r"(?:질문이야|확인해줘|답해줘|조회해줘|부탁해)\.\s+.*알려주세요",
            question,
        ), case["id"]
        assert not re.search(r"(?<![가-힣])([가-힣]{2,})\s+\1(?![가-힣])", question), case["id"]


def test_semantic_golden_set_references_only_known_semantics() -> None:
    cases = _load_cases()
    domains = {str(item["name"]) for item in SCHEMA["canonical_domains"]}
    contracts = {str(item["name"]): item for item in SCHEMA["semantic_query_contracts"]}
    metrics = {str(item["name"]): item for item in SCHEMA["canonical_metrics"]}
    attributes = {str(item["name"]): item for item in SCHEMA["semantic_attributes"]}
    paths = {str(item["name"]): item for item in SCHEMA["semantic_join_graph"]["safe_paths"]}
    tables = {
        str(item.get("physical_table") or item["name"]).rsplit(".", 1)[-1]
        for item in SCHEMA["tables"]
    }
    queries = {str(item["name"]): item for item in VERIFIED_QUERIES}
    verified_facts: dict[str, tuple[str, set[str], list[str]]] = {}

    for case in cases:
        if case["expected_action"] == "unsupported":
            assert case["domain"] is None, case["id"]
        else:
            assert case["domain"] in domains, case["id"]
        assert set(case["expected_tables"]).issubset(tables), case["id"]
        assert set(case["source"]["metrics"]).issubset(metrics), case["id"]
        assert set(case["source"]["attributes"]).issubset(attributes), case["id"]
        assert set(case["source"]["join_paths"]).issubset(paths), case["id"]

        if case["expected_action"] != "sql":
            assert case["expected_sql"] is None, case["id"]
            if case["expected_action"] == "clarify":
                assert case["expected_missing_parameters"], case["id"]
            continue

        assert case["expected_sql"] is not None, case["id"]
        assert "tbdaaat18" not in case["expected_tables"], case["id"]
        for parameter in case["parameters"]["required"]:
            assert _has_parameter_evidence(case["question_ko"], parameter), (
                case["id"],
                parameter,
                case["question_ko"],
            )

        answer = case["expected_sql"]
        if answer["kind"] == "verified_query":
            query = queries[answer["name"]]
            assert str(query.get("runtime_mode") or "active") != "reference_only", case["id"]
            if answer["name"] not in verified_facts:
                verified_facts[answer["name"]] = (
                    hashlib.sha256(str(query["sql"]).strip().encode("utf-8")).hexdigest(),
                    _actual_from_join_tables(str(query["sql"])),
                    _validate_sql_against_schema(
                        str(query["sql"]), sorted(_actual_from_join_tables(str(query["sql"])))
                    ),
                )
            facts = verified_facts[answer["name"]]
            assert facts[0] == answer["template_sha256"]
            assert facts[1] == set(case["expected_tables"])
            assert not facts[2], case["id"]
        elif answer["kind"] == "semantic_generation":
            contract = contracts[answer["contract"]]
            assert contract.get("execution_mode") == "semantic_generation", case["id"]
            assert sorted(
                source_tables_for_question(contract, case["question_ko"])
            ) == case["expected_tables"]
            if answer.get("reference_query"):
                reference = queries[answer["reference_query"]["name"]]
                assert reference.get("runtime_mode") == "reference_only"
        else:
            assert answer["kind"] == "semantic_spec", case["id"]
            assert set(answer["metric_expressions"]).issubset(metrics), case["id"]
            for path_name in case["source"]["join_paths"]:
                assert paths[path_name]["confidence"] == "curated", (case["id"], path_name)


def test_core_questions_route_to_their_independent_contract_labels() -> None:
    core = [case for case in _load_cases() if case["labels"]["bucket"] == "core"]
    source_counts = Counter(case["source"]["id"] for case in core)
    assert Counter(source_counts.values()) == {16: 30, 24: 5}
    assert len(source_counts) == 35

    for case in core:
        candidates = semantic_query_contract_candidates(
            SCHEMA,
            case["question_ko"],
            max_count=1,
            routing_only=True,
        )
        assert candidates and candidates[0]["name"] == case["source"]["id"], case["id"]

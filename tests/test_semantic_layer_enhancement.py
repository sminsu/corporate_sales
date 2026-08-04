from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from text2sql_agent import schema, workflow


ROOT = Path(__file__).resolve().parents[1]
RAW_SEMANTIC = yaml.safe_load((ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))
COLUMN_SECTIONS = ("dimensions", "measures", "time_dimensions")


def _business_columns(table: dict) -> list[dict]:
    return [column for section in COLUMN_SECTIONS for column in table.get(section, [])]


def _raw_table(name: str) -> dict:
    return next(table for table in RAW_SEMANTIC["tables"] if table["name"] == name)


def test_raw_yaml_has_one_semantic_contract_without_legacy_duplicates() -> None:
    legacy_keys = {"metrics", "relationships", "verified_queries", "llm_semantic_contract"}

    assert legacy_keys.isdisjoint(RAW_SEMANTIC)
    assert {
        "sql_generation_contract",
        "canonical_domains",
        "semantic_entities",
        "canonical_metrics",
        "semantic_join_graph",
        "query_references",
    } <= RAW_SEMANTIC.keys()


def test_business_columns_have_compact_role_specific_metadata() -> None:
    redundant_expr = []
    missing_measure_semantics = []
    missing_time_semantics = []

    for table in RAW_SEMANTIC["tables"]:
        for section in COLUMN_SECTIONS:
            for column in table.get(section, []):
                expr = str(column.get("expr") or "").strip().strip('"')
                if expr and expr.lower() == str(column.get("name") or "").lower():
                    redundant_expr.append(f"{table['name']}.{column.get('name')}")
        for measure in table.get("measures", []):
            if not measure.get("aggregation") or not measure.get("unit"):
                missing_measure_semantics.append(f"{table['name']}.{measure.get('name')}")
        for time_column in table.get("time_dimensions", []):
            if not time_column.get("format") or not time_column.get("role"):
                missing_time_semantics.append(f"{table['name']}.{time_column.get('name')}")

    assert redundant_expr == []
    assert missing_measure_semantics == []
    assert missing_time_semantics == []


def test_source_inventory_matches_every_physical_business_column() -> None:
    inventory = RAW_SEMANTIC["semantic_layer_metadata"]["source_inventory"]
    physical_tables = [
        table
        for table in RAW_SEMANTIC["tables"]
        if table.get("relation_type") != "request_scoped_values_cte"
    ]
    assert inventory == {
        "physical_tables": 31,
        "logical_relations": 0,
        "source_columns": 2140,
        "queryable_business_columns": 2068,
        "excluded_technical_columns": 72,
    }
    assert len(physical_tables) == inventory["physical_tables"]
    assert all(table.get("relation_type") != "request_scoped_values_cte" for table in RAW_SEMANTIC["tables"])

    source_total = 0
    business_total = 0
    excluded_total = 0
    count_issues = []
    for table in physical_tables:
        metadata = table.get("source_metadata") or {}
        actual_business_count = len(_business_columns(table))
        excluded = metadata.get("excluded_technical_columns") or []
        source_count = metadata.get("source_column_count")
        queryable_count = metadata.get("queryable_column_count")
        if queryable_count != actual_business_count or source_count != actual_business_count + len(excluded):
            count_issues.append(
                {
                    "table": table["name"],
                    "actual_business": actual_business_count,
                    "metadata_business": queryable_count,
                    "metadata_source": source_count,
                    "excluded": len(excluded),
                }
            )
        source_total += int(source_count or 0)
        business_total += actual_business_count
        excluded_total += len(excluded)

    assert count_issues == []
    assert source_total == inventory["source_columns"]
    assert business_total == inventory["queryable_business_columns"]
    assert excluded_total == inventory["excluded_technical_columns"]
    assert source_total == business_total + excluded_total


@pytest.mark.parametrize(
    (
        "attribute_name",
        "column_name",
        "expected_tables",
        "expected_count",
        "sample_code",
        "sample_label",
    ),
    [
        ("occupation_type", "직업유형구분코드", {"tbdaaat01", "tmdaa1d01"}, 9, "7", "주부"),
        ("stock_listing_status", "주식상장비상장구분코드", {"tbdaaat01", "tmdaa1d01"}, 2, "2", "비상장"),
        ("customer_industry_classification", "업종구분코드", {"tbdaaat01", "tmdaa1d01"}, 58, "05", "주부"),
        ("head_office_classification", "본사구분코드", {"tbdaaat01", "tmdaa1d01"}, 2, "1", "지점"),
        ("corporate_legal_form", "기업형태구분코드", {"tbdaaat01", "tmdaa1d01"}, 12, "01", "주식회사"),
        ("branch_classification", "부점구분코드", {"tbdaacb02"}, 27, "03", "지점"),
        ("branch_store_type", "점포유형구분코드", {"tbdaacb02"}, 6, "3", "직장인 특화점포(오피스 밀집지역)"),
        ("branch_holding_company", "지주회사구분코드", {"tbdaacb02"}, 22, "01", "은행"),
    ],
)
def test_user_provided_detail_codebooks_are_bound_to_real_columns(
    attribute_name: str,
    column_name: str,
    expected_tables: set[str],
    expected_count: int,
    sample_code: str,
    sample_label: str,
) -> None:
    attribute = next(
        item for item in RAW_SEMANTIC["semantic_attributes"] if item["name"] == attribute_name
    )
    mapped_tables = {mapping["table"] for mapping in attribute["source_mappings"]}
    mapped_columns = {
        (mapping["table"], mapping["column"])
        for mapping in attribute["source_mappings"]
    }

    assert attribute["parameter_name"] == column_name
    assert attribute["codebook_status"] == "verified"
    assert attribute["codebook_validity"]["valid_to"] == "9999-12-31"
    assert attribute["value_semantics_provenance"] == "user_provided_business_codebook"
    assert len(attribute["value_semantics"]) == expected_count
    assert attribute["value_semantics"][sample_code] == sample_label
    assert all(isinstance(code, str) for code in attribute["value_semantics"])
    assert mapped_tables == expected_tables
    assert mapped_columns == {(table, column_name) for table in expected_tables}
    if expected_tables == {"tbdaaat01", "tmdaa1d01"}:
        roles = {mapping["table"]: mapping["role"] for mapping in attribute["source_mappings"]}
        assert roles["tbdaaat01"].startswith("current_")
        assert roles["tmdaa1d01"].startswith("monthly_")

    for table_name in expected_tables:
        column = next(
            item for item in _business_columns(_raw_table(table_name)) if item["name"] == column_name
        )
        assert column["codebook_ref"] == attribute_name

    assert schema.resolve_semantic_attribute_value(
        schema.SCHEMA,
        attribute_name,
        f"{column_name} {sample_label}",
    ) == sample_code
    context = schema.build_semantic_attributes_summary(
        schema.SCHEMA,
        f"{sample_label} {column_name}",
    )
    assert column_name in context
    assert f'"{sample_code}"' in context


def test_detail_codebooks_match_the_complete_verified_content() -> None:
    expected_hashes = {
        "occupation_type": "b0b1196c91980647ce63b11fd4d9901eb6acab1587744a167135f7d47c5e82c5",
        "stock_listing_status": "6cf7a35e7d365a60e1c150492bf05c400f5db63c836f6d9626c58b5d665118a6",
        "customer_industry_classification": "b075b9c161c09d239f5905a048b0f1eb3688eceb8c2b2a8681f87c49b0965528",
        "head_office_classification": "5fa828bad8a2e7171d7d54582ff638dc31576bec4ab44e64dc2fa8d3d60fe103",
        "corporate_legal_form": "2b96fdff0e00aa453f6b49d21fc7cb17acb4194c2ca4d5aeef9e6f332a9cdd68",
        "branch_classification": "0895b2e24201c0494d23fa8d415ae325a42e8964c1bca8b450cdc0565abbbbdb",
        "branch_store_type": "4e0a086b9268fcd85d67f68655c3ab7355d41f52bd32ea6831640e4764173da4",
        "branch_holding_company": "583312b64e50c9e357ad853d9872de0b359d1eddf16f949547b078f31fe45368",
    }
    attributes = {item["name"]: item for item in RAW_SEMANTIC["semantic_attributes"]}

    actual_hashes = {}
    for name in expected_hashes:
        attribute = attributes[name]
        payload = {
            "values": attribute["value_semantics"],
            "validity": attribute["codebook_validity"],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        actual_hashes[name] = hashlib.sha256(canonical).hexdigest()

    assert actual_hashes == expected_hashes


@pytest.mark.parametrize(
    ("question", "expected_table", "wrong_snapshot"),
    [
        ("주식회사 고객 목록", "tbdaaat01", "tmdaa1d01"),
        ("2025년 6월 주식회사 고객 목록", "tmdaa1d01", "tbdaaat01"),
        ("2025년 6월 기준 최신 주식회사 고객 목록", "tmdaa1d01", "tbdaaat01"),
        ("현재 주식회사 고객 목록", "tbdaaat01", "tmdaa1d01"),
        ("2025년 이용 고객 중 현재 기업형태가 주식회사", "tbdaaat01", "tmdaa1d01"),
        ("2025년 6월 현재 기업형태가 주식회사 고객", "tmdaa1d01", "tbdaaat01"),
        ("기업형태를 기준으로 주식회사 고객 목록", "tbdaaat01", "tmdaa1d01"),
    ],
)
def test_customer_detail_codebooks_choose_the_right_snapshot(
    question: str,
    expected_table: str,
    wrong_snapshot: str,
) -> None:
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == expected_table
    assert wrong_snapshot not in ranked


def test_verified_codebook_metadata_and_reference_reach_the_prompt() -> None:
    context = schema.build_semantic_attributes_summary(
        schema.SCHEMA,
        "2025년 6월 주식회사 기업형태 고객 목록",
    )
    details = workflow._table_details(
        ["tbdaaat01"],
        "주식회사 기업형태구분코드",
        max_columns=48,
    )

    assert "codebook_status: verified" in context
    assert "value_semantics_provenance: user_provided_business_codebook" in context
    assert '"default_valid_from": "2018-05-28"' in context
    assert '"period_role_prefix": "monthly_"' in context
    assert "codebook_ref=corporate_legal_form" in details


def test_company_own_industry_does_not_retrieve_the_legacy_customer_codebook() -> None:
    candidates = schema.semantic_attribute_candidates(
        schema.SCHEMA,
        "기업업종별 카드 이용금액",
        domain_name="card_usage",
        max_count=12,
    )

    assert "customer_industry_classification" not in {
        item["name"] for item in candidates
    }
    assert workflow._rule_rank_tables("기업업종별 카드 이용금액")[0] == "tbdaabt30"


def test_temporal_cautions_do_not_retrieve_unrelated_attributes() -> None:
    candidates = schema.semantic_attribute_candidates(
        schema.SCHEMA,
        "2025년 당시 주식회사 고객 목록",
        max_count=12,
    )

    assert "corporate_legal_form" in {item["name"] for item in candidates}
    assert "enterprise_size" not in {item["name"] for item in candidates}
    assert workflow._rule_rank_tables("2025년 당시 주식회사 고객 목록")[0] == "tmdaa1d01"
    assert "tbdaaat01" not in workflow._rule_rank_tables("2025년 당시 주식회사 고객 목록")


def test_inflected_generic_words_do_not_retrieve_unrelated_attributes() -> None:
    candidates = schema.semantic_attribute_candidates(
        schema.SCHEMA,
        "고객의 현재 주식회사 목록",
        max_count=12,
    )

    assert [item["name"] for item in candidates] == ["corporate_legal_form"]
    assert schema.semantic_attribute_candidates(
        schema.SCHEMA,
        "고객의 카드 이용금액",
        max_count=12,
    ) == []
    assert schema.semantic_attribute_candidates(
        schema.SCHEMA,
        "법인 고객의 이용금액",
        max_count=12,
    ) == []


def test_business_number_is_not_mistaken_for_a_historical_period() -> None:
    question = "사업자번호 2012345678인 대기업 고객 목록"

    assert not schema._has_historical_period_expression(question)
    assert workflow._rule_rank_tables(question)[0] == "tbdaaat01"


@pytest.mark.parametrize(
    "question",
    ["2025년 당시 대기업 고객 목록", "2025년 6월 대기업 고객 목록"],
)
def test_historical_enterprise_size_list_uses_monthly_snapshot(question: str) -> None:
    contracts = {
        item["name"]
        for item in schema.semantic_query_contract_candidates(
            schema.SCHEMA,
            question,
            max_count=6,
        )
    }
    ranked = workflow._rule_rank_tables(question)

    assert "current_enterprise_size_customer_list" not in contracts
    assert "historical_enterprise_size_customer_list" in contracts
    assert ranked[0] == "tmdaa1d01"
    assert "tbdaaat01" not in ranked
    assert ranked == ["tmdaa1d01"]


def test_analyze_question_rejects_llm_alternate_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "_call_llm", lambda *_args, **_kwargs: "tbdaaat01")
    question = "2025년 6월 주식회사 고객 목록"

    catalog = workflow._compact_table_catalog(
        workflow._rule_rank_tables(question),
        workflow._attribute_snapshot_exclusions(question),
    )
    result = workflow.analyze_question(
        {
            "question": question,
            "selected_domain": "customer_card_portfolio",
            "domain_context": "테스트 도메인",
            "domain_routing_trace": "테스트 라우팅",
        }
    )

    assert "tmdaa1d01" in catalog
    assert not any(line.startswith("- tbdaaat01 (") for line in catalog.splitlines())
    assert result["selected_tables"][0] == "tmdaa1d01"
    assert "tbdaaat01" not in result["selected_tables"]


def test_analyze_question_does_not_let_llm_override_authoritative_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        workflow,
        "_call_llm",
        lambda *_args, **_kwargs: pytest.fail("authoritative selection must bypass the LLM"),
    )

    result = workflow.analyze_question(
        {
            "question": "2026년 대기업 기업카드 이용금액 뽑아줘",
            "selected_domain": "card_usage",
            "domain_context": "테스트 도메인",
            "domain_routing_trace": "테스트 라우팅",
        }
    )

    assert result["selected_tables"] == ["tbdaaat01", "tmdaa3e16"]


@pytest.mark.parametrize(
    ("label", "question_label"),
    [("주부", "주부"), ("주부", "주부인"), ("지점", "지점"), ("은행", "은행"), ("기타", "기타")],
)
def test_bare_duplicate_code_label_requests_the_classification_axis(
    label: str,
    question_label: str,
) -> None:
    result = workflow.check_sql_gen_params(
        {
            "question": f"{question_label} 고객 목록",
            "query_frame": {"entities": [{"type": "customer"}]},
        }
    )

    assert result["param_stage"] == "need_params"
    assert [item["name"] for item in result["missing_params"]] == [f"{label}분류축"]


def test_explicit_duplicate_code_axis_does_not_request_clarification() -> None:
    result = workflow.check_sql_gen_params(
        {"question": "직업유형 주부 고객 목록"}
    )

    assert result == {"missing_params": [], "param_stage": "done"}


def test_standalone_duplicate_still_clarifies_next_to_a_longer_label() -> None:
    result = workflow.check_sql_gen_params(
        {"question": "기타 고객과 기타 제조업 고객 목록"}
    )

    assert [item["name"] for item in result["missing_params"]] == ["기타분류축"]


@pytest.mark.parametrize(
    ("label", "attribute_names"),
    [
        ("주부", ["occupation_type", "customer_industry_classification"]),
        ("지점", ["head_office_classification", "branch_classification"]),
        ("은행", ["customer_industry_classification", "branch_holding_company"]),
        ("기타", ["enterprise_size", "corporate_legal_form"]),
    ],
)
def test_bare_duplicate_labels_do_not_guess_a_code(
    label: str,
    attribute_names: list[str],
) -> None:
    assert all(
        schema.resolve_semantic_attribute_value(schema.SCHEMA, name, label) == ""
        for name in attribute_names
    )


@pytest.mark.parametrize(
    ("question", "target", "expected_code", "competitor"),
    [
        ("직업유형 주부 고객", "occupation_type", "7", "customer_industry_classification"),
        ("고객업종구분 주부 고객", "customer_industry_classification", "05", "occupation_type"),
        ("고객 본사구분 지점", "head_office_classification", "1", "branch_classification"),
        ("부점구분 지점", "branch_classification", "03", "head_office_classification"),
        ("고객업종구분 은행", "customer_industry_classification", "14", "branch_holding_company"),
        ("부점 계열사구분 은행", "branch_holding_company", "01", "customer_industry_classification"),
    ],
)
def test_duplicate_labels_resolve_with_an_explicit_attribute_cue(
    question: str,
    target: str,
    expected_code: str,
    competitor: str,
) -> None:
    assert schema.resolve_semantic_attribute_value(schema.SCHEMA, target, question) == expected_code
    assert schema.resolve_semantic_attribute_value(schema.SCHEMA, competitor, question) == ""


@pytest.mark.parametrize(
    ("question", "expected_table"),
    [
        ("도/소매업 업종구분코드 고객 목록", "tbdaaat01"),
        ("비상장 법인 고객 목록", "tbdaaat01"),
        ("기업형복합점 부점 목록", "tbdaacb02"),
        ("KB금융지주 부점 목록", "tbdaacb02"),
    ],
)
def test_detail_codebooks_prioritize_their_mapped_table(
    question: str,
    expected_table: str,
) -> None:
    assert workflow._rule_rank_tables(question)[0] == expected_table


@pytest.mark.parametrize(
    "question",
    [
        "급여생활자(대기업이상) 고객 목록",
        "대기업금융지점 부점 목록",
        "기타제조업 고객 목록",
        "개인/가계서비스업 고객 목록",
        "기타 계열사 부점 목록",
        "2026년 급여생활자(대기업이상) 고객의 기업카드 이용금액",
    ],
)
def test_longer_foreign_code_labels_do_not_trigger_enterprise_size_contracts(
    question: str,
) -> None:
    assert schema.resolve_semantic_attribute_value(schema.SCHEMA, "enterprise_size", question) == ""
    contract_names = {
        contract["name"]
        for contract in schema.semantic_query_contract_candidates(
            schema.SCHEMA,
            question,
            max_count=6,
        )
    }
    assert {
        "enterprise_size_corporate_card_usage",
        "current_enterprise_size_customer_list",
    }.isdisjoint(contract_names)


def test_explicit_enterprise_size_survives_a_longer_foreign_code_label() -> None:
    question = "대기업 중 기타제조업 고객 목록"

    assert schema.resolve_semantic_attribute_value(
        schema.SCHEMA,
        "enterprise_size",
        question,
    ) == "1"


@pytest.mark.parametrize(
    "question",
    ["기업형태 기타 고객 목록", "기타 기업형태 고객 목록"],
)
def test_equal_code_labels_follow_the_explicit_attribute_cue(question: str) -> None:
    assert schema.resolve_semantic_attribute_value(
        schema.SCHEMA,
        "corporate_legal_form",
        question,
    ) == "99"
    assert schema.resolve_semantic_attribute_value(schema.SCHEMA, "enterprise_size", question) == ""
    assert "current_enterprise_size_customer_list" not in {
        contract["name"]
        for contract in schema.semantic_query_contract_candidates(
            schema.SCHEMA,
            question,
            max_count=6,
        )
    }


def test_explicit_enterprise_size_wins_the_equal_other_label() -> None:
    question = "기업규모 기타 고객 목록"

    assert schema.resolve_semantic_attribute_value(schema.SCHEMA, "enterprise_size", question) == "9"
    assert schema.resolve_semantic_attribute_value(
        schema.SCHEMA,
        "corporate_legal_form",
        question,
    ) == ""


def test_encrypted_customer_table_matches_source_contract_and_stays_restricted() -> None:
    table = _raw_table("tbdaaat18")
    columns = {column["name"]: column for column in _business_columns(table)}
    expected_physical_types = {
        "고객식별자": "VARCHAR2(10)",
        "고객관리번호": "VARCHAR2(5)",
        "실적기준년월일": "VARCHAR2(8)",
        "회원일련번호": "VARCHAR2(8)",
        "고객고유번호평문길이": "NUMBER(3)",
        "고객고유번호": "VARCHAR2(48)",
        "주민등록번호": "VARCHAR2(24)",
        "주민사업자등록번호": "VARCHAR2(24)",
        "고객명": "VARCHAR2(88)",
        "영문고객명": "VARCHAR2(88)",
        "세대주명": "VARCHAR2(88)",
        "고객전자주소": "VARCHAR2(176)",
        "휴대폰번호": "VARCHAR2(48)",
        "고객대표전화지역번호": "VARCHAR2(24)",
        "고객대표전화번호": "VARCHAR2(48)",
        "고객자택전화번호": "VARCHAR2(48)",
        "고객직장전화번호": "VARCHAR2(48)",
        "자택상세주소": "VARCHAR2(216)",
        "직장상세주소": "VARCHAR2(216)",
        "고객대표상세주소": "VARCHAR2(216)",
        "계좌번호": "VARCHAR2(48)",
        "BC카드결제계좌번호": "VARCHAR2(48)",
    }

    assert table["korean_name"] == "고객회원카드 (고객암호화정보)"
    assert table["primary_key"] == ["고객식별자", "고객관리번호"]
    assert table["semantic_visibility"] == "restricted"
    assert table["data_classification"] == "restricted_personal_information"
    assert set(columns) == set(expected_physical_types)
    assert {
        name: column["physical_data_type"]
        for name, column in columns.items()
    } == expected_physical_types
    assert columns["고객고유번호평문길이"]["default"] == 0
    assert columns["고객고유번호평문길이"] in table["dimensions"]
    assert table.get("measures", []) == []
    assert next(
        column for column in table["time_dimensions"] if column["name"] == "실적기준년월일"
    )["role"] == "snapshot_date"
    assert table["entity_occurrence_rules"] == {
        "insert": "신규고객 생성 시 적재",
        "update": "주소·연락처·결제 관련 사항 등 변동 시 적재",
        "delete": "해당 없음",
    }
    assert {
        "주민등록번호",
        "고객전자주소",
        "휴대폰번호",
        "고객대표상세주소",
        "계좌번호",
        "BC카드결제계좌번호",
    } <= set(table["restricted_columns"])


def test_monthly_corporate_customer_table_matches_excel_contract() -> None:
    table = _raw_table("tmdaa1d12")
    columns = {column["name"]: column for column in _business_columns(table)}
    technical_columns = {
        "시스템최종갱신일시",
        "시스템최종갱신식별자",
        "시스템최종거래일시",
        "ADW시스템최종갱신일시",
    }

    assert table["korean_name"] == "월기업고객기본"
    assert table["primary_key"] == ["기준년월", "기준년월일", "고객식별자"]
    assert table["grain"].startswith("기준년월 × 기준년월일 × 고객식별자")
    assert len(columns) == 75
    assert technical_columns.isdisjoint(columns)
    assert all(column.get("physical_data_type") for column in columns.values())
    assert {
        "기준년월": "VARCHAR2(6)",
        "기준년월일": "VARCHAR2(8)",
        "고객식별자": "VARCHAR2(10)",
        "기업총한도금액": "NUMBER(15)",
        "유효기업신용카드수": "NUMBER(7)",
        "금월신용카드이용금액": "NUMBER(15)",
    } == {
        name: columns[name]["physical_data_type"]
        for name in (
            "기준년월",
            "기준년월일",
            "고객식별자",
            "기업총한도금액",
            "유효기업신용카드수",
            "금월신용카드이용금액",
        )
    }
    assert table["source_metadata"] == {
        "workbook_entity": "DAA월기업고객기본",
        "source_file": "예시.xlsx",
        "source_column_count": 79,
        "queryable_column_count": 75,
        "excluded_technical_columns": [
            "시스템최종갱신일시",
            "시스템최종갱신식별자",
            "시스템최종거래일시",
            "ADW시스템최종갱신일시",
        ],
    }


def test_all_safe_paths_have_valid_endpoints_and_quoted_korean_join_columns() -> None:
    paths = RAW_SEMANTIC["semantic_join_graph"]["safe_paths"]
    entities = {entity["name"]: entity for entity in RAW_SEMANTIC["semantic_entities"]}
    tables = {}
    for table in RAW_SEMANTIC["tables"]:
        for name in (table.get("name"), str(table.get("physical_table") or "").rsplit(".", 1)[-1]):
            if name:
                tables[str(name)] = table

    assert len(paths) == 37
    assert len({path["name"] for path in paths}) == len(paths)

    issues = []
    for path in paths:
        path_name = path["name"]
        endpoint_tables = set()
        for entity_key, table_key in (("from_entity", "from_table"), ("to_entity", "to_table")):
            entity = entities.get(path.get(entity_key))
            if entity is None:
                issues.append(f"{path_name}: unknown {entity_key}={path.get(entity_key)}")
                continue
            entity_table = str(entity.get("physical_table") or "").rsplit(".", 1)[-1]
            declared_table = str(path.get(table_key) or "").rsplit(".", 1)[-1]
            if entity_table != declared_table:
                issues.append(f"{path_name}: {entity_key} table {entity_table} != {declared_table}")
            if declared_table not in tables:
                issues.append(f"{path_name}: unknown endpoint table {declared_table}")
            endpoint_tables.add(declared_table)

        join_sql = str(path.get("sql") or "")
        quoted_refs = re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\.\s*"([^"]+)"', join_sql)
        referenced_tables = {table_name for table_name, _ in quoted_refs}
        if referenced_tables != endpoint_tables:
            issues.append(
                f"{path_name}: join tables {sorted(referenced_tables)} != endpoints {sorted(endpoint_tables)}"
            )
        unquoted_sql = re.sub(r'"[^"]*"', "", join_sql)
        if re.search(r"[가-힣]", unquoted_sql):
            issues.append(f"{path_name}: unquoted Korean identifier in {join_sql}")
        for table_name, column_name in quoted_refs:
            table = tables.get(table_name)
            known_columns = {column["name"] for column in _business_columns(table or {})}
            if column_name not in known_columns:
                issues.append(f"{path_name}: unknown column {table_name}.{column_name}")

    assert issues == []


def test_snapshot_dedup_overlap_and_monthly_sales_policies_are_explicit() -> None:
    customer = _raw_table("tbdaa1d12")
    customer_policy = customer["aggregation_policy"]
    customer_dedup = customer_policy["snapshot_deduplication"]

    assert customer["table_kind"] == "daily_snapshot"
    assert customer_policy["deduplicate_before_aggregation"] is True
    assert customer_dedup["period_expression"] == 'SUBSTR("기준년월일", 1, 6)'
    assert customer_dedup["partition_by"] == ["고객식별자", "파생기준년월"]
    assert customer_dedup["order_by"] == '"기준년월일" DESC'
    assert customer_dedup["keep"] == "ROW_NUMBER() = 1"

    overlap_groups = {group["name"]: group for group in customer_policy["overlap_groups"]}
    card_axes = overlap_groups["card_count_axes"]
    assert set(card_axes["canonical_axis"]) == {"유효기업신용카드수", "유효기업체크카드수"}
    assert set(card_axes["alternate_axis"]) == {"유효기업개별카드수", "유효기업공용카드수"}
    assert "함께 합산하지 않는다" in card_axes["rule"]
    usage = overlap_groups["usage_breakdowns"]
    assert set(usage["canonical_total"]) == {"금월신용카드이용금액", "금월체크카드이용금액"}
    assert {"해외", "KBPay", "국세", "지방세", "4대보험", "일반", "전략"} <= set(
        usage["overlapping_breakdowns"]
    )
    assert "재가산하지 않는다" in usage["rule"]

    merchant_enrichment = _raw_table("tmdaaus01")
    merchant_policy = merchant_enrichment["aggregation_policy"]
    merchant_dedup = merchant_policy["snapshot_deduplication"]
    assert merchant_enrichment["table_kind"] == "monthly_snapshot"
    assert merchant_policy["deduplicate_before_aggregation"] is True
    assert merchant_dedup["partition_by"] == ["기준년월", "가맹점번호"]
    assert merchant_dedup["order_by"] == '"기준년월일" DESC'
    assert merchant_dedup["keep"] == "ROW_NUMBER() = 1"
    forbidden_alias = merchant_policy["forbidden_metric_aliases"]["가맹점총지급금액"]
    assert "월매출" in forbidden_alias and "사용하지 않는다" in forbidden_alias

    monthly_sales = _raw_table("tmdaa5e11")["aggregation_policy"]
    formula = monthly_sales["canonical_formulas"]["가맹점월매출금액"]
    assert "가맹점일시불매출금액" in formula
    assert "가맹점할부매출금액" in formula
    assert "금년가맹점신용판매매출금액" in monthly_sales["forbidden_as_monthly_sales"]


def test_customer_month_enterprise_size_join_requires_dedup_on_both_sides() -> None:
    customer_month = _raw_table("tmdaa1d01")
    dedup = customer_month["aggregation_policy"]["snapshot_deduplication"]

    assert dedup["partition_by"] == ["기준년월", "고객식별자"]
    assert dedup["source_grain"] == "기준년월 × 고객식별자 × 고객관리번호"
    assert 'COUNT(DISTINCT "기업규모구분코드") = 1' == dedup["consistency_guard"]
    assert dedup["canonical_value"] == 'MAX("기업규모구분코드")'

    path = next(
        item
        for item in RAW_SEMANTIC["semantic_join_graph"]["safe_paths"]
        if item["name"] == "customer_monthly_snapshot_to_card_monthly_performance"
    )
    assert path["from_table"] == "tmdaa1d01"
    assert path["to_table"] == "tmdaa3e16"
    assert path["join_type"] == "one_to_many_after_customer_month_dedup"
    assert 'tmdaa1d01."기준년월" = tmdaa3e16."기준년월"' in path["sql"]
    assert 'tmdaa1d01."고객식별자" = tmdaa3e16."고객식별자"' in path["sql"]
    assert "고객×월 이용금액 1행" in path["caution"]

    current_path = next(
        item
        for item in RAW_SEMANTIC["semantic_join_graph"]["safe_paths"]
        if item["name"] == "customer_to_card_monthly_performance"
    )
    assert current_path["from_table"] == "tbdaaat01"
    assert current_path["to_table"] == "tmdaa3e16"
    assert current_path["join_type"] == "one_to_many_after_customer_dedup"
    assert 'tbdaaat01."고객식별자" = tmdaa3e16."고객식별자"' == current_path["sql"]
    assert "고객별 기업규모구분코드가 하나로 일치" in current_path["caution"]


def test_snapshot_canonical_metrics_require_time_and_scope_filters() -> None:
    metrics = {metric["name"]: metric for metric in RAW_SEMANTIC["canonical_metrics"]}
    expected_tokens = {
        "총여신한도금액": ("최신", "기준월 또는 기준일"),
        "관리기업연체원금": ("관리기업목록", "최신", "기준월 또는 기준일"),
        "기대대손충당금": ("기준년월",),
    }

    for metric_name, tokens in expected_tokens.items():
        filters = " ".join(str(value) for value in metrics[metric_name].get("required_filters", []))
        assert filters, metric_name
        for token in tokens:
            assert token in filters, f"{metric_name}: required filter token {token!r} missing from {filters!r}"


def test_table_details_obey_global_budget_and_exclude_runtime_scope_cte() -> None:
    details = workflow._table_details(
        ["tbdaa1d12", "tmdaaus01", "tmdaa5e11", "tbdaabt30"],
        "2026년 4월 고매출 법인가맹점과 기업카드 이용금액",
        max_columns=16,
        max_total_columns=48,
    )
    column_lines = re.findall(r"^  - .* \[(?:차원|지표|시간),", details, flags=re.MULTILINE)

    assert len(column_lines) <= 48
    assert details.count("### ") == 4

    assert workflow._table_details(["managed_scope"], "내가 관리하는 기업 목록") == ""


@pytest.mark.parametrize(
    ("sql", "expected_issue"),
    [
        (
            'SELECT c."고객식별자" FROM {prefix}tbdaaat18 c LIMIT 1',
            "접근이 제한된 테이블",
        ),
        (
            'SELECT m."기업회원이메일주소" FROM {prefix}tsmagcca1 m LIMIT 1',
            "제한된 민감 컬럼",
        ),
        (
            'SELECT m."계좌번호" FROM {prefix}tsmagcca1 m LIMIT 1',
            "제한된 민감 컬럼",
        ),
        (
            'SELECT m."대표카드번호" FROM {prefix}tsmagcca1 m LIMIT 1',
            "제한된 민감 컬럼",
        ),
    ],
)
def test_schema_validator_rejects_restricted_tables_and_columns(sql: str, expected_issue: str) -> None:
    rendered = sql.format(prefix=schema.DB_SCHEMA_PREFIX)

    issues = schema._validate_sql_against_schema(rendered, [])

    assert any(expected_issue in issue for issue in issues), issues


@pytest.mark.parametrize(
    ("question", "domain_name"),
    [
        ("도미노 브랜드 가맹점 수 몇개야?", "merchant_sales"),
        (
            "KB국민 기업카드 보유회원 중에 현재 시점 기준 6개월 무실적인 기업 회원 명단을 알고 싶어",
            "corporate_sales_targeting",
        ),
        (
            "내가 관리하고 있는 기업회원 중 연체 발생한 기업회원을 알려줘",
            "relationship_sales_management",
        ),
    ],
)
def test_representative_retrieval_context_is_small_and_domain_relevant(question: str, domain_name: str) -> None:
    references = schema.find_relevant_references(schema.SCHEMA, question, domain_name=domain_name)
    verified_queries = schema.find_relevant_queries(schema.SCHEMA, question, domain_name=domain_name)

    reference_count = len(re.findall(r"(?m)^- \*\*", references))
    verified_query_count = len(re.findall(r"(?m)^Q:", verified_queries))
    assert 1 <= reference_count <= 2
    assert 1 <= verified_query_count <= 1


def test_domino_sales_reference_does_not_include_unrelated_delinquency_context() -> None:
    references = schema.find_relevant_references(
        schema.SCHEMA,
        "도미노 브랜드 가맹점 수 몇개야?",
        domain_name="merchant_sales",
    )

    assert "연체현황_조회" not in references
    assert "관리기업_연체" not in references
    assert "연체" not in references


def test_merchant_sales_phrase_routes_without_targeting_domain_leakage() -> None:
    result = workflow.route_domain(
        {"question": "2025년 12월 도미노피자 가맹점별 매출액을 높은 순으로 보여줘"}
    )

    assert result["selected_domain"] == "merchant_sales"


def test_brand_card_holder_reference_uses_daily_latest_snapshot_lineage() -> None:
    reference = next(
        item
        for item in RAW_SEMANTIC["query_references"]
        if item["intent"] == "브랜드가맹점_기업카드보유고객"
    )
    verified_query = next(
        item
        for item in schema.SCHEMA["verified_queries"]
        if item["name"] == "brand_merchants_with_corporate_card"
    )

    assert reference["primary_table"] == "tbdaaus01"
    assert "최신 기준년월일" in reference["join_rule"]
    assert re.search(r"\bFROM\s+(?:[A-Za-z_][A-Za-z0-9_]*\.)?tbdaaus01\b", verified_query["sql"])
    assert "ROW_NUMBER() OVER" in verified_query["sql"]

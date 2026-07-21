from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import web_service
from text2sql_agent import workflow
from text2sql_agent.managed_scope import (
    MAX_MANAGED_COMPANIES,
    ManagedScopeParseError,
    normalize_business_number,
    parse_business_number_list,
    parse_managed_scope_upload,
    render_athena_business_number_values,
)
from text2sql_agent.tools.sql_builders import _apply_params_to_vq, _coerce_sql_param
from text2sql_agent.tools.verified_queries import load_external_verified_queries


def _xlsx_bytes(rows: list[list[object]], *, sheet_name: str = "목록") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def test_business_number_normalization_deduplicates_and_preserves_leading_zero() -> None:
    assert normalize_business_number("123-45-67890") == "1234567890"
    assert parse_business_number_list(["001-23-45678", "0012345678", 1234567890]) == [
        "0012345678",
        "1234567890",
    ]
    assert render_athena_business_number_values(["0012345678", "123-45-67890"]) == (
        "('0012345678'), ('1234567890')"
    )


@pytest.mark.parametrize(
    "value",
    [
        "1234567890'); DROP TABLE customer; --",
        ["1234567890", "1 OR 1=1"],
        "12345",
    ],
)
def test_business_number_sql_boundary_rejects_malformed_or_injection_input(value: object) -> None:
    with pytest.raises(ValueError):
        _coerce_sql_param("관리기업목록", value, {"type": "business_number_list"})


def test_business_number_list_enforces_configurable_and_service_limits() -> None:
    with pytest.raises(ManagedScopeParseError, match="최대 2개"):
        parse_business_number_list(["1234567890", "2345678901", "3456789012"], max_items=2)
    assert MAX_MANAGED_COMPANIES == 5_000


def test_txt_upload_supports_cp949_header_and_reports_duplicates() -> None:
    content = "사업자등록번호\n123-45-67890\n1234567890\n234-56-78901\n".encode("cp949")

    parsed = parse_managed_scope_upload("관리기업.txt", content)

    assert parsed["business_numbers"] == ["1234567890", "2345678901"]
    assert parsed["count"] == 2
    assert parsed["duplicates_removed"] == 1


def test_txt_upload_rejects_invalid_rows_instead_of_silently_ignoring_them() -> None:
    content = "사업자등록번호\n123-45-67890\n잘못된값\n".encode()

    with pytest.raises(ManagedScopeParseError, match="잘못된 행"):
        parse_managed_scope_upload("관리기업.txt", content)


def test_excel_upload_finds_header_alias_and_ignores_optional_columns() -> None:
    content = _xlsx_bytes(
        [
            ["안내", "테스트"],
            ["사업자 번호", "메모"],
            ["001-23-45678", "첫 기업"],
            ["1234567890", "둘째 기업"],
            ["123-45-67890", "중복"],
        ],
        sheet_name="관리목록",
    )

    parsed = parse_managed_scope_upload("관리기업.xlsx", content)

    assert parsed["business_numbers"] == ["0012345678", "1234567890"]
    assert parsed["duplicates_removed"] == 1
    assert parsed["source_sheet"] == "관리목록"


def test_delivered_excel_template_round_trips_through_runtime_parser() -> None:
    template = Path(web_service.MANAGED_SCOPE_TEMPLATE_PATH)
    parsed = parse_managed_scope_upload(template.name, template.read_bytes())

    assert template.is_file()
    assert parsed["count"] == 3
    assert parsed["business_numbers"] == ["1234567890", "2345678901", "3456789012"]


def test_upload_parse_and_template_endpoints_work_without_multipart() -> None:
    content = _xlsx_bytes([["사업자등록번호"], ["001-23-45678"], ["123-45-67890"]])
    with TestClient(web_service.app) as client:
        response = client.post(
            "/api/managed-company-scope/parse",
            params={"filename": "관리기업.xlsx"},
            content=content,
            headers={"Content-Type": "application/octet-stream", "X-User-ID": "scope-user"},
        )
        template_response = client.get("/api/managed-company-scope/template")

    assert response.status_code == 200
    assert response.json()["business_numbers"] == ["0012345678", "1234567890"]
    assert template_response.status_code == 200
    assert template_response.content.startswith(b"PK")


def test_pasted_scope_is_parsed_deterministically_and_masked_in_session_message() -> None:
    missing = [
        {
            "name": "관리기업목록",
            "type": "business_number_list",
            "description": "요청별 사업자등록번호",
        }
    ]

    parsed = web_service._natural_params_by_rule("123-45-67890, 234-56-78901", missing)

    assert parsed == {"관리기업목록": ["1234567890", "2345678901"]}
    assert web_service._user_message_text("추가 입력", parsed) == "추가 입력: 관리기업목록 2개"
    assert "1234567890" not in str(web_service._result_conditions({"user_provided_params": parsed}))


def test_managed_scope_accepts_raw_parameter_text_without_file_upload() -> None:
    query = {
        item["name"]: item for item in load_external_verified_queries()
    }["managed_company_delinquency"]
    state = workflow._new_initial_state("2026년 4월 관리기업 중 연체 기업을 알려줘")
    state.update(
        {
            "matched_query_name": query["name"],
            "matched_query_sql": query["sql"],
            "matched_query_params": query["parameters"],
            "user_provided_params": {
                "관리기업목록": "001-23-45678,\n123-45-67890",
                "기준년월": "202604",
            },
        }
    )

    with patch.object(workflow, "_call_llm", return_value="{}"):
        result = workflow.extract_and_apply_params(state)

    assert result["param_stage"] == "done"
    assert result["extracted_params"]["관리기업목록"] == ["0012345678", "1234567890"]
    assert "VALUES ('0012345678'), ('1234567890')" in result["final_sql"]


def test_initial_question_can_supply_scope_and_period_without_a_followup_round_trip() -> None:
    query = {
        item["name"]: item for item in load_external_verified_queries()
    }["managed_company_delinquency"]
    state = workflow._new_initial_state(
        "2026년 4월 관리기업 123-45-67890, 234-56-78901 중 연체 기업을 알려줘"
    )
    state.update(
        {
            "matched_query_name": query["name"],
            "matched_query_sql": query["sql"],
            "matched_query_params": query["parameters"],
        }
    )

    with patch.object(workflow, "_call_llm", return_value="{}"):
        result = workflow.extract_and_apply_params(state)

    assert result["param_stage"] == "done"
    assert result["extracted_params"]["기준년월"] == "202604"
    assert result["extracted_params"]["관리기업목록"] == ["1234567890", "2345678901"]
    assert "VALUES ('1234567890'), ('2345678901')" in result["final_sql"]


@pytest.mark.parametrize(
    "name",
    [
        "managed_company_usage_anomalies",
        "managed_company_delinquency",
        "managed_company_limit_reduction",
    ],
)
def test_managed_company_vq_renders_request_scoped_values_without_physical_list_table(name: str) -> None:
    query = {item["name"]: item for item in load_external_verified_queries()}[name]
    params = {
        "관리기업목록": ["001-23-45678", "1234567890"],
        "기준년월": "202604",
        "limit": 10,
    }

    sql = _apply_params_to_vq(query["sql"], params, name, query["parameters"])

    assert "VALUES ('0012345678'), ('1234567890')" in sql
    assert "list_k120879_tmp" not in sql
    assert "{관리기업목록}" not in sql


def test_verified_management_query_precedes_broader_forced_tool() -> None:
    state = workflow._new_initial_state("내가 관리하고 있는 기업회원 중 한도 감액이 발생한 기업회원을 알려줘")
    selected_vq = {
        "selected_capability_type": "verified_query",
        "selected_capability_name": "managed_company_limit_reduction",
        "selected_tool": "",
        "tool_params": {},
        "matched_query_name": "managed_company_limit_reduction",
        "matched_query_sql": "SELECT 1",
        "matched_query_params": {},
    }
    with (
        patch.object(workflow, "_select_verified_query_capability", return_value=selected_vq),
        patch.object(workflow, "_rule_match_tool", return_value="corporate_limit_low_utilization_members"),
    ):
        selected = workflow.select_tool(state)

    assert selected["matched_query_name"] == "managed_company_limit_reduction"
    assert selected["selected_tool"] == ""


def test_answer_model_prompt_masks_scope_identifiers_in_question_sql_and_rows() -> None:
    captured: dict[str, str] = {}

    def fake_llm(prompt: str, **_: object) -> str:
        captured["prompt"] = prompt
        return "관리기업 조회 결과입니다."

    state = workflow._new_initial_state("123-45-67890 관리기업의 연체를 알려줘")
    state.update(
        {
            "final_sql": 'WITH managed_scope ("사업자등록번호") AS (VALUES (\'1234567890\')) SELECT 1',
            "query_columns": ["사업자등록번호", "기업명"],
            "query_rows": [("1234567890", "테스트기업")],
        }
    )
    with patch.object(workflow, "_call_llm", side_effect=fake_llm):
        result = workflow.generate_answer(state)

    assert result["answer"]
    assert "1234567890" not in captured["prompt"]
    assert "123-45-67890" not in captured["prompt"]
    assert "[사업자등록번호]" in captured["prompt"]

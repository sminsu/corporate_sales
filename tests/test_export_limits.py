from pathlib import Path
from unittest.mock import patch

import yaml
import web_service
import pytest

from text2sql_agent.config import DEFAULT_QUERY_ROW_LIMIT, MAX_QUERY_ROW_LIMIT
from text2sql_agent import workflow
from text2sql_agent.tools.registry import EXCEL_CASE_SQL_TOOLS
from text2sql_agent.tools.sql_builders import _coerce_sql_param


ROOT = Path(__file__).resolve().parents[1]


def test_default_list_limits_are_one_million() -> None:
    verified = yaml.safe_load(
        (ROOT / "text2sql_agent" / "tools" / "sql_verified_queries.yaml").read_text(encoding="utf-8")
    )["verified_queries"]
    legacy_defaults = [
        query["name"]
        for query in verified
        if str((query.get("parameters", {}).get("limit") or {}).get("default")) == "100"
    ]
    semantic = yaml.safe_load((ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))

    assert DEFAULT_QUERY_ROW_LIMIT == 1_000_000
    assert legacy_defaults == []
    assert semantic["sql_generation_contract"]["result_defaults"]["list_limit"] == 1_000_000
    assert all(
        parameter.get("default") != 100
        for tool in EXCEL_CASE_SQL_TOOLS
        for parameter in tool.get("parameters", [])
        if parameter.get("name") == "limit"
    )


def test_explicit_top_n_is_preserved_and_oversized_limits_are_capped() -> None:
    specs = [{"name": "limit", "type": "integer"}]

    assert workflow._extract_params_by_rule("상위 25개를 보여줘", specs)["limit"] == 25
    assert workflow._normalize_params({"limit": 2_000_000}, specs)["limit"] == MAX_QUERY_ROW_LIMIT
    assert _coerce_sql_param("limit", 2_000_000, {"type": "integer"}) == MAX_QUERY_ROW_LIMIT


def test_web_export_reloads_full_rows_only_for_data_formats() -> None:
    stored = {"question": "조회", "query_columns": ["값"], "query_rows": [[1]], "final_sql": "SELECT 1"}
    prepared = {**stored, "query_rows": [[1], [2]]}

    with (
        patch.object(web_service._SESSION_STORE, "get_result", return_value=stored),
        patch.object(web_service.agent, "prepare_export_result", return_value=prepared) as prepare,
        patch.object(web_service.agent, "export_to_word", return_value="/tmp/report.docx"),
        patch.object(web_service.agent, "export_to_excel", return_value="/tmp/report.xlsx") as export_excel,
        patch.object(web_service, "_register_file", return_value={"token": "t", "filename": "report", "url": "api/files/t"}),
    ):
        web_service.export_result(web_service.ExportRequest(result_id="r", format="word"), x_user_id="u")
        prepare.assert_not_called()

        web_service.export_result(web_service.ExportRequest(result_id="r", format="excel"), x_user_id="u")
        prepare.assert_called_once_with(stored)
        export_excel.assert_called_once_with(prepared)


def test_web_data_export_does_not_fall_back_to_preview_on_reload_error() -> None:
    stored = {"query_columns": ["값"], "query_rows": [[1]], "final_sql": "SELECT 1"}
    with (
        patch.object(web_service._SESSION_STORE, "get_result", return_value=stored),
        patch.object(web_service.agent, "prepare_export_result", side_effect=RuntimeError),
        pytest.raises(web_service.HTTPException) as error,
    ):
        web_service.export_result(web_service.ExportRequest(result_id="r", format="csv"), x_user_id="u")

    assert error.value.status_code == 500

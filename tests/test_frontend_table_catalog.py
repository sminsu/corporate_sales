from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

import web_service
from text2sql_agent.schema import build_table_summary
from text2sql_agent.time_policy import accumulation_policy_for, format_accumulation_policy


def test_accumulation_policy_has_a_compact_korean_catalog_summary() -> None:
    policy = {
        "cadence": "previous_day",
        "query_time_dimension": "기준년월일",
        "format": "YYYYMMDD",
        "lag_days": 1,
        "has_reference_month": False,
    }

    assert format_accumulation_policy(policy) == (
        "전일(D-1) · 기간 컬럼 기준년월일(YYYYMMDD) · 기준년월 컬럼 없음"
    )
    summary = build_table_summary(
        {
            "tables": [
                {
                    "name": "example",
                    "description": "테스트",
                    "grain": "1일 1행",
                    "accumulation_policy": policy,
                }
            ]
        }
    )
    assert "accumulation_policy: 전일(D-1)" in summary


def test_table_catalog_api_returns_visible_semantic_tables() -> None:
    expected = [
        table
        for table in web_service.SCHEMA.get("tables", [])
        if web_service._is_semantic_table_visible(table)
    ]

    with TestClient(web_service.app) as client:
        response = client.get("/api/tables")

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == len(expected)
    assert len(payload["tables"]) == len(expected)
    assert payload["semantic_layer_version"]
    assert set(payload["tables"][0]) == {
        "name",
        "physical_table",
        "korean_name",
        "description",
        "grain",
        "accumulation_policy",
    }
    assert all(table["name"] and table["physical_table"] for table in payload["tables"])

    governed = next((table for table in payload["tables"] if table["accumulation_policy"]), None)
    assert governed is not None
    assert governed["accumulation_policy"]["cadence"]
    assert governed["accumulation_policy"]["summary"]


def test_table_period_api_reads_the_range_each_governed_table_actually_holds() -> None:
    """카탈로그는 주기가 아니라 실제 보유 구간을 보여준다. 기간 컬럼이 없는
    마스터 테이블은 읽을 값이 없으므로 응답에서 빠진다."""
    governed = {
        str(table.get("name"))
        for table in web_service.SCHEMA.get("tables", [])
        if web_service._is_semantic_table_visible(table)
        and accumulation_policy_for(str(table.get("name") or ""))
    }
    ungoverned = {
        str(table.get("name"))
        for table in web_service.SCHEMA.get("tables", [])
        if web_service._is_semantic_table_visible(table)
    } - governed

    with patch.object(web_service, "loaded_period_range", return_value=("202401", "202607")):
        with TestClient(web_service.app) as client:
            response = client.get("/api/tables/periods")

    assert response.status_code == 200
    periods = response.json()["periods"]
    assert set(periods) == governed
    assert ungoverned and not (set(periods) & ungoverned)
    assert periods["tmdaa5e11"] == {"min": "202401", "max": "202607"}


def test_table_period_api_reports_unreadable_ranges_as_null() -> None:
    """MIN/MAX 를 못 읽어도 카탈로그는 열려야 한다. 값 없음은 오류가 아니다."""
    with patch.object(web_service, "loaded_period_range", return_value=None):
        with TestClient(web_service.app) as client:
            response = client.get("/api/tables/periods")

    assert response.status_code == 200
    periods = response.json()["periods"]
    assert periods
    assert set(periods.values()) == {None}


def test_frontend_shows_the_loaded_period_range_next_to_each_catalog_toggle() -> None:
    source = web_service.STATIC_DIR.joinpath("index.html").read_text(encoding="utf-8")
    styles = web_service.STATIC_DIR.joinpath("styles.css").read_text(encoding="utf-8")
    period_cell = source.split("function tablePeriodCell(table) {", 1)[1].split("\n}", 1)[0]
    summary_markup = source.split('<details class="table-catalog-item">', 1)[1].split(
        "</summary>", 1
    )[0]

    assert 'const data = await api("api/tables/periods");' in source
    assert "loadTablePeriods();" in source
    assert "${tablePeriodCell(table)}" in summary_markup
    assert summary_markup.index("${tablePeriodCell(table)}") < summary_markup.index(
        'class="table-catalog-toggle"'
    )
    assert "state.tablePeriods[table.name]" in period_cell
    assert "formatPeriodValue(range.min)} ~ ${formatPeriodValue(range.max)" in period_cell
    assert "보유 구간 확인 중" in period_cell
    assert "기간 컬럼 없음" in period_cell
    assert ".table-catalog-period {" in styles


def test_frontend_exposes_table_catalog_in_user_guide_and_tables_command() -> None:
    source = web_service.STATIC_DIR.joinpath("index.html").read_text(encoding="utf-8")
    styles = web_service.STATIC_DIR.joinpath("styles.css").read_text(encoding="utf-8")
    rail_markup = source.split('<nav id="railMenu"', 1)[1].split("</nav>", 1)[0]
    topbar_markup = source.split('<header class="topbar"', 1)[1].split("</header>", 1)[0]
    guide_markup = source.split('<dialog id="userGuideDialog"', 1)[1].split("</dialog>", 1)[0]
    guide_click_handler = source.split("function showUserGuide() {", 1)[1].split("\n}", 1)[0]
    tables_command_handler = source.split("function openTableCatalog() {", 1)[1].split("\n}", 1)[0]

    assert 'id="tableCatalogPanel"' not in rail_markup
    assert "사용 테이블" not in rail_markup
    assert "data-user-guide" in topbar_markup
    assert 'id="tableCatalogPanel"' in guide_markup
    assert "사용 테이블" in guide_markup
    assert 'data-tab="tables"' not in source
    assert 'id="tab-tables"' not in source
    assert 'id="tableCatalogSearch"' in source
    assert 'const data = await api("api/tables");' in source
    assert "table.accumulation_policy?.summary" in source
    assert 'if (question.toLowerCase() === "/tables")' in source
    assert 'if (tableCatalogSearch) tableCatalogSearch.value = "";' in source
    assert "userGuideDialog.showModal();" in guide_click_handler
    assert "loadTableCatalog();" in guide_click_handler
    assert "userGuideDialog.showModal();" in tables_command_handler
    assert "loadTableCatalog();" in tables_command_handler
    assert 'tableCatalogSearch?.focus({ preventScroll: true });' in tables_command_handler
    assert "openTableCatalog();" in source
    assert "grid-template-columns: repeat(4, 1fr);" in styles

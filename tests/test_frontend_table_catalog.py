from __future__ import annotations

from fastapi.testclient import TestClient

import web_service


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
    }
    assert all(table["name"] and table["physical_table"] for table in payload["tables"])


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
    assert 'if (question.toLowerCase() === "/tables")' in source
    assert 'if (tableCatalogSearch) tableCatalogSearch.value = "";' in source
    assert "userGuideDialog.showModal();" in guide_click_handler
    assert "loadTableCatalog();" in guide_click_handler
    assert "userGuideDialog.showModal();" in tables_command_handler
    assert "loadTableCatalog();" in tables_command_handler
    assert 'tableCatalogSearch?.focus({ preventScroll: true });' in tables_command_handler
    assert "openTableCatalog();" in source
    assert "grid-template-columns: repeat(4, 1fr);" in styles

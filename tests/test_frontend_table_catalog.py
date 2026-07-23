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


def test_frontend_exposes_table_tab_search_and_tables_command() -> None:
    source = web_service.STATIC_DIR.joinpath("index.html").read_text(encoding="utf-8")
    styles = web_service.STATIC_DIR.joinpath("styles.css").read_text(encoding="utf-8")

    assert 'data-tab="tables"' in source
    assert 'id="tab-tables"' in source
    assert 'id="tableCatalogSearch"' in source
    assert 'const data = await api("api/tables");' in source
    assert 'if (question.toLowerCase() === "/tables")' in source
    assert 'if (tableCatalogSearch) tableCatalogSearch.value = "";' in source
    assert "openTableCatalog();" in source
    assert "grid-template-columns: repeat(5, 1fr);" in styles

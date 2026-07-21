from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
INDEX_HTML = ROOT / "web" / "static" / "index.html"


def test_managed_scope_is_a_direct_parameter_field_without_upload_controls() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert 'name="${escapeHtml(name)}" data-param-field rows="5"' in source
    assert "사업자등록번호를 줄바꿈이나 쉼표로 입력하세요." in source
    assert "managed-scope-input-hint" in source
    assert "data-managed-scope-file" not in source
    assert "TXT/Excel 파일 선택" not in source
    assert "api/managed-company-scope/parse" not in source
    assert "api/managed-company-scope/template" not in source


def test_managed_scope_is_not_advertised_as_a_semantic_table() -> None:
    semantic = yaml.safe_load((ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))

    assert all(table["name"] != "managed_scope" for table in semantic["tables"])
    assert "runtime_scope_inputs" not in semantic["semantic_layer_metadata"]
    assert "managed_scope" not in (ROOT / "semantic_layer.yaml").read_text(encoding="utf-8")

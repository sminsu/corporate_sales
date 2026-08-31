from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
INDEX_HTML = ROOT / "web" / "static" / "index.html"


def test_managed_scope_uses_an_inline_parameter_form_without_a_popup() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert "사업자등록번호를 줄바꿈이나 쉼표로 입력하세요." in source
    assert "pauseProgressForParams()" in source
    assert 'id="inlineParamForm"' in source
    assert "renderInlineParamForm(params)" in source
    assert "inlineParamDefault(param" in source
    assert "현재 월 기준 기본값" in source
    assert source.index('id="progressPanel"') < source.index('id="inlineParamForm"') < source.index('id="chartBox"')
    assert "display_question: displayQuestion" in source
    assert "hideInlineParamForm();\n  submitQuestion({" in source
    assert "syncQueryModeUi();\n  if (pendingMessage)" in source
    assert 'id="paramPanel"' not in source
    assert "openParamDialog" not in source
    assert "data-managed-scope-file" not in source
    assert "TXT/Excel 파일 선택" not in source
    assert "api/managed-company-scope/parse" not in source
    assert "api/managed-company-scope/template" not in source


def test_managed_scope_is_not_advertised_as_a_semantic_table() -> None:
    semantic = yaml.safe_load((ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))

    assert all(table["name"] != "managed_scope" for table in semantic["tables"])
    assert "runtime_scope_inputs" not in semantic["semantic_layer_metadata"]
    assert "managed_scope" not in (ROOT / "semantic_layer.yaml").read_text(encoding="utf-8")

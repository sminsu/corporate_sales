from __future__ import annotations

from pathlib import Path

import web_service


INDEX_HTML = Path(__file__).parents[1] / "web" / "static" / "index.html"
STYLES_CSS = INDEX_HTML.parent / "styles.css"


def test_stylesheet_path_works_for_file_preview_and_web_server() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert STYLES_CSS.is_file()
    assert 'href="styles.css?v=' in source
    assert 'href="static/styles.css' not in source
    response = web_service.static_fallback("styles.css")
    assert Path(response.path) == STYLES_CSS
    assert response.media_type == "text/css"


def test_examples_use_api_response_without_merging_frontend_defaults() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert "Array.isArray(data?.examples) ? data.examples : []" in source
    assert "new Set([...(data.examples || []), ...DEFAULT_EXAMPLES])" not in source
    assert 'if (!examples.length) throw new Error("예시 질문 응답이 비어 있습니다.")' in source


def test_default_examples_are_only_rendered_in_api_fallback() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    load_examples = source.split("async function loadExamples() {", 1)[1].split(
        "function extractTemplateParameters", 1
    )[0]
    before_catch, fallback = load_examples.split("} catch {", 1)

    assert "DEFAULT_EXAMPLES" not in before_catch
    assert "state.exampleQuestions = normalizedQuestions(DEFAULT_EXAMPLES)" in fallback
    assert "renderPromptSuggestionContent()" in fallback
    assert 'renderExampleButtons(heroExamples, DEFAULT_EXAMPLES.slice(0, 3), "starter-chip")' in fallback
    assert '<div id="heroExamples" class="starter-grid" aria-label="예시 질문"></div>' in source


def test_followup_mode_uses_result_recommendations_instead_of_examples() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    renderer = source.split("function renderPromptSuggestionContent() {", 1)[1].split(
        "async function loadExamples()", 1
    )[0]
    assert 'const isFollowup = state.queryMode === "followup" && canUseFollowupMode()' in renderer
    assert "isFollowup ? state.recommendedQuestions : state.exampleQuestions" in renderer
    assert 'isFollowup ? "추천 질문" : "예시 질문"' in renderer
    assert 'isFollowup ? "example recommendation" : "example"' in renderer

    render_result = source.split("function renderResult(data, context = {}) {", 1)[1].split(
        "async function submitFollowup", 1
    )[0]
    assert "state.recommendedQuestions = normalizedQuestions(data.suggestions)" in render_result
    assert 'state.queryMode = context.mode === "followup" && state.canFollowup ? "followup" : "new"' in render_result

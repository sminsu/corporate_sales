from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import web_service
from text2sql_agent import config as agent_config


INDEX_HTML = Path(__file__).parents[1] / "web" / "static" / "index.html"
STYLES_CSS = INDEX_HTML.parent / "styles.css"
ACCESS_DENIED_HTML = INDEX_HTML.parent / "access-denied.html"


def test_stylesheet_path_works_for_file_preview_and_web_server() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert STYLES_CSS.is_file()
    assert 'href="styles.css"' in source
    assert 'href="static/styles.css' not in source
    response = web_service.static_fallback("styles.css")
    assert Path(response.path) == STYLES_CSS
    assert response.media_type == "text/css"


def test_prefix_path_without_trailing_slash_keeps_relative_assets_working() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    base_script_index = source.index('const base = document.createElement("base")')

    assert base_script_index < source.index("window.location.replace")
    assert base_script_index < source.index('href="styles.css')
    assert 'base.href = `${path}/`' in source
    with TestClient(web_service.app) as client:
        response = client.get("/8080", follow_redirects=False)
        stylesheet = client.get("/8080/styles.css")
        logo = client.get("/8080/kb-logo-transparent.png")
        font = client.get("/8080/fonts/KBfgTextM.ttf")
    assert response.status_code == 200
    assert response.headers.get("location") is None
    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert logo.status_code == 200
    assert logo.headers["content-type"].startswith("image/png")
    assert font.status_code == 200
    assert font.content[:4] == b"\x00\x01\x00\x00"


def test_frontend_uses_allowed_login_id_for_access_and_request_user_id() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    denied = ACCESS_DENIED_HTML.read_text(encoding="utf-8")

    auth_guard = source.split("<title>기업영업지원 에이전트</title>", 1)[1].split(
        "text2sql:console:theme:v2", 1
    )[0]
    assert '.includes(localStorage.getItem("loginID"))' in auth_guard
    assert 'window.location.replace("access-denied.html")' in auth_guard
    assert 'const loginId = String(localStorage.getItem("loginID") || "").trim();' in source
    assert 'if (loginId) return loginId;' in source
    assert '"X-User-ID": WEBAPP_USER_ID' in source
    assert "접속 권한이 없습니다" in denied
    assert Path(web_service.static_fallback("access-denied.html").path) == ACCESS_DENIED_HTML


def test_hero_examples_use_api_response_without_merging_defaults() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert "Array.isArray(data?.examples) ? data.examples : []" in source
    assert "new Set([...(data.examples || []), ...DEFAULT_EXAMPLES])" not in source
    assert 'if (!examples.length) throw new Error("예시 질문 응답이 비어 있습니다.")' in source
    assert '<div id="heroExamples" class="starter-grid" aria-label="예시 질문"></div>' in source


def test_user_guide_opens_from_below_the_hero_description() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")

    description_index = source.index("필요한 기간, 대상, 지표를 자연스럽게 질문하면")
    trigger_index = source.index('id="userGuideBtn"')
    examples_index = source.index('id="heroExamples"')
    assert description_index < trigger_index < examples_index
    assert '<dialog id="userGuideDialog"' in source
    assert 'aria-labelledby="userGuideTitle"' in source
    assert "userGuideDialog.showModal()" in source
    assert 'class="user-guide-actions" method="dialog"' in source
    assert ".user-guide-dialog::backdrop" in css


def test_hero_default_examples_are_only_rendered_in_api_fallback() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    load_examples = source.split("async function loadExamples() {", 1)[1].split(
        "function renderTableCatalog", 1
    )[0]
    before_catch, fallback = load_examples.split("} catch {", 1)

    assert "DEFAULT_EXAMPLES" not in before_catch
    assert 'renderExampleButtons(heroExamples, DEFAULT_EXAMPLES.slice(0, 3), "starter-chip")' in fallback


def test_composer_hides_examples_context_and_new_query_mode() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert 'id="promptSuggestions"' not in source
    assert 'id="contextStrip"' not in source
    assert 'data-query-mode="new"' not in source
    assert "state.queryMode" not in source
    assert 'id="followupInput"' not in source
    assert "주민등록번호 등 고객 민감 정보를 입력하지 않도록 주의해 주세요." in source
    assert 'id="composerHint"' not in source
    assert "Enter 전송" not in source


def test_completed_query_keeps_composer_in_followup_flow() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    submit_handler = source.split("function handleSubmit() {", 1)[1].split(
        'cancelRequestBtn.addEventListener("click", cancelActiveRequest)', 1
    )[0]
    render_result = source.split("function renderResult(data, context = {}) {", 1)[1].split(
        "async function submitFollowup", 1
    )[0]

    assert "else if (state.lastResultId && state.canFollowup)" in submit_handler
    assert "submitFollowup(question" in submit_handler
    assert "data.result_id && !data.error && (data.rows?.length || data.sql" in render_result
    assert "syncQueryModeUi()" in render_result
    assert 'id="submitBtn"' not in source
    assert 'event.key === "Enter" && !event.shiftKey' in source


def test_progress_is_rendered_below_each_question_and_labels_followups() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    css = (INDEX_HTML.parent / "styles.css").read_text(encoding="utf-8")
    start_progress = source.split("function startProgress(", 1)[1].split("function finishProgress", 1)[0]

    assert source.index('id="resultPanel"') < source.index('id="answerBox"') < source.index('id="progressPanel"')
    assert 'id="progressModeLabel"' in source
    assert '"후속 질문 처리 중"' in start_progress
    assert 'status: "pending"' in source
    assert '.filter((message) => message.status !== "pending")' in source
    assert "appendPendingUserMessage(state.activeRequest?.question || state.pendingQuestion)" in start_progress
    assert "function progressContextLines(progress)" in source
    assert "선택 도메인:" in source
    assert "실행 경로:" in source
    assert "후속 처리 경로:" in source
    assert ".result > .progress-panel" in css
    assert ".progress-context" in css


def test_followup_recommendations_are_limited_to_three() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    renderer = source.split("function renderSuggestions(suggestions) {", 1)[1].split(
        "function renderResult", 1
    )[0]

    assert "normalizedQuestions(suggestions).slice(0, 3)" in renderer
    assert agent_config.FOLLOWUP_SUGGESTION_MAX_COUNT <= 3


def test_followup_errors_are_appended_to_the_conversation() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    followup = source.split("async function submitFollowup", 1)[1].split(
        "async function submitQuestion", 1
    )[0]

    assert "state.pendingQuestion = followupQuestion;" in followup
    assert 'showInlineError(message, "모델 연결 오류", { appendMessage: true });' in followup
    assert 'showInlineError(message, "후속 질문 처리 오류", { appendMessage: true });' in followup


def test_error_panel_is_rendered_below_the_conversation_result() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert source.index('id="resultPanel"') < source.index('id="errorPanel"') < source.index('id="queryPanel"')

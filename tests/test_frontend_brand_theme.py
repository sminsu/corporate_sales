from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
INDEX_HTML = ROOT / "web" / "static" / "index.html"
STYLES_CSS = INDEX_HTML.parent / "styles.css"


def test_frontend_uses_requested_branding_without_sidebar_logo() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    sidebar = source.split('<aside class="rail"', 1)[1].split("</aside>", 1)[0]

    assert 'class="brand"' not in source
    assert "kb-logo" not in sidebar
    assert "<h1>기업영업지원 에이전트</h1>" not in source
    assert "<h2>기업영업지원 에이전트</h2>" in source
    assert "<title>기업영업지원 에이전트</title>" in source


def test_frontend_shows_memory_notice_and_resizable_sidebar() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert "긴 대화 시 이전 내용을 기억하지 못할 수 있어요. 필요한 정보는 다시 말씀해 주세요." in source
    assert 'id="railResizeHandle"' in source
    assert 'id="railToggleBtn"' in source
    assert 'aria-label="새 대화"' in source
    assert "<span>새 대화</span>" in source
    assert 'addEventListener("pointerdown"' in source
    assert 'classList.toggle("rail-collapsed"' in source
    assert "grid-template-columns: var(--rail-width" in css
    assert "grid-template-columns: 52px minmax(0, 1fr);" in css
    assert ".app.rail-collapsed .rail-new-session span" in css
    assert "border-bottom: 0;" in css


def test_recent_sessions_are_grouped_by_last_activity() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")

    for label in ("오늘", "이전 7일", "이전 30일"):
        assert f'label: "{label}"' in source
    assert 'label: "최근 3일"' not in source
    assert "session.updated_at || session.created_at" in source
    assert "ageDays <= group.maxDays" in source
    assert ".session-group-label" in css


def test_question_focus_uses_a_black_border_without_blue_outline() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")

    composer_focus = css.split(".composer:focus-within {", 1)[1].split("}", 1)[0]
    question_focus = css.split("#questionInput:focus-visible {", 1)[1].split("}", 1)[0]

    assert "border-color: #1a1a1a;" in composer_focus
    assert "outline: none;" in question_focus
    assert "rgba(88, 205, 188" not in composer_focus


def test_hero_accepts_an_image_and_data_tools_are_high_contrast() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    data_tools = css.split(".rail-tools > summary {", 1)[1].split("}", 1)[0]

    assert '<img src="kb-logo-transparent.png" alt="" />' in source
    assert "background: #ffffff;" in data_tools
    assert "color: #1a1a1a;" in data_tools
    assert 'content: "⌃";' in css
    assert ".hero-badge img {" in css


def test_right_inspector_has_a_close_control_on_every_viewport() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    close_handler = source.split("function closeInspector", 1)[1].split(
        "function syncInspectorViewport", 1
    )[0]

    assert 'id="inspectorClose" class="inspector-close"' in source
    assert 'aria-label="상세 패널 닫기"' in source
    assert 'appEl.classList.add("inspector-collapsed")' in close_handler
    assert 'appEl.classList.remove("inspector-mobile-open")' in close_handler
    assert ".inspector-close {" in css
    assert "display: grid;" in css.split(".inspector-close {", 1)[1].split("}", 1)[0]


def test_result_source_badges_only_appear_in_the_right_inspector() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert '<div class="result-kicker hidden">' in source


def test_completed_progress_steps_collapse_their_details() -> None:
    css = STYLES_CSS.read_text(encoding="utf-8")

    assert ".progress-steps > li.done .progress-card-desc," in css
    collapsed = css.split(".progress-steps > li.done .progress-detail {", 1)[1].split("}", 1)[0]
    assert "display: none;" in collapsed


def test_light_theme_is_default_and_uses_dark_text_on_light_code_panels() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = STYLES_CSS.read_text(encoding="utf-8")
    light_theme = css.split('html[data-theme="light"] {', 1)[1].split("}", 1)[0]

    assert '<html lang="ko" data-theme="light">' in html
    assert 'localStorage.getItem("text2sql:console:theme:v2")' in html
    assert 'const THEME_KEY = "text2sql:console:theme:v2";' in html
    assert 'if (storedTheme === "dark")' in html
    assert "--muted: #1a1a1a;" in light_theme
    assert "--subtle: #1a1a1a;" in light_theme
    assert "--code-bg: #ffffff;" in light_theme
    assert "--code-text: #1a1a1a;" in light_theme

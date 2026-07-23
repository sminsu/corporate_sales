from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
INDEX_HTML = ROOT / "web" / "static" / "index.html"
STYLES_CSS = INDEX_HTML.parent / "styles.css"
LOGO_PNG = INDEX_HTML.parent / "kb-logo.png"


def test_frontend_uses_requested_branding_and_logo_asset() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert LOGO_PNG.is_file()
    assert 'src="kb-logo.png?v=' in source
    assert "<h1>기업영업 &amp; 프랜차이즈 에이전트</h1>" in source
    assert "<title>기업영업 &amp; 프랜차이즈 에이전트</title>" in source
    assert '<div class="brand-mark">KB</div>' not in source


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

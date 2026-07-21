from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "web" / "static" / "index.html"


def test_frontend_reports_stream_correlation_ids_and_missing_terminal_event() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    assert 'response.headers.get("x-stream-request-id")' in source
    assert 'response.headers.get("x-stream-message-id")' in source
    assert 'response.headers.get("x-app-release")' in source
    assert 'error.name = "StreamTransportError"' in source
    assert 'error?.name === "AbortError" || options.signal?.aborted' in source
    assert 'transportError("reader_failed", error)' in source
    assert 'transportError("terminal_event_missing")' in source
    assert "[SSE stream interrupted]" in source
    assert "오류 참조:" in source

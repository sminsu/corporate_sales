"""WebApp-compatible API adapter package."""

from .adapter import adapt_sse_stream, compatible_json_response

__all__ = ["adapt_sse_stream", "compatible_json_response"]

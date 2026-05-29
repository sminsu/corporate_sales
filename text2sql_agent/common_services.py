"""Adapters for the narrow kbcard-agent-common surface used by this app."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

try:
    import kbcard_agent_common as _common_package
    from kbcard_agent_common.embedding import EmbeddingClient, TEIEmbeddingClient
    from kbcard_agent_common.llm import KBCardAsyncOpenAI, KBCardOpenAI
except ModuleNotFoundError:
    _common_package = None
    EmbeddingClient = None
    KBCardAsyncOpenAI = None
    KBCardOpenAI = None
    TEIEmbeddingClient = None


@dataclass(frozen=True)
class TraceContext:
    """Request identifiers kept locally without depending on common observability."""

    message_id: str
    request_id: str
    trace_id: str
    session_id: str | None = None


def common_package_status() -> dict[str, Any]:
    return {
        "installed": _common_package is not None,
        "package": "kbcard_agent_common",
        "version": getattr(_common_package, "__version__", "") if _common_package is not None else "",
    }


def common_feature_status() -> dict[str, Any]:
    return {
        "package": _common_package is not None,
        "llm": KBCardOpenAI is not None,
        "async_llm": KBCardAsyncOpenAI is not None,
        "embedding": EmbeddingClient is not None,
        "tei_embedding": TEIEmbeddingClient is not None,
    }


def common_error_name(exc: BaseException) -> str:
    return type(exc).__name__


def common_http_status(exc: BaseException) -> int:
    error_name = type(exc).__name__
    if error_name == "ConfigurationError":
        return 400
    if error_name == "RetryableProviderError":
        return 503
    if error_name in {"ProviderError", "LLMHTTPError"}:
        return 502
    if error_name == "CapabilityNotSupportedError":
        return 422
    return 500


def create_trace_context(
    *,
    session_id: str | None = None,
    message_id: str | int | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
) -> TraceContext:
    prefix = "text2sql"
    return TraceContext(
        message_id=str(message_id) if message_id else f"{prefix}-msg-{uuid4()}",
        request_id=request_id or f"{prefix}-req-{uuid4()}",
        trace_id=trace_id or f"{prefix}-trace-{uuid4()}",
        session_id=session_id,
    )


def observability_context(*, context: Any, agent_name: str, user_id: str):
    return nullcontext()


def emit_execution_log(
    *,
    context: Any,
    user_id: str,
    agent_name: str,
    status: str,
    total_latency_ms: int,
    question: str,
    result: dict[str, Any],
    error: str = "",
) -> dict[str, Any] | None:
    return None


def emit_module_event(
    *,
    module: str,
    event_type: str,
    status: str,
    latency_ms: int,
    context: Any | None = None,
    agent_name: str = "text2sql-v4",
    user_id: str = "system",
    **kwargs: Any,
) -> dict[str, Any] | None:
    return None

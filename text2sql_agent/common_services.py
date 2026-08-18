"""Adapters for the narrow kbcard-agent-common surface used by this app."""

from __future__ import annotations

import os
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

try:
    import kbcard_agent_common as _common_package
    from kbcard_agent_common.embedding import EmbeddingClient, TEIEmbeddingClient
    from kbcard_agent_common.llm import KBCardAsyncOpenAI, KBCardOpenAI
    from kbcard_agent_common.observability.logger import KBCardLogger
except ModuleNotFoundError:
    _common_package = None
    EmbeddingClient = None
    KBCardLogger = None
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


_CURRENT_CONTEXT: ContextVar[TraceContext | None] = ContextVar("text2sql_trace_context", default=None)
_CURRENT_AGENT_NAME: ContextVar[str] = ContextVar("text2sql_agent_name", default="text2sql-v4")
_CURRENT_USER_ID: ContextVar[str] = ContextVar("text2sql_user_id", default="system")
_LOGGER: Any | None = None


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
        "logging": KBCardLogger is not None,
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


def _get_logger() -> Any | None:
    """Create one KBCardLogger from the common config, then reuse it."""

    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER
    if KBCardLogger is None:
        return None

    from .config import AGENT_SERVICE_NAME, COMMON_CONFIG

    logger = logging.getLogger(AGENT_SERVICE_NAME)
    formatter = os.getenv("KBCARD_LOG_FORMAT", "pretty")
    level = os.getenv("KBCARD_LOG_LEVEL", "INFO")

    if COMMON_CONFIG is not None:
        _LOGGER = KBCardLogger.from_config(COMMON_CONFIG, logger=logger, formatter=formatter, level=level)
        return _LOGGER

    configure = getattr(KBCardLogger, "configure", None)
    if callable(configure):
        _LOGGER = configure(logger=logger, formatter=formatter, level=level)
        return _LOGGER

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    _LOGGER = KBCardLogger(logger=logger, formatter=formatter)
    return _LOGGER


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _context_fields(context: Any | None) -> dict[str, Any]:
    resolved = context or _CURRENT_CONTEXT.get()
    if resolved is None:
        return {}
    return {
        "message_id": getattr(resolved, "message_id", ""),
        "request_id": getattr(resolved, "request_id", ""),
        "trace_id": getattr(resolved, "trace_id", ""),
        "session_id": getattr(resolved, "session_id", None),
    }


def _clean_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean_metadata(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_clean_metadata(item) for item in value]
    return value


def _result_summary(result: dict[str, Any]) -> dict[str, Any]:
    rows = result.get("rows") or result.get("query_rows") or []
    columns = result.get("columns") or result.get("query_columns") or []
    answer = str(result.get("answer") or "")
    return {
        "status": result.get("status"),
        "result_id": result.get("result_id"),
        "source": result.get("source"),
        "selected_domain": result.get("selected_domain"),
        "selected_capability_type": result.get("selected_capability_type"),
        "selected_capability_name": result.get("selected_capability_name"),
        "selected_tool": result.get("selected_tool"),
        "row_count": len(rows) if isinstance(rows, list) else None,
        "column_count": len(columns) if isinstance(columns, list) else None,
        "answer_length": len(answer),
        "has_sql": bool(result.get("sql") or result.get("final_sql")),
        "missing_params": result.get("missing_params"),
    }


def _emit_log(level: str, message: str, **metadata: Any) -> dict[str, Any] | None:
    logger = _get_logger()
    if logger is None:
        return None
    cleaned = _clean_metadata(metadata)
    try:
        from .pii import mask_pii_for_storage

        cleaned = mask_pii_for_storage(cleaned)
    except Exception:
        # Never fall back to raw log metadata when the masking service is down.
        safe_keys = {
            "agent_name",
            "event_type",
            "latency_ms",
            "log_level",
            "message_id",
            "module",
            "request_id",
            "status",
            "total_latency_ms",
            "trace_id",
        }
        cleaned = {key: value for key, value in cleaned.items() if key in safe_keys}
        cleaned["pii_masking_failed"] = True
    log_method = getattr(logger, "_log", None)
    if callable(log_method):
        return log_method(level, message, **cleaned)

    event_method = getattr(logger, "event", None)
    if callable(event_method):
        event_type = str(cleaned.pop("event_type", "log"))
        module = str(cleaned.pop("module", "agent"))
        status = str(cleaned.pop("status", "ERROR" if level.upper() == "ERROR" else "SUCCESS"))
        latency_ms = cleaned.pop("latency_ms", cleaned.pop("total_latency_ms", 0))
        try:
            latency_value = int(latency_ms or 0)
        except (TypeError, ValueError):
            latency_value = 0
        agent_name = str(cleaned.pop("agent_name", _CURRENT_AGENT_NAME.get()))
        error_type = cleaned.pop("error_type", None)
        error_message = cleaned.pop("error_message", None)
        cleaned["message"] = message
        return event_method(
            agent_name=agent_name,
            module=module,
            event_type=event_type,
            status=status,
            latency_ms=latency_value,
            context=None,
            log_level=level,
            metadata=cleaned,
            error_type=error_type,
            error_message=error_message,
        )

    info_method = getattr(logger, "info", None)
    if callable(info_method):
        return info_method(message, **cleaned)

    standard_logger = getattr(logger, "_logger", None)
    if standard_logger is not None:
        standard_logger.log(getattr(logging, level.upper(), logging.INFO), message, extra={"metadata": cleaned})
    return None


@contextmanager
def observability_context(*, context: Any, agent_name: str, user_id: str):
    context_token = _CURRENT_CONTEXT.set(context)
    agent_token = _CURRENT_AGENT_NAME.set(agent_name)
    user_token = _CURRENT_USER_ID.set(user_id)
    try:
        yield
    finally:
        _CURRENT_USER_ID.reset(user_token)
        _CURRENT_AGENT_NAME.reset(agent_token)
        _CURRENT_CONTEXT.reset(context_token)


def _base_event_fields(
    *,
    context: Any | None,
    agent_name: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    fields = {
        **_context_fields(context),
        "agent_name": agent_name or _CURRENT_AGENT_NAME.get(),
        "user_id": user_id or _CURRENT_USER_ID.get(),
    }
    return _clean_metadata(fields)


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
    normalized_status = status.upper()
    metadata = {
        **_base_event_fields(context=context, agent_name=agent_name, user_id=user_id),
        "event_type": "agent_execution",
        "status": normalized_status,
        "total_latency_ms": total_latency_ms,
        "question_length": len(question),
        "result": _result_summary(result),
        "error_type": "AgentExecutionError" if error else None,
        "error_message": error or None,
    }
    message = "agent execution completed" if normalized_status == "SUCCESS" else "agent execution failed"
    level = "ERROR" if normalized_status == "ERROR" else "INFO"
    return _emit_log(level, message, **metadata)


def emit_module_event(
    *,
    module: str,
    event_type: str,
    status: str,
    latency_ms: int,
    context: Any | None = None,
    agent_name: str | None = None,
    user_id: str | None = None,
    **kwargs: Any,
) -> dict[str, Any] | None:
    log_level = str(kwargs.pop("log_level", "INFO"))
    normalized_status = status.upper()
    metadata = {
        **_base_event_fields(context=context, agent_name=agent_name, user_id=user_id),
        "event_type": event_type,
        "module": module,
        "status": normalized_status,
        "latency_ms": latency_ms,
        **kwargs,
    }
    message = f"{module} {event_type} {normalized_status.lower()}"
    return _emit_log(log_level, message, **metadata)

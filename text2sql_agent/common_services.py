"""Small adapters for kbcard-agent-common features used by this Text2SQL app."""

from __future__ import annotations

import os
from typing import Any

try:
    import kbcard_agent_common as _common_package
    from kbcard_agent_common import errors as common_errors
    from kbcard_agent_common.observability import (
        AUDIT_EVENT_TYPES,
        AuditLogDetails,
        ExecutionLogRecord,
        KBCardLogger,
        TraceContext,
        emit_module_event as _emit_common_module_event,
        get_current_observability_context,
        observability_context as _common_observability_context,
    )
except ModuleNotFoundError:
    _common_package = None
    common_errors = None
    AUDIT_EVENT_TYPES = frozenset()
    AuditLogDetails = None
    ExecutionLogRecord = None
    KBCardLogger = None
    TraceContext = None
    _emit_common_module_event = None
    get_current_observability_context = None
    _common_observability_context = None

_LOGGER: Any = None


def common_package_status() -> dict[str, Any]:
    return {
        "installed": _common_package is not None,
        "package": "kbcard_agent_common",
        "version": getattr(_common_package, "__version__", "") if _common_package is not None else "",
    }


def common_feature_status() -> dict[str, Any]:
    return {
        "package": _common_package is not None,
        "observability": KBCardLogger is not None,
        "errors": common_errors is not None,
        "execution_logger_ready": get_common_logger() is not None,
    }


def common_error_name(exc: BaseException) -> str:
    return type(exc).__name__


def common_http_status(exc: BaseException) -> int:
    if common_errors is None:
        return 500
    if isinstance(exc, getattr(common_errors, "ConfigurationError", ())):
        return 400
    if isinstance(exc, getattr(common_errors, "RetryableProviderError", ())):
        return 503
    if isinstance(exc, getattr(common_errors, "ProviderError", ())):
        return 502
    if isinstance(exc, getattr(common_errors, "CapabilityNotSupportedError", ())):
        return 422
    if isinstance(exc, getattr(common_errors, "KBCardAgentError", ())):
        return 500
    return 500


def get_common_logger():
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER
    if KBCardLogger is None:
        return None
    _LOGGER = KBCardLogger.configure()
    return _LOGGER


def create_trace_context(
    *,
    session_id: str | None = None,
    message_id: str | int | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
):
    if TraceContext is None:
        return None
    return TraceContext.create(
        message_id=str(message_id) if message_id else None,
        request_id=request_id,
        trace_id=trace_id,
        session_id=session_id,
        prefix="text2sql",
    )


def observability_context(*, context: Any, agent_name: str, user_id: str):
    logger = get_common_logger()
    if _common_observability_context is None or logger is None or context is None:
        return _null_context()
    return _common_observability_context(
        logger=logger,
        context=context,
        agent_name=agent_name,
        user_id=user_id,
        environment=os.getenv("KBCARD_AGENT_ENV", "local"),
        service_name=os.getenv("KBCARD_SERVICE_NAME", "text2sql-v4"),
        payload_mode=os.getenv("KBCARD_LOG_PAYLOAD_MODE", "summary"),
    )


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
    logger = get_common_logger()
    if ExecutionLogRecord is None or logger is None or context is None:
        return None
    record = ExecutionLogRecord(
        context=context,
        user_id=user_id,
        agent_name=agent_name,
        status=status,
        total_latency_ms=total_latency_ms,
        input_payload={"query": question},
        output_payload={
            "status": result.get("status") or ("ERROR" if error else "complete"),
            "answer": result.get("answer", ""),
            "error": error,
            "result_id": result.get("result_id", ""),
        },
        is_pii_masked=True,
        log_level="ERROR" if status == "ERROR" else "INFO",
        metadata={
            "question_type": result.get("question_type", ""),
            "selected_tool": result.get("selected_tool", ""),
            "matched_query_name": result.get("matched_query_name", ""),
            "selected_tables": result.get("selected_tables", []),
            "row_count": len(result.get("rows", []) or result.get("query_rows", []) or []),
        },
    )
    return logger.execution(record)


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
    if _emit_common_module_event is None:
        return None
    if get_current_observability_context is not None and get_current_observability_context() is not None:
        return _emit_common_module_event(
            module=module,
            event_type=event_type,
            status=status,
            latency_ms=latency_ms,
            **kwargs,
        )

    logger = get_common_logger()
    if logger is None or context is None:
        return None

    action = kwargs.pop("action", None)
    target = kwargs.pop("target", None)
    data_scope = kwargs.pop("data_scope", None)
    if event_type in AUDIT_EVENT_TYPES and "audit" not in kwargs and AuditLogDetails is not None:
        kwargs["audit"] = AuditLogDetails(
            actor={"user_id": user_id, "service_name": agent_name},
            action=action or "invoke",
            target=target or {"system": module},
            authorization={"decision": "allowed"},
            data_scope=data_scope or {},
        )

    return logger.event(
        context=context,
        agent_name=agent_name,
        environment=os.getenv("KBCARD_AGENT_ENV", "local"),
        module=module,
        event_type=event_type,
        status=status,
        latency_ms=latency_ms,
        **kwargs,
    )


class _null_context:
    def __enter__(self):
        return None

    def __exit__(self, *_: object) -> bool:
        return False

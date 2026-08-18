"""PII masking client used only at persistence and logging boundaries."""

from __future__ import annotations

import json
from typing import Any, Iterator

import httpx

from . import config


PII_STORAGE_REDACTION = "[PII 마스킹 실패로 저장하지 않은 내용]"


class PiiMaskingError(RuntimeError):
    """Raised when content cannot be safely masked for persistence."""


_CLIENT: httpx.Client | None = None


def _get_client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        if not config.PII_BASE_URL:
            raise PiiMaskingError("PII masking endpoint is not configured")
        _CLIENT = httpx.Client(
            base_url=config.PII_BASE_URL,
            headers={config.PII_AGENT_HEADER: config.PII_AGENT_NAME},
            timeout=config.PII_TIMEOUT,
        )
    return _CLIENT


def close_pii_client() -> None:
    global _CLIENT
    if _CLIENT is not None:
        _CLIENT.close()
    _CLIENT = None


def mask_pii_text(text: str) -> str:
    """Return the API-masked text, never silently returning raw text on failure."""

    value = str(text or "")
    if not config.PII_MASKING_ENABLED or not value:
        return value
    try:
        response = _get_client().post(
            config.PII_ENDPOINT,
            json={"text": value, "mask": True},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise PiiMaskingError("PII masking request failed") from exc

    masked = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(masked, str):
        raise PiiMaskingError("PII masking response has no text")
    if payload.get("detected") is True and payload.get("mask_applied") is not True:
        raise PiiMaskingError("PII was detected but not masked")
    return masked


def _collect_strings(value: Any, target: list[str]) -> None:
    if isinstance(value, str):
        target.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _collect_strings(item, target)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_strings(item, target)


def _replace_strings(value: Any, replacements: Iterator[str]) -> Any:
    if isinstance(value, str):
        return next(replacements)
    if isinstance(value, dict):
        return {str(key): _replace_strings(item, replacements) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_strings(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_strings(item, replacements) for item in value)
    return value


def mask_pii_for_storage(value: Any) -> Any:
    """Mask every string value with one API call while preserving its structure."""

    if not config.PII_MASKING_ENABLED:
        return value
    strings: list[str] = []
    _collect_strings(value, strings)
    if not strings:
        return value
    masked_json = mask_pii_text(json.dumps(strings, ensure_ascii=False))
    try:
        masked_strings = json.loads(masked_json)
    except json.JSONDecodeError as exc:
        raise PiiMaskingError("PII masking response changed the JSON structure") from exc
    if (
        not isinstance(masked_strings, list)
        or len(masked_strings) != len(strings)
        or not all(isinstance(item, str) for item in masked_strings)
    ):
        raise PiiMaskingError("PII masking response changed the value count")
    return _replace_strings(value, iter(masked_strings))

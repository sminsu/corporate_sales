"""LLM chat and embedding clients, backed entirely by kbcard-agent-common.

This module is the single seam between the agent and the common SDK. It exposes a
tiny surface the rest of the code depends on: ``_call_llm``, ``_normalize_llm_text``,
``_get_embedding``, ``_get_embeddings_batch``, and ``_cosine_similarity``.
"""

import json
import math
import os
import re
import time
from typing import Any

from kbcard_agent_common.embedding import EmbeddingClient, TEIEmbeddingClient
from kbcard_agent_common.errors import RetryableProviderError
from kbcard_agent_common.llm import KBCardOpenAI

from .config import (
    COMMON_CONFIG,
    EMBED_API_KEY,
    EMBED_BASE_URL,
    EMBED_MODEL,
    EMBED_TIMEOUT,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_ENDPOINT_PATH,
    LLM_EXTRA_BODY,
    LLM_EXTRA_HEADERS,
    LLM_MAX_TOKENS,
    LLM_MODEL,
    LLM_PROVIDER,
    LLM_TEMPERATURE,
    LLM_TIMEOUT,
)

# A short system message that establishes the assistant's role for every call.
# Individual prompts still carry their task-specific instructions; this just gives
# the model consistent grounding (role separation) without touching each call site.
DEFAULT_SYSTEM_PROMPT = (
    "당신은 KB카드 법인영업 데이터베이스를 다루는 한국어 데이터 분석 어시스턴트입니다. "
    "주어진 스키마/메트릭/규칙에 충실하게, 요청한 출력 형식만 정확히 반환하세요."
)

# Number of additional attempts after a transient (retryable) provider failure.
_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
_RETRY_BACKOFF_SECONDS = float(os.getenv("LLM_RETRY_BACKOFF", "0.5"))

_LLM_CLIENT: KBCardOpenAI | None = None
_EMBED_CLIENT: Any = None


def _get_llm_client() -> KBCardOpenAI:
    """Build (once) the common chat client from YAML config or a direct endpoint."""
    global _LLM_CLIENT
    if _LLM_CLIENT is None:
        if COMMON_CONFIG is not None and COMMON_CONFIG.llm is not None:
            _LLM_CLIENT = KBCardOpenAI.from_config(COMMON_CONFIG, default_model=LLM_MODEL)
        else:
            _LLM_CLIENT = KBCardOpenAI.from_endpoint(
                base_url=LLM_BASE_URL,
                default_model=LLM_MODEL,
                api_key=LLM_API_KEY or None,
                provider=LLM_PROVIDER,
                endpoint_path=LLM_ENDPOINT_PATH,
                timeout=LLM_TIMEOUT,
            )
    return _LLM_CLIENT


def _get_embed_client():
    """Build (once) the common embedding client from YAML config or a TEI endpoint."""
    global _EMBED_CLIENT
    if _EMBED_CLIENT is None:
        if COMMON_CONFIG is not None and COMMON_CONFIG.embedding is not None:
            _EMBED_CLIENT = EmbeddingClient.from_config(COMMON_CONFIG)
        else:
            _EMBED_CLIENT = TEIEmbeddingClient(
                base_url=EMBED_BASE_URL,
                model=EMBED_MODEL,
                api_key=EMBED_API_KEY or None,
                timeout=EMBED_TIMEOUT,
            )
    return _EMBED_CLIENT


def close_common_clients() -> None:
    """Release HTTP resources held by the cached clients (called on shutdown)."""
    global _LLM_CLIENT, _EMBED_CLIENT
    for client in (_LLM_CLIENT, _EMBED_CLIENT):
        close = getattr(client, "close", None)
        if callable(close):
            close()
    _LLM_CLIENT = None
    _EMBED_CLIENT = None


def _with_retry(operation, *, what: str):
    """Run ``operation``, retrying on transient provider errors with linear backoff.

    Non-retryable errors propagate immediately. After the retries are exhausted the
    original RetryableProviderError is re-raised so the web layer can map it to a
    503 status (see common_services.common_http_status).
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return operation()
        except RetryableProviderError:
            if attempt >= _MAX_RETRIES:
                raise
            time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))


def _call_llm(prompt: str, system: str | None = None) -> str:
    """Send one prompt as a user message (with a system role) and return the text."""
    messages = [
        {"role": "system", "content": system or DEFAULT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]

    def _create():
        return _get_llm_client().chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
            timeout=LLM_TIMEOUT,
            extra_body=LLM_EXTRA_BODY,
            extra_headers=LLM_EXTRA_HEADERS,
        )

    response = _with_retry(_create, what="LLM chat completion")
    return _normalize_llm_text(response.content)


def _normalize_llm_text(text: Any) -> str:
    """Coerce a model response to a clean string, dropping any <reasoning> block."""
    if isinstance(text, str):
        value = text
    elif isinstance(text, (dict, list)):
        value = json.dumps(text, ensure_ascii=False)
    elif text is None:
        value = ""
    else:
        value = str(text)
    return re.sub(r"<reasoning>.*?</reasoning>\s*", "", value, flags=re.DOTALL).strip()


def probe_llm() -> dict[str, Any]:
    """Readiness probe: attempt a 1-token chat completion via the common client."""
    try:
        _get_llm_client().chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "ping"}],
            temperature=0,
            max_tokens=1,
            timeout=10.0,
            extra_body=LLM_EXTRA_BODY,
            extra_headers=LLM_EXTRA_HEADERS,
        )
        return {
            "llm_ready": True,
            "llm_status": "ready",
            "llm_detail": "chat completion probe succeeded",
        }
    except Exception as exc:  # noqa: BLE001 - report any failure as not-ready
        detail = str(exc)
        status = "auth_error" if ("401" in detail or "403" in detail) else "unavailable"
        return {
            "llm_ready": False,
            "llm_status": status,
            "llm_detail": f"chat completion probe failed: {type(exc).__name__}: {detail}",
        }


def _get_embedding(text: str) -> list[float]:
    """Embed a single text value."""
    return _with_retry(lambda: _get_embed_client().embed_text(text), what="Embedding request")


def _get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Embed multiple texts, preserving input order."""
    return _with_retry(lambda: _get_embed_client().embed_texts(texts), what="Embedding batch request")


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length vectors (0.0 if either is zero)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

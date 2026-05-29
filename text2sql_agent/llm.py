"""LLM chat and embedding clients."""

import math
import re
from typing import Any
from urllib.parse import quote

import requests

from .config import (
    COMMON_CONFIG,
    VLLM_API_KEY,
    VLLM_API_KEY_HEADER,
    VLLM_API_KEY_PREFIX,
    VLLM_BASE_URL,
    VLLM_EMBED_API_KEY,
    VLLM_EMBED_API_KEY_HEADER,
    VLLM_EMBED_MODEL,
    VLLM_EMBED_API_KEY_PREFIX,
    VLLM_EMBED_TIMEOUT,
    VLLM_EMBED_EXTRA_HEADERS,
    VLLM_EMBED_URL,
    VLLM_ENDPOINT_PATH,
    VLLM_EXTRA_BODY,
    VLLM_EXTRA_HEADERS,
    VLLM_MODEL,
    VLLM_PROVIDER,
    VLLM_TEMPERATURE,
    VLLM_TIMEOUT,
    VLLM_TRANSPORT,
    VLLM_MAX_TOKENS,
)

try:
    from kbcard_agent_common.embedding import EmbeddingClient, TEIEmbeddingClient
    from kbcard_agent_common.llm import KBCardOpenAI
except ModuleNotFoundError:
    EmbeddingClient = None
    KBCardOpenAI = None
    TEIEmbeddingClient = None

_COMMON_LLM_CLIENT: Any = None
_COMMON_EMBED_CLIENT: Any = None


class LLMHTTPError(RuntimeError):
    """HTTP failures from the configured LLM endpoint with non-secret context."""


def _api_key_headers(api_key: str, header_name: str, value_prefix: str) -> dict[str, str]:
    if not api_key or not header_name:
        return {}
    header_value = f"{value_prefix.strip()} {api_key}".strip() if value_prefix else api_key
    return {header_name: header_value}


def _uses_default_bearer_auth(header_name: str, value_prefix: str) -> bool:
    return header_name.lower() == "authorization" and value_prefix.lower() == "bearer"


def _llm_auth_headers() -> dict[str, str]:
    return _api_key_headers(VLLM_API_KEY, VLLM_API_KEY_HEADER, VLLM_API_KEY_PREFIX)


def _embedding_auth_headers() -> dict[str, str]:
    return _api_key_headers(VLLM_EMBED_API_KEY, VLLM_EMBED_API_KEY_HEADER, VLLM_EMBED_API_KEY_PREFIX)


def _merged_headers(*headers: dict[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    for item in headers:
        if item:
            merged.update(item)
    return merged


def _response_excerpt(resp: requests.Response, limit: int = 1200) -> str:
    text = (resp.text or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


def _raise_for_llm_status(resp: requests.Response, *, operation: str, url: str) -> None:
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        detail = _response_excerpt(resp)
        message = (
            f"{operation} failed: HTTP {resp.status_code} from {url} "
            f"(transport={VLLM_TRANSPORT}, provider={VLLM_PROVIDER}, model={VLLM_MODEL})"
        )
        if detail:
            message = f"{message}; response={detail}"
        raise LLMHTTPError(message) from exc


def _annotate_common_llm_error(exc: Exception) -> LLMHTTPError:
    url = f"{VLLM_BASE_URL.rstrip('/')}/{VLLM_ENDPOINT_PATH.lstrip('/')}"
    return LLMHTTPError(
        f"LLM call failed via common client at {url} "
        f"(transport={VLLM_TRANSPORT}, provider={VLLM_PROVIDER}, model={VLLM_MODEL}): {exc}"
    )


def _get_common_llm_client():
    global _COMMON_LLM_CLIENT
    if _COMMON_LLM_CLIENT is not None:
        return _COMMON_LLM_CLIENT

    if VLLM_TRANSPORT == "bedrock_converse" or KBCardOpenAI is None:
        return None

    if (
        _uses_default_bearer_auth(VLLM_API_KEY_HEADER, VLLM_API_KEY_PREFIX)
        and COMMON_CONFIG is not None
        and getattr(COMMON_CONFIG, "llm", None) is not None
    ):
        _COMMON_LLM_CLIENT = KBCardOpenAI.from_config(COMMON_CONFIG, default_model=VLLM_MODEL)
    else:
        _COMMON_LLM_CLIENT = KBCardOpenAI.from_endpoint(
            base_url=VLLM_BASE_URL,
            default_model=VLLM_MODEL,
            api_key=(
                VLLM_API_KEY
                if _uses_default_bearer_auth(VLLM_API_KEY_HEADER, VLLM_API_KEY_PREFIX)
                else None
            ),
            provider=VLLM_PROVIDER,
            endpoint_path=VLLM_ENDPOINT_PATH,
            timeout=VLLM_TIMEOUT,
        )
    return _COMMON_LLM_CLIENT


def _get_common_embed_client():
    global _COMMON_EMBED_CLIENT
    if _COMMON_EMBED_CLIENT is not None:
        return _COMMON_EMBED_CLIENT

    if TEIEmbeddingClient is None:
        return None

    if (
        COMMON_CONFIG is not None
        and getattr(COMMON_CONFIG, "embedding", None) is not None
        and EmbeddingClient is not None
    ):
        _COMMON_EMBED_CLIENT = EmbeddingClient.from_config(COMMON_CONFIG)
    else:
        _COMMON_EMBED_CLIENT = TEIEmbeddingClient(
            base_url=VLLM_EMBED_URL,
            model=VLLM_EMBED_MODEL,
            api_key=(
                VLLM_EMBED_API_KEY
                if _uses_default_bearer_auth(VLLM_EMBED_API_KEY_HEADER, VLLM_EMBED_API_KEY_PREFIX)
                else None
            ),
            timeout=VLLM_EMBED_TIMEOUT,
            extra_headers=_merged_headers(
                VLLM_EMBED_EXTRA_HEADERS,
                None
                if _uses_default_bearer_auth(
                    VLLM_EMBED_API_KEY_HEADER,
                    VLLM_EMBED_API_KEY_PREFIX,
                )
                else _embedding_auth_headers(),
            ),
        )
    return _COMMON_EMBED_CLIENT


def _call_llm(prompt: str) -> str:
    if VLLM_TRANSPORT == "bedrock_converse":
        return _call_bedrock_converse(prompt)

    common_client = _get_common_llm_client()
    if common_client is not None:
        extra_headers = _merged_headers(
            VLLM_EXTRA_HEADERS,
            None
            if _uses_default_bearer_auth(VLLM_API_KEY_HEADER, VLLM_API_KEY_PREFIX)
            else _llm_auth_headers(),
        )
        try:
            response = common_client.chat.completions.create(
                model=VLLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=VLLM_TEMPERATURE,
                max_tokens=VLLM_MAX_TOKENS,
                timeout=VLLM_TIMEOUT,
                extra_body=VLLM_EXTRA_BODY,
                extra_headers=extra_headers or None,
            )
        except Exception as exc:
            raise _annotate_common_llm_error(exc) from exc
        return _normalize_llm_text(response.content)

    url = f"{VLLM_BASE_URL.rstrip('/')}/{VLLM_ENDPOINT_PATH.lstrip('/')}"
    headers = {
        "Content-Type": "application/json",
        **_merged_headers(VLLM_EXTRA_HEADERS, _llm_auth_headers()),
    }
    data = {
        "model": VLLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": VLLM_TEMPERATURE,
        "max_tokens": VLLM_MAX_TOKENS,
        **VLLM_EXTRA_BODY,
    }
    resp = requests.post(url, json=data, headers=headers, timeout=VLLM_TIMEOUT)
    _raise_for_llm_status(resp, operation="LLM chat completion", url=url)
    return _normalize_llm_text(resp.json()["choices"][0]["message"]["content"])


def _call_bedrock_converse(prompt: str) -> str:
    url = f"{VLLM_BASE_URL.rstrip('/')}/model/{quote(VLLM_MODEL, safe='')}/converse"
    headers = {
        "Content-Type": "application/json",
        **_merged_headers(VLLM_EXTRA_HEADERS, _llm_auth_headers()),
    }
    data = {
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"temperature": VLLM_TEMPERATURE, "maxTokens": VLLM_MAX_TOKENS},
    }
    resp = requests.post(url, json=data, headers=headers, timeout=VLLM_TIMEOUT)
    _raise_for_llm_status(resp, operation="Bedrock converse", url=url)
    return _normalize_llm_text(_extract_bedrock_converse_text(resp.json()))


def _normalize_llm_text(text: str) -> str:
    return re.sub(r"<reasoning>.*?</reasoning>\s*", "", text, flags=re.DOTALL).strip()


def _extract_bedrock_converse_text(payload: dict[str, Any]) -> str:
    content = payload.get("output", {}).get("message", {}).get("content", [])
    if not isinstance(content, list):
        return ""
    return "".join(item.get("text", "") for item in content if isinstance(item, dict))


def _get_embedding(text: str) -> list[float]:
    common_client = _get_common_embed_client()
    if common_client is not None:
        return common_client.embed_text(text)

    url = f"{VLLM_EMBED_URL}/v1/embeddings"
    headers = {
        "Content-Type": "application/json",
        **_merged_headers(VLLM_EMBED_EXTRA_HEADERS, _embedding_auth_headers()),
    }
    data = {"model": VLLM_EMBED_MODEL, "input": text}
    resp = requests.post(url, json=data, headers=headers, timeout=VLLM_EMBED_TIMEOUT)
    _raise_for_llm_status(resp, operation="Embedding request", url=url)
    return resp.json()["data"][0]["embedding"]


def _get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    common_client = _get_common_embed_client()
    if common_client is not None:
        return common_client.embed_texts(texts)

    url = f"{VLLM_EMBED_URL}/v1/embeddings"
    headers = {
        "Content-Type": "application/json",
        **_merged_headers(VLLM_EMBED_EXTRA_HEADERS, _embedding_auth_headers()),
    }
    data = {"model": VLLM_EMBED_MODEL, "input": texts}
    resp = requests.post(url, json=data, headers=headers, timeout=VLLM_EMBED_TIMEOUT)
    _raise_for_llm_status(resp, operation="Embedding batch request", url=url)
    results = resp.json()["data"]
    results.sort(key=lambda x: x["index"])
    return [r["embedding"] for r in results]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

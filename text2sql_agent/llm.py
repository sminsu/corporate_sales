"""vLLM chat and embedding clients."""

import math
from typing import Any

import requests

from .config import (
    VLLM_API_KEY,
    VLLM_BASE_URL,
    VLLM_EMBED_API_KEY,
    VLLM_EMBED_MODEL,
    VLLM_EMBED_URL,
    VLLM_MODEL,
)

try:
    from kbcard_agent_common.embedding import TEIEmbeddingClient
    from kbcard_agent_common.llm import KBCardOpenAI
except ModuleNotFoundError:
    KBCardOpenAI = None
    TEIEmbeddingClient = None

_COMMON_LLM_CLIENT: Any = None
_COMMON_EMBED_CLIENT: Any = None


def _get_common_llm_client():
    global _COMMON_LLM_CLIENT
    if _COMMON_LLM_CLIENT is not None:
        return _COMMON_LLM_CLIENT

    if KBCardOpenAI is None:
        return None

    _COMMON_LLM_CLIENT = KBCardOpenAI.from_endpoint(
        base_url=VLLM_BASE_URL,
        default_model=VLLM_MODEL,
        api_key=VLLM_API_KEY,
        timeout=120,
    )
    return _COMMON_LLM_CLIENT


def _get_common_embed_client():
    global _COMMON_EMBED_CLIENT
    if _COMMON_EMBED_CLIENT is not None:
        return _COMMON_EMBED_CLIENT

    if TEIEmbeddingClient is None:
        return None

    _COMMON_EMBED_CLIENT = TEIEmbeddingClient(
        base_url=VLLM_EMBED_URL,
        model=VLLM_EMBED_MODEL,
        api_key=VLLM_EMBED_API_KEY,
        timeout=60,
    )
    return _COMMON_EMBED_CLIENT


def _call_llm(prompt: str) -> str:
    common_client = _get_common_llm_client()
    if common_client is not None:
        response = common_client.chat.completions.create(
            model=VLLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            timeout=120,
        )
        return response.content

    url = f"{VLLM_BASE_URL}/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {VLLM_API_KEY}",
    }
    data = {
        "model": VLLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    resp = requests.post(url, json=data, headers=headers, timeout=120)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _get_embedding(text: str) -> list[float]:
    common_client = _get_common_embed_client()
    if common_client is not None:
        return common_client.embed_text(text)

    url = f"{VLLM_EMBED_URL}/v1/embeddings"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {VLLM_EMBED_API_KEY}",
    }
    data = {"model": VLLM_EMBED_MODEL, "input": text}
    resp = requests.post(url, json=data, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def _get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    common_client = _get_common_embed_client()
    if common_client is not None:
        return common_client.embed_texts(texts)

    url = f"{VLLM_EMBED_URL}/v1/embeddings"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {VLLM_EMBED_API_KEY}",
    }
    data = {"model": VLLM_EMBED_MODEL, "input": texts}
    resp = requests.post(url, json=data, headers=headers, timeout=60)
    resp.raise_for_status()
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

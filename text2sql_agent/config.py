"""Runtime configuration and filesystem paths for the Text2SQL agent."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = BASE_DIR / "schema_v6_gptoss.yaml"

load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env.local", override=True)


def _env(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return default


LLM_BASE_URL = _env("LLM_BASE_URL", "VLLM_BASE_URL", default="http://localhost:8000")
LLM_MODEL = _env("LLM_MODEL", "VLLM_MODEL", default="gpt-oss")
LLM_API_KEY = _env("LLM_API_KEY", "OPENAI_API_KEY", "VLLM_API_KEY", default="EMPTY")

EMBED_BASE_URL = _env("EMBED_BASE_URL", "EMBEDDING_BASE_URL", "VLLM_EMBED_URL", default=LLM_BASE_URL)
EMBED_MODEL = _env("EMBED_MODEL", "EMBEDDING_MODEL", "VLLM_EMBED_MODEL", default="embedding-model")
EMBED_API_KEY = _env("EMBED_API_KEY", "EMBEDDING_API_KEY", "VLLM_EMBED_API_KEY", default=LLM_API_KEY)

# Backward-compatible names used by the current service code and README.
VLLM_BASE_URL = LLM_BASE_URL
VLLM_MODEL = LLM_MODEL
VLLM_API_KEY = LLM_API_KEY

VLLM_EMBED_URL = EMBED_BASE_URL
VLLM_EMBED_MODEL = EMBED_MODEL
VLLM_EMBED_API_KEY = EMBED_API_KEY
EMBED_MATCH_THRESHOLD = float(os.getenv("EMBED_MATCH_THRESHOLD", "0.75"))
ENABLE_EMBEDDING_PRECOMPUTE = os.getenv("ENABLE_EMBEDDING_PRECOMPUTE", "false").lower() == "true"

BAD_DEBT_OUTPUT_DIR = os.getenv("BAD_DEBT_OUTPUT_DIR", str(BASE_DIR / "output"))
REPORT_DIR = BASE_DIR / "reports"

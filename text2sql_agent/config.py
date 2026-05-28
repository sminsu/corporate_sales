"""Runtime configuration and filesystem paths for the Text2SQL agent."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = BASE_DIR / "schema_v6_gptoss.yaml"

VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://localhost:8000")
VLLM_MODEL = os.getenv("VLLM_MODEL", "gpt-oss")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "EMPTY")

VLLM_EMBED_URL = os.getenv("VLLM_EMBED_URL", VLLM_BASE_URL)
VLLM_EMBED_MODEL = os.getenv("VLLM_EMBED_MODEL", "embedding-model")
VLLM_EMBED_API_KEY = os.getenv("VLLM_EMBED_API_KEY", VLLM_API_KEY)
EMBED_MATCH_THRESHOLD = float(os.getenv("EMBED_MATCH_THRESHOLD", "0.75"))
ENABLE_EMBEDDING_PRECOMPUTE = os.getenv("ENABLE_EMBEDDING_PRECOMPUTE", "false").lower() == "true"

BAD_DEBT_OUTPUT_DIR = os.getenv("BAD_DEBT_OUTPUT_DIR", str(BASE_DIR / "output"))
REPORT_DIR = BASE_DIR / "reports"

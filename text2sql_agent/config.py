"""Runtime configuration and filesystem paths for the Text2SQL agent."""

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
SCHEMA_PATH = BASE_DIR / "schema_v6_gptoss.yaml"
BEDROCK_MANTLE_REGIONS = {
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "ap-northeast-1",
    "ap-south-1",
    "ap-southeast-2",
    "ap-southeast-3",
    "eu-central-1",
    "eu-north-1",
    "eu-south-1",
    "eu-west-1",
    "eu-west-2",
    "sa-east-1",
}

load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env.local", override=True)


def _env(*names: str, default: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return default


def _default_llm_base_url() -> str:
    aws_region = _default_bedrock_region()
    if os.getenv("AWS_BEARER_TOKEN_BEDROCK") and aws_region:
        if _default_bedrock_endpoint_kind(aws_region) == "runtime":
            return f"https://bedrock-runtime.{aws_region}.amazonaws.com"
        return f"https://bedrock-mantle.{aws_region}.api.aws"
    return "http://localhost:8000"


def _default_bedrock_endpoint_kind(region: str) -> str:
    explicit_kind = os.getenv("BEDROCK_ENDPOINT_KIND") or os.getenv("AWS_BEDROCK_ENDPOINT_KIND")
    if explicit_kind in {"mantle", "runtime"}:
        return explicit_kind
    return "mantle" if region in BEDROCK_MANTLE_REGIONS else "runtime"


def _default_bedrock_region() -> str | None:
    for name in ("BEDROCK_REGION", "AWS_BEDROCK_REGION", "AWS_REGION", "AWS_DEFAULT_REGION"):
        region = os.getenv(name)
        if region:
            return region
    return None


def _default_llm_model() -> str:
    return os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL") or "gpt-oss"


def _default_llm_endpoint_path(base_url: str) -> str:
    if "bedrock-runtime." in base_url:
        return "/openai/v1/chat/completions"
    return "/v1/chat/completions"


LLM_BASE_URL = _env("LLM_BASE_URL", "VLLM_BASE_URL", default=_default_llm_base_url())
LLM_MODEL = _env("LLM_MODEL", "VLLM_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", default=_default_llm_model())
LLM_API_KEY = _env(
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "BEDROCK_API_KEY",
    "VLLM_API_KEY",
    default="EMPTY",
)
LLM_ENDPOINT_PATH = _env("LLM_ENDPOINT_PATH", "VLLM_ENDPOINT_PATH", default=_default_llm_endpoint_path(LLM_BASE_URL))
LLM_PROVIDER = _env("LLM_PROVIDER", default="bedrock" if "bedrock-" in LLM_BASE_URL else "openai_compatible")

EMBED_BASE_URL = _env("EMBED_BASE_URL", "EMBEDDING_BASE_URL", "VLLM_EMBED_URL", default=LLM_BASE_URL)
EMBED_MODEL = _env("EMBED_MODEL", "EMBEDDING_MODEL", "VLLM_EMBED_MODEL", default="embedding-model")
EMBED_API_KEY = _env("EMBED_API_KEY", "EMBEDDING_API_KEY", "VLLM_EMBED_API_KEY", default=LLM_API_KEY)

# Backward-compatible names used by the current service code and README.
VLLM_BASE_URL = LLM_BASE_URL
VLLM_MODEL = LLM_MODEL
VLLM_API_KEY = LLM_API_KEY
VLLM_ENDPOINT_PATH = LLM_ENDPOINT_PATH
VLLM_PROVIDER = LLM_PROVIDER

VLLM_EMBED_URL = EMBED_BASE_URL
VLLM_EMBED_MODEL = EMBED_MODEL
VLLM_EMBED_API_KEY = EMBED_API_KEY
EMBED_MATCH_THRESHOLD = float(os.getenv("EMBED_MATCH_THRESHOLD", "0.75"))
ENABLE_EMBEDDING_PRECOMPUTE = os.getenv("ENABLE_EMBEDDING_PRECOMPUTE", "false").lower() == "true"

BAD_DEBT_OUTPUT_DIR = os.getenv("BAD_DEBT_OUTPUT_DIR", str(BASE_DIR / "output"))
REPORT_DIR = BASE_DIR / "reports"

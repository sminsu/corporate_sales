"""Runtime configuration and filesystem paths for the Text2SQL agent."""

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

try:
    from kbcard_agent_common.config import KBCardConfig
    from kbcard_agent_common.llm import ModelRegistry
except ModuleNotFoundError:
    KBCardConfig = None
    ModelRegistry = None

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


def _path_from_env(*names: str, default: Path | None = None) -> Path | None:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            path = Path(value)
            return path if path.is_absolute() else BASE_DIR / path
    if default is None or not default.exists():
        return None
    return default if default.is_absolute() else BASE_DIR / default


COMMON_CONFIG_PATH = _path_from_env(
    "KBCARD_CONFIG_PATH",
    "KBCARD_AGENT_CONFIG_PATH",
    "AGENT_CONFIG_PATH",
    default=Path("config/agent.local.yaml"),
)


def _load_common_config(path: Path | None) -> Any | None:
    if path is None or KBCardConfig is None:
        return None
    return KBCardConfig.from_yaml(path)


COMMON_CONFIG = _load_common_config(COMMON_CONFIG_PATH)


def _common_llm_settings() -> Any | None:
    if COMMON_CONFIG is None or COMMON_CONFIG.llm is None:
        return None
    return COMMON_CONFIG.get_llm_settings()


def _common_llm_model_config() -> Any | None:
    llm_settings = _common_llm_settings()
    if llm_settings is None or ModelRegistry is None:
        return None
    registry_path = Path(llm_settings.model_registry_path)
    if not registry_path.is_absolute():
        registry_path = (COMMON_CONFIG_PATH.parent if COMMON_CONFIG_PATH is not None else BASE_DIR) / registry_path
    return ModelRegistry.from_yaml(registry_path).get(llm_settings.default_model)


def _common_embedding_settings() -> Any | None:
    if COMMON_CONFIG is None or COMMON_CONFIG.embedding is None:
        return None
    return COMMON_CONFIG.require_embedding()


def _common_retrieval_store() -> Any | None:
    if COMMON_CONFIG is None or COMMON_CONFIG.retrieval is None:
        return None
    return COMMON_CONFIG.retrieval.store


COMMON_LLM_SETTINGS = _common_llm_settings()
COMMON_LLM_MODEL_CONFIG = _common_llm_model_config()
COMMON_EMBEDDING_SETTINGS = _common_embedding_settings()
COMMON_RETRIEVAL_STORE = _common_retrieval_store()


def _common_agent_value(name: str, default: str) -> str:
    if COMMON_CONFIG is None:
        return default
    value = getattr(COMMON_CONFIG.agent, name, None)
    return value or default


def _common_model_value(name: str, default: str) -> str:
    if COMMON_LLM_MODEL_CONFIG is None:
        return default
    value = getattr(COMMON_LLM_MODEL_CONFIG, name, None)
    return value if value not in (None, "") else default


def _common_embedding_value(name: str, default: str) -> str:
    if COMMON_EMBEDDING_SETTINGS is None:
        return default
    value = getattr(COMMON_EMBEDDING_SETTINGS, name, None)
    return value if value not in (None, "") else default


def _common_model_api_key() -> str:
    if COMMON_LLM_MODEL_CONFIG is None:
        return ""
    explicit_key = getattr(COMMON_LLM_MODEL_CONFIG, "api_key", "")
    if explicit_key:
        return explicit_key
    get_api_key = getattr(COMMON_LLM_MODEL_CONFIG, "get_api_key", None)
    return get_api_key() if callable(get_api_key) else ""


def _default_llm_base_url() -> str:
    common_base_url = _common_model_value("base_url", "")
    if common_base_url:
        return common_base_url
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


def _looks_like_native_bedrock_model(model: str) -> bool:
    return model.startswith(
        (
            "amazon.",
            "anthropic.",
            "cohere.",
            "global.anthropic.",
            "meta.",
            "mistral.",
            "us.amazon.",
            "us.anthropic.",
            "us.meta.",
        )
    )


def _default_llm_model(base_url: str) -> str:
    if COMMON_LLM_SETTINGS is not None:
        return COMMON_LLM_SETTINGS.default_model
    if "bedrock-runtime.ap-northeast-2.amazonaws.com" in base_url:
        return "global.anthropic.claude-sonnet-4-5-20250929-v1:0"
    if "bedrock-runtime." in base_url:
        return "openai.gpt-oss-20b-1:0"
    return os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL") or "gpt-oss"


def _default_llm_transport(base_url: str, model: str) -> str:
    if "bedrock-runtime." in base_url and _looks_like_native_bedrock_model(model):
        return "bedrock_converse"
    return "openai_chat"


def _default_llm_endpoint_path(base_url: str, transport: str) -> str:
    common_endpoint_path = _common_model_value("endpoint_path", "")
    if common_endpoint_path:
        return common_endpoint_path
    if transport == "bedrock_converse":
        return "/model/{model_id}/converse"
    if "bedrock-runtime." in base_url:
        return "/openai/v1/chat/completions"
    return "/v1/chat/completions"


LLM_BASE_URL = _env("LLM_BASE_URL", "VLLM_BASE_URL", default=_default_llm_base_url())
LLM_MODEL = _env("LLM_MODEL", "VLLM_MODEL", "BEDROCK_MODEL", default=_default_llm_model(LLM_BASE_URL))
LLM_TRANSPORT = _env("LLM_TRANSPORT", "LLM_API_MODE", "VLLM_TRANSPORT", default=_default_llm_transport(LLM_BASE_URL, LLM_MODEL))
LLM_API_KEY = _env(
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "BEDROCK_API_KEY",
    "VLLM_API_KEY",
    default=_common_model_api_key() or "EMPTY",
)

LLM_ENDPOINT_PATH = _env("LLM_ENDPOINT_PATH", "VLLM_ENDPOINT_PATH", default=_default_llm_endpoint_path(LLM_BASE_URL, LLM_TRANSPORT))
LLM_PROVIDER = _env(
    "LLM_PROVIDER",
    default=_common_model_value("provider", "bedrock" if "bedrock-" in LLM_BASE_URL else "openai_compatible"),
)
LLM_API_KEY_HEADER = _env("LLM_API_KEY_HEADER", "LLM_AUTH_HEADER", "VLLM_API_KEY_HEADER", default="Authorization")
LLM_API_KEY_PREFIX = _env(
    "LLM_API_KEY_PREFIX",
    "LLM_AUTH_PREFIX",
    "VLLM_API_KEY_PREFIX",
    default="Bearer" if LLM_API_KEY_HEADER.lower() == "authorization" else "",
)
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", default=str(COMMON_LLM_SETTINGS.temperature if COMMON_LLM_SETTINGS is not None else 0)))
LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", default=str(COMMON_LLM_SETTINGS.max_tokens if COMMON_LLM_SETTINGS is not None and COMMON_LLM_SETTINGS.max_tokens else 4096)))
LLM_TIMEOUT = float(_env("LLM_TIMEOUT", default=str(COMMON_LLM_SETTINGS.timeout if COMMON_LLM_SETTINGS is not None and COMMON_LLM_SETTINGS.timeout else 120)))

EMBED_BASE_URL = _env(
    "EMBED_BASE_URL",
    "EMBEDDING_BASE_URL",
    "VLLM_EMBED_URL",
    default=_common_embedding_value("base_url", LLM_BASE_URL),
)
EMBED_MODEL = _env("EMBED_MODEL", "EMBEDDING_MODEL", "VLLM_EMBED_MODEL", default=_common_embedding_value("model", "embedding-model"))
EMBED_API_KEY = _env("EMBED_API_KEY", "EMBEDDING_API_KEY", "VLLM_EMBED_API_KEY", default=_common_embedding_value("api_key", LLM_API_KEY))
EMBED_API_KEY_HEADER = _env("EMBED_API_KEY_HEADER", "EMBEDDING_API_KEY_HEADER", "VLLM_EMBED_API_KEY_HEADER", default=LLM_API_KEY_HEADER)
EMBED_API_KEY_PREFIX = _env("EMBED_API_KEY_PREFIX", "EMBEDDING_API_KEY_PREFIX", "VLLM_EMBED_API_KEY_PREFIX", default=LLM_API_KEY_PREFIX)
EMBED_TIMEOUT = float(_env("EMBED_TIMEOUT", default=str(_common_embedding_value("timeout", "60"))))

AGENT_NAME = _env("KBCARD_AGENT_NAME", default=_common_agent_value("name", "text2sql-v4"))
AGENT_ENVIRONMENT = _env("KBCARD_AGENT_ENV", default=_common_agent_value("environment", "local"))
AGENT_SERVICE_NAME = _env("KBCARD_SERVICE_NAME", default=_common_agent_value("service_name", AGENT_NAME))

DB_DSN_ENV = _env(
    "KBCARD_POSTGRES_DSN_ENV",
    "DB_DSN_ENV",
    default=getattr(COMMON_RETRIEVAL_STORE, "dsn_env", "KBCARD_POSTGRES_DSN") if COMMON_RETRIEVAL_STORE is not None else "KBCARD_POSTGRES_DSN",
)
DB_DSN = _env("DATABASE_URL", "DB_DSN", "POSTGRES_DSN", DB_DSN_ENV, default="")
DB_HOST = _env("DB_HOST", default="localhost")
DB_PORT = int(_env("DB_PORT", default="5432"))
DB_NAME = _env("DB_NAME", default="postgres")
DB_USER = _env("DB_USER", default=os.getenv("USER", "postgres"))
DB_PASSWORD = _env("DB_PASSWORD", default="")
DB_POOL_MAX = int(_env("DB_POOL_MAX", default=str(getattr(COMMON_RETRIEVAL_STORE, "pool_max_size", 10))))

# Backward-compatible names used by the current service code and README.
VLLM_BASE_URL = LLM_BASE_URL
VLLM_MODEL = LLM_MODEL
VLLM_API_KEY = LLM_API_KEY
VLLM_API_KEY_HEADER = LLM_API_KEY_HEADER
VLLM_API_KEY_PREFIX = LLM_API_KEY_PREFIX
VLLM_ENDPOINT_PATH = LLM_ENDPOINT_PATH
VLLM_PROVIDER = LLM_PROVIDER
VLLM_TRANSPORT = LLM_TRANSPORT
VLLM_TEMPERATURE = LLM_TEMPERATURE
VLLM_MAX_TOKENS = LLM_MAX_TOKENS
VLLM_TIMEOUT = LLM_TIMEOUT

VLLM_EMBED_URL = EMBED_BASE_URL
VLLM_EMBED_MODEL = EMBED_MODEL
VLLM_EMBED_API_KEY = EMBED_API_KEY
VLLM_EMBED_API_KEY_HEADER = EMBED_API_KEY_HEADER
VLLM_EMBED_API_KEY_PREFIX = EMBED_API_KEY_PREFIX
VLLM_EMBED_TIMEOUT = EMBED_TIMEOUT
EMBED_MATCH_THRESHOLD = float(os.getenv("EMBED_MATCH_THRESHOLD", "0.75"))
ENABLE_EMBEDDING_PRECOMPUTE = os.getenv("ENABLE_EMBEDDING_PRECOMPUTE", "false").lower() == "true"

BAD_DEBT_OUTPUT_DIR = os.getenv("BAD_DEBT_OUTPUT_DIR", str(BASE_DIR / "output"))
REPORT_DIR = BASE_DIR / "reports"

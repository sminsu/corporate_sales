"""Runtime configuration and filesystem paths for the Text2SQL agent."""

import os
from pathlib import Path
from typing import Any

import yaml
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
    if default is None:
        return None
    path = default if default.is_absolute() else BASE_DIR / default
    return path if path.exists() else None


COMMON_CONFIG_PATH = _path_from_env(
    "KBCARD_CONFIG_PATH",
    "KBCARD_AGENT_CONFIG_PATH",
    "AGENT_CONFIG_PATH",
    default=Path("config/agent.local.yaml"),
)


def _load_yaml(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open(encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    return data if isinstance(data, dict) else {}


RAW_AGENT_CONFIG = _load_yaml(COMMON_CONFIG_PATH)


def _load_common_config(path: Path | None) -> Any | None:
    if path is None or KBCardConfig is None:
        return None
    return KBCardConfig.from_yaml(path)


COMMON_CONFIG = _load_common_config(COMMON_CONFIG_PATH)


def _raw_section(name: str) -> dict[str, Any]:
    value = RAW_AGENT_CONFIG.get(name)
    return value if isinstance(value, dict) else {}


def _value(source: Any, name: str, default: Any) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        value = source.get(name)
    else:
        value = getattr(source, name, None)
    return default if value in (None, "") else value


def _as_str(value: Any, default: str) -> str:
    return str(default if value in (None, "") else value)


RAW_AGENT_SETTINGS = _raw_section("agent")
RAW_LLM_SETTINGS = _raw_section("llm")
RAW_EMBEDDING_SETTINGS = _raw_section("embedding")


def _common_llm_settings() -> Any | None:
    if COMMON_CONFIG is None:
        return None
    llm = getattr(COMMON_CONFIG, "llm", None)
    if llm is None:
        return None
    return COMMON_CONFIG.get_llm_settings()


def _common_embedding_settings() -> Any | None:
    if COMMON_CONFIG is None:
        return None
    embedding = getattr(COMMON_CONFIG, "embedding", None)
    if embedding is None:
        return None
    return COMMON_CONFIG.require_embedding()


COMMON_LLM_SETTINGS = _common_llm_settings()
COMMON_EMBEDDING_SETTINGS = _common_embedding_settings()


def _agent_value(name: str, default: str) -> str:
    source = getattr(COMMON_CONFIG, "agent", None) if COMMON_CONFIG is not None else RAW_AGENT_SETTINGS
    return _as_str(_value(source, name, default), default)


def _llm_value(name: str, default: Any) -> Any:
    source = COMMON_LLM_SETTINGS if COMMON_LLM_SETTINGS is not None else RAW_LLM_SETTINGS
    return _value(source, name, default)


def _embedding_value(name: str, default: Any) -> Any:
    source = (
        COMMON_EMBEDDING_SETTINGS
        if COMMON_EMBEDDING_SETTINGS is not None
        else RAW_EMBEDDING_SETTINGS
    )
    return _value(source, name, default)


def _model_registry_path() -> Path | None:
    value = _llm_value("model_registry_path", "")
    if not value:
        return None
    path = Path(str(value))
    if path.is_absolute():
        return path
    base = COMMON_CONFIG_PATH.parent if COMMON_CONFIG_PATH is not None else BASE_DIR
    return (base / path).resolve()


def _load_model_config() -> Any | None:
    registry_path = _model_registry_path()
    default_model = _llm_value("default_model", "")
    if registry_path is None or not default_model:
        return None
    if ModelRegistry is not None:
        return ModelRegistry.from_yaml(registry_path).get(str(default_model))

    registry_data = _load_yaml(registry_path)
    models = registry_data.get("models", registry_data)
    if not isinstance(models, dict):
        return None
    model_config = models.get(str(default_model))
    return model_config if isinstance(model_config, dict) else None


COMMON_LLM_MODEL_CONFIG = _load_model_config()


def _model_value(name: str, default: str) -> str:
    return _as_str(_value(COMMON_LLM_MODEL_CONFIG, name, default), default)


def _model_api_key() -> str:
    explicit_key = _value(COMMON_LLM_MODEL_CONFIG, "api_key", "")
    if explicit_key:
        return str(explicit_key)
    env_name = _value(COMMON_LLM_MODEL_CONFIG, "api_key_env", "")
    if env_name:
        return os.getenv(str(env_name), "")
    get_api_key = getattr(COMMON_LLM_MODEL_CONFIG, "get_api_key", None)
    return get_api_key() if callable(get_api_key) else ""


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


def _default_llm_base_url() -> str:
    model_base_url = _model_value("base_url", "")
    if model_base_url:
        return model_base_url
    aws_region = _default_bedrock_region()
    if os.getenv("AWS_BEARER_TOKEN_BEDROCK") and aws_region:
        if _default_bedrock_endpoint_kind(aws_region) == "runtime":
            return f"https://bedrock-runtime.{aws_region}.amazonaws.com"
        return f"https://bedrock-mantle.{aws_region}.api.aws"
    return "http://localhost:8000"


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
    default_model = _llm_value("default_model", "")
    if default_model:
        return str(default_model)
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
    model_endpoint_path = _model_value("endpoint_path", "")
    if model_endpoint_path:
        return model_endpoint_path
    if transport == "bedrock_converse":
        return "/model/{model_id}/converse"
    if "bedrock-runtime." in base_url:
        return "/openai/v1/chat/completions"
    return "/v1/chat/completions"


LLM_BASE_URL = _env("LLM_BASE_URL", "VLLM_BASE_URL", default=_default_llm_base_url())
LLM_MODEL = _env("LLM_MODEL", "VLLM_MODEL", "BEDROCK_MODEL", default=_default_llm_model(LLM_BASE_URL))
LLM_TRANSPORT = _env(
    "LLM_TRANSPORT",
    "LLM_API_MODE",
    "VLLM_TRANSPORT",
    default=_default_llm_transport(LLM_BASE_URL, LLM_MODEL),
)
LLM_API_KEY = _env(
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "BEDROCK_API_KEY",
    "VLLM_API_KEY",
    default=_model_api_key() or "EMPTY",
)
LLM_ENDPOINT_PATH = _env(
    "LLM_ENDPOINT_PATH",
    "VLLM_ENDPOINT_PATH",
    default=_default_llm_endpoint_path(LLM_BASE_URL, LLM_TRANSPORT),
)
LLM_PROVIDER = _env(
    "LLM_PROVIDER",
    default=_model_value("provider", "bedrock" if "bedrock-" in LLM_BASE_URL else "openai_compatible"),
)
LLM_API_KEY_HEADER = _env(
    "LLM_API_KEY_HEADER",
    "LLM_AUTH_HEADER",
    "VLLM_API_KEY_HEADER",
    default="Authorization",
)
LLM_API_KEY_PREFIX = _env(
    "LLM_API_KEY_PREFIX",
    "LLM_AUTH_PREFIX",
    "VLLM_API_KEY_PREFIX",
    default="Bearer" if LLM_API_KEY_HEADER.lower() == "authorization" else "",
)
LLM_TEMPERATURE = float(
    _env("LLM_TEMPERATURE", default=_as_str(_llm_value("temperature", 0), "0"))
)
LLM_MAX_TOKENS = int(
    _env("LLM_MAX_TOKENS", default=_as_str(_llm_value("max_tokens", 4096), "4096"))
)
LLM_TIMEOUT = float(
    _env("LLM_TIMEOUT", default=_as_str(_llm_value("timeout", 120), "120"))
)
LLM_EXTRA_BODY = _llm_value("extra_body", {})
if not isinstance(LLM_EXTRA_BODY, dict):
    LLM_EXTRA_BODY = {}
LLM_EXTRA_HEADERS = _llm_value("extra_headers", {})
if not isinstance(LLM_EXTRA_HEADERS, dict):
    LLM_EXTRA_HEADERS = {}

EMBED_BASE_URL = _env(
    "EMBED_BASE_URL",
    "EMBEDDING_BASE_URL",
    "VLLM_EMBED_URL",
    default=_as_str(_embedding_value("base_url", LLM_BASE_URL), LLM_BASE_URL),
)
EMBED_MODEL = _env(
    "EMBED_MODEL",
    "EMBEDDING_MODEL",
    "VLLM_EMBED_MODEL",
    default=_as_str(_embedding_value("model", "embedding-model"), "embedding-model"),
)
EMBED_API_KEY = _env(
    "EMBED_API_KEY",
    "EMBEDDING_API_KEY",
    "VLLM_EMBED_API_KEY",
    default=_as_str(_embedding_value("api_key", LLM_API_KEY), LLM_API_KEY),
)
EMBED_API_KEY_HEADER = _env(
    "EMBED_API_KEY_HEADER",
    "EMBEDDING_API_KEY_HEADER",
    "VLLM_EMBED_API_KEY_HEADER",
    default=LLM_API_KEY_HEADER,
)
EMBED_API_KEY_PREFIX = _env(
    "EMBED_API_KEY_PREFIX",
    "EMBEDDING_API_KEY_PREFIX",
    "VLLM_EMBED_API_KEY_PREFIX",
    default=LLM_API_KEY_PREFIX,
)
EMBED_TIMEOUT = float(
    _env("EMBED_TIMEOUT", default=_as_str(_embedding_value("timeout", 60), "60"))
)
EMBED_EXTRA_HEADERS = _embedding_value("extra_headers", {})
if not isinstance(EMBED_EXTRA_HEADERS, dict):
    EMBED_EXTRA_HEADERS = {}

AGENT_NAME = _env("KBCARD_AGENT_NAME", default=_agent_value("name", "text2sql-v4"))
AGENT_ENVIRONMENT = _env("KBCARD_AGENT_ENV", default=_agent_value("environment", "local"))
AGENT_SERVICE_NAME = _env("KBCARD_SERVICE_NAME", default=_agent_value("service_name", AGENT_NAME))

DB_DSN_ENV = _env("KBCARD_POSTGRES_DSN_ENV", "DB_DSN_ENV", default="KBCARD_POSTGRES_DSN")
DB_DSN = _env("DATABASE_URL", "DB_DSN", "POSTGRES_DSN", DB_DSN_ENV, default="")
DB_HOST = _env("DB_HOST", default="localhost")
DB_PORT = int(_env("DB_PORT", default="5432"))
DB_NAME = _env("DB_NAME", default="postgres")
DB_USER = _env("DB_USER", default=os.getenv("USER", "postgres"))
DB_PASSWORD = _env("DB_PASSWORD", default="")
DB_POOL_MAX = int(_env("DB_POOL_MAX", default="10"))

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
VLLM_EXTRA_BODY = LLM_EXTRA_BODY
VLLM_EXTRA_HEADERS = LLM_EXTRA_HEADERS

VLLM_EMBED_URL = EMBED_BASE_URL
VLLM_EMBED_MODEL = EMBED_MODEL
VLLM_EMBED_API_KEY = EMBED_API_KEY
VLLM_EMBED_API_KEY_HEADER = EMBED_API_KEY_HEADER
VLLM_EMBED_API_KEY_PREFIX = EMBED_API_KEY_PREFIX
VLLM_EMBED_TIMEOUT = EMBED_TIMEOUT
VLLM_EMBED_EXTRA_HEADERS = EMBED_EXTRA_HEADERS
EMBED_MATCH_THRESHOLD = float(os.getenv("EMBED_MATCH_THRESHOLD", "0.75"))
ENABLE_EMBEDDING_PRECOMPUTE = os.getenv("ENABLE_EMBEDDING_PRECOMPUTE", "false").lower() == "true"

BAD_DEBT_OUTPUT_DIR = os.getenv("BAD_DEBT_OUTPUT_DIR", str(BASE_DIR / "output"))
REPORT_DIR = BASE_DIR / "reports"

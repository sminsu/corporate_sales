"""Runtime configuration for the Text2SQL agent.

LLM and embedding settings come from the kbcard-agent-common YAML (agent config +
model registry) with optional environment-variable overrides. Secrets stay in the
environment; endpoints and model defaults live in the YAML. The common SDK is the
single source of truth for inference and embedding — there is no raw HTTP fallback.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from kbcard_agent_common.config import KBCardConfig
from kbcard_agent_common.llm import ModelRegistry

BASE_DIR = Path(__file__).resolve().parents[1]

load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR / ".env.local", override=True)

_SCHEMA_PATH_ENV = os.getenv("SEMANTIC_SCHEMA_PATH", "")
SCHEMA_PATH = (
    Path(_SCHEMA_PATH_ENV)
    if _SCHEMA_PATH_ENV and Path(_SCHEMA_PATH_ENV).is_absolute()
    else BASE_DIR / (_SCHEMA_PATH_ENV or "semantic_layer.yaml")
)


def _env(*names: str, default: str = "") -> str:
    """Return the first non-empty environment variable, else the default."""
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_int(*names: str, default: int) -> int:
    raw = _env(*names, default=str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default
        
def _or(value, default):
    """Return ``value`` unless it is None (so 0 / 0.0 are kept, not replaced)."""
    return default if value is None else value


def _clean_api_key(value: str) -> str:
    """Treat the empty string and the vLLM 'EMPTY' sentinel as 'no key'."""
    return "" if value.strip().upper() in ("", "EMPTY") else value


def _resolve_config_path() -> Path | None:
    """Locate the common agent YAML from env, falling back to config/agent.local.yaml."""
    for name in ("KBCARD_CONFIG_PATH", "KBCARD_AGENT_CONFIG_PATH", "AGENT_CONFIG_PATH"):
        value = os.getenv(name)
        if value:
            path = Path(value)
            return path if path.is_absolute() else BASE_DIR / path
    default = BASE_DIR / "config" / "agent.local.yaml"
    return default if default.exists() else None


COMMON_CONFIG_PATH = _resolve_config_path()
COMMON_CONFIG = KBCardConfig.from_yaml(COMMON_CONFIG_PATH) if COMMON_CONFIG_PATH else None

_LLM = COMMON_CONFIG.llm if COMMON_CONFIG is not None else None
_EMBED = COMMON_CONFIG.embedding if COMMON_CONFIG is not None else None
_AGENT = COMMON_CONFIG.agent if COMMON_CONFIG is not None else None


def _llm_model_config():
    """Look up the endpoint config for the default model in the registry."""
    if _LLM is None:
        return None
    try:
        return ModelRegistry.from_yaml(_LLM.model_registry_path).get(_LLM.default_model)
    except Exception:
        return None


_MODEL = _llm_model_config()

# ---------------------------------------------------------------------------
# LLM settings (common YAML -> env override -> default)
# ---------------------------------------------------------------------------
LLM_MODEL = _env("LLM_MODEL", default=(_LLM.default_model if _LLM else "gpt-oss"))
LLM_BASE_URL = _env("LLM_BASE_URL", default=(_MODEL.base_url if _MODEL else "http://localhost:8000"))
LLM_ENDPOINT_PATH = _env(
    "LLM_ENDPOINT_PATH",
    default=(_MODEL.endpoint_path if _MODEL else "/v1/chat/completions"),
)
LLM_PROVIDER = _MODEL.provider if _MODEL else "openai_compatible"
LLM_API_KEY = _clean_api_key(
    _env("LLM_API_KEY", "OPENAI_API_KEY", default=((_MODEL.get_api_key() if _MODEL else "") or ""))
)
LLM_TEMPERATURE = float(_env("LLM_TEMPERATURE", default=str(_or(_LLM.temperature if _LLM else None, 0))))
LLM_MAX_TOKENS = int(_env("LLM_MAX_TOKENS", default=str(_or(_LLM.max_tokens if _LLM else None, 10000))))
LLM_TIMEOUT = float(_env("LLM_TIMEOUT", default=str(_or(_LLM.timeout if _LLM else None, 120))))
LLM_EXTRA_BODY = dict(_LLM.extra_body) if _LLM and _LLM.extra_body else {}
LLM_EXTRA_HEADERS = dict(_LLM.extra_headers) if _LLM and _LLM.extra_headers else {}

# ---------------------------------------------------------------------------
# Embedding settings
# ---------------------------------------------------------------------------
EMBED_BASE_URL = _env("EMBED_BASE_URL", "EMBEDDING_BASE_URL", default=(_EMBED.base_url if _EMBED else LLM_BASE_URL))
EMBED_MODEL = _env("EMBED_MODEL", "EMBEDDING_MODEL", default=(_EMBED.model if _EMBED and _EMBED.model else "embedding-model"))
EMBED_API_KEY = _clean_api_key(
    _env("EMBED_API_KEY", "EMBEDDING_API_KEY", default=((_EMBED.api_key if _EMBED else None) or ""))
)
EMBED_TIMEOUT = float(_env("EMBED_TIMEOUT", default=str(_or(_EMBED.timeout if _EMBED else None, 60))))
EMBED_BATCH_SIZE = max(1, int(_env("EMBED_BATCH_SIZE", "EMBEDDING_BATCH_SIZE", default="10")))

# README의 "라우팅 정밀도" 권장값과 동일한 정밀도 우선 기본값. 낮추면 verified query
# 오탐(비슷하지만 다른 의도 매칭)이 늘어나므로 recall이 필요할 때만 명시적으로 낮춘다.
EMBED_MATCH_THRESHOLD = float(os.getenv("EMBED_MATCH_THRESHOLD", "0.86"))
ENABLE_EMBEDDING_PRECOMPUTE = _env_bool("ENABLE_EMBEDDING_PRECOMPUTE", False)
ENABLE_VERIFIED_QUERY_MATCHING = _env_bool("ENABLE_VERIFIED_QUERY_MATCHING", True)
ENABLE_VERIFIED_QUERY_LLM_FALLBACK = _env_bool("ENABLE_VERIFIED_QUERY_LLM_FALLBACK", False)
VERIFIED_QUERY_LLM_CANDIDATE_LIMIT = int(os.getenv("VERIFIED_QUERY_LLM_CANDIDATE_LIMIT", "8"))
VERIFIED_QUERY_MIN_LEXICAL_SCORE = int(os.getenv("VERIFIED_QUERY_MIN_LEXICAL_SCORE", "2"))
VERIFIED_QUERY_RULE_MATCH_THRESHOLD = int(os.getenv("VERIFIED_QUERY_RULE_MATCH_THRESHOLD", "5"))
VERIFIED_QUERY_RULE_MATCH_MARGIN = int(os.getenv("VERIFIED_QUERY_RULE_MATCH_MARGIN", "2"))
# ---------------------------------------------------------------------------
# Web follow-up suggestion generation
# ---------------------------------------------------------------------------
# static: 기존 규칙 기반 추천만 사용
# llm: LLM 추천만 사용 (실패 시 빈 목록)
# auto: LLM 추천을 먼저 만들고 실패하면 규칙 기반 추천으로 fallback
# 작은 사내 모델에서는 본 질의가 끝난 뒤 추천용 LLM 호출이 지연과 장애면을
# 불필요하게 늘리므로 기본은 결정론적 static이며 llm/auto는 명시적으로 opt-in한다.
FOLLOWUP_SUGGESTION_MODE = _env("WEBAPP_FOLLOWUP_SUGGESTION_MODE", "FOLLOWUP_SUGGESTION_MODE", default="static").strip().lower()
if FOLLOWUP_SUGGESTION_MODE not in {"static", "llm", "auto"}:
    FOLLOWUP_SUGGESTION_MODE = "static"
FOLLOWUP_SUGGESTION_MIN_COUNT = max(1, min(5, _env_int("WEBAPP_FOLLOWUP_SUGGESTION_MIN_COUNT", "FOLLOWUP_SUGGESTION_MIN_COUNT", default=4)))
FOLLOWUP_SUGGESTION_MAX_COUNT = max(
    FOLLOWUP_SUGGESTION_MIN_COUNT,
    min(5, _env_int("WEBAPP_FOLLOWUP_SUGGESTION_MAX_COUNT", "FOLLOWUP_SUGGESTION_MAX_COUNT", default=5)),
)
FOLLOWUP_SUGGESTION_ROW_LIMIT = max(1, min(50, _env_int("WEBAPP_FOLLOWUP_SUGGESTION_ROW_LIMIT", "FOLLOWUP_SUGGESTION_ROW_LIMIT", default=20)))

# ---------------------------------------------------------------------------
# Agent identity
# ---------------------------------------------------------------------------
AGENT_NAME = _env("KBCARD_AGENT_NAME", default=(_AGENT.name if _AGENT else "text2sql-v4"))
AGENT_ENVIRONMENT = _env("KBCARD_AGENT_ENV", default=(_AGENT.environment if _AGENT else "local"))
AGENT_SERVICE_NAME = _env("KBCARD_SERVICE_NAME", default=((_AGENT.service_name if _AGENT else None) or AGENT_NAME))

# ---------------------------------------------------------------------------
# Database backend selection
# ---------------------------------------------------------------------------
# "postgres" (기본) 또는 "athena". execute_sql이 이 값으로 실행 백엔드를 분기한다.
DB_BACKEND = _env("DB_BACKEND", default="postgres").strip().lower()

# ---------------------------------------------------------------------------
# PostgreSQL connection
# ---------------------------------------------------------------------------
DB_DSN_ENV = _env("KBCARD_POSTGRES_DSN_ENV", "DB_DSN_ENV", default="KBCARD_POSTGRES_DSN")
DB_DSN = _env("DATABASE_URL", "DB_DSN", "POSTGRES_DSN", DB_DSN_ENV, default="")
DB_HOST = _env("DB_HOST", default="localhost")
DB_PORT = int(_env("DB_PORT", default="5432"))
DB_NAME = _env("DB_NAME", default="postgres")
DB_USER = _env("DB_USER", default=os.getenv("USER", "postgres"))
DB_PASSWORD = _env("DB_PASSWORD", default="")
DB_POOL_MAX = int(_env("DB_POOL_MAX", default="10"))

# ---------------------------------------------------------------------------
# Amazon Athena connection (DB_BACKEND=athena 일 때 사용)
# ---------------------------------------------------------------------------
# region/S3 staging/workgroup/database는 환경변수로 받는다. 인증은 표준 AWS 자격증명
# 체인(환경변수/AWS_PROFILE/IAM 역할)을 그대로 사용하므로 키를 코드/설정에 두지 않는다.
ATHENA_REGION = _env("ATHENA_REGION", "AWS_REGION", "AWS_DEFAULT_REGION", default="ap-northeast-2")
ATHENA_S3_STAGING_DIR = _env("ATHENA_S3_STAGING_DIR", "ATHENA_S3_OUTPUT", default="")
ATHENA_WORKGROUP = _env("ATHENA_WORKGROUP", default="primary")
ATHENA_DATABASE = _env("ATHENA_DATABASE", "ATHENA_SCHEMA", default="card_system")
ATHENA_CATALOG = _env("ATHENA_CATALOG", default="AwsDataCatalog")
# 선택: profile 기반 자격증명을 쓰고 싶을 때만 지정 (없으면 기본 체인).
ATHENA_PROFILE = _env("ATHENA_PROFILE", "AWS_PROFILE", default="")
# 선택: VPC 엔드포인트나 사내 프록시 등 커스텀 Athena endpoint URL.
# 비워두면 boto3가 region 기반 기본 endpoint를 사용한다.
ATHENA_ENDPOINT_URL = _env("ATHENA_ENDPOINT_URL", default="")

# ---------------------------------------------------------------------------
# SQL schema/namespace qualifier (테이블 prefix)
# ---------------------------------------------------------------------------
# 모든 테이블 참조에 붙는 스키마 한정자. PostgreSQL은 schema로 해석된다.
# Athena는 pyathena connection의 schema_name(ATHENA_DATABASE)을 이미 사용하므로 기본적으로
# SQL에는 prefix를 붙이지 않는다. Athena에서 database-qualified table을 강제하고 싶을 때만
# DB_SCHEMA를 명시한다. prefix를 완전히 빼려면 DB_SCHEMA=none(또는 "-")로 둔다.
_DEFAULT_SCHEMA = "" if DB_BACKEND == "athena" else "card_system"
DB_SCHEMA = _env("DB_SCHEMA", "DB_TABLE_SCHEMA", default=_DEFAULT_SCHEMA).strip()
if DB_SCHEMA.lower() in ("none", "-", "null"):
    DB_SCHEMA = ""
# SQL에 쓰는 prefix 문자열 (스키마가 비면 prefix 없음). 예: "card_system." 또는 "".
DB_SCHEMA_PREFIX = f"{DB_SCHEMA}." if DB_SCHEMA else ""

# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------
BAD_DEBT_OUTPUT_DIR = os.getenv("BAD_DEBT_OUTPUT_DIR", str(BASE_DIR / "output"))
REPORT_DIR = BASE_DIR / "reports"

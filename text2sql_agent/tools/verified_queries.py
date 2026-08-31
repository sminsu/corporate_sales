"""External verified query definitions backed by deterministic SQL builders."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import yaml

from ..config import DB_SCHEMA, DB_SCHEMA_PREFIX, DEFAULT_QUERY_ROW_LIMIT
from .sql_builders import (
    _vq_sql_가맹점매출순위,
    _vq_sql_가맹점카드소지현황,
    _vq_sql_기업별연체현황,
    _vq_sql_심사승인율,
    _vq_sql_업종별매출,
    _vq_sql_월별이용금액,
    _vq_sql_카드등급별연체,
    _vq_sql_한도사용률,
)


DEFAULT_VERIFIED_QUERY_PATH = Path(__file__).with_name("sql_verified_queries.yaml")


BuilderFn = Callable[[dict], str]


_BUILDER_SPECS: dict[str, tuple[BuilderFn, dict, tuple[tuple[str, str], ...]]] = {
    "review_approval_rate": (
        _vq_sql_심사승인율,
        {"기간_시작": "{기간_시작}", "기간_종료": "{기간_종료}"},
        (),
    ),
    "monthly_corporate_card_usage": (
        _vq_sql_월별이용금액,
        {"기간_시작": "{기간_시작}", "기간_종료": "{기간_종료}"},
        (),
    ),
    "top_merchants_by_revenue": (
        _vq_sql_가맹점매출순위,
        {"기간_시작": "{기간_시작}", "기간_종료": "{기간_종료}", "limit": 10},
        (),
    ),
    "merchant_card_possession_performance": (
        _vq_sql_가맹점카드소지현황,
        {"기준년월": "209901", "카드만료기준일": "20990131", "limit": DEFAULT_QUERY_ROW_LIMIT},
        (("20990131", "{카드만료기준일}"), ("209901", "{기준년월}")),
    ),
    "credit_utilization": (
        _vq_sql_한도사용률,
        {"기간_시작": "{기간_시작}", "기간_종료": "{기간_종료}", "limit": 20},
        (),
    ),
    "sales_by_industry": (
        _vq_sql_업종별매출,
        {"기간_시작": "{기간_시작}", "기간_종료": "{기간_종료}"},
        (),
    ),
    "delinquency_status": (
        _vq_sql_기업별연체현황,
        {"기간_시작": "{기간_시작}", "기간_종료": "{기간_종료}", "limit": 20},
        (),
    ),
    "card_delinquency_by_grade": (
        _vq_sql_카드등급별연체,
        {"기간_시작": "{기간_시작}", "기간_종료": "{기간_종료}"},
        (),
    ),
}


def _resolve_verified_query_path(path: str | None = None) -> Path:
    raw = path or os.getenv("VERIFIED_QUERY_FILE_PATH", "")
    if not raw:
        return DEFAULT_VERIFIED_QUERY_PATH
    resolved = Path(raw)
    return resolved if resolved.is_absolute() else DEFAULT_VERIFIED_QUERY_PATH.parents[2] / resolved


def _sql_from_builder(builder_name: str) -> str:
    if builder_name not in _BUILDER_SPECS:
        raise ValueError(f"Unknown verified query SQL builder: {builder_name}")
    builder, params, replacements = _BUILDER_SPECS[builder_name]
    sql = builder(dict(params))
    for old, new in replacements:
        sql = sql.replace(old, new)
    return sql


def _rewrite_schema_prefix(sql: str) -> str:
    if DB_SCHEMA == "card_system":
        return sql
    return sql.replace("card_system.", DB_SCHEMA_PREFIX)


def load_external_verified_queries(path: str | None = None) -> list[dict]:
    query_path = _resolve_verified_query_path(path)
    if not query_path.exists():
        return []
    with open(query_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    entries = data if isinstance(data, list) else data.get("verified_queries", [])
    verified_queries = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        normalized = dict(entry)
        limit = (normalized.get("parameters") or {}).get("limit")
        if isinstance(limit, dict) and str(limit.get("default")) == "100":
            limit["default"] = str(DEFAULT_QUERY_ROW_LIMIT)
        builder_name = normalized.pop("sql_builder", "")
        if builder_name and not normalized.get("sql"):
            normalized["sql"] = _sql_from_builder(str(builder_name))
        if isinstance(normalized.get("sql"), str):
            normalized["sql"] = _rewrite_schema_prefix(normalized["sql"])
        if normalized.get("name") and normalized.get("sql"):
            verified_queries.append(normalized)
    return verified_queries

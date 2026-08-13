"""Table load cadence and date-availability helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Mapping
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")

TABLE_ACCUMULATION_POLICIES: dict[str, dict[str, object]] = {
    "tbdaaat03": {
        "cadence": "daily",
        "query_time_dimension": "실적기준년월일",
        "format": "YYYYMMDD",
    },
    "tddaa3d01": {
        "cadence": "daily",
        "query_time_dimension": "기준년월일",
        "format": "YYYYMMDD",
    },
    "tbdaaat18": {
        "cadence": "daily",
        "query_time_dimension": "실적기준년월일",
        "format": "YYYYMMDD",
    },
    "tddaa3e21": {
        "cadence": "daily",
        "query_time_dimension": "기준년월일",
        "format": "YYYYMMDD",
    },
    "tddaa3e23": {
        "cadence": "daily",
        "query_time_dimension": "기준년월일",
        "format": "YYYYMMDD",
    },
    "tmdaa3e16": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
    "tddaa3l01": {
        "cadence": "daily",
        "query_time_dimension": "기준년월일",
        "format": "YYYYMMDD",
    },
    "tbdaabt30": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
    "tmdaa5d01": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
    "tmdaa5e11": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
    "tmdaa1d01": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
    "tmdaaaa12": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
    "tbdaabt08": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
    "tbdccam02": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
    "tbdaadt01": {
        "cadence": "daily",
        "query_time_dimension": "실적기준년월일",
        "format": "YYYYMMDD",
        "available_days": 10,
        "historical_source": {
            "table": "tmdaa5d01",
            "query_time_dimension": "기준년월",
            "format": "YYYYMM",
        },
    },
    "tbdaaus01": {
        "cadence": "previous_day",
        "query_time_dimension": "기준년월일",
        "format": "YYYYMMDD",
        "lag_days": 1,
        "has_reference_month": False,
        "historical_source": {
            "table": "tmdaaus01",
            "query_time_dimension": "기준년월",
            "format": "YYYYMM",
        },
    },
    "tmdaaus01": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
    "tbmewcm94": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
    "tbmaisd06": {
        "cadence": "yearly",
        "query_time_dimension": "기준년",
        "format": "YYYY",
    },
    "tbdaa1d12": {
        "cadence": "previous_day",
        "query_time_dimension": "기준년월일",
        "format": "YYYYMMDD",
        "lag_days": 1,
        "has_reference_month": False,
        "historical_source": {
            "table": "tmdaa1d12",
            "query_time_dimension": "기준년월",
            "format": "YYYYMM",
        },
    },
    "tmdaa1d12": {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    },
}


def accumulation_policy_for(table_name: str) -> dict[str, object] | None:
    """Return the declared availability policy for a physical table."""
    return TABLE_ACCUMULATION_POLICIES.get(str(table_name).strip().lower())


def kst_today(now: datetime | None = None) -> date:
    """Return today's business date in Korea Standard Time."""
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    return current.astimezone(KST).date()


def previous_day_ymd(today: date | None = None) -> str:
    """Return the latest available YYYYMMDD for previous-day tables."""
    business_date = today or kst_today()
    return (business_date - timedelta(days=1)).strftime("%Y%m%d")


def recent_window_ymd(days: int = 10, today: date | None = None) -> tuple[str, str]:
    """Return the inclusive YYYYMMDD bounds for a recent-day window."""
    if days < 1:
        raise ValueError("days must be at least 1")
    business_date = today or kst_today()
    return (
        (business_date - timedelta(days=days - 1)).strftime("%Y%m%d"),
        business_date.strftime("%Y%m%d"),
    )


def format_accumulation_policy(policy: Mapping[str, object] | None) -> str:
    """Render a compact Korean summary for catalogs and SQL prompts."""
    if not policy:
        return ""
    cycle = {
        "daily": "매일",
        "monthly": "매월",
        "yearly": "매년",
        "previous_day": "전일(D-1)",
    }.get(str(policy.get("cadence") or ""), str(policy.get("cadence") or ""))
    column = str(policy.get("query_time_dimension") or "")
    period_format = str(policy.get("format") or "")
    parts = [cycle, f"기간 컬럼 {column}({period_format})"]
    if policy.get("available_days"):
        parts.append(f"최근 {policy['available_days']}일만 조회 가능")
    historical_source = policy.get("historical_source")
    if isinstance(historical_source, Mapping):
        parts.append(
            "과거 조회 "
            f"{historical_source.get('table')}.{historical_source.get('query_time_dimension')}"
            f"({historical_source.get('format')})"
        )
    if policy.get("has_reference_month") is False:
        parts.append("기준년월 컬럼 없음")
    return " · ".join(part for part in parts if part)

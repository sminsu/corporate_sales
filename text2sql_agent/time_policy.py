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
        # 월 실적은 달이 닫힌 뒤 적재된다. 이번 달 실적은 일 적재 가맹점기본에만
        # 있는데 이름 접미사가 달라 tbd/tmd 짝으로는 못 찾으므로 직접 선언한다.
        # 두 테이블이 함께 가진 컬럼만 돌릴 수 있다(_live_source_missing_columns).
        "live_source": {
            "table": "tbdaadt01",
            "query_time_dimension": "실적기준년월일",
            "format": "YYYYMMDD",
        },
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
    # available_days 를 두지 않는다. 적재가 최근 N일 단위로 돈다는 것과, 조회할 때
    # 그 창을 조건으로 걸어야 한다는 것은 다른 이야기다. 이 값이 있으면
    # _availability_policy_issues() 가 tbdaadt01 을 쓰는 모든 SQL에
    # "실적기준년월일 기간 조건이 필요합니다" 를 붙여, 가맹점 마스터 조회
    # ("도미노피자 가맹점 기본 정보")까지 기간 필터를 강요했다.
    "tbdaadt01": {
        "cadence": "daily",
        "query_time_dimension": "실적기준년월일",
        "format": "YYYYMMDD",
        # 마스터는 가맹점번호 1건이 grain 이다. 질문이 날짜를 말하지 않으면 실적기준
        # 년월일을 조건으로 걸지 않는다. 걸면 사용자가 묻지 않은 범위가 답에 섞인다.
        "filter_when_dated_only": True,
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


def live_source_for(table_name: str) -> dict[str, object] | None:
    """Return the D-1 sibling that already holds a monthly table's open month.

    A month-end snapshot only lands once its month closes, so ``tmdaaus01``
    has no 202608 row on 2026-08-14 while its daily twin ``tbdaaus01`` does.
    The pair is read back out of ``historical_source`` rather than listed a
    second time, so registering one new tbd/tmd twin governs both directions.

    Pairs whose names do not share a suffix (``tmdaa5e11`` and its daily
    ``tbdaadt01``) declare ``live_source`` explicitly instead.
    """
    target = str(table_name).strip().lower()
    declared = (TABLE_ACCUMULATION_POLICIES.get(target) or {}).get("live_source")
    if isinstance(declared, Mapping):
        return dict(declared)
    if not target.startswith("tmd"):
        return None
    sibling = f"tbd{target[3:]}"
    policy = TABLE_ACCUMULATION_POLICIES.get(sibling) or {}
    if policy.get("cadence") != "previous_day":
        return None
    historical_source = policy.get("historical_source")
    if not isinstance(historical_source, Mapping):
        return None
    if str(historical_source.get("table") or "").strip().lower() != target:
        return None
    return {
        "table": sibling,
        "query_time_dimension": policy.get("query_time_dimension"),
        "format": policy.get("format"),
    }


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


def load_axis_window_ymd(scope: str, today: date | None = None) -> tuple[str, str]:
    """Return inclusive YYYYMMDD bounds for "이번 주"/"이번 달" on a load axis.

    적재 축(실적기준년월일)을 주·달로 좁혀 달라는 질문은 월 스냅샷으로 돌릴 수 없다.
    주는 월 스냅샷에 없는 단위이고, 이번 달은 달이 닫혀야 적재되기 때문이다.
    """
    business_date = today or kst_today()
    if scope == "week":
        start = business_date - timedelta(days=business_date.weekday())
    elif scope == "month":
        start = business_date.replace(day=1)
    else:
        raise ValueError(f"알 수 없는 적재 축 범위: {scope}")
    return start.strftime("%Y%m%d"), business_date.strftime("%Y%m%d")


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
    if policy.get("filter_when_dated_only"):
        parts.append(f"질문이 날짜를 말할 때만 {column} 조건")
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

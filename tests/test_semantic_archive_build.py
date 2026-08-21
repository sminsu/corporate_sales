from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import patch

from scripts.v2_build.build_semantic_layer_v2 import (
    apply_accumulation_policies,
    apply_corporate_customer_semantic_overrides,
    apply_merchant_monthly_sales_surface_forms,
    apply_recent_merchant_semantic_overrides,
    apply_sql_contract,
)
from text2sql_agent import schema
from text2sql_agent.schema import source_tables_for_question


def test_build_reapplies_monthly_archive_policies() -> None:
    rebuilt = deepcopy(schema.SCHEMA)
    tables = {item["name"]: item for item in rebuilt["tables"]}
    for name in ("tmdaa1d12", "tbdaa1d12", "tbdaaus01"):
        tables[name].pop("accumulation_policy", None)

    assert apply_accumulation_policies(rebuilt) == 21
    assert tables["tmdaa1d12"]["accumulation_policy"] == {
        "cadence": "monthly",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    }
    assert tables["tbdaa1d12"]["accumulation_policy"]["historical_source"] == {
        "table": "tmdaa1d12",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    }
    assert tables["tbdaaus01"]["accumulation_policy"]["historical_source"] == {
        "table": "tmdaaus01",
        "query_time_dimension": "기준년월",
        "format": "YYYYMM",
    }


def test_build_reapplies_safe_monthly_merchant_semantics() -> None:
    rebuilt = deepcopy(schema.SCHEMA)

    assert apply_recent_merchant_semantic_overrides(rebuilt) == 6
    assert apply_recent_merchant_semantic_overrides(rebuilt) == 6

    contracts = {item["name"]: item for item in rebuilt["semantic_query_contracts"]}
    assert contracts["named_merchant_store_sales_at_month"]["filters"][-1] == (
        "tmdaa5d01.가맹점명 contains 가맹점명"
    )

    paths = {
        item["name"]: item for item in rebuilt["semantic_join_graph"]["safe_paths"]
    }
    assert "merchant_to_monthly_snapshot" not in paths
    assert paths["domestic_sales_to_merchant"]["to_table"] == "tmdaa5d01"
    assert paths["domestic_sales_to_merchant"]["sql"] == (
        'tbdaabt30."기준년월" = tmdaa5d01."기준년월" AND '
        'tbdaabt30."가맹점번호" = tmdaa5d01."가맹점번호"'
    )
    assert paths["merchant_to_monthly_performance"]["from_table"] == "tmdaa5d01"
    assert paths["merchant_to_monthly_performance"]["sql"] == (
        'tmdaa5d01."기준년월" = tmdaa5e11."기준년월" AND '
        'tmdaa5d01."가맹점번호" = tmdaa5e11."가맹점번호"'
    )

    references = {item["intent"]: item for item in rebuilt["query_references"]}
    for intent in ("월별_카드매출_조회", "가맹점별_매출순위"):
        assert "tmdaa5d01" in references[intent]["join_tables"]
        assert "tbdaadt01" not in str(references[intent])


def test_build_separates_current_corporate_snapshot_from_monthly_history() -> None:
    rebuilt = deepcopy(schema.SCHEMA)

    assert apply_corporate_customer_semantic_overrides(rebuilt) == 42
    assert apply_corporate_customer_semantic_overrides(rebuilt) == 42

    # 유효 기업카드 보유 원천은 현재(D-1)와 명시 과거 월로 갈라져 있어야 한다.
    # source_selection 이 없으면 네 원천이 모두 최상위 후보 점수를 받아,
    # "현재 기준" 질문에서 월말 스냅샷이 일 스냅샷을 이겼다.
    holding = next(
        item
        for item in rebuilt["semantic_attributes"]
        if item["name"] == "corporate_card_holding"
    )
    assert [item["table"] for item in holding["source_mappings"]] == [
        "tbdaa1d12",
        "tmdaa1d12",
        "tbdaaus01",
        "tmdaaus01",
    ]
    assert [item["role"] for item in holding["source_mappings"]] == [
        "current_corporate_card_holding",
        "monthly_corporate_card_holding",
        "current_merchant_card_holding",
        "monthly_merchant_card_holding",
    ]
    assert holding["source_selection"]["default_role_prefix"] == "current_"
    assert holding["source_selection"]["period_role_prefix"] == "monthly_"
    assert holding["source_selection"]["open_month_uses_current"] is True
    assert sum("KST 전일" in item for item in holding["semantic_cautions"]) == 1

    entities = {item["name"]: item for item in rebuilt["semantic_entities"]}
    assert "KST 전일" in entities["corporate_customer_daily"]["use_when"][0]
    assert "과거 기준년월" in entities["corporate_customer_no_usage_snapshot"]["use_when"][0]

    metrics = {item["name"]: item for item in rebuilt["canonical_metrics"]}
    for name in (
        "기업월카드이용금액",
        "기업월평균카드이용금액",
        "관리기업월이용금액",
        "기업월국세이용금액",
        "기업월KB페이이용금액",
    ):
        assert metrics[name]["source_table"] == "tmdaa1d12"
        assert metrics[name]["default_time_dimension"] == "기준년월"
        assert "tbdaa1d12" not in str(metrics[name])

    for name in (
        "기업유효카드수",
        "기업한도소진율",
        "기업잔여한도금액",
        "기업한도사용금액",
        "총여신한도금액",
        "관리기업연체원금",
    ):
        rendered_filters = " ".join(metrics[name]["required_filters"])
        assert "KST 전일(D-1)" in rendered_filters
        assert "고객별 월 최신" not in rendered_filters

    contracts = {item["name"]: item for item in rebuilt["semantic_query_contracts"]}
    assert contracts["corporate_member_with_monthly_usage_count"]["source_tables"] == [
        "tmdaa1d12"
    ]
    assert contracts["corporate_limit_status_at_month"]["source_table_policy"] == {
        "default": ["tbdaa1d12"],
        "current_snapshot_when": [
            "현재",
            "현재기준",
            "현재 시점",
            "오늘",
            "전일",
            "어제",
            "최신",
            "지금",
        ],
        "current_snapshot": ["tbdaa1d12"],
        "period_snapshot": ["tmdaa1d12"],
    }
    assert contracts["corporate_card_churn_after_usage"]["source_table_policy"][
        "current_snapshot"
    ] == ["tbdaa1d12", "tmdaa1d12"]
    assert contracts["current_corporate_half_year_usage_decline"]["source_tables"] == [
        "tbdaa1d12",
        "tmdaa1d12",
    ]
    assert "기간·현재 표현이 모두 없으면 추가 입력" in contracts[
        "corporate_card_active_without_recent_usage"
    ]["time_policy"]["required_parameter"]
    assert "현재월 tbdaa1d12 D-1 누적금액" in contracts[
        "corporate_check_card_only_high_average"
    ]["time_policy"]["average_window"]
    assert "현재는 KST 전일" in contracts["corporate_limit_status_at_month"][
        "deduplication"
    ]["snapshot"]
    assert "현재는 KST 전일" in contracts["corporate_card_low_limit_utilization"][
        "filters"
    ][0]

    references = {item["intent"]: item for item in rebuilt["query_references"]}
    assert references["기업고객_여신한도"]["source_table_policy"]["period_snapshot"] == [
        "tmdaa1d12"
    ]
    assert references["관리기업_6개월_이상징후"]["primary_table"] == "tmdaa1d12"
    assert references["관리기업_한도감액"]["primary_table"] == "tmdaa1d12"
    assert "tbdaa1d12 KST 전일" in references["관리기업_연체"]["join_rule"]
    assert "현재월 누적 이용은 D-1" in references["체크카드만_고액이용"]["join_rule"]
    assert "명시 과거 월은 tmdaa1d12" in references["기업고객_여신한도"]["join_rule"]
    assert "명시 과거 월은 tmdaa1d12" in references["저소진율_한도영업대상"]["join_rule"]

    paths = {
        item["name"]: item for item in rebuilt["semantic_join_graph"]["safe_paths"]
    }
    assert paths["corporate_monthly_to_domestic_sales"]["sql"] == (
        'tmdaa1d12."기준년월" = tbdaabt30."기준년월" AND '
        'tmdaa1d12."고객식별자" = tbdaabt30."기업고객식별자"'
    )
    assert paths["customer_to_corporate_monthly"]["to_table"] == "tmdaa1d12"
    assert "KST 전일 1행" in paths["corporate_daily_to_domestic_sales"]["caution"]

    glossary = {item["term"]: item for item in rebuilt["glossary"]}
    assert "tmdaa1d12를 지정 기준년월" in glossary["유실적"]["sql_hint"]
    assert "현재는 tbdaa1d12" in glossary["기업한도"]["canonical"]
    assert "tmdaa1d12를 고객×월 최신행" in glossary["월평균이용금액"]["sql_hint"]
    assert "명시 과거 월은 tmdaa1d12" in glossary["관리기업"]["sql_hint"]


def test_build_sql_contract_describes_current_and_archive_axes_separately() -> None:
    rebuilt = deepcopy(schema.SCHEMA)

    apply_sql_contract(rebuilt)

    contract = rebuilt["sql_generation_contract"]
    rendered = str(contract)
    assert "tbdaa1d12 월" not in contract["time_resolution"]
    assert "KST 전일(D-1)" in contract["time_resolution"]["tbdaa1d12 현재"]
    assert "물리 기준년월 컬럼 없음" in rendered
    assert "tmdaa1d12 과거 월" in contract["time_resolution"]
    assert "현재 한도는 tbdaa1d12의 KST 전일" in rendered


def test_live_period_wording_keeps_current_snapshot_but_old_as_of_month_archives() -> None:
    """현재월·D-1 은 라이브 스냅샷, 지난 월은 월별 아카이브로 간다.

    날짜를 박아 두면 자정을 넘길 때 기대값이 어긋난다("2026-08-11 현재" 는 8/12
    에만 D-1 이다). 기준일을 고정하고 거기서 상대적으로 계산한다.
    """
    contract = next(
        item
        for item in schema.SCHEMA["semantic_query_contracts"]
        if item["name"] == "corporate_limit_status_at_month"
    )

    today = date(2026, 8, 12)
    previous_day = today - timedelta(days=1)
    past_month = date(today.year, today.month, 1) - timedelta(days=120)

    with patch.object(schema, "kst_today", return_value=today):
        current_month = source_tables_for_question(
            contract, f"{today.year}년 {today.month}월 현재 기업 한도"
        )
        d_minus_one = source_tables_for_question(
            contract, f"{previous_day:%Y-%m-%d} 현재 기업 한도"
        )
        archived = source_tables_for_question(
            contract, f"{past_month.year}년 {past_month.month}월 현재 기업 한도"
        )

    assert current_month == ["tbdaa1d12"]
    assert d_minus_one == ["tbdaa1d12"]
    assert archived == ["tmdaa1d12"]


def test_build_gives_merchant_monthly_sales_the_same_surface_forms_as_its_count_sibling() -> None:
    """가맹점월매출건수만 '가맹점매출건수' 를 갖고 있던 v1 비대칭을 메운다."""
    metrics = {
        item["name"]: item for item in deepcopy(schema.SCHEMA)["canonical_metrics"]
    }

    assert "가맹점매출건수" in metrics["가맹점월매출건수"]["synonyms"]
    assert "가맹점매출금액" in metrics["가맹점월매출금액"]["synonyms"]


def test_build_surface_form_override_is_idempotent_by_failing_loudly() -> None:
    """이미 적용된 스키마에 다시 돌리면 조용히 넘어가지 않고 멈춘다."""
    import pytest

    with pytest.raises(SystemExit):
        apply_merchant_monthly_sales_surface_forms(deepcopy(schema.SCHEMA))


def test_build_declares_the_card_only_usage_metrics_on_the_card_monthly_fact() -> None:
    """할부·CA 이용금액은 tmdaa3e16 에만 있는 컬럼인데 지표 선언이 없었다."""
    metrics = {
        item["name"]: item for item in deepcopy(schema.SCHEMA)["canonical_metrics"]
    }

    for name, column, synonym in (
        ("카드월할부이용금액", "금월할부이용금액", "할부이용금액"),
        ("카드월CA이용금액", "금월CA이용금액", "CA이용금액"),
    ):
        metric = metrics[name]
        assert metric["source_table"] == "tmdaa3e16"
        assert metric["default_time_dimension"] == "기준년월"
        assert column in metric["expression"]
        assert synonym in metric["synonyms"]

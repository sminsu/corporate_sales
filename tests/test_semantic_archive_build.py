from copy import deepcopy

from scripts.v2_build.build_semantic_layer_v2 import (
    apply_accumulation_policies,
    apply_corporate_customer_semantic_overrides,
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

    assert apply_corporate_customer_semantic_overrides(rebuilt) == 41
    assert apply_corporate_customer_semantic_overrides(rebuilt) == 41

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
    contract = next(
        item
        for item in schema.SCHEMA["semantic_query_contracts"]
        if item["name"] == "corporate_limit_status_at_month"
    )

    assert source_tables_for_question(contract, "2026년 8월 현재 기업 한도") == [
        "tbdaa1d12"
    ]
    assert source_tables_for_question(contract, "2026-08-11 현재 기업 한도") == [
        "tbdaa1d12"
    ]
    assert source_tables_for_question(contract, "2026년 4월 현재 기업 한도") == [
        "tmdaa1d12"
    ]

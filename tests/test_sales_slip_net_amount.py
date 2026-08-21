"""전표 금액·건수는 매출전표종류구분코드로 부호를 정해 순액으로 더한다.

매출전표(tbdaabt30 국내·tbdaabt08 해외) 한 행은 정당·정정·취소 중 한 종류이고,
취소전표도 금액을 양수로 들고 있다. 그래서 SUM("매출금액") 과 COUNT("매출전표번호") 는
취소·환급분까지 더해 이용금액과 건수를 부풀린다. 업무 규칙(사용자 제공)은
정당(1)·취소정정(4)은 더하고 정정(2)·취소(3)·청구보류(5)·체크환급(6)은 빼는 순액이다.

v1 은 이 코드값을 몰라서 query_references.영업대상군_결제처업종별이용금액 의 rules 에
"취소·환입 순액 규칙은 문서에 없으므로 임의 코드 필터를 만들지 않는다" 라고 적어 두고
전표 금액을 그대로 SUM 했고, verified query 는 전표취소구분코드로 취소 건을 빼려 했다.
전표취소구분코드는 취소 사유(가맹점번호 상이·분할취소 등) 코드라 취소 여부를
가려 주지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from text2sql_agent.tools.verified_queries import load_external_verified_queries
from text2sql_agent.workflow import _validate_sales_slip_net_amount

ROOT = Path(__file__).resolve().parents[1]
RAW_SEMANTIC = yaml.safe_load((ROOT / "semantic_layer.yaml").read_text(encoding="utf-8"))
TABLES = {table["name"]: table for table in RAW_SEMANTIC["tables"]}
SLIP_TABLE = TABLES["tbdaabt30"]
OVERSEAS_SLIP_TABLE = TABLES["tbdaabt08"]
POSITIVE_CODES = "IN ('1','4')"
NET_COUNT = "THEN 1 ELSE -1 END)"


def _column(table: dict, name: str) -> dict:
    for section in ("dimensions", "measures", "time_dimensions"):
        for column in table.get(section) or []:
            if column["name"] == name:
                return column
    raise AssertionError(f"{table['name']}.{name} 없음")


@pytest.mark.parametrize("table_name", ["tbdaabt30", "tbdaabt08"])
def test_slip_type_codes_are_declared(table_name: str) -> None:
    column = _column(TABLES[table_name], "매출전표종류구분코드")

    assert column["value_semantics"] == {
        "1": "정당전표",
        "2": "정정전표",
        "3": "취소전표",
        "4": "취소정정전표",
        "5": "청구보류",
        "6": "체크환급",
    }
    assert column["value_semantics_provenance"] == "user_provided_business_codebook"
    # 컬럼 상세는 135자까지만 프롬프트에 렌더된다(_table_details).
    assert len(column["description"]) <= 135
    assert "정당(1)·취소정정(4)" in column["description"]


def test_cancel_reason_codes_are_declared_and_marked_unfit_for_filtering() -> None:
    """전표취소구분코드로 취소 건을 빼려던 verified query 들이 있었다."""
    column = _column(SLIP_TABLE, "전표취소구분코드")

    assert column["value_semantics"] == {
        "1": "가맹점번호 상이",
        "2": "원인전표검증생략",
        "3": "분할취소",
        "4": "자동상계비대상",
    }
    assert column["value_semantics_provenance"] == "user_provided_business_codebook"
    assert len(column["description"]) <= 135
    assert "매출전표종류구분코드로 가리며" in column["description"]


@pytest.mark.parametrize(
    ("table_name", "measures"),
    [
        ("tbdaabt30", ["매출금액", "매출미화금액", "봉사료", "미화봉사료", "부가가치세", "가맹점수수료"]),
        ("tbdaabt08", ["매출금액", "매출미화금액"]),
    ],
)
def test_slip_measures_say_they_are_not_a_plain_sum(
    table_name: str, measures: list[str]
) -> None:
    for measure in measures:
        description = _column(TABLES[table_name], measure)["description"]
        assert "순액으로 집계한다." in description, measure
        # 설명은 135자까지만 렌더된다. 원문이 길면 순액 규칙이 잘려 나갔다.
        assert len(description) <= 135, (measure, len(description))


def test_table_formulas_net_out_cancelled_slips() -> None:
    """테이블 aggregation_policy 는 전표 테이블이 라우팅되면 항상 렌더된다."""
    formulas = SLIP_TABLE["aggregation_policy"]["canonical_formulas"]
    overseas = OVERSEAS_SLIP_TABLE["aggregation_policy"]["canonical_formulas"]

    for name, column in (
        ("카드매출금액", "매출금액"),
        ("이용금액", "매출금액"),
        ("봉사료", "봉사료"),
        ("부가가치세", "부가가치세"),
        ("가맹점수수료", "가맹점수수료"),
    ):
        assert POSITIVE_CODES in formulas[name], formulas[name]
        assert formulas[name].endswith(f'ELSE -"{column}" END)'), formulas[name]
    assert formulas["매출건수"].endswith(NET_COUNT), formulas["매출건수"]
    assert POSITIVE_CODES in overseas["해외매출금액"]
    assert overseas["매출건수"].endswith(NET_COUNT)
    # aggregation_policy 값은 프롬프트에 900자까지만 렌더된다(_table_details).
    assert len(json.dumps(formulas, ensure_ascii=False)) <= 900


def test_slip_metrics_and_contract_use_the_net_expression() -> None:
    metrics = {item["name"]: item for item in RAW_SEMANTIC["canonical_metrics"]}
    contract = next(
        item
        for item in RAW_SEMANTIC["semantic_query_contracts"]
        if item["name"] == "corporate_card_usage_by_merchant_industry"
    )

    for name in ("카드매출금액", "해외매출금액", "매출건수"):
        assert POSITIVE_CODES in metrics[name]["expression"], metrics[name]["expression"]
        assert metrics[name]["semantic_cautions"]
    assert metrics["매출건수"]["expression"].endswith(NET_COUNT)
    assert POSITIVE_CODES in contract["calculation"]["usage_amount"]


@pytest.mark.parametrize(
    ("intent", "field"),
    [
        ("월별_카드매출_조회", "매출금액"),
        ("업종별_매출", "매출"),
        ("영업대상군_결제처업종별이용금액", "이용금액"),
    ],
)
def test_query_reference_columns_use_the_net_expression(intent: str, field: str) -> None:
    reference = next(
        item for item in RAW_SEMANTIC["query_references"] if item["intent"] == intent
    )

    assert POSITIVE_CODES in reference["recommended_columns"][field]


def test_target_group_reference_no_longer_calls_the_net_rule_undocumented() -> None:
    reference = next(
        item
        for item in RAW_SEMANTIC["query_references"]
        if item["intent"] == "영업대상군_결제처업종별이용금액"
    )
    rules = " ".join(reference["rules"])

    assert "문서에 없으므로" not in rules
    assert "매출전표종류구분코드로 부호를 정해 순액으로 집계한다" in rules


def test_grain_rule_carries_the_net_expression() -> None:
    rules = RAW_SEMANTIC["sql_generation_contract"]["grain_and_aggregation_rules"]

    assert any(POSITIVE_CODES in rule for rule in rules)


def test_verified_queries_net_out_cancelled_slips() -> None:
    """전표취소구분코드는 취소 사유 코드라 취소 건을 걸러 주지 않았다."""
    slip_queries = [
        query
        for query in load_external_verified_queries()
        if ("tbdaabt30" in query["sql"] or "tbdaabt08" in query["sql"])
        and "매출금액" in query["sql"]
    ]

    assert slip_queries
    for query in slip_queries:
        assert "전표취소구분코드" not in query["sql"], query["name"]
        assert not _validate_sales_slip_net_amount(query["sql"]), query["name"]


def test_validator_flags_a_plain_count_of_slips() -> None:
    plain = (
        'SELECT COUNT(DISTINCT t."매출전표번호") AS "매출건수" FROM tbdaabt30 t'
    )
    net = (
        'SELECT SUM(CASE WHEN t."매출전표종류구분코드" IN (\'1\',\'4\') THEN 1 ELSE -1 END) '
        'AS "매출건수" FROM tbdaabt30 t'
    )

    assert _validate_sales_slip_net_amount(plain)
    assert not _validate_sales_slip_net_amount(net)


def test_validator_covers_overseas_slips_and_fee_columns() -> None:
    overseas = 'SELECT SUM(t."매출금액") FROM tbdaabt08 t'
    fees = 'SELECT SUM(t."봉사료"), SUM(t."부가가치세"), SUM(t."가맹점수수료") FROM tbdaabt30 t'
    rate = 'SELECT AVG(t."가맹점수수료율") FROM tbdaabt30 t'

    assert _validate_sales_slip_net_amount(overseas)
    assert _validate_sales_slip_net_amount(fees)
    # 요율은 순액 대상이 아니다.
    assert not _validate_sales_slip_net_amount(rate)


def test_validator_flags_a_plain_sum_and_accepts_the_net_sum() -> None:
    plain = 'SELECT SUM(t."매출금액") AS "이용금액" FROM tbdaabt30 t'
    net = (
        'SELECT SUM(CASE WHEN t."매출전표종류구분코드" IN (\'1\',\'4\') '
        'THEN t."매출금액" ELSE -t."매출금액" END) AS "이용금액" FROM tbdaabt30 t'
    )

    assert _validate_sales_slip_net_amount(plain)
    assert not _validate_sales_slip_net_amount(net)


def test_validator_leaves_slip_type_breakdowns_and_other_tables_alone() -> None:
    breakdown = (
        'SELECT t."매출전표종류구분코드", SUM(t."매출금액") AS "매출금액" '
        'FROM tbdaabt30 t GROUP BY t."매출전표종류구분코드"'
    )
    merchant = 'SELECT SUM(COALESCE(m."가맹점일시불매출금액", 0)) FROM tmdaa5e11 m'

    assert not _validate_sales_slip_net_amount(breakdown)
    assert not _validate_sales_slip_net_amount(merchant)

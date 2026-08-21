"""지표 하나를 분류축 하나로 쪼개는 질의는 둘을 함께 가진 테이블로 가야 한다.

goldenset v3 에서 "X를 Y별로 알려줘" 꼴의 질의 여섯 건이 엉뚱한 답을 냈다.

    2026년 1월 법인카드 유이자 할부 이용금액을 카드거래매체별로 알려줘
    2025년 11월 기업카드 체크카드 이용금액을 발급유형별로 보여줘
    현재 기준 기업 유효 기업 신용카드 수를 소매비소매별로 알려줘
    ...

원인이 세 갈래였다.

1. 월 지표 컬럼은 이름이 "금월" 로 시작하는데 질문은 그 접두사를 빼고 부른다.
   스키마의 금월 컬럼 75개가 전부 접두사 없는 표면형을 동의어로 갖고 있지 않아서,
   질문이 지표를 불러도 그 컬럼이 프롬프트 컬럼 예산에서 잘려 나갔다.
2. "법인카드"·"기업카드" 라는 낱말이 유효 기업카드 보유 속성의 코드값 이름과
   같아서, 그 속성의 원천 네 곳(기업 일·월 스냅샷, 가맹점 일·월 enrichment)이
   모두 최상위 후보 점수를 받았다. 카드 월실적을 물었는데 가맹점 월 enrichment 가
   1순위였고, "현재 기준" 질문에서는 월말 스냅샷이 일 스냅샷을 이겼다.
3. 컬럼 겹침 점수가 min(hits, 4) * 3 이라 지표와 축을 함께 가진 테이블(6점)과
   하나만 가진 테이블(3점)의 차이가 속성 점수 36점에 묻혔다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from scripts.v2_build.build_semantic_layer_v2 import SINGLE_CHARACTER_SYNONYM_EXCEPTIONS
from text2sql_agent import workflow
from text2sql_agent.schema import SCHEMA, _validate_sql_against_schema
from text2sql_agent.v2.column_synonyms import compact

ROOT = Path(__file__).resolve().parents[1]

# (질문, 정답 테이블, 프롬프트에 반드시 있어야 하는 컬럼)
FAILED_QUESTIONS = [
    (
        "2026년 1월 법인카드 유이자 할부 이용금액을 카드거래매체별로 알려줘",
        "tmdaa3e16",
        ("금월유이자할부이용금액", "카드거래매체구분코드"),
    ),
    (
        "2026년 3월 법인카드 소득공제 제외대상금액을 우수고객서비스별로 알려줘",
        "tbdaabt30",
        ("소득공제제외대상금액", "우수고객서비스구분코드"),
    ),
    (
        "2025년 11월 기업카드 체크카드 이용금액을 발급유형별로 보여줘",
        "tmdaa3e16",
        ("금월체크카드이용금액", "발급유형구분코드"),
    ),
    (
        "현재 기준 기업 유효 기업 신용카드 수를 소매비소매별로 알려줘",
        "tbdaa1d12",
        ("유효기업신용카드수", "소매비소매구분코드"),
    ),
    (
        "가맹점 선지급 처리금액을 가맹점서비스별로 알려줘",
        "tbdaadt01",
        ("가맹점선지급처리금액", "가맹점서비스구분코드"),
    ),
    (
        "2025년 12월 기준 유효 기업 신용카드 수를 기업거래정지 여부별로 알려줘",
        "tmdaa1d12",
        ("유효기업신용카드수", "기업거래정지여부"),
    ),
]


def _column(table_name: str, column_name: str) -> dict:
    table = next(item for item in SCHEMA["tables"] if item["name"] == table_name)
    for section in ("dimensions", "measures", "time_dimensions"):
        for column in table.get(section) or []:
            if column["name"] == column_name:
                return column
    raise AssertionError(f"{table_name}.{column_name} 없음")


@pytest.mark.parametrize(("question", "table", "columns"), FAILED_QUESTIONS)
def test_metric_and_axis_pick_the_table_that_has_both(
    question: str, table: str, columns: tuple[str, ...]
) -> None:
    ranked = workflow._rule_rank_tables(question)

    assert ranked, question
    assert ranked[0] == table, (question, ranked)


@pytest.mark.parametrize(("question", "table", "columns"), FAILED_QUESTIONS)
def test_metric_and_axis_columns_reach_the_prompt(
    question: str, table: str, columns: tuple[str, ...]
) -> None:
    """프롬프트는 테이블당 컬럼 12~16개만 싣는다. 지표가 잘리면 SQL을 쓸 수 없다."""
    details = workflow._table_details(workflow._rule_rank_tables(question), question)

    for column in columns:
        assert f"- {column} [" in details, (question, column)


def test_reference_month_prefix_is_a_declared_synonym() -> None:
    """질문은 "체크카드 이용금액" 이라고 부르고 컬럼명은 금월체크카드이용금액 이다."""
    assert "체크카드이용금액" in _column("tmdaa3e16", "금월체크카드이용금액")["synonyms"]
    assert "유이자할부이용금액" in _column("tmdaa3e16", "금월유이자할부이용금액")["synonyms"]

    stripped = [
        (table["name"], column["name"])
        for table in SCHEMA["tables"]
        for column in table.get("measures") or []
        if column["name"].startswith("금월")
        and column["name"][2:] not in {"CA수수료", "체크카드소액신용채권잔액"}
        and column["name"][2:] not in {
            compact(value) for value in column.get("synonyms") or []
        }
    ]
    assert not stripped


def test_reference_month_stem_never_shadows_a_real_column() -> None:
    """금월CA수수료의 어간 "CA수수료" 는 다른 테이블에 실제로 있는 컬럼명이다."""
    declared = {
        column["name"]
        for table in SCHEMA["tables"]
        for section in ("dimensions", "measures", "time_dimensions")
        for column in table.get(section) or []
    }
    for table in SCHEMA["tables"]:
        for column in table.get("measures") or []:
            name = column["name"]
            if not name.startswith("금월"):
                continue
            stem = name[2:]
            if stem in declared:
                assert stem not in (column.get("synonyms") or []), name


def test_single_character_synonyms_are_gone() -> None:
    """v1 은 11개 테이블의 "기준년월" 에 동의어 '월' 을 달아 두었다.

    그 한 글자가 "가맹점 월 승인한도금액" 같은 질문에서 기준년월을 가진 테이블
    전부에 컬럼 단서 점수를 줬다. 테이블 랭킹은 "기준년월" 자체를 이미 흔한
    용어로 빼 두는데 동의어가 그 구멍을 다시 열었다.

    예외는 빌드 스크립트가 이름을 대고 열어 둔 것만 허용한다(앱구분코드의 '앱').
    """
    short = {
        (table["name"], column["name"], synonym)
        for table in SCHEMA["tables"]
        for section in ("dimensions", "measures", "time_dimensions")
        for column in table.get(section) or []
        for synonym in column.get("synonyms") or []
        if len(compact(synonym)) < 2
    }

    assert short == SINGLE_CHARACTER_SYNONYM_EXCEPTIONS


def test_card_holding_sources_split_current_from_explicit_month() -> None:
    attribute = next(
        item
        for item in SCHEMA["semantic_attributes"]
        if item["name"] == "corporate_card_holding"
    )
    current = "현재 기준 유효 기업 신용카드 수를 소매비소매별로 알려줘"
    past = "2025년 12월 기준 유효 기업 신용카드 수를 소매비소매별로 알려줘"

    def tables(question: str) -> set[str]:
        return {
            mapping["table"]
            for mapping in workflow._attribute_source_mappings(question, attribute)
        }

    assert tables(current) == {"tbdaa1d12", "tbdaaus01"}
    assert tables(past) == {"tmdaa1d12", "tmdaaus01"}
    assert workflow._attribute_snapshot_exclusions(current) == {"tmdaa1d12", "tmdaaus01"}
    assert workflow._attribute_snapshot_exclusions(past) == {"tbdaa1d12", "tbdaaus01"}


def test_coverage_bonus_needs_a_single_widest_table() -> None:
    """같은 개수를 가진 테이블이 여럿이면(일/월 스냅샷 짝) 적재 정책이 고를 몫이다."""
    tied = "가맹점 선지급 처리금액을 가맹점서비스별로 알려줘"

    # tbdaadt01 과 tmdaa5d01 이 두 컬럼을 똑같이 갖고 있다. 가점이 붙었다면
    # 적재 정책이 마스터를 고르기 전에 두 테이블이 같은 점수로 묶여 있어야 한다.
    assert workflow._rule_rank_tables(tied)[0] == "tbdaadt01"


@pytest.mark.parametrize(("question", "table", "columns"), FAILED_QUESTIONS)
def test_goldenset_answer_survives_the_static_guardrails(
    question: str, table: str, columns: tuple[str, ...]
) -> None:
    """정답 SQL이 정적 검증에서 반려되면 에이전트는 재시도만 반복한다."""
    del columns
    case = next(
        item
        for item in _goldenset_v3()
        if item["question"] == question
    )
    sql = workflow._apply_accumulation_historical_sources(question, case["sql"])
    tables = [str(name).rsplit(".", 1)[-1] for name in case["expected_tables"]]

    issues = [
        issue
        for issue in _validate_sql_against_schema(sql, tables)
        if not re.search(r"prefix|card_system", issue)
    ]
    issues += workflow._validate_required_semantic_tables(question, sql, tables)
    issues += workflow._validate_recent_month_semantics(question, sql)
    issues += workflow._validate_requested_row_constraints(question, sql)
    issues += workflow._validate_corporate_entity_grain(question, sql)
    issues += workflow._validate_sales_slip_net_amount(sql)
    issues += workflow._availability_policy_issues(question, sql, tables)

    assert not issues, (question, issues)


def _goldenset_v3() -> list[dict]:
    path = ROOT / "tests" / "fixtures" / "corporate_sales_text2sql_goldenset_v3.jsonl"
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# 골든셋은 축을 컬럼명 그대로 적는다("가맹점사업주체별"). 사용자는 접두사를 뗀
# 표면형으로 부르고("사업주체별"), 그러면 그 컬럼이 프롬프트에서 사라졌다.
USER_PHRASED_AXES = [
    ("2026년 6월 사업주체에 따라 가맹점수 나눠줘", "tmdaa5d01", "가맹점사업주체구분코드"),
    ("2026년 6월 사업주체별 가맹점수를 알려줘", "tmdaa5d01", "가맹점사업주체구분코드"),
    ("2026년 6월 해지된 가맹점수를 해지사유별로 알려줘", "tmdaa5d01", "가맹점해지사유구분코드"),
]


@pytest.mark.parametrize(("question", "table", "column"), USER_PHRASED_AXES)
def test_user_phrased_axis_column_reaches_the_prompt(
    question: str, table: str, column: str
) -> None:
    details = workflow._table_details(workflow._rule_rank_tables(question), question)

    assert f"- {column} [" in details, (question, column)


def test_date_columns_do_not_eat_the_whole_column_budget() -> None:
    """날짜 컬럼 15개인 tmdaa5d01 이 예산 16칸을 날짜로 다 채우던 회귀를 막는다.

    기간 조건에 필요한 건 조회 시간축 하나다. 나머지 날짜는 질문이 이름을 대면
    올라온다.
    """
    question = "2026년 6월 사업주체에 따라 가맹점수 나눠줘"
    details = workflow._table_details(["tmdaa5d01"], question)
    chosen = re.findall(r"^  - (\S+) \[(차원|지표|시간),", details, flags=re.MULTILINE)

    assert ("기준년월", "시간") in chosen
    assert sum(1 for _, role in chosen if role == "시간") <= 6, chosen


# 할부·CA 이용금액 컬럼을 가진 테이블은 카드 월실적(tmdaa3e16) 하나뿐인데, "기업카드"·
# "법인카드" 라는 낱말에 걸린 유효 기업카드 보유 속성(+36)이 그 원천을 눌렀다.
CARD_ONLY_METRICS = [
    ("기업카드 할부 이용금액은?", "금월할부이용금액"),
    ("기업 신용카드 CA 이용금액은?", "금월CA이용금액"),
    ("2026년 5월 법인카드 할부 이용금액은?", "금월할부이용금액"),
    ("2026년 5월 기업카드 CA 이용금액은?", "금월CA이용금액"),
    ("2026년 5월 기업 회원의 신용카드 할부 이용금액은?", "금월할부이용금액"),
]


@pytest.mark.parametrize(("question", "column"), CARD_ONLY_METRICS)
def test_sole_owner_of_the_requested_measure_ranks_first(
    question: str, column: str
) -> None:
    """지표를 가진 유일한 테이블이 속성 원천보다 앞서야 한다."""
    ranked = workflow._rule_rank_tables(question)

    assert ranked[0] == "tmdaa3e16", (question, ranked)
    assert f"- {column} [" in workflow._table_details(ranked, question)


@pytest.mark.parametrize(("question", "column"), CARD_ONLY_METRICS)
def test_card_only_metric_needs_no_member_master_join(
    question: str, column: str
) -> None:
    """회원 마스터(tbdaaat03)는 금액 컬럼이 없다. 조인하면 모집단만 좁아진다."""
    ranked = workflow._rule_rank_tables(question)

    assert "tbdaaat03" not in ranked, (question, ranked)


def test_generic_measure_synonym_does_not_win_the_sole_owner_bonus() -> None:
    """전표의 매출금액은 '이용금액' 동의어로 걸린다. 그것만으로 원천이 되지 않는다.

    표면형 구체성으로 가리지 않으면 tbdaabt30·tbdaabt08·tmdaa3e16 세 곳이 묶여
    가점이 아무에게도 안 가고, 속성 원천이 그대로 1순위를 지킨다.
    """
    ranked = workflow._rule_rank_tables("기업카드 할부 이용금액은?")

    assert ranked.index("tmdaa3e16") < ranked.index("tbdaabt30")

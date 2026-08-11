"""v2 semantic layer 가 v1의 드롭인 교체본인지, 그리고 실패 원인을 실제로 없앴는지."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from text2sql_v2.column_synonyms import phrase_in_text
from text2sql_v2.sql_contract import PROMPT_LIST_RENDER_LIMIT

V2_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = V2_DIR.parent
FIXTURE = Path(__file__).parent / "fixtures" / "goldenset_v2_sql_errors.json"

COLUMN_SECTIONS = ("dimensions", "measures", "time_dimensions")

# 되묻기 회귀 방지. 이 표현이 계약에 다시 들어오면 모델이 다시 자연어로 답한다.
FORBIDDEN_CONTRACT_PHRASES = ("추가 입력을 요청", "입력을 요청한다", "직접 입력하도록 요청")


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def v1() -> dict:
    return _load(REPO_ROOT / "semantic_layer.yaml")


@pytest.fixture(scope="module")
def v2() -> dict:
    return _load(V2_DIR / "semantic_layer.yaml")


@pytest.fixture(scope="module")
def goldenset() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _column_index(schema: dict) -> dict[tuple[str, str], dict]:
    index: dict[tuple[str, str], dict] = {}
    for table in schema.get("tables", []):
        for section in COLUMN_SECTIONS:
            for column in table.get(section) or []:
                index[(str(table.get("name")), str(column.get("name")))] = column
    return index


# ---------------------------------------------------------------------------
# 드롭인 호환성
# ---------------------------------------------------------------------------
def test_table_and_column_sets_are_unchanged(v1: dict, v2: dict) -> None:
    """컬럼을 잃으면 기존에 성공했던 질의가 깨진다."""
    assert set(_column_index(v1)) == set(_column_index(v2))


def test_top_level_structure_is_preserved(v1: dict, v2: dict) -> None:
    assert set(v1) <= set(v2)
    assert set(v2) - set(v1) == {"column_codebooks"}


def test_grain_and_primary_keys_are_unchanged(v1: dict, v2: dict) -> None:
    def signature(schema: dict) -> dict:
        return {
            str(table.get("name")): (
                str(table.get("grain")),
                tuple(str(key) for key in table.get("primary_key") or []),
                str(table.get("primary_time_dimension") or ""),
            )
            for table in schema.get("tables", [])
        }

    assert signature(v1) == signature(v2)


def test_version_is_marked_as_v2(v2: dict) -> None:
    metadata = v2["semantic_layer_metadata"]
    assert metadata["version"].endswith("-v2")
    assert metadata["derived_from"] == "semantic_layer.yaml (v1)"


# ---------------------------------------------------------------------------
# 동의어 전파
# ---------------------------------------------------------------------------
def test_same_named_columns_share_synonyms(v2: dict) -> None:
    """이름이 같은 컬럼은 어느 테이블에 있어도 같은 동의어 집합을 갖는다."""
    by_name: dict[str, list[set[str]]] = {}
    for (table, column_name), column in _column_index(v2).items():
        by_name.setdefault(column_name, []).append({str(s) for s in column.get("synonyms") or []})

    inconsistent = {name: sets for name, sets in by_name.items() if len(sets) > 1 and len({frozenset(s) for s in sets}) > 1}
    assert inconsistent == {}, f"동의어가 테이블마다 다른 컬럼: {sorted(inconsistent)[:5]}"


def test_merchant_status_column_gets_codebook_everywhere(v2: dict) -> None:
    """v1에서는 tmdaa5e11 에만 있던 정보가 같은 이름의 모든 컬럼에 있어야 한다."""
    index = _column_index(v2)
    holders = [key for key in index if key[1] == "가맹점상태구분코드"]
    assert len(holders) >= 4
    for key in holders:
        column = index[key]
        assert column.get("value_semantics") == {"1": "정상", "2": "거래정지", "3": "해지"}, key
        assert column.get("value_semantics_provenance") == "user_provided_business_codebook"
        assert "가맹점상태구분" in (column.get("synonyms") or []), key


@pytest.mark.parametrize(
    ("column_name", "code", "label"),
    [
        ("가맹점상태구분코드", "2", "거래정지"),
        ("그룹최고고객구분코드", "1", "VVIP"),
        ("사업자자산건전성구분코드", "3", "회수의문"),
        ("소매비소매구분코드", "3", "소매"),
        ("상품중분류구분코드", "CP51", "기업신용카드"),
        ("카드등급구분코드", "4", "골드"),
        ("카드등급그룹코드", "4", "골드"),
        ("카드결제기관구분코드", "004", "국민은행"),
    ],
)
def test_codebook_values_applied(v2: dict, column_name: str, code: str, label: str) -> None:
    """0811 xlsx 8종의 코드값이 컬럼 value_semantics 로 들어갔는지."""
    index = _column_index(v2)
    holders = [column for (_, name), column in index.items() if name == column_name]
    assert holders, f"{column_name} 컬럼이 없다"
    assert all(column.get("value_semantics", {}).get(code) == label for column in holders)


def test_retired_codes_are_not_offered_as_filters(v2: dict) -> None:
    """유효종료된 코드는 인라인 value_semantics 에 넣지 않는다(코드북 원본에만 남긴다)."""
    index = _column_index(v2)
    payment = next(column for (_, name), column in index.items() if name == "카드결제기관구분코드")
    assert "000" not in payment["value_semantics"]      # 미분류, 2009년 종료
    assert "021" not in payment["value_semantics"]      # 조흥은행, 2009년 종료
    assert payment["value_semantics"]["092"] == "토스뱅크"

    book = next(b for b in v2["column_codebooks"] if b["column"] == "카드결제기관구분코드")
    retired = {value["code"] for value in book["values"] if value["status"] == "retired"}
    assert "000" in retired and "021" in retired


# ---------------------------------------------------------------------------
# SQL 생성 계약
# ---------------------------------------------------------------------------
def test_contract_no_longer_tells_the_model_to_ask(v2: dict) -> None:
    """guard 332건의 직접 원인. 되묻기 지시가 남아 있으면 다시 재현된다."""
    contract = json.dumps(v2["sql_generation_contract"], ensure_ascii=False)
    for phrase in FORBIDDEN_CONTRACT_PHRASES:
        assert phrase not in contract, f"되묻기 지시가 남아 있다: {phrase}"
    assert "ambiguity_rules" not in v2["sql_generation_contract"]


def test_contract_requires_sql_only_output(v2: dict) -> None:
    rules = " ".join(v2["sql_generation_contract"]["output_contract"])
    assert "항상 SELECT 또는 WITH로 시작하는 단일 SQL" in rules
    assert "되묻지 않는다" in rules


def test_contract_lists_fit_the_prompt_render_limit(v2: dict) -> None:
    """build_semantic_contract_summary() 는 리스트를 12개까지만 렌더한다.

    v1 의 grain_and_aggregation_rules 는 13개여서 마지막 규칙이 조용히 버려졌다.
    """
    oversized = {
        key: len(value)
        for key, value in v2["sql_generation_contract"].items()
        if isinstance(value, list) and len(value) > PROMPT_LIST_RENDER_LIMIT
    }
    assert oversized == {}


def test_output_contract_is_rendered_before_other_rules(v2: dict) -> None:
    """계약은 dict 순서대로 렌더되므로 출력 규칙이 앞에 와야 힘이 있다."""
    keys = list(v2["sql_generation_contract"])
    assert keys.index("output_contract") < keys.index("athena_rules")
    assert keys.index("output_contract") < keys.index("evidence_order")


@pytest.mark.parametrize("banned", ["QUALIFY", "expr::type", "윈도함수"])
def test_athena_rules_cover_observed_syntax_failures(v2: dict, banned: str) -> None:
    rules = " ".join(v2["sql_generation_contract"]["athena_rules"])
    assert banned in rules


# ---------------------------------------------------------------------------
# 회귀: 실제 실패 조합
# ---------------------------------------------------------------------------
def test_column_matching_gaps_are_mostly_closed(v2: dict, goldenset: dict) -> None:
    """v1에서 질문↔컬럼 매칭이 실패한 조합의 대부분이 해소돼야 한다."""
    index = _column_index(v2)
    gaps = goldenset["column_gaps"]
    unresolved = []
    for gap in gaps:
        column = index.get((gap["table"], gap["column"]))
        assert column is not None, gap
        terms = [gap["column"], *(column.get("synonyms") or [])]
        if not any(phrase_in_text(gap["question"], term) for term in terms):
            unresolved.append(f"{gap['table']}.{gap['column']} <- {gap['question']}")

    resolved_ratio = 1 - len(unresolved) / len(gaps)
    assert resolved_ratio >= 0.85, (
        f"해소율 {resolved_ratio:.0%} (기준 85%). 남은 조합 예시: {unresolved[:5]}"
    )


def test_previously_reported_columns_are_now_reachable(v2: dict) -> None:
    """CSV 에서 모델이 '컬럼이 없다'고 답했던 대표 사례."""
    index = _column_index(v2)
    cases = [
        ("tmdaaus01", "가맹점상태구분코드", "2026년 7월 기준 가맹점 상태구분별 가맹점 수를 알려줘"),
        ("tmdaaus01", "가맹점배달가능여부", "2026년 7월 기준 배달이 가능한 가맹점이 몇 개인지 알려줘"),
        ("tbdaaat05", "상품중분류구분코드", "현재 기준 상품중분류별 평균 기업카드 신용한도금액 상위 15개를 알려줘"),
        ("tbdaaat05", "교부유형구분코드", "현재 기준 카드좌수를 교부유형별 비중으로 나눠서 보여줘"),
        ("tbdaaat05", "발급유형구분코드", "현재 기준 회원수를 발급유형 기준으로 집계해줘"),
        ("tbdaabt30", "카드브랜드구분코드", "2024년 12월 카드브랜드별 전표당 평균 가맹점수수료를 알려줘"),
        ("tbmewcm94", "금융회계구분코드", "금융회계 구분별로 2026년 1월 회원수를 보여줘"),
        ("tbmaisd06", "자산건전성분류구분코드", "2024년 자산건전성 분류별 특수채권편입원금을 알려줘"),
        ("tmdaa3e16", "회수방법구분코드", "2024년 10월 금월할부이용금액을 회수방법 기준으로 집계해줘"),
    ]
    for table, column_name, question in cases:
        column = index[(table, column_name)]
        terms = [column_name, *(column.get("synonyms") or [])]
        assert any(phrase_in_text(question, term) for term in terms), f"{table}.{column_name} <- {question}"

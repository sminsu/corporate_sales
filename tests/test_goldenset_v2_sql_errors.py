"""goldenset v2 실행 실패 40건(SQL 자체가 안 만들어진 것)에서 역산한 회귀 테스트.

    column_not_found 27 · guard 9 · group_by 3 · syntax 1

원인은 네 가지였다.

1. 없는 컬럼 (27건)
   정적 검증이 별칭 붙은 `a."컬럼"` 만 봐서, 실패 SQL 이 주로 쓰는 별칭 없는
   `"컬럼"` 은 Athena 가 거절할 때까지 아무도 못 잡았다.
2. 워크북 툴 오발동 (7건)
   "충당금" 이라는 단어만으로 대손비용률 툴이 골라져, 문장 4개를 이어 붙이고
   가맹점명이 하드코딩된 SQL 이 나갔다.
3. EXPRESSION_NOT_AGGREGATE (3건)
   GROUP BY 쿼리에서 CROSS JOIN 으로 끌어온 전체합을 집계 없이 SELECT 했다.
4. 평가 하네스 오탐 (2건)
   주석 안의 세미콜론을 다중 문장으로 셌다. 운영에서는 실행됐을 SQL 이다.
"""

from __future__ import annotations

import pytest

from scripts.run_goldenset_answers import assert_read_only
from text2sql_agent import workflow
from text2sql_agent.tools.registry import TOOL_MAP
from text2sql_agent.v2.column_repair import repair_columns
from text2sql_agent.v2.sql_dialect_guard import rewrite_cross_join_scalars


@pytest.fixture(scope="module")
def index() -> tuple[dict, dict]:
    return workflow._table_column_index()


# ---------------------------------------------------------------------------
# 1. 없는 컬럼 — 고칠 수 있으면 고치고, 못 고치면 어디에 있는지 알려준다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "wrong", "right"),
    [
        # 가운데 토막을 빠뜨린 이름 (cs-golden-v2-0087)
        (
            'SELECT SUM("금월일반이용금액") AS x FROM tbdaa1d12',
            "금월일반이용금액",
            "금월일반매출이용금액",
        ),
        # 오타 (cs-golden-v2-0769 / 0714)
        (
            'SELECT "cd기능구분코드" FROM tddaa3d01',
            "cd기능구분코드",
            "CD기기능구분코드",
        ),
    ],
)
def test_near_miss_column_names_are_repaired(index, sql: str, wrong: str, right: str) -> None:
    table_columns, owners = index
    repaired, issues = repair_columns(sql, table_columns, column_owners=owners)
    assert f'"{right}"' in repaired
    assert f'"{wrong}"' not in repaired
    assert issues == []


def test_date_granularity_is_never_repaired_silently(index) -> None:
    """기준년월(YYYYMM) → 기준년월일(YYYYMMDD)은 에러 대신 틀린 값을 만든다."""
    table_columns, owners = index
    sql = 'SELECT "기준년월", SUM("가맹점일시불매입금액") FROM tbdaaus01 GROUP BY "기준년월"'
    repaired, issues = repair_columns(sql, table_columns, column_owners=owners)
    assert '"기준년월일"' not in repaired
    assert any("기준년월" in issue for issue in issues)


@pytest.mark.parametrize(
    ("sql", "missing", "expected_table"),
    [
        # 그 이름이 다른 테이블에 있다 (cs-golden-v2-0744)
        (
            'SELECT "모바일카드여부", COUNT(*) FROM tbdaa1d12 GROUP BY "모바일카드여부"',
            "모바일카드여부",
            "tbdaaat05",
        ),
        # 조인한 테이블도 스코프에 들어가야 한다 (cs-golden-v2-0253)
        (
            'SELECT a."BSS등급구분코드" FROM tmdaa3e16 t JOIN tbdaaat01 a ON a."고객식별자" = t."고객식별자"',
            "BSS등급구분코드",
            "",
        ),
    ],
)
def test_unresolved_columns_report_where_they_actually_live(
    index,
    sql: str,
    missing: str,
    expected_table: str,
) -> None:
    table_columns, owners = index
    _, issues = repair_columns(sql, table_columns, column_owners=owners)
    hit = next((issue for issue in issues if missing in issue), "")
    assert hit, issues
    if expected_table:
        assert expected_table in hit


def test_inline_subquery_columns_are_checked(index) -> None:
    """FROM (SELECT ...) 안쪽을 통째로 건너뛰면 cs-golden-v2-0188 을 놓친다."""
    table_columns, owners = index
    sql = (
        'SELECT * FROM (SELECT "회원일련번호", '
        'ROW_NUMBER() OVER (ORDER BY "기준년월일" DESC) AS rn FROM tbdaaat05) t WHERE rn = 1'
    )
    repaired, issues = repair_columns(sql, table_columns, column_owners=owners)
    assert '"실적기준년월일"' in repaired or any("기준년월일" in issue for issue in issues)


def test_output_alias_does_not_mask_a_missing_source_column(index) -> None:
    """m."부점코드" AS "부점코드" 의 alias 때문에 원천 검사가 건너뛰어졌다."""
    table_columns, owners = index
    sql = 'SELECT m."부점코드" AS "부점코드" FROM tbdaaat05 m GROUP BY m."부점코드"'
    _, issues = repair_columns(sql, table_columns, column_owners=owners)
    assert any("부점코드" in issue for issue in issues)


def test_valid_sql_is_left_alone(index) -> None:
    table_columns, owners = index
    sql = (
        'WITH ranked AS (SELECT a."고객식별자", a."기업총한도금액", '
        'ROW_NUMBER() OVER (PARTITION BY a."고객식별자" ORDER BY a."기준년월일" DESC) AS rn '
        'FROM tbdaa1d12 a WHERE a."기준년월일" BETWEEN \'20260701\' AND \'20260731\') '
        'SELECT SUM("기업총한도금액") AS "총한도" FROM ranked WHERE rn = 1'
    )
    repaired, issues = repair_columns(sql, table_columns, column_owners=owners)
    assert repaired == sql
    assert issues == []


# ---------------------------------------------------------------------------
# 2. 워크북 툴은 자기 질문에만 발동한다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "question",
    [
        "2025년 9월 원화대출잔액을 충당금 차주 구분 기준으로 집계해줘",
        "2026년 7월 기업 대손충당금과 원화대출잔액을 알려줘",
        "2026년 7월 기업 대손충당금을 회계계정과목별로 보여줘",
        "2026년 7월 기업 대손충당금을 상품코드별 상위 10개로 보여줘",
        "2026년 7월 기업 대손충당금을 유효회원 구분별로 보여줘",
        "2026년 상반기 월별 기대대손충당금과 전월 대비 증감률을 함께 보여줘",
        "2025년 6월 충당금 차주 구분별 기대대손충당금 순위를 30위까지 매겨줘",
    ],
)
def test_bad_debt_tool_does_not_fire_on_plain_allowance_questions(question: str) -> None:
    assert not workflow._tool_request_is_supported(question, TOOL_MAP["대손비용률_분석"])


def test_bad_debt_tool_still_fires_on_its_own_question() -> None:
    question = "2026년 7월 도미노피자 대손비용률을 알려줘"
    assert workflow._tool_request_is_supported(question, TOOL_MAP["대손비용률_분석"])


# ---------------------------------------------------------------------------
# 3. EXPRESSION_NOT_AGGREGATE
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            'SELECT a."x", SUM(a."y") / NULLIF(t.total, 0) AS r '
            "FROM base a CROSS JOIN total t GROUP BY a.\"x\"",
            "NULLIF(MAX(t.total), 0)",
        ),
        (
            'SELECT a."x", CAST(SUM(a."y") AS DOUBLE) / NULLIF(p."개월수", 0) AS r '
            'FROM base a CROSS JOIN params p GROUP BY a."x"',
            'NULLIF(MAX(p."개월수"), 0)',
        ),
    ],
)
def test_cross_join_scalars_are_wrapped_in_max(sql: str, expected: str) -> None:
    assert expected in rewrite_cross_join_scalars(sql)


@pytest.mark.parametrize(
    "sql",
    [
        # GROUP BY 가 없으면 문제가 아니다.
        'SELECT SUM(a."y") / NULLIF(t.total, 0) FROM base a CROSS JOIN total t',
        # 이미 집계로 감싼 값은 건드리지 않는다.
        'SELECT a."x", SUM(a."y") / NULLIF(MAX(t.total), 0) '
        'FROM base a CROSS JOIN total t GROUP BY a."x"',
        # CROSS JOIN 이 아닌 별칭은 대상이 아니다.
        'SELECT a."x", b."y" FROM base a JOIN other b ON a."k" = b."k" GROUP BY a."x", b."y"',
    ],
)
def test_cross_join_rewrite_leaves_correct_sql_alone(sql: str) -> None:
    assert rewrite_cross_join_scalars(sql) == sql


# ---------------------------------------------------------------------------
# 4. 평가 하네스 read-only 가드
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        'SELECT "a" FROM t WHERE rn = 1 /* No column for "교체 카드" exists; using a proxy */',
        'SELECT "a", -- approximated grade column; using closest semantic match\n       "b" FROM t',
        "SELECT '세미콜론; 포함 문자열' AS a FROM t",
    ],
)
def test_semicolons_inside_comments_and_literals_are_not_multiple_statements(sql: str) -> None:
    assert_read_only(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "-- [월초충당금]\nWITH a AS (SELECT 1) SELECT 1\n\n-- [월말충당금]\nWITH b AS (SELECT 1) SELECT 1",
    ],
)
def test_real_multi_statement_sql_is_still_blocked(sql: str) -> None:
    with pytest.raises(ValueError, match="다중 문장"):
        assert_read_only(sql)


def test_write_sql_is_still_blocked() -> None:
    with pytest.raises(ValueError):
        assert_read_only("DELETE FROM t")


# ---------------------------------------------------------------------------
# 5. 선행 주석이 붙은 SQL을 되묻기로 오판하지 않는다 (v3 실행 실패 26건)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        '-- 근거 한 줄\nSELECT 1 FROM t',
        '/* 무이자 구분 컬럼이 없어 전체 할부로 집계한다 */\nSELECT 1 FROM t',
        '/*\n  여러 줄 근거\n*/ WITH a AS (SELECT 1) SELECT * FROM a',
        '-- 첫 줄\n/* 둘째 */\nSELECT 1 FROM t',
    ],
)
def test_leading_comments_are_not_treated_as_prose(sql: str) -> None:
    """output_contract 가 "근거를 주석으로 남긴다" 라고 시키는데 블록 주석을 빠뜨려
    멀쩡한 SQL이 되묻기로 분류되고 재시도를 한 번 버렸다."""
    from text2sql_agent.v2.sql_dialect_guard import looks_like_prose, looks_like_sql

    assert looks_like_sql(sql)
    assert not looks_like_prose(sql)


@pytest.mark.parametrize(
    "text",
    [
        "해당 컬럼이 스키마에 없습니다. 컬럼명을 알려주시면 SQL을 만들어 드리겠습니다.",
        "죄송합니다. 요청하신 지표를 만들 수 없습니다.",
        "",
    ],
)
def test_real_prose_is_still_detected(text: str) -> None:
    from text2sql_agent.v2.sql_dialect_guard import looks_like_prose

    assert looks_like_prose(text)

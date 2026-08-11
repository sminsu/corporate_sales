"""Athena 방언 가드. 실패한 agent SQL 원문으로 검증한다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from text2sql_v2.sql_dialect_guard import (
    audit_sql,
    looks_like_prose,
    looks_like_sql,
    normalize_sql,
    prose_reason,
    rewrite_postgres_casts,
)

FIXTURE = Path(__file__).parent / "fixtures" / "goldenset_v2_sql_errors.json"


@pytest.fixture(scope="module")
def goldenset() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# PostgreSQL 캐스트
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SUM(a)::FLOAT", "CAST(SUM(a) AS DOUBLE)"),
        ('SUM("금월이용합계금액")::FLOAT', 'CAST(SUM("금월이용합계금액") AS DOUBLE)'),
        ("(SUM(a) - SUM(b))::FLOAT", "CAST((SUM(a) - SUM(b)) AS DOUBLE)"),
        ("a.연체금액::FLOAT", "CAST(a.연체금액 AS DOUBLE)"),
        ('"금액"::DECIMAL(18,2)', 'CAST("금액" AS DECIMAL(18,2))'),
        ("cnt::INT", "CAST(cnt AS INTEGER)"),
        # 중첩된 두 개를 모두 바꾼다.
        ("SUM(a)::FLOAT / SUM(b)::FLOAT", "CAST(SUM(a) AS DOUBLE) / CAST(SUM(b) AS DOUBLE)"),
    ],
)
def test_rewrites_postgres_cast(sql: str, expected: str) -> None:
    assert rewrite_postgres_casts(sql) == expected


def test_cast_rewrite_preserves_surrounding_sql() -> None:
    sql = "SELECT ROUND(SUM(x)::FLOAT / NULLIF(SUM(y), 0), 2) AS r FROM t WHERE a = 'b::c'"
    result = rewrite_postgres_casts(sql)
    assert "CAST(SUM(x) AS DOUBLE)" in result
    assert "NULLIF(SUM(y), 0)" in result
    assert result.startswith("SELECT ROUND(")
    assert result.endswith("FROM t WHERE a = 'b::c'")


def test_cast_rewrite_is_idempotent() -> None:
    once = rewrite_postgres_casts("SUM(a)::FLOAT / 2")
    assert rewrite_postgres_casts(once) == once


def test_normalize_sql_leaves_clean_sql_untouched() -> None:
    sql = 'SELECT CAST(SUM("a") AS DOUBLE) FROM t'
    assert normalize_sql(sql) == sql


# ---------------------------------------------------------------------------
# 정적 감사
# ---------------------------------------------------------------------------
def test_flags_qualify() -> None:
    sql = """WITH latest AS (
      SELECT * FROM tbdaaus01 WHERE SUBSTR("기준년월일",1,6)='202602'
      QUALIFY ROW_NUMBER() OVER (PARTITION BY "가맹점번호" ORDER BY "기준년월일" DESC) = 1
    ) SELECT "시군구" FROM latest GROUP BY "시군구\""""
    assert any("QUALIFY" in issue for issue in audit_sql(sql))


def test_flags_window_function_in_where() -> None:
    sql = """SELECT "가맹점번호" FROM tbdaaus01
    WHERE SUBSTR("기준년월일", 1, 6) = '202604'
      AND ROW_NUMBER() OVER (PARTITION BY "가맹점번호" ORDER BY "기준년월일" DESC) = 1"""
    assert any("윈도함수" in issue for issue in audit_sql(sql))


def test_allows_window_function_in_select_and_filter_outside() -> None:
    """정상 패턴은 통과해야 한다. 아니면 재시도 루프가 끝나지 않는다."""
    sql = """WITH ranked AS (
      SELECT a."가맹점번호", a."가맹점상태구분코드",
        ROW_NUMBER() OVER (PARTITION BY a."가맹점번호" ORDER BY a."기준년월일" DESC) AS rn
      FROM tmdaaus01 a WHERE a."기준년월" = '202607'
    )
    SELECT r."가맹점상태구분코드", COUNT(DISTINCT r."가맹점번호") AS "가맹점수"
    FROM ranked r WHERE r.rn = 1 GROUP BY r."가맹점상태구분코드\""""
    assert audit_sql(sql) == []


def test_ignores_keywords_inside_string_literals() -> None:
    sql = "SELECT 'QUALIFY' AS label, 'a::b' AS note FROM t WHERE x = 1"
    assert audit_sql(sql) == []


def test_ignores_keywords_inside_comments() -> None:
    sql = "-- QUALIFY was removed\nSELECT x FROM t WHERE y = 1 /* no ::cast here */"
    assert audit_sql(sql) == []


def test_flags_unbalanced_parentheses_from_truncation() -> None:
    sql = """WITH params AS (SELECT '202607' AS "기준년월"), corp AS (
    SELECT c."고객식별자" FROM tmdaa1d12 c CROSS JOIN params p GROUP BY c"""
    assert any("괄호" in issue for issue in audit_sql(sql))


def test_flags_sql_ending_mid_clause() -> None:
    assert any("끊긴" in issue or "끝났다" in issue for issue in audit_sql("SELECT a, b FROM t WHERE x = 1 AND"))


def test_empty_sql_is_reported() -> None:
    assert audit_sql("   ") == ["빈 SQL이다."]


def test_flags_condition_appended_without_where() -> None:
    """cs-golden-v2-0122: FROM joined 뒤에 WHERE 없이 AND 조건이 붙었다."""
    sql = 'SELECT "가맹점번호" FROM joined\nAND "가맹점명" LIKE \'%기준%\' ORDER BY "가맹점번호"'
    assert any("WHERE 없이" in issue for issue in audit_sql(sql))


def test_all_but_one_recorded_dialect_failure_is_detected(goldenset: dict) -> None:
    """CSV의 방언 실패 원문 중 정적 감사로 잡기로 한 것은 모두 잡는다.

    cs-golden-v2-0925(EXPRESSION_NOT_AGGREGATE)만 예외다. SELECT 블록을 나누지 않고
    검사하면 정상 참고 쿼리를 오탐하므로 감사에서 빼고 프롬프트 규칙으로만 예방한다.
    """
    undetected = [
        row["id"] for row in goldenset["dialect_failures"] if not audit_sql(row["agent_sql"])
    ]
    assert undetected == ["cs-golden-v2-0925"], f"감사 결과가 예상과 다르다: {undetected}"


# ---------------------------------------------------------------------------
# 프로즈 가드
# ---------------------------------------------------------------------------
def test_detects_sql_shapes() -> None:
    assert looks_like_sql("SELECT 1")
    assert looks_like_sql("  with a as (select 1) select * from a")
    assert looks_like_sql("-- 최신 스냅샷\nSELECT 1")
    assert not looks_like_sql("가맹점 상태구분 컬럼명을 알려주세요")


def test_every_recorded_prose_failure_is_detected(goldenset: dict) -> None:
    """guard 차단을 유발한 자연어 응답을 전부 프로즈로 판정한다."""
    missed = [
        row["id"] for row in goldenset["prose_failures"] if not looks_like_prose(row["agent_response"])
    ]
    assert missed == [], f"프로즈로 못 잡은 응답: {missed}"


def test_prose_reason_names_the_clarifying_phrase() -> None:
    reason = prose_reason("해당 컬럼이 스키마에 존재하지 않습니다. 컬럼명을 알려주시면 작성해 드리겠습니다.")
    assert "알려주시면" in reason
    assert "되묻지" in reason


def test_prose_reason_handles_empty_response() -> None:
    assert "빈 응답" in prose_reason("")

"""번호·식별자 컬럼에 한글 이름을 비교한 SQL 은 재시도로 돌려보낸다.

"쿠팡 상반기 이용금액 알려줘" 는 검증 쿼리·semantic contract 어느 쪽에도 안 걸려
LLM 생성 경로로 가고, 라우팅 1·2순위인 매출전표 팩트(tbdaabt08·tbdaabt30)는
프롬프트 컬럼 예산(테이블당 12개)에서 가맹점명이 잘려 나가 가맹점번호만 남는다.
그래서 모델이 LOWER("가맹점번호") LIKE LOWER('%쿠팡%') 를 썼다. 번호·식별자 컬럼은
전부 숫자·영숫자 식별자이므로 한글 이름과의 비교는 언제나 틀린 SQL 이다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from text2sql_agent.workflow import _validate_name_literal_id_columns

ROOT = Path(__file__).resolve().parents[1]
REPORTED_SQL = (
    'SELECT SUM(a."매출금액") FROM card_system.tbdaabt30 a\n'
    "WHERE a.\"기준년월\" BETWEEN '202601' AND '202606'\n"
    "  AND LOWER(\"가맹점번호\") LIKE LOWER('%쿠팡%')"
)


def test_name_literal_on_identifier_column_is_rejected() -> None:
    issues = _validate_name_literal_id_columns(REPORTED_SQL, ["tbdaabt30", "tmdaa1d12"])

    assert len(issues) == 1
    assert "가맹점번호" in issues[0]
    assert "쿠팡" in issues[0]
    # 재시도 프롬프트가 갈 곳을 알도록 실제 이름 컬럼을 짚어 준다.
    assert "tmdaa1d12.기업명" in issues[0]


def test_identifier_column_shapes_are_all_caught() -> None:
    for sql in (
        "WHERE 가맹점번호 = '쿠팡'",
        'WHERE a."가맹점번호" LIKE \'%쿠팡%\'',
        "WHERE 사업자등록번호 = '쿠팡'",
        "WHERE 고객식별자 = '쿠팡'",
    ):
        assert _validate_name_literal_id_columns(sql, []), sql


def test_legitimate_identifier_predicates_pass() -> None:
    for sql in (
        "WHERE 가맹점번호 = '1234567890'",
        "WHERE 기업명 LIKE '%쿠팡%'",
        # 차량번호는 한 글자 한글을 정상 값으로 품는다.
        "WHERE RF차량번호 LIKE '%가%'",
        "WHERE a.가맹점번호 IS NOT NULL AND b.기업명 = '쿠팡'",
        # 치환 전 템플릿의 플레이스홀더는 값이 아니다.
        "WHERE 가맹점번호 = '{가맹점번호}'",
    ):
        assert _validate_name_literal_id_columns(sql, []) == [], sql


def test_no_verified_query_trips_the_guard() -> None:
    document = yaml.safe_load(
        (ROOT / "text2sql_agent" / "tools" / "sql_verified_queries.yaml").read_text(
            encoding="utf-8"
        )
    )
    flagged = [
        query["name"]
        for query in document["verified_queries"]
        if str(query.get("sql") or "").strip()
        and _validate_name_literal_id_columns(str(query["sql"]), [])
    ]

    assert flagged == []

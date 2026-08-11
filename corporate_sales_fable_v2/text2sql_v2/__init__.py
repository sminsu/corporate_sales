"""corporate_sales_fable v2 — goldenset v2 SQL 오류 대응 모듈.

v1 코드는 건드리지 않는다. 적용 방식은 두 단계다.

1단계: 데이터만 교체 (코드 변경 없음)
    v2 의 semantic_layer.yaml 과 sql_verified_queries.yaml 은 v1 과 같은 스키마라서
    환경변수만 바꾸면 그대로 붙는다.

        export SEMANTIC_SCHEMA_PATH=corporate_sales_fable_v2/semantic_layer.yaml
        export VERIFIED_QUERY_FILE_PATH=corporate_sales_fable_v2/sql_verified_queries.yaml

    이것만으로 되묻기 지시 제거, 컬럼 동의어·코드북 보강, 참고 SQL의 잘못된 테이블과
    PostgreSQL 캐스트가 함께 반영된다.

2단계: 매처·가드 연결 (v1 코드 3곳 수정)
    docs/integration.md 참고. 1단계로 남는 오차를 줄인다.

        column_synonyms.phrase_in_text   질문↔컬럼 매칭 (띄어쓰기·중첩 조사)
        sql_dialect_guard.normalize_sql  실행 전 캐스트 자동 교정
        sql_dialect_guard.audit_sql      QUALIFY·윈도함수 WHERE·잘림 검출
        sql_dialect_guard.looks_like_prose / prose_reason  되묻기 응답 재시도
"""

from .column_synonyms import compact, derive_column_synonyms, phrase_in_text
from .sql_contract import (
    ATHENA_RULES_V2,
    GRAIN_RULES_V2,
    OUTPUT_CONTRACT_V2,
    PROMPT_LIST_RENDER_LIMIT,
    RESOLUTION_DEFAULTS_V2,
)
from .sql_dialect_guard import (
    audit_sql,
    looks_like_prose,
    looks_like_sql,
    normalize_sql,
    prose_reason,
    rewrite_postgres_casts,
)
from .verified_query_audit import unregistered_tables, unresolved_identifiers

__all__ = [
    "ATHENA_RULES_V2",
    "GRAIN_RULES_V2",
    "OUTPUT_CONTRACT_V2",
    "PROMPT_LIST_RENDER_LIMIT",
    "RESOLUTION_DEFAULTS_V2",
    "audit_sql",
    "compact",
    "derive_column_synonyms",
    "looks_like_prose",
    "looks_like_sql",
    "normalize_sql",
    "phrase_in_text",
    "prose_reason",
    "rewrite_postgres_casts",
    "unregistered_tables",
    "unresolved_identifiers",
]

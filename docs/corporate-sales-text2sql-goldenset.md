# 기업영업·프랜차이즈 Text2SQL 골든셋 v1

`semantic_layer.yaml`(31개 물리 테이블 / 42개 canonical metric / 37개 semantic query contract)과
`text2sql_agent/tools/sql_verified_queries.yaml`(66개 verified query)을 근거로 생성한
질의–Athena 정답 SQL 쌍 1,000건이다. 기존 `semantic_layer_golden_v1.jsonl`이 SQL 문자열 대신
계약(contract) 일치를 검사하는 데이터라면, 이 골든셋은 실행 가능한 Athena SQL 자체를 정답으로 둔다.

## 파일

| 파일 | 설명 |
|---|---|
| `corporate_sales_text2sql_goldenset_v1.jsonl` | 평가 러너 입력용. 한 줄에 한 케이스 |
| `corporate_sales_text2sql_goldenset_v1.csv` | 현업 검수용. UTF-8 BOM, Excel에서 바로 열림 |

## 레코드 스키마

```json
{
  "id": "cs-golden-0001",
  "question": "2026년 1월 기준 법인회원의 유실적 업체수를 알려줘",
  "sql": "WITH ranked AS ( ... )",
  "sql_dialect": "athena_v3_trino",
  "domain": "corporate_sales_targeting",
  "category": "유실적 업체수",
  "expected_tables": ["card_system.tbdaa1d12"],
  "metrics": ["기업월카드이용금액"],
  "semantic_contract": "corporate_member_with_monthly_usage_count",
  "difficulty": "easy",
  "reference_date": "2026-08-10"
}
```

`semantic_contract`는 해당 케이스가 대응하는 `semantic_query_contracts` 또는 verified query 이름이며,
자동 SQL 생성 경로로 평가할 케이스는 빈 문자열이다.

## 구성

| 도메인 | 건수 | 주요 분석 축 |
|---|---:|---|
| corporate_sales_targeting | 249 | 무실적·이탈·한도여유·교차판매 영업대상, 브랜드 가맹점주 기업카드 보유 |
| merchant_sales | 248 | 브랜드·업종·지역별 가맹점 수/매출/수수료, 폐업, 결제금융기관 |
| card_usage | 194 | 월별·기업규모별·업종별·해외·브랜드별 법인카드 이용금액과 증감률 |
| customer_card_portfolio | 155 | 기업고객·회원·카드 좌수, 신규 발급/해지, 상품·등급 분포 |
| credit_risk | 154 | 한도·소진율, 연체, 특수채권, 대손충당금, 거래정지 |
| **합계** | **1,000** | 196개 세부 카테고리, 물리 테이블 28종 사용 |

난이도는 `easy` 140, `medium` 369, `hard` 491건이다. `hard`는 스냅샷 중복 제거, 다중 CTE,
window 함수, ratio-of-sums, 다중 테이블 조인 중 둘 이상이 필요한 케이스다.

## 생성 규칙

기준일은 `2026-08-10`으로 고정하고 SQL에는 `'202607'` 같은 절대 연월 리터럴만 사용한다.
"최근 N개월 폐업"처럼 semantic layer가 `CURRENT_DATE` 기준을 명시한 지표만 예외로
`DATE_ADD('month', -N, DATE '2026-08-10')` 형태를 사용해 실행 시점과 무관하게 결과가 고정되도록 했다.

SQL은 `sql_generation_contract.athena_rules`를 따른다. 단일 읽기 전용 SELECT/WITH,
한글 식별자는 큰따옴표, 문자열은 작은따옴표, `CAST(... AS DOUBLE)`·`ROW_NUMBER() OVER`·
`DATE_FORMAT`·`LOWER() LIKE LOWER()` 등 Athena engine v3 문법만 사용하고,
선언되지 않은 `year`/`month` 파티션 컬럼은 만들지 않는다.

집계 의미도 계약을 따른다. `tbdaa1d12`·`tmdaaus01` 같은 일별 스냅샷은
고객(가맹점)×월 최신 행으로 먼저 축소한 뒤 집계하고, 한도소진율은
`1 - SUM(잔여한도)/SUM(총한도)`로 재계산하며 기업별 비율을 평균내지 않는다.
월평균 이용금액은 데이터 누락월을 0원으로 포함한 달력 월수로 나눈다.
가맹점 월매출은 `tmdaa5e11`의 일시불+할부이며 `가맹점총지급금액`으로 대체하지 않는다.
가맹점주 인원은 `대표고객식별자`, 가맹점 운영 기업은 `기업고객식별자`로 구분해 집계한다.
신용+체크(카드 종류 축)와 개별+공용(소유 형태 축)은 함께 합산하지 않는다.

## 제외 범위

- 관리기업·담당기업·전담직원·로그인 계정 등 사용자 권한 범위에 의존하는 질문
- 문서 파싱을 전제로 하는 질문
- 공공기업·공기업 대상 질문
- 개인정보 테이블 `tbdaaat18` 참조
- 현행 업무에 없는 1~5번 영업대상 분류

## 검증

생성 시 다음을 모두 통과했다.

- 정규화(NFKC·공백·문장부호 제거) 후 질문 중복 0건
- 모든 SQL이 `SELECT` 또는 `WITH`로 시작하는 단일문이며 쓰기 키워드 없음
- SQL이 참조하는 테이블 28종이 모두 semantic layer에 존재하고 restricted 테이블 미사용
- SQL의 모든 한글 식별자가 해당 테이블의 실제 컬럼이거나 쿼리 내 정의된 alias
- `sqlglot` Trino 파서로 1,000건 전부 파싱 성공 (실패 0건)

파서 통과는 문법·컬럼 존재 검증이며 실제 데이터 결과(denotation) 검증은 아니다.
외부 벤치마크로 동결하기 전에는 도메인별 표본을 현업이 확인하는 단계를 권장한다.

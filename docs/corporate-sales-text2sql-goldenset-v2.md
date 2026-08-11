# 기업영업·프랜차이즈 Text2SQL 골든셋 v2

v1은 1,000행이었지만 리터럴(기준월·브랜드명·임계금액)만 바꾼 반복이 많아 실제 SQL 구조는 218종에 불과했다.
v2는 **모든 행의 SQL 구조가 서로 다르도록** 다시 생성했다. 문자열·숫자 리터럴을 모두 마스킹한 뒤
비교했을 때 1,000행이 1,000개의 서로 다른 쿼리 구조를 가진다.

| 항목 | v1 | v2 |
|---|---:|---:|
| 레코드 수 | 1,000 | 1,000 |
| 고유 SQL 구조(리터럴 마스킹 후) | 218 | **1,000** |
| GROUP BY 조합 | — | 374 |
| SQL에 등장하는 서로 다른 식별자 | — | 683 |
| 사용 물리 테이블 | 28 | 28 |
| 세부 카테고리 | 196 | 208 |

## 파일

| 파일 | 설명 |
|---|---|
| `corporate_sales_text2sql_goldenset_v2.jsonl` | 평가 러너 입력용 |
| `corporate_sales_text2sql_goldenset_v2.csv` | 현업 검수용(UTF-8 BOM) |

레코드 스키마는 v1과 같고 `query_shape` 필드가 추가됐다. SQL은 **스키마 접두사 없이 테이블명만** 사용한다
(`FROM tbdaa1d12 a`). Athena 접속의 `ATHENA_DATABASE` 가 기본 스키마가 되므로 그대로 실행되며,
database-qualified 이름이 필요한 환경에서만 실행 시점에 접두사를 붙인다
(`run_goldenset_answers.py --schema kbcard_db`).

```json
{
  "id": "cs-golden-v2-0001",
  "question": "2026년 1월 기준 법인회원의 유실적 업체수를 알려줘",
  "sql": "WITH ranked AS ( ... )",
  "sql_dialect": "athena_v3_trino",
  "domain": "corporate_sales_targeting",
  "category": "유실적 업체수",
  "query_shape": "business_case",
  "expected_tables": ["tbdaa1d12"],
  "metrics": ["기업월카드이용금액"],
  "semantic_contract": "corporate_member_with_monthly_usage_count",
  "difficulty": "easy",
  "reference_date": "2026-08-10"
}
```

## 생성 방식

두 갈래를 합쳐 만들었다.

**업무 케이스 218건 (`query_shape: business_case`)** 은 semantic query contract와 verified query에
직접 대응하는 쿼리다. 무실적·이탈·교차판매 영업대상 발굴, 한도소진율, 반기 이용금액 하락,
브랜드 가맹점주 기업카드 보유, 최근 N개월 폐업 가맹점, 가맹점 포트폴리오 대손충당금률,
발급 후 해지 좌수처럼 다단계 CTE와 스냅샷 중복 제거가 필요한 케이스가 여기 속한다.
v1에서 같은 구조가 반복되던 것을 구조당 1건으로 줄이고, 남은 1건이 서로 다른 기준월·브랜드·임계값을
갖도록 배분했다.

**조합형 축 질의 782건** 은 원천 × 지표 × 분석축 × 질의형태를 교차해 만들었다. 20개 원천 테이블에서
지표(측정 컬럼과 집계 방식)와 분석축(그룹핑 컬럼)을 뽑고, 아래 12가지 질의형태 중 하나를 입혀
매번 다른 SQL 구조가 나오도록 했다.

`simple` 분류축 집계 · `topn` 상위 N · `share` 구성비(window) · `trend` 월별 추이 ·
`mom` 전월 대비 증감(LAG) · `bucket` 구간 분포(CASE) · `having` 임계 조건 · `rank` 순위(RANK) ·
`two_period` 전년 동월 비교 · `per_entity` 단위당 평균 · `above_avg` 전체 평균 초과 그룹 ·
`cross` 두 축 교차.

덕분에 v1에서 거의 다루지 않던 컬럼까지 평가 범위에 들어왔다. 가맹점 월실적의 최근 3·6·9·12개월 매출과
수입수수료·미수금·선지급, 가맹점 월말요약의 시도·시군구·마케팅 업종·배달/포장/주차 속성과 영업일·휴일·성별
카드이용비중, 카드 월실적의 일시불·할부·CA·해외·리볼빙·무이자할부·청구·미청구·연체 계열, 전표의 수수료·봉사료·
부가가치세·원천징수, 기업고객 스냅샷의 국세·지방세·4대보험·KB페이·전략매출, 업종 마스터의 브랜드별 수수료율과
원가율, 연체·특수채권·충당금 계열이 모두 포함된다.

## 규칙 (v1과 동일)

기준일은 `2026-08-10` 고정, SQL에는 절대 연월 리터럴만 사용한다("최근 N개월 폐업"처럼 semantic layer가
`CURRENT_DATE` 기준으로 정의한 지표만 예외).

집계 의미는 `sql_generation_contract`를 따른다. `tbdaa1d12`·`tmdaaus01` 같은 일별 스냅샷은
고객(가맹점)×기간 최신 행으로 먼저 축소한 뒤 집계하고, 한도소진율은 `1 - SUM(잔여)/SUM(총한도)`로
재계산하며, 월평균은 누락월을 0원으로 포함한 달력 월수로 나눈다. 가맹점 월매출은 `tmdaa5e11`의
일시불+할부이고, 가맹점주 인원은 `대표고객식별자`, 운영 기업은 `기업고객식별자`로 구분한다.
신용+체크(카드 종류 축)와 개별+공용(소유 형태 축)은 함께 합산하지 않는다.

제외 범위도 같다. 관리기업·담당기업·전담직원·로그인 계정 등 사용자 권한 범위 의존 질문, 문서 파싱 전제 질문,
공공기업 대상 질문, 개인정보 테이블 `tbdaaat18` 참조를 모두 배제했다.

## 검증

- 리터럴 마스킹 후 SQL 구조 중복 **0건** (1,000행 = 1,000구조)
- 정규화 후 질문 중복 0건
- 모든 SQL이 단일 읽기 전용 SELECT/WITH, 쓰기 키워드 0건
- 참조 테이블 28종이 모두 semantic layer에 존재, restricted 테이블 미사용
- SQL의 모든 한글 식별자가 해당 테이블의 실제 컬럼이거나 쿼리 내 정의된 alias
- `sqlglot` Trino 파서 1,000건 전부 파싱 성공(실패 0)

도메인 분포는 corporate_sales_targeting 201, card_usage 201, merchant_sales 200,
customer_card_portfolio 200, credit_risk 198이며 난이도는 hard 537 / medium 324 / easy 139이다.

문법·스키마 검증이지 실제 데이터 결과(denotation) 검증은 아니다. 조합형 축 질의는 컬럼 의미상
성립하는 조합만 남기도록 지표·축 풀을 손으로 골랐지만, 벤치마크로 동결하기 전 도메인별 표본을
현업이 한 번 훑어보는 단계를 권장한다.

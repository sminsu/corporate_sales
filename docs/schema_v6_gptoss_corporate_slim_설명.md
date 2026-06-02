# schema_v6_gptoss_corporate_slim.yaml 구조 설명

이 YAML은 **gpt-oss 기반 Text2SQL(자연어 → SQL 변환)을 위한 시맨틱 모델**입니다.
자연어 질문을 안전한 읽기 전용 SQL로 바꾸기 위해 "테이블 / 컬럼 / 지표 / 조인 / 예시"를 계층적으로 정의해 둔 일종의 **LLM용 사용설명서**입니다.

대상 도메인은 KB카드 법인/기업영업(가맹점·채권·충당금)이며, 자연어 질문을 받으면 위 계층(추상적 의미·규칙)에서 아래 계층(구체적 실체)으로 좁혀가며 SQL을 생성합니다.

---

## 전체 계층 구조 한눈에 보기

```
[메타/규약 계층]   ← LLM에게 "어떻게 행동할지" 지시
  model_info              모델 기본 정보
  llm_semantic_contract   SQL 생성 규칙 + 근거 우선순위 + 모호성 처리

[비즈니스 의미 계층]  ← "무엇을, 언제 쓸지" 정의
  canonical_domains       업무 영역(도메인) 정의
  semantic_entities       논리적 엔티티(테이블의 의미적 추상화)
  semantic_join_graph     안전한 조인 경로
  canonical_metrics       표준 지표 공식

[실행 규칙 계층]   ← "시간/집계/라우팅" 처리 규칙
  time_resolution_rules   시간 표현 → 컬럼 변환
  metric_generation_rules 지표 생성 시 주의사항
  intent_routing          질문 의도 → 테이블/액션 매핑
  result_shape_defaults   결과 형태 기본값(LIMIT 등)

[물리 스키마 계층]   ← 실제 DB 구조
  tables                  실제 테이블/컬럼 상세 정의
  relationships           테이블 간 물리적 관계

[참조/예시 계층]   ← LLM이 베끼는 정답지
  metrics                 (간이) 지표 목록
  glossary                용어 사전
  query_references        의도별 SQL 패턴 템플릿
  verified_queries        검증된 정답 SQL 예시 (최우선 근거)
  domains                 도메인-테이블-지표-권한 매핑
```

핵심 아이디어는 **위 계층은 "추상적 의미·규칙", 아래 계층은 "구체적 실체"** 이고,
LLM은 질문을 받으면 **위에서 아래로 좁혀가며** (도메인 선택 → 엔티티 선택 → 지표/조인 선택 → 실제 컬럼 매핑 → 예시 참조) SQL을 만듭니다.

---

## 1. `model_info` — 모델 기본 정보

이 시맨틱 모델 자체의 신원 정보입니다.

- `name` / `description`: 모델 이름과 설명 (KB카드 법인/기업영업 축약본)
- `database` / `schema` / `dialect`: 대상 DB는 `card_system`, SQL 방언은 `postgres`
- `target_llm: gpt-oss`: 이 파일을 소비하는 LLM
- `version` / `updated_at` / `change_summary`: 버전 이력

---

## 2. `llm_semantic_contract` — LLM 실행 규약 (가장 중요한 행동 지침)

LLM이 SQL을 만들 때 **반드시 지켜야 할 규칙**입니다.

- `sql_output_rules`: SELECT/WITH로만 시작, `card_system.` prefix 필수, `SELECT *` 금지, 비율은 `NULLIF(분모,0)` 등 안전 규칙
- `evidence_priority`: **근거 우선순위** — 질문에 답할 때 무엇을 먼저 믿을지의 순서
  1. `verified_queries` (검증된 예시)
  2. `query_references` (의도별 패턴)
  3. `canonical_metrics` (공식 지표 정의)
  4. `semantic_entities` + `semantic_join_graph`
  5. `tables` + `glossary`
- `ambiguity_policy`: 모호한 질문일 때 어느 테이블을 쓸지 결정하는 규칙 (예: 한도/잔여한도/연체 → `tmdaa1d12` 우선)

➡️ **사용 시점**: 모든 SQL 생성의 최상위 가드레일. 항상 적용.

---

## 3. `canonical_domains` — 업무 영역(도메인) 정의

질문이 **어느 업무 영역**에 속하는지 분류하는 최상위 비즈니스 단위입니다. 4개 도메인이 있습니다.

각 도메인의 필드:

- **`name`**: 도메인 이름 (예: `기업영업기업고객관리`)
- **`priority`**: 우선순위 숫자. 질문이 여러 도메인에 걸칠 때 **숫자가 높은 도메인을 먼저 선택**. (기업고객관리/가맹점관리=10 > 채권관리=9 > 리스크손익=8)
- **`business_scope`**: 이 도메인이 다루는 업무 범위를 한 문장으로 설명
- **`keywords`**: 이 도메인으로 라우팅하는 트리거 단어들 (질문에 "한도", "연체"가 있으면 기업고객관리로)
- **`primary_entities`**: 이 도메인의 주력 엔티티(논리 테이블)
- **`default_fact_table`**: 기본으로 쓸 물리 테이블 (예: `tmdaa1d12`)
- **`default_time_dimension`**: 기본 시간축 (예: `기준년월`)
- **`default_filters`**: 이 도메인에서 기본 적용할 필터 (예: 거래정지/폐업 제외)
- **`preferred_metrics`**: 이 도메인에서 주로 쓰는 지표 목록

➡️ **사용 시점**: 질문을 받자마자 **가장 먼저** 어느 도메인인지 판단할 때.

| 도메인 | priority | 주제 | 기본 테이블 |
|---|---|---|---|
| 기업영업기업고객관리 | 10 | 한도/이용/연체/신용등급 | tmdaa1d12 |
| 기업영업가맹점관리 | 10 | 가맹점 매입/지급/수수료 | tbdaaus01 |
| 기업영업채권관리 | 9 | 특수채권/연체 | tmdaaus01 |
| 기업영업리스크손익 | 8 | 대손충당금/PD/LGD | tbmewcm94 |

---

## 4. `semantic_entities` — 논리적 엔티티 (의미 계층의 핵심)

물리 테이블을 **비즈니스 의미로 추상화**한 것입니다. 물리 테이블명(`tmdaa1d12`)은 외우기 어려우니 `corporate_customer_enrichment_monthly` 같은 **의미 있는 이름**으로 다룹니다.

각 엔티티의 필드:

- **`name`** / **`korean_name`**: 영문 논리명 / 한글명
- **`physical_table`**: 매핑되는 실제 테이블 (`card_system.tmdaa1d12`)
- **`grain`**: ⭐ **한 행(row)이 의미하는 단위**. 가장 중요한 개념. 예: "기업고객-기준년월-기준년월일 1건" = 한 행 = 한 고객의 특정 월/일 스냅샷. grain을 알아야 중복 집계를 피할 수 있음
- **`role`**: 엔티티 종류
  - `aggregate_fact`: 집계성 사실 테이블 (스냅샷)
  - `fact`: 원장성 사실 테이블
  - `dimension`: 분류/마스터 테이블 (업종, 가맹점 마스터)
- **`primary_key`**: 행을 유일하게 식별하는 키
- **`canonical_dimensions`**: 이 엔티티에서 **그룹화/필터에 쓰는 분류성 컬럼** (고객식별자, 기업명, 신용등급 등)
- **`canonical_facts`**: 이 엔티티에서 **집계(SUM/AVG)하는 수치성 컬럼** (한도금액, 이용금액, 연체원금 등)
- **`semi_additive_warning`**: ⚠️ 합산 주의 경고. "스냅샷이라 여러 월을 단순 합산하면 중복됨 → 기준년월 유지하라"
- **`use_when`**: 이 엔티티를 **써야 하는** 상황
- **`avoid_when`**: 이 엔티티를 **쓰면 안 되는** 상황

➡️ **사용 시점**: 도메인 선택 후 → 어떤 테이블을 쓸지, 그 테이블에서 무엇이 차원이고 무엇이 측정값인지 판단할 때.

> **`canonical_dimensions` vs `canonical_facts` 차이**: dimension은 "쪼개는 기준"(GROUP BY/WHERE 대상), fact는 "더하는 값"(SUM/AVG 대상). 이게 SQL의 SELECT 구조를 결정합니다.

---

## 5. `semantic_join_graph` — 안전한 조인 경로

테이블을 어떻게 안전하게 JOIN할지 정의합니다. `safe_paths` 아래에 허용된 조인만 나열되어 있습니다.

각 경로의 필드:

- **`name`**: 조인 경로 이름
- **`from_entity` / `to_entity`**: 조인하는 두 엔티티
- **`join_type`**: 관계 유형 (`many_to_one`, `one_to_many`, `many_to_many_cautious`)
- **`sql`**: 실제 조인 조건식 (예: `a.가맹점번호 = b.가맹점번호 AND ...`)
- **`caution`**: ⚠️ 중복 집계 위험 등 주의사항 (예: "고객-월 사전 집계 후 조인")
- **`use_when`**: 이 조인을 쓸 상황

➡️ **사용 시점**: 질문이 여러 테이블을 결합해야 할 때. **여기 없는 조인은 하면 안 됨** (`combined_risk_sales_query` intent에서 "safe_paths만 사용" 명시).

---

## 6. `canonical_metrics` — 표준 지표 공식

"한도사용률", "기업고객수" 같은 **비즈니스 지표의 공식 정의**입니다. (evidence_priority 3순위)

각 지표의 필드:

- **`name`**: 지표명 (예: `기업한도사용률`)
- **`domain`**: 소속 도메인
- **`source_entity` / `source_table`**: 계산 출처 엔티티/테이블
- **`expression`**: ⭐ **실제 SQL 집계식** (예: `(SUM(기업총한도금액) - SUM(기업총잔여한도금액))::FLOAT / NULLIF(SUM(기업총한도금액), 0)`)
- **`numerator_metric` / `denominator_metric`**: 비율 지표일 때 분자/분모 (가독성용 설명)
- **`default_time_dimension`**: 기본 시간축
- **`additive`**: ⭐ **합산 가능 여부**
  - `true`: 기간 합산해도 안전 (이용금액 등)
  - `false`: 합산하면 안 됨 (한도, 고객수, 잔액 — 스냅샷성)
- **`non_additive_by`**: 어떤 축에서 합산 불가인지 (예: 기준년월, 고객식별자)
- **`unit`**: 단위 (`KRW`, `count`, `ratio`)
- **`synonyms`**: 동의어 (사용자가 "한도소진율"이라 해도 이 지표로 매핑)
- **`required_filters`**: 필수 필터 (채권/충당금 지표는 `개인기업구분코드 = '2'`)
- **`preferred_dimensions`**: 이 지표와 함께 자주 보는 차원

➡️ **사용 시점**: 사용자가 "매출/연체율/한도사용률" 등 **이름 있는 지표**를 물을 때. `expression`을 그대로 SQL에 사용.

---

## 7. 실행 규칙 4종

### `time_resolution_rules` — 시간 표현 변환

사용자의 시간 표현을 컬럼/조건으로 변환.

- `user_expression`: "YYYY년 MM월", "일별", "월별", "연도별", "최근 기준"
- → 각각에 맞는 `monthly_columns` / `preferred_columns` / `rule` 제공
- 예: "최근 기준" → `default_time_dimension에서 MAX 값을 서브쿼리로`

### `metric_generation_rules` — 지표 생성 주의사항

- `ratio_metrics`: 비율은 같은 GROUP BY 레벨에서 NULLIF로 나눔
- `snapshot_metrics`: 스냅샷 수치는 기간 합산 주의
- `count_distinct`: 건수는 grain에 맞는 DISTINCT 키 사용
- `amount_metrics`: 금액은 `COALESCE(컬럼, 0)`
- `tmdaa1d12_snapshot`: tmdaa1d12 한도/카드수는 합산 금지

### `intent_routing` — 질문 의도 → 액션 매핑

질문 유형별로 어떤 테이블/지표를 쓸지 라우팅.

- `intent`: 의도 이름 (corporate_customer_query, metric_query, detailed_list, comparison_or_ranking 등)
- `choose_when`: 이 의도로 분류하는 조건
- `action`: 취할 행동 (예: "tmdaa1d12 선택하고 기준년월 시간축 사용")

### `result_shape_defaults` — 결과 형태 기본값

- `limit_for_detail: 100` (목록), `limit_for_ranking: 10` (순위)
- `order_by_for_time_series` / `order_by_for_ranking`: 정렬 기본값

---

## 8. `tables` — 물리 스키마 상세 (실제 DB 구조)

실제 테이블의 **모든 컬럼 정의**. 가장 구체적인 계층입니다. (evidence_priority 5순위)

테이블별 필드:

- **`name` / `physical_table`**: 테이블명
- **`description`**: 테이블 설명
- **`grain` / `primary_key`**: 행 단위 / 기본키
- **`time_dimensions`**: 시간 관련 컬럼들 (기준년월, 기준년월일 등)
- **`dimensions`**: 분류성 컬럼 (텍스트/코드)
- **`measures`**: 수치성 컬럼 (집계 대상)
- **`filters`**: 미리 정의된 필터 조건

각 컬럼(dimension/measure)의 하위 필드:

- `name`: 컬럼명
- `description`: 설명
- `expr`: 실제 SQL 표현식 (보통 컬럼명 그대로)
- `data_type`: `VARCHAR` / `NUMERIC`
- `synonyms`: 동의어
- `default_aggregation`: 측정값의 기본 집계 (`sum`/`avg`/`max`)
- `unit`: 단위
- `unique`: 유일값 여부 (PK 표시)

8개 테이블: `tmdaa1d12`(기업고객 월별), `tbdaadb17`(업종코드), `tbdaadt01`(가맹점 마스터), `tbdaaus01`(일별 가맹점), `tmdaaus01`(특수채권), `tbmewcm94`(대손충당금), `tbmaisd06`(월별 가맹점).

➡️ **사용 시점**: 실제 컬럼명/타입/집계 방식을 확정할 때. SQL의 최종 컬럼 매핑.

---

## 9. `relationships` — 물리적 테이블 관계

`semantic_join_graph`의 물리 테이블 버전. 실제 테이블명 기준 조인식과 관계 타입(many_to_one 등), 중복 주의 설명 포함.

---

## 10. 참조/예시 계층

### `metrics` — 간이 지표 목록

`canonical_metrics`의 축약 버전 (name, sql, source_table, type, unit). `&1`, `*1` 같은 YAML 앵커/별칭으로 synonyms를 공유.

### `glossary` — 용어 사전

비즈니스 용어 정의. (evidence_priority 5순위)

- `term`: 용어 (기업고객, 한도사용률, MCC 등)
- `canonical`: 표준 정의/공식
- `description`: 설명
- `aliases`: 별칭
- `sql_hint`: SQL 작성 힌트 (예: "기준년월 형식은 'YYYYMM'")

### `query_references` — 의도별 SQL 패턴 템플릿

의도별 SQL 작성 가이드. (evidence_priority 2순위)

- `intent`: 의도
- `when_user_says`: 트리거 발화 예시
- `rules`: 작성 규칙
- `sql_pattern`: `{변수}` 자리표시자가 있는 SQL 템플릿
- `primary_table` / `join_tables`: 주 테이블/조인 테이블

### `verified_queries` — 검증된 정답 SQL ⭐

**가장 신뢰도 높은 근거** (evidence_priority 1순위). 실제로 검증된 질문-SQL 쌍.

- `name`: 쿼리 이름
- `question`: 자연어 질문
- `sql`: 완성된 정답 SQL
- `parameters`: 파라미터 정의 (type, required, sample_values, default)
- `tags`: 검색용 태그

➡️ **사용 시점**: 질문이 이 예시와 비슷하면 **거의 그대로 베껴서** 사용. 최우선.

### `domains` — 도메인 종합 매핑

`canonical_domains`와 별개로, 도메인별 **테이블 + 지표 + 접근 권한(`access_roles`)**을 묶음. (analyst, manager, risk_team 등 권한 제어용)

---

## 핵심 요약: LLM이 질문을 처리하는 흐름

질문이 들어오면 대략 이 순서로 좁혀갑니다.

1. **`llm_semantic_contract`** 규칙을 항상 머리에 둠
2. **`verified_queries`**에 비슷한 예시가 있나? → 있으면 그대로 사용 (끝)
3. 없으면 **`intent_routing` / `canonical_domains`**로 의도·도메인 분류 (keyword·priority)
4. 도메인의 `default_fact_table`, **`semantic_entities`**로 테이블 선택 (`use_when`/`avoid_when`, `grain` 확인)
5. **`canonical_metrics`**에서 지표 공식 가져옴 (`additive` 여부로 합산 가능성 판단)
6. 여러 테이블이면 **`semantic_join_graph.safe_paths`**만 사용
7. **`time_resolution_rules`**로 시간 조건, **`metric_generation_rules`**로 집계 안전성 처리
8. **`tables`**에서 실제 컬럼명·타입 확정, **`glossary`**로 용어 해석
9. **`result_shape_defaults`**로 LIMIT/ORDER BY 마무리

이 파일에서 가장 자주 등장하고 가장 중요한 개념은 **`grain`(행 단위)**과 **`additive`(합산 가능 여부)**입니다.
두 가지 모두 "스냅샷 데이터를 여러 기간 합산하면 중복된다"는 위험을 막기 위한 장치이고,
거의 모든 경고(`semi_additive_warning`, `caution`, `metric_generation_rules`)가 이 문제를 가리킵니다.

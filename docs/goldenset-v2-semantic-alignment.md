# 골든셋 v2 — 시맨틱 레이어 `2026-08-14.1-v2` 기준 재생성

이전에는 시맨틱 레이어가 바뀔 때마다 골든셋을 부분 교정했다. 이번에는 **현재 레이어를
단일 출처로 삼아 1,000건을 처음부터 다시 생성**했다. 교정 누락이 남지 않고, 레이어가
다시 바뀌어도 같은 방식으로 재생성하면 된다.

```bash
python scripts/audit_goldenset_sql.py
# 시맨틱 레이어 2026-08-14.1-v2 / 케이스 1000건
# 문제 케이스 0건
```

## 재생성에서 달라진 것

**지표·분석축 풀을 레이어로 필터한다.** 생성기가 쓰던 지표·축 중 현재 레이어에 없는 7개를
자동으로 떨어뜨렸다. 이전처럼 "만들고 나서 고치는" 단계가 사라졌다.

| 종류 | 테이블 | 항목 | 빠진 컬럼 |
|---|---|---|---|
| 지표 | tmdaa3e16 | 평균 통합 BSS 평점 | 통합BSS평점 |
| 지표 | tbdaaat03 | 평균 통합 BSS 평점 / 평균 통합 ASS 평점 | 통합BSS평점, 통합ASS평점 |
| 지표 | tbdaadb17 | 평균 JCB 수수료율 | JCB카드수수료율 |
| 지표 | tddaa3e21 | 평균 통합 ASS 평점 | 통합ASS평점 |
| 축 | tbdaa1d12 | 소호 BSS 등급 | 소호로지스틱BSS등급구분코드 |
| 축 | tmdaa5e11 | 선취후취 | 선취후취구분코드 (이 테이블에 없음) |

**별칭 충돌을 생성기에서 없앴다.** 상품 마스터 조인 별칭을 `pd` 로 바꿔
`tbmewcm94`(별칭 `p`)와 겹치지 않게 했다. 이전 파일에서 실행 불가였던 5건이 구조적으로
다시 생기지 않는다.

**스키마 접두사는 처음부터 붙이지 않는다.** `FROM tbdaa1d12 a` 형태이며,
`ATHENA_DATABASE` 로 접속한 세션에서 그대로 돈다.

**정렬 전순서와 채점 메타데이터가 생성 단계에 들어갔다.** 아래 두 절 참고.

이전 파일 대비 질문 892건이 그대로 남았고 108건이 교체됐다(빠진 지표·축 자리에 다른
조합이 들어왔다). 구조 중복은 여전히 0이다.

## 구성

| 항목 | 값 |
|---|---|
| 레코드 | 1,000 |
| 고유 SQL 구조(리터럴 마스킹 후) | 1,000 |
| 고유 질문 | 1,000 |
| 도메인 | card_usage 201 · corporate_sales_targeting 201 · customer_card_portfolio 200 · merchant_sales 200 · credit_risk 198 |
| 난이도 | hard 538 · medium 323 · easy 139 |
| 사용 물리 테이블 | 28 |
| 질의형태 | business_case 218 + 조합형 12종 782 |

## 채점(에이전트 SQL 비교) 관점

### 1. LIMIT 동점 비결정성 제거 — 265건

`ORDER BY "총매출금액" DESC LIMIT 20` 처럼 정렬 키가 측정값 하나뿐이면 20위 경계 동점에서
**어느 행이 뽑히는지가 실행할 때마다 달라진다**. 정답이 흔들리면 `--verify` 가 없는
드리프트를 만들고, 에이전트가 똑같이 맞혀도 경계 행이 달라 `mismatch` 로 떨어진다.

GROUP BY 가 있으면 그룹 키 전체를, 없으면 최상위 스코프의 고유 키(가맹점번호·고객식별자·
회원일련번호 등)를 꼬리에 붙여 전순서로 만들었다. `LIMIT 1000000` 목록형에도 적용해
`--fetch-rows`·`--answer-rows` 로 잘릴 때 남는 행이 고정되게 했다.

결과: 정렬에 민감한 케이스 중 `tie_risk` **0건**.

### 2. 채점용 메타데이터

| 필드 | 값 | 쓰임 |
|---|---|---|
| `answer_shape` | scalar 19 · single_row 28 · grouped 822 · list 131 | 스칼라는 허용오차 비교, 목록형은 행 잘림 주의 |
| `row_limit` | 정수 또는 null | 후보를 같은 상한으로 맞춰 돌릴 기준 |
| `order_sensitive` | LIMIT 절단 케이스 | 행 **집합**이 정렬에 좌우되는가 |
| `total_order` | ORDER BY 전순서 여부 | |
| `tie_risk` | 0건 | order_sensitive 인데 전순서가 아닌 것 |
| `implicit_filters` | 질문에 없는 정답 SQL 필터 | mismatch 원인 구분 |
| `semantic_layer_version` | `2026-08-14.1-v2` | 어느 레이어로 만든 정답인지 |

`implicit_filters` 가 실무에서 가장 쓸모 있다. `2026년 5월 카드브랜드별 금월이용합계금액
구성비를 알려줘` 의 정답 SQL에는 `개인기업구분코드 = '2'` 가 있지만 질문에는 "기업"이라는
말이 없다. 이 에이전트는 기업영업 전용이라 프롬프트 스코프 절로 이 필터를 항상 걸도록
설계돼 있어 질문을 고치지 않았고, 대신 사실을 필드로 남겨 mismatch 판정 때
"스코프 누락"인지 바로 갈리게 했다.

`가맹점상태구분코드 = '1'`, `사용여부 = '1'` 처럼 질문에 안 드러난 필터가 소수 더 있지만
해당 케이스는 verified query 에 대응하는 `business_case` 라, 문구를 바꾸면 VQ 매칭 경로가
달라져 평가 대상 자체가 바뀐다. 그래서 질문은 그대로 두고 필드로만 남겼다.

## 감사

`scripts/audit_goldenset_sql.py` 는 Athena에 던지기 전에 "실행하면 반드시 실패할" SQL을
찾는다. 실행 비용이 0이므로 레이어가 바뀔 때마다 돌린다.

| 코드 | 뜻 | 대응 Athena 오류 |
|---|---|---|
| `parse` | Trino 방언 파싱 실패 | SYNTAX_ERROR |
| `dup_alias` | 한 별칭이 서로 다른 관계에 바인딩 | 별칭 중복·모호 |
| `wrong_table` | `a."컬럼"` 이 별칭 a 의 테이블에 없음 | COLUMN_NOT_FOUND |
| `unknown_column` | 어느 테이블에도 없고 정의되지도 않음 | COLUMN_NOT_FOUND |
| `ambiguous` | 관계 2개 이상 스코프의 한정자 없는 공통 컬럼 | AMBIGUOUS_NAME |
| `not_aggregated` | GROUP BY 쿼리의 집계 밖 컬럼이 그룹키에 없음 | EXPRESSION_NOT_AGGREGATE |
| `non_numeric_agg` | VARCHAR 컬럼에 SUM/AVG | 타입 불일치 |
| `removed_column` | 선택 테이블에 없는 이름을 컬럼으로 사용 | COLUMN_NOT_FOUND |
| `restricted_table` | `tbdaaat18` 참조 | 정책 위반 |
| `table_mismatch` | `expected_tables` 메타와 실제 참조 불일치 | (메타 오류) |

스코프를 CTE·서브쿼리 단위로 나눠 보므로 CTE 안팎에서 같은 별칭을 재사용하는 정상 SQL을
오탐하지 않고, `SUM(CASE WHEN "기준년월" = ... THEN 금액 END)` 처럼 조건절에만 문자열
컬럼이 오는 경우도 값 위치와 구분한다.

## 정답 결과는 새로 뽑아야 한다

SQL이 새로 생성됐으므로 기존 정답 결과가 있으면 버리고 처음부터 돌린다.

```bash
python scripts/run_goldenset_answers.py --restart --limit 20   # 표본 먼저
python scripts/run_goldenset_answers.py                        # 나머지 이어서
```

## 정적으로 못 잡는 것

- 파티션·적재 상태에 따른 0행. 브랜드명·기업명 리터럴이 실제 데이터와 다르면 구문은 맞아도
  결과가 비어 나온다. 표본 실행에서 `empty` 비중을 먼저 본다.
- `BETWEEN 'YYYYMM01' AND 'YYYYMM31'` 같은 문자열 경계 비교. verified query 관례를 따랐고
  문자열 비교라 동작에는 문제가 없다.
- 대용량 스캔. `--max-scanned-gb` 로 제한하며 도메인별로 나눠 돌린다.

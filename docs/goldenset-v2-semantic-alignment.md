# 골든셋 v2 — 시맨틱 레이어 정합 교정

`semantic_layer.yaml` 을 `2026-08-14.1-v2` 로 올리면서 골든셋 정답 SQL을 다시 맞췄다.
정적 감사는 `scripts/audit_goldenset_sql.py` 로 언제든 재현된다.

```bash
python scripts/audit_goldenset_sql.py
# 시맨틱 레이어 2026-08-14.1-v2 / 케이스 1000건
# 문제 케이스 0건
```

## 이번에 고친 것

### 1. 별칭 충돌 5건 — 실행하면 반드시 실패하던 SQL

`tbmewcm94`(대손충당금)의 별칭이 `p` 인데 상품 마스터 조인에도 `p` 를 써서, 같은 스코프에
두 관계가 같은 이름으로 묶여 있었다. 조인 조건도 `p."상품코드" = p."상품코드"` 라는
항등식이 되어 있었다.

```sql
-- before (cs-golden-v2-0848)
FROM tbmewcm94 p
LEFT JOIN tbdaada72 p
  ON p."상품코드" = p."상품코드"

-- after
FROM tbmewcm94 p
LEFT JOIN tbdaada72 pd
  ON p."상품코드" = pd."상품코드"
```

상품 마스터에만 있는 컬럼(`한글상품명`)의 참조도 `pd` 로 옮겼다. 대상은
`cs-golden-v2-0848 · 0883 · 0893 · 0908 · 0928` 이며, 모두 "카드 상품명별 충당금·회원수"
계열이다. 이 5건은 v1 폴더 사본에도 같은 형태로 있어 함께 고쳤다.

### 2. 삭제 컬럼 17건 — 이미 적용돼 있던 교정 확인

`V2_APPLIED.md` 에 기록된 대로 평가·JCB 컬럼 20개가 시맨틱 레이어에서 빠지면서
`소호로지스틱BSS등급구분코드` 를 쓰던 케이스가 `tbdaa1d12`(일별) → `tmdaa1d12`(월 스냅샷)로
옮겨져 있었다. 감사 결과 이 17건은 현재 레이어와 정합하며 추가 수정이 필요 없다.

## 감사 항목

`scripts/audit_goldenset_sql.py` 는 Athena에 던지기 전에 "실행하면 반드시 실패할" SQL을
찾는다. 실행 비용이 0이므로 시맨틱 레이어가 바뀔 때마다 돌리면 된다.

| 코드 | 뜻 | 대응하는 Athena 오류 |
|---|---|---|
| `parse` | Trino 방언 파싱 실패 | SYNTAX_ERROR |
| `dup_alias` | 한 별칭이 서로 다른 관계에 바인딩 | 별칭 중복·모호 |
| `wrong_table` | `a."컬럼"` 이 별칭 a 의 테이블에 없음 | COLUMN_NOT_FOUND |
| `unknown_column` | 어느 테이블에도 없고 쿼리에서 정의되지도 않음 | COLUMN_NOT_FOUND |
| `ambiguous` | 관계 2개 이상 스코프의 한정자 없는 공통 컬럼 | AMBIGUOUS_NAME |
| `not_aggregated` | GROUP BY 쿼리에서 집계 밖 컬럼이 그룹키에 없음 | EXPRESSION_NOT_AGGREGATE |
| `non_numeric_agg` | VARCHAR 컬럼에 SUM/AVG | 타입 불일치 |
| `removed_column` | 선택한 테이블에 없는 이름을 컬럼으로 사용 | COLUMN_NOT_FOUND |
| `restricted_table` | `tbdaaat18` 참조 | 정책 위반 |
| `table_mismatch` | `expected_tables` 메타와 실제 참조 불일치 | (메타 오류) |

스코프를 CTE·서브쿼리 단위로 나눠 보기 때문에, 같은 별칭이 CTE 안팎에서 다른 대상을
가리키는 정상 SQL을 오탐하지 않는다. 집계 타입 검사도 `SUM(CASE WHEN "기준년월" = ... THEN 금액 END)`
처럼 조건절에만 문자열 컬럼이 오는 경우를 값 위치와 구분한다.

`--json reports/goldenset-audit.json` 을 주면 문제 목록을 파일로 남긴다. 문제가 하나라도
있으면 종료 코드 1이므로 CI에 그대로 붙일 수 있다.

## 아직 정적으로 못 잡는 것

감사는 문법·스키마·타입 정합만 본다. 다음은 실제 실행으로만 확인된다.

- 파티션·적재 상태에 따른 0행. 특히 브랜드명·기업명 리터럴이 실제 데이터와 다르면
  구문은 맞아도 결과가 비어 나온다.
- `BETWEEN 'YYYYMM01' AND 'YYYYMM31'` 처럼 존재하지 않는 날짜를 경계로 쓰는 문자열 비교.
  현재 verified query 관례를 그대로 따랐고 문자열 비교라 동작에는 문제가 없다.
- 대용량 스캔. `scripts/run_goldenset_answers.py --max-scanned-gb` 로 제한하며 표본부터 돌린다.

## 채점(에이전트 SQL 비교) 관점 보강

정답 SQL이 문법적으로 맞는 것과, 에이전트가 만든 SQL과 **공정하게 비교되는 것**은 다른
문제다. `run_goldenset_eval.py` 의 판정 체계(`exact` / `match` / `value_match` /
`subset_match` / `required_match`)를 기준으로 다시 훑어 두 가지를 손봤다.

### 1. LIMIT 동점 비결정성 제거 — 266건

`ORDER BY "총매출금액" DESC LIMIT 20` 처럼 정렬 키가 측정값 하나뿐이면, 20위 경계에
동점이 있을 때 **어느 행이 뽑히는지가 실행할 때마다 달라진다**. 정답 결과 자체가
비결정적이면 `--verify` 가 없는 드리프트를 만들고, 에이전트가 똑같이 맞혀도 경계 행이
달라 `mismatch` 로 떨어진다.

정렬이 전순서가 되도록 꼬리 키를 붙였다. GROUP BY 가 있으면 그룹 키 전체를, 없으면
최상위 스코프의 고유 키(가맹점번호·고객식별자·회원일련번호 등)를 덧붙인다.

```sql
-- before
ORDER BY "총매출금액" DESC
LIMIT 20

-- after
ORDER BY "총매출금액" DESC,
  i."업종대분류코드명",
  i."가맹점업종명"
LIMIT 20
```

`LIMIT 1000000` 목록형에도 같이 적용했다. 저장 행 상한(`--answer-rows`)이나
`--fetch-rows` 로 잘릴 때 어느 행이 남는지가 고정되기 때문이다.

결과: 정렬에 민감한 케이스(LIMIT 있는 228건) 중 **전순서가 아닌 것 0건**.

### 2. 채점용 메타데이터 6종 추가

값이 어긋났을 때 "에이전트가 틀린 것"인지 "골든셋 컨벤션 차이"인지 가르려면, 케이스가
어떤 성격인지 먼저 보여야 한다. 다음 필드를 각 레코드에 넣었다.

| 필드 | 값 | 쓰임 |
|---|---|---|
| `answer_shape` | `scalar` 19 · `single_row` 28 · `grouped` 822 · `list` 131 | 스칼라는 허용오차 비교, 목록형은 행 잘림 주의 |
| `row_limit` | 정수 또는 null | 후보와 같은 상한으로 맞춰 돌릴 때 기준 |
| `order_sensitive` | 228건 true | LIMIT 절단이라 행 **집합**이 정렬에 좌우되는 케이스 |
| `total_order` | 314건 true | ORDER BY 가 전순서인가 |
| `tie_risk` | 0건 | `order_sensitive` 인데 전순서가 아닌 것 — 이번에 전부 해소 |
| `implicit_filters` | 706건 보유 | 질문에 안 드러나지만 정답 SQL이 건 필터 |

`implicit_filters` 가 가장 중요하다. 예를 들어
`2026년 5월 카드브랜드별 금월이용합계금액 구성비를 알려줘` 의 정답 SQL에는
`개인기업구분코드 = '2'` 가 들어 있지만 질문에는 "기업"이라는 말이 없다. 이 에이전트는
기업영업 전용이라 프롬프트의 스코프 절로 이 필터를 **항상** 걸도록 설계돼 있으므로
질문을 고치지 않았다. 대신 이 사실을 필드로 남겨서, mismatch 가 났을 때
"스코프를 안 걸어서 틀린 것"인지 한눈에 갈리게 했다.

분포는 기업 스코프 371건, 스냅샷 중복 제거 다수, 최신 스냅샷 기준·정상 가맹점만·
사용 중인 업종코드만·부점 적용기간 유효행만이 소수다.

### 3. 질문은 고치지 않았다

`가맹점상태구분코드 = '1'`, `사용여부 = '1'` 처럼 질문에 안 드러난 필터가 8건 더 있지만
질문 문구는 그대로 뒀다. 이 케이스들은 verified query(`brand_active_merchant_count` 등)에
대응하는 `business_case` 라, 질문을 바꾸면 VQ 매칭 자체가 달라져 평가하려던 경로가
바뀐다. 대신 `implicit_filters` 에 남겼다.

### 정답 결과 재실행이 필요하다

266건의 정답 SQL이 바뀌었으므로 이미 뽑아 둔 정답 결과가 있으면 그 건들만 다시 돌린다.

```bash
python scripts/run_goldenset_answers.py --rerun-all \
  --ids $(python - <<'EOF'
import json
print(" ".join(r["id"] for r in map(json.loads,
      open("tests/fixtures/corporate_sales_text2sql_goldenset_v2.jsonl", encoding="utf-8"))
      if r.get("tie_risk") is not None and r.get("order_sensitive")))
EOF
)
```

아직 `reports/goldenset-v2-answers.jsonl` 이 없다면 신경 쓸 것 없이 처음부터 돌리면 된다.

# goldenset v2 실행 실패 40건 — SQL 자체가 안 만들어진 것들

`goldenset-v2-sql-errors.csv` 40건. mismatch(값이 다름)와 달리 **SQL이 실행조차
안 된** 경우다. 분포는 `column_not_found` 27 · `guard` 9 · `group_by` 3 · `syntax` 1.

대상 모델은 gemma4·gpt-oss 급 로컬 LLM이라 "규칙을 더 잘 쓰기"로는 안 잡힌다.
Athena가 거절하기 **전에** 잡고, 고칠 수 있으면 고치고, 못 고치면 재시도에
쓸 만한 정보를 주는 쪽으로 갔다.

## 1. 없는 컬럼 (27건) — 가장 큰 덩어리

정적 검증(`_validate_qualified_columns`)이 별칭 붙은 `a."컬럼"` 만 봤다. 실패한
SQL은 거의 다 별칭 없는 `"컬럼"` 을 쓴다. 그래서 아무도 못 잡고 Athena까지 갔다.

두 갈래였다.

| 갈래 | 예 | 건수 |
|---|---|---|
| 스키마에 아예 없는 이름 | `"교부유형"`←교부유형구분코드, `"금월일반이용금액"`←금월일반매출이용금액, `"시도"`←한글시도명 | 16 |
| 있지만 그 테이블에 없음 | `tbdaaus01."기준년월"`(일별 테이블), `tbdaa1d12."모바일카드여부"`(tbdaaat05에 있음) | 11 |

**대응** — `text2sql_agent/v2/column_repair.py` 신규, `validate_sql()` 에 연결.

- CTE 본문과 인라인 서브쿼리를 **각각의 스코프**로 본다. SQL 전체 합집합으로
  보면 "이 CTE 안에서는 없는 컬럼"을 놓친다(실제 cs-golden-v2-0064).
- 확실한 것만 자동 교정한다. ① 대소문자·구분자만 다른 완전 일치 ② 접미사
  변형(구분코드/코드/명/여부) ③ 후보가 딱 하나인 근사 이름.
  **날짜 단위가 달라지는 교정은 금지**한다. `기준년월`(YYYYMM)을
  `기준년월일`(YYYYMMDD)로 바꾸면 에러 대신 조용히 틀린 값이 나온다.
- 못 고치면 재시도 메시지에 **어디에 있는지**를 적는다.

  ```
  tbdaa1d12에 "모바일카드여부" 컬럼이 없습니다.
  그 이름은 tbdaaat05."모바일카드여부", tddaa3d01."모바일카드여부" 에 있으니
  해당 테이블을 쓰거나 조인하세요.
  ```

  로컬 모델에게 "컬럼이 없습니다"는 아무 정보가 아니지만 위 문장은 다르다.

결과: **27건 중 4건 자동 교정, 22건 구체적 재시도 힌트, 1건 미해결.**
남은 1건(cs-golden-v2-0679)은 모델이 `'ALL' AS "교부유형"` 으로 없는 축을
지어낸 경우라 컬럼 해석으로는 못 고친다.

찾는 김에 매처 버그도 하나 잡았다. `FROM a JOIN b` 에서 `JOIN` 이 `a` 의 별칭으로
소비돼 **조인한 테이블이 스코프에 아예 안 들어가고 있었다**. 같은 모양의 정규식이
`schema._validate_qualified_columns()` 에도 있다(아래 "남은 것" 참고).

## 2. 워크북 툴 오발동 (7건)

"충당금" 단어만으로 `대손비용률_분석` 툴이 골라져, 문장 4개를 `-- [월초충당금]`
머리말로 이어 붙이고 가맹점명이 `%도미노피자%` 로 하드코딩된 SQL이 나갔다.
읽기 전용 가드가 "SELECT/WITH로 시작하지 않는 SQL" 로 거절한다.

    질문: 2025년 9월 원화대출잔액을 충당금 차주 구분 기준으로 집계해줘
    나간 것: 월초충당금·월말충당금·상각내역·대손비용률종합 4개 쿼리

현재 코드의 `_tool_request_is_supported()` 가 이미 7건 전부를 막는다(평가 실행이
그 가드보다 앞선다). 회귀 테스트로 고정만 했다. 툴이 정당하게 선택됐을 때
`sql` 필드는 **표시용 이어붙인 문자열**이고 실행은 툴이 내부에서 이미 끝낸다는
점은 그대로 두었다 — 평가 하네스가 그걸 실행 가능한 SQL로 오해한 것이 겹쳤다.

## 3. EXPRESSION_NOT_AGGREGATE (3건)

전체합을 CROSS JOIN으로 끌어와 구성비를 계산하는 모양이 반복된다.

```sql
SELECT ls."기업총한도금액", SUM(ls."유효기업체크카드수"),
       ROUND(CAST(SUM(...) AS DOUBLE) * 100.0 / NULLIF(t.total_check_cards, 0), 2)
FROM latest_snapshot ls CROSS JOIN total t
GROUP BY ls."기업총한도금액"
-- EXPRESSION_NOT_AGGREGATE: 't.total_check_cards' must be an aggregate expression
```

`athena_rules` 에 같은 지시가 이미 있는데 모델이 안 지킨다.
→ `sql_dialect_guard.rewrite_cross_join_scalars()`: GROUP BY가 있는 SELECT에서
CROSS JOIN 별칭 참조가 집계 밖에 있으면 `MAX(...)` 로 감싼다. 한 행짜리 원천이라
값이 바뀌지 않는다. **3건 전부 자동 교정.**

## 4. 평가 하네스 오탐 (2건)

`scripts/run_goldenset_answers.py::assert_read_only` 가 `";" in sql` 로 다중 문장을
판정해서, 주석 안의 세미콜론에 걸렸다.

```sql
WHERE rn = 1 /* No column for "교체 카드" exists; using total valid cards as a proxy */
"그룹등급", -- approximated grade column; using closest semantic match
```

운영 가드(`db._validate_read_only_sql`)는 sqlparse로 주석을 지우고 판정한다.
하네스를 같은 기준으로 맞췄다. 세미콜론 없이 이어 붙인 다중 `WITH`(위 2번의
워크북 툴 출력)는 계속 막는다.

**이 2건은 에이전트 문제가 아니라 평가 문제였다.** 운영에서는 실행됐을 SQL이다.

## 5. syntax (1건)

`FROM joined AND "가맹점명" LIKE '%기준%가맹점%'` — WHERE 없는 dangling AND.
`sql_dialect_guard.audit_sql()` 이 이미 잡아 재시도로 돌린다.

다만 진짜 원인은 따로 있었다. 질문
"2026년 7월 **기준 가맹점** 대표자로 등록된 관계를 관계구분별로 보여줘" 에서
날짜를 지우자 `기준 가맹점` 이 가맹점명으로 추출돼 엉뚱한 LIKE 필터가 붙었다.
→ `_extract_merchant_name_by_rule()` 의 generic 집합과 접두·접미 제거에
`기준`·`가맹점`·`당월`·`금월`·`전월` 을 추가했다.

## 남은 것

- **cs-golden-v2-0679** — 모델이 없는 축을 `'ALL' AS "교부유형"` 으로 지어냈다.
  컬럼 해석이 아니라 "질문이 요청한 축이 선택 테이블에 없다"를 생성 시점에
  알려야 하는 문제다(테이블 선택 쪽 과제와 같은 뿌리).
- **`schema._validate_qualified_columns()` 의 같은 정규식 버그** — `FROM a JOIN b`
  의 `JOIN` 을 별칭으로 삼켜 조인 테이블을 놓친다. `column_repair` 쪽만
  고쳤다. 기존 동작에 의존하는 곳이 있을 수 있어 별도로 다루는 게 안전하다.
- **`tbdaaus01`(일별) ↔ `tmdaaus01`(월별) 혼동** — 한 글자 차이라 모델이 자주
  틀린다. 지금은 재시도 힌트로만 잡는다. 월 단위 질문에 일별 테이블이 후보로
  오르지 않게 하는 편이 근본적이다.

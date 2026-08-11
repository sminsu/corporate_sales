# goldenset v2 SQL 오류 400건 원인 분석

입력: `goldenset-v2-sql-errors.csv` (400행), `0811/*.xlsx` (상세 컬럼 8종)

| error_kind | 건수 | Athena 메시지 |
|---|---:|---|
| guard | 332 | `ValueError: SELECT/WITH로 시작하지 않는 SQL` |
| column_not_found | 57 | `COLUMN_NOT_FOUND` |
| syntax | 10 | `mismatched input ...` |
| group_by | 1 | `EXPRESSION_NOT_AGGREGATE` |

난이도별로는 hard 216 / medium 125 / easy 59, 도메인별로는
customer_card_portfolio 98 / credit_risk 93 / corporate_sales_targeting 93 /
card_usage 70 / merchant_sales 46 이다.

## 먼저 확인한 것: 스키마 누락이 아니다

`expected_sql` 이 참조하는 모든 컬럼을 semantic layer 와 대조했다. **미등록 컬럼은 0개**다.
즉 정답 SQL이 쓰는 컬럼은 모두 정의돼 있었고, 문제는 그 컬럼이 프롬프트까지 전달되지
않은 것이었다.

---

## 원인 1 — 계약이 모델에게 되묻도록 지시했다 (guard 332건)

`semantic_layer.yaml` 의 `sql_generation_contract.ambiguity_rules[0]`:

> 필수 기간·대상·코드값을 확정할 수 없으면 임의 값을 만들지 않고 **추가 입력을 요청한다.**

이 계약 전체가 `build_semantic_contract_summary()` 를 통해 매 프롬프트에 렌더된다
(`text2sql_agent/schema.py:988`). 프롬프트 하단의 SQL 작성 규칙 16번은 "순수 SQL만
반환"이라 서로 충돌하는데, 모델은 되묻는 쪽을 골랐다. `ambiguity_rules` 에는
"입력을 요청한다"가 2곳, "직접 입력하도록 요청한다"가 1곳 더 있었다.

실제 응답 (cs-golden-v2-0044, `2026년 7월 기준 가맹점 상태구분별 가맹점 수를 알려줘`):

> 가맹점의 **상태구분**을 나타내는 컬럼명을 알려주시면, 해당 컬럼별로 2026-07 기준
> 가맹점 수를 집계하는 SQL을 제공해 드리겠습니다.

이 응답이 실행 단계의 읽기 전용 가드(`scripts/run_goldenset_answers.py:189`)에 걸려
`ValueError` 가 된다. guard 332건 중 221건에 "알려주시면/알려주세요" 류 표현이 있었다.

**대응** — `ambiguity_rules` 를 삭제하고 두 섹션으로 나눴다.
- `output_contract` (7개): 항상 SQL만, 컬럼을 되묻지 않음, 접미사 변형으로 재대조,
  못 찾으면 가장 가까운 컬럼으로 SQL 완성
- `resolution_defaults` (12개): 되묻는 대신 적용할 기본값 해석

`output_contract` 를 dict 맨 앞으로 옮겨 프롬프트 최상단에 렌더되게 했다.

## 원인 2 — 질문 표면형이 컬럼과 연결되지 않았다 (guard·column_not_found)

프롬프트에 넣을 컬럼은 `_table_details()` 가 점수로 고른다. 질문 구문이 컬럼명이나
synonyms 와 맞으면 +70점을 받는다(`text2sql_agent/workflow.py:2995`). 매칭에 실패하면
점수를 못 받고, 테이블당 예산(테이블 4개 선택 시 12개 컬럼)에서 잘려나간다.
프롬프트에 없는 컬럼은 모델에게 존재하지 않는 컬럼이다.

세 가지가 겹쳤다.

**(a) 같은 컬럼인데 테이블마다 동의어가 달랐다.**

| 테이블 | 컬럼 | v1 synonyms |
|---|---|---|
| tmdaa5e11 | 가맹점상태구분코드 | `['가맹점상태', '가맹점 상태']` |
| tmdaaus01 | 가맹점상태구분코드 | 없음 |
| tbdaadt01 | 가맹점상태구분코드 | 없음 |
| tmdaa5d01 | 가맹점상태구분코드 | 없음 |

정답 SQL이 쓰는 `tmdaaus01` 쪽에 동의어가 없어서 매칭이 실패했다. 같은 현상이
상품중분류구분코드(9개 테이블 중 1개만 보유), 카드등급구분코드 등에서 반복됐다.

**(b) 동의어가 있어도 질문 표면형과 어긋났다.**

`'가맹점상태'` 는 `"가맹점 상태구분별"` 을 잡지 못한다. 매칭 후 위치에 오는 `'구'` 가
어절 경계가 아니기 때문이다. 필요한 형태는 `'가맹점상태구분'` 이었다.

**(c) v1 매처의 두 가지 한계.**

`_phrase_in_text()` (`text2sql_agent/schema.py:367`)는
- phrase 에 적힌 토큰 사이에서만 구분자를 허용한다 →
  `'기업카드신용한도금액'` 이 `"기업카드 신용한도금액"` 을 놓친다
- 조사를 한 개만 허용한다 → `"회원자격별로"`(`별`+`로`)를 놓친다.
  실패 조합의 상당수가 이 한 글자였다.

**대응**
- 같은 이름 컬럼의 동의어를 합쳤다 (107건 전파)
- 컬럼명에서 표면형을 유도해 추가했다 (3,243건). 접미사 사슬
  `구분코드 → 구분 → 어간`, `여부 → 서술어`, 띄어쓰기 변형, 분류 단위 생략형
- 구분자·중첩 조사에 둔감한 매처를 만들었다 (`column_synonyms.phrase_in_text`)

접미사를 벗겨 `'카드'`·`'가맹점'` 같은 흔한 명사만 남으면 버린다. 안 그러면 모든 카드
관련 컬럼이 `'카드'` 하나로 뭉쳐 오검출이 된다.

### 측정 1 — 실제 프롬프트에 컬럼이 들어가는가

가장 중요한 지표다. 회귀 픽스처 271건을 실제 파이프라인
(`_rule_rank_tables()` → `_table_details()`)에 통과시켜 정답 SQL이 쓰는 컬럼이
프롬프트에 실제로 들어가는지 셌다.

| 구성 | 프롬프트 도달 |
|---|---:|
| v1 스키마 + v1 매처 | 59/271 (22%) |
| v2 스키마 + v1 매처 (1단계) | 166/271 (61%) |
| **v2 스키마 + v2 매처 (2단계)** | **190/271 (70%)** |

1단계가 +39%p, 2단계가 +9%p를 가져간다. 1단계만으로도 큰 폭으로 개선되지만
아래 대표 사례처럼 2단계가 필요한 질의가 남는다.

### 측정 2 — 매처가 구문을 잡는가

매처 단독 성능(테이블 선택·컬럼 예산 영향 제외). 위 지표가 실제 결과이고
이건 그 원인 지표다.

| 구성 | 매칭 실패 | 감소 |
|---|---:|---:|
| v1 스키마 + v1 매처 | 481 | — |
| v2 스키마 + v1 매처 | 132 | 73% |
| v2 스키마 + v2 매처 | 46 | 90% |

### 동의어만 늘리는 것으로는 부족하다

컬럼을 고르는 `_table_details()` 가 v1 매처를 쓰기 때문에, v1 매처가 못 잡는
표면형은 동의어를 아무리 넣어도 잘려나간다. 대표 사례
(`2026년 7월 기준 가맹점 상태구분별 가맹점 수를 알려줘`):

| 구성 | `가맹점상태구분코드` 프롬프트 포함 | 선택 테이블 |
|---|---|---|
| v1 스키마 + v1 매처 | 안 됨 | `tmdaa1d12`, `tbdaadt01` |
| v2 스키마 + v1 매처 | **안 됨** | `tmdaa1d12`, `tbdaadt01` |
| v2 스키마 + v2 매처 | 됨 (코드값 포함) | `tbdaadt01`, `tmdaa1d12`, `tbdaaus01`, `tmdaa5d01` |

v1 매처는 동의어에 적힌 **토큰 사이**에서만 공백을 허용한다. `'가맹점상태구분'` 은
질문의 `"가맹점 상태구분별"`(가맹점·상태 사이 공백)을 잡지 못하고, 띄어쓰기 변형을
넣어도 조합이 어긋나면 또 빗나간다. v2 매처는 글자 사이마다 구분자를 허용해 조합
문제를 없앤다. 매처 교체 후 테이블 선택도 정답 테이블 쪽으로 개선됐다.

### 남은 30%는 컬럼이 아니라 테이블 선택 문제다

2단계에서도 81/271이 프롬프트에 못 들어간다. 최다 사례를 추적한 결과 원인은
컬럼 예산이 아니라 **테이블 선택**이었다.

```
질문: 현재 기준 상품중분류별 평균 기업카드 신용한도금액 상위 15개를 알려줘
정답 테이블: tbdaaat05 (기업카드신용한도금액·상품중분류구분코드 보유)
선택 테이블: ['tbdaa1d12']          ← tbdaaat05 가 아예 선택되지 않았다
컬럼 예산 48 → 96 으로 올려도: 여전히 미포함 (테이블이 없으니 무관)
```

`_rule_rank_tables()` 는 점수 3점 이상인 테이블만 최대 4개 고른다. 정답 테이블이
canonical_metrics·semantic_attributes·query_references 어디에도 걸리지 않으면
컬럼 동의어가 아무리 좋아도 후보에 오르지 못한다. 이번 작업은 **선택된 테이블 안에서
컬럼이 살아남는 문제**를 고쳤고, 테이블 랭킹 자체는 별개 과제로 남는다.

남은 미도달 상위: `tbdaaat05.기업카드신용한도금액`(11),
`tbdaaat03.할부요율그룹구분코드`(4), `tbdaadb17.업종대분류코드명`(4),
`tbdaaat03.회원자격코드`(3).

## 원인 3 — 참고 SQL 한 건이 22건의 오류를 만들었다 (column_not_found)

`sql_verified_queries.yaml` 의 SQL은 프롬프트의 "참고 SQL 예시"로 들어간다.
`special_debt_by_asset_quality` 는

```sql
FROM card_system.tmdaaus01      -- 가맹점 월말요약
WHERE 개인기업구분코드 = '2'
```

인데 SELECT 하는 `자산건전성분류구분코드`, `특수채권편입원금`, `연체회차`,
`특수채권관리번호` 는 전부 `tbmaisd06`(특수채권) 컬럼이다. `tmdaaus01` 에는 그중 어느
것도 없다. 모델은 이 예시를 **글자 그대로 베껴** 4개 질문에 동일한 SQL을 냈고,
`COLUMN_NOT_FOUND: '개인기업구분코드'` 22건이 됐다.

`merchant_risk_combined_customer_month` 도 `tbmaisd06` 에서 `가맹점번호`,
`기업고객식별자`, `전월일시불매입금액` 을 읽고 있어 같은 종류의 오류다.

**대응** — 두 쿼리의 원천 테이블을 고치고, 66개 쿼리 전체를 semantic layer 로 검증하는
감사기(`verified_query_audit.py`)를 만들어 테스트에 넣었다.

## 원인 4 — 방언 오류도 참고 SQL을 베낀 결과였다 (syntax 10 + 집계 1)

| 유형 | 건수 | 내용 |
|---|---:|---|
| `QUALIFY` | 4 | Snowflake/Teradata 문법. Athena에 없음 |
| WHERE 안의 `ROW_NUMBER()` | 3 | `EXPRESSION_NOT_SCALAR` |
| `expr::FLOAT` | 2 | PostgreSQL 캐스트 |
| 출력 잘림 | 3 | `mismatched input '<EOF>'` |
| WHERE 없이 붙은 `AND` | 1 | `mismatched input 'AND'` |
| CROSS JOIN 스칼라 미집계 | 1 | `EXPRESSION_NOT_AGGREGATE` |

`::FLOAT` 는 참고 SQL에 8곳 있었고, 실패한 agent SQL과 문자열이 일치한다.

```
참고 SQL 2345행 : ROUND(SUM(금월이용합계금액)::FLOAT / NULLIF(SUM(금월이용합계건수), 0), 0)
cs-golden-v2-0486: ROUND(SUM("금월이용합계금액")::FLOAT / NULLIF(SUM("금월이용합계건수"), 0), 0)
```

출력 잘림 3건은 `generate_sql()` 의 `max_tokens=2048` 이 원인이다
(`text2sql_agent/workflow.py:3353`).

**대응**
- 참고 SQL의 `::TYPE` 8곳을 `CAST(... AS ...)` 로 교정. `FLOAT` 은 Trino 타입이 아니라
  실수 나눗셈 의도에 맞는 `DOUBLE` 로 바꿨다
- `athena_rules` 에 QUALIFY·윈도함수 WHERE·CROSS JOIN 스칼라 집계 규칙 추가
- 실행 전 자동 교정(`normalize_sql`)과 정적 감사(`audit_sql`) 제공

## 부수 발견 — 프롬프트 규칙이 조용히 잘리고 있었다

`build_semantic_contract_summary()` 는 리스트를 앞 12개까지만 렌더한다
(`for item in value[:12]`, `text2sql_agent/schema.py:1003`). v1 의
`grain_and_aggregation_rules` 는 13개여서 마지막 규칙("전월 대비 증감률은 요청 시작월보다
한 달 앞선 월까지 읽어 LAG를 계산")이 **프롬프트에 들어간 적이 없다.**

규칙을 12개로 압축해 전부 전달되게 하고, 12개를 넘기면 빌드가 실패하도록 검사를 넣었다.

## 수치 요약

| 원인 | 관련 오류 | 대응 위치 |
|---|---:|---|
| 되묻기 지시 | guard 332 | semantic layer 계약 |
| 동의어 단절·표면형 불일치 | guard·column_not_found 다수 | semantic layer 컬럼 + 매처 |
| 참고 SQL 원천 테이블 오류 | column_not_found 22 | 참고 SQL |
| `::FLOAT` 예시 전파 | syntax 2 | 참고 SQL |
| QUALIFY / 윈도함수 WHERE | syntax 7 | 계약 규칙 + 정적 감사 |
| `max_tokens` 부족 | syntax 3 | 코드 설정 (2단계) |
| 렌더 한도 초과 | 규칙 1개 유실 | 계약 리스트 압축 |

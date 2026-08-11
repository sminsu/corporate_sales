# 오답 정리 (`scripts/report_goldenset_failures.py`)

채점 결과에서 **`sql_error`** 와 **`mismatch`** 만 뽑아 원인별로 정리하는 스크립트다.
`run_goldenset_eval.py` 가 "몇 점인가"를 알려준다면, 이쪽은 "왜 틀렸나"를 알려준다.

- `sql_error` — **난이도별**로 묶어 질의·도메인·오류 유형·오류 원문을 정리한다(CSV에는 정답 SQL도 함께).
- `mismatch` — **무엇이 어긋났는지**(컬럼 구성 / 행 수 / 단일 수치 / 행 값 / 참조 테이블)를 판정하고,
  값이 다르면 어느 컬럼의 어느 행이 얼마나 다른지까지 짚는다. 정답 SQL과 후보 SQL도 함께 싣는다.

```bash
python scripts/run_goldenset_eval.py         # (선행) 채점
python scripts/report_goldenset_failures.py  # 오답 정리
```

## 실행

```bash
# 0) 파일 없이 내부 로직만 점검
python scripts/report_goldenset_failures.py --self-test

# 1) 기본 경로로 정리
python scripts/report_goldenset_failures.py

# 2) 어려운 케이스만, 값 차이 샘플을 더 많이
python scripts/report_goldenset_failures.py --difficulty hard --max-samples 10

# 3) 특정 도메인만
python scripts/report_goldenset_failures.py --domain merchant_sales

# 4) near·no_sql 까지 포함
python scripts/report_goldenset_failures.py --verdicts sql_error mismatch near no_sql

# 5) 화면에도 바로 출력
python scripts/report_goldenset_failures.py --print
```

행 값 비교에는 골든셋의 정답 행이 필요해 `--cases` 를 함께 읽는다(기본값은 채점 때와 같은
`*_answered.jsonl`). 골든셋을 못 찾으면 값 비교만 생략하고 나머지는 그대로 정리한다.

같은 `id` 가 여러 번 있으면(중단 후 재개하면 그렇게 된다) 마지막 채점만 본다.

## 산출물

| 경로 | 내용 |
|---|---|
| `reports/goldenset-v2-failures.md` | 사람이 읽는 정리본. 난이도별 SQL 오류 + mismatch 케이스별 상세 |
| `reports/goldenset-v2-failures.json` | 같은 내용의 기계 판독용(집계 + 전체 케이스) |
| `reports/goldenset-v2-sql-errors.csv` | 난이도 / id / 도메인 / 질의 / 오류 유형 / 오류 원문 / 정답 SQL / 후보 SQL |
| `reports/goldenset-v2-mismatches.csv` | id / 대표 원인 / 기대·실제 컬럼·행수·값 / 누락 테이블 / 어긋난 컬럼 / 샘플 / 정답 SQL / 후보 SQL |

마크다운은 `--max-cases`(기본 60)까지만 상세를 싣고, CSV·JSON에는 전부 담는다.

## 정답 SQL과 후보 SQL을 나란히

mismatch 상세에는 골든셋의 **정답 SQL** 과 에이전트의 **후보 SQL** 을 접이식 블록으로 함께 싣는다.
"값이 왜 다른가"는 대개 두 SQL을 나란히 놓는 순간 드러난다.

```
- 값: 기대 1000 / 실제 1250 (+25.00%)

<details><summary>정답 SQL</summary>   ← 고객별 최신 행만(ROW_NUMBER, rn = 1)
<details><summary>후보 SQL</summary>   ← 기간 내 모든 행을 셈
```

정답 SQL이 길어 리포트가 안 읽히면 `--sql-chars` 로 자른다(기본 1500자, `0` 이면 전체).
CSV·JSON에는 자르지 않고 담으므로 전문이 필요하면 그쪽을 본다.

```bash
python scripts/report_goldenset_failures.py --sql-chars 0     # 마크다운에도 전문
```

정답 SQL은 골든셋(`--cases`)에서 가져오므로, 골든셋을 못 찾으면 이 블록만 비고 나머지 정리는 그대로 된다.

## SQL 오류 분류

오류 원문에서 유형과 대상 이름을 뽑아 분류한다.

| 유형 | 뜻 | 대표 문구 |
|---|---|---|
| `column_not_found` | 컬럼 없음 | `Column '가맹점명' cannot be resolved` |
| `table_not_found` | 테이블/스키마 없음 | `Table '...' does not exist` |
| `function_not_found` | 함수 없음 | `Function '...' not registered` |
| `ambiguous` | 이름 모호 | `'...' is ambiguous` |
| `type_mismatch` | 타입 불일치 | `Cannot apply operator: varchar = bigint` |
| `group_by` | 집계/GROUP BY 오류 | `must be an aggregate expression or appear in GROUP BY` |
| `division_by_zero` | 0으로 나눔 | `Division by zero` |
| `syntax` | 구문 오류 | `mismatched input 'FROM'` |
| `permission` | 권한 거부 | `AccessDenied`, `not authorized` |
| `throttle` / `timeout` / `resource` | 쓰로틀링 / 타임아웃 / 리소스 초과 | `TooManyRequests`, `TIMED_OUT`, `exhausted resources` |
| `hive_data` | 원천 데이터·파티션 오류 | `HIVE_BAD_DATA` 등 |
| `guard` | 우리 쪽 읽기 전용 가드가 차단 | `읽기 전용이 아닌 키워드 포함` |
| `network` | 접속 실패 | `EndpointConnectionError` |
| `other` | 위에 안 걸림 | — |

`column_not_found` / `table_not_found` / `function_not_found` 는 못 찾은 **이름까지 뽑아** 모으므로,
"어떤 컬럼을 반복해서 헛짚는가"가 바로 보인다. 스키마 프롬프트나 시맨틱 레이어에서 손볼 지점이다.

오류 문구에서 줄 위치·따옴표 리터럴·숫자를 지운 **시그니처**로도 묶어 준다. 문구가 조금씩 달라도
같은 원인이면 한 줄로 모인다.

## mismatch 원인

한 케이스에 원인이 여러 개 겹칠 수 있어 전부 기록하고, 그중 하나를 대표 원인으로 뽑는다.
대표 원인의 우선순위는 다음 순서다(결과의 '모양' 문제가 먼저, 표기 문제는 뒤).

| 원인 | 뜻 | 대체로 무엇을 의심하나 |
|---|---|---|
| `column_count` | 컬럼 수가 다름 | SELECT 목록 구성, 불필요한 컬럼 추가/누락 |
| `scalar_value` | 단일 수치 값이 다름 | 집계 대상·필터 조건 |
| `row_count` | 행 수가 다름 | 필터, 조인 중복, GROUP BY 단위 |
| `row_values` | 행은 맞는데 값이 다름 | 금액 컬럼 선택, 합계 기준, 기간 |
| `row_membership` | 포함된 행 자체가 다름 | 필터 조건, 정렬 후 상위 N |
| `column_names` | 컬럼 이름만 다름 | 표기 문제. 단독이면 사실상 정답에 가깝다 |
| `tables` | 참조 테이블이 다름 | 테이블 선택·조인 경로 |

값 차이는 컬럼 단위로 짚는다. 앞쪽 분류축 컬럼으로 행을 맞춘 뒤(왼쪽부터 넓혀가며 양쪽에서
유일해지는 최소 조합을 키로 쓴다), 컬럼별로 몇 행이 어긋났는지와 실제 값 쌍을 보여준다.
수치면 증감률도 함께 낸다.

```
- 컬럼 `월이용금액`: 1/3행 불일치 (키 `기준년월`)
  - 202603: 기대 `3000` / 실제 `2500` (-16.67%)
```

키가 될 컬럼이 없으면(측정값까지 다 써야 유일해지는 경우) 행 수가 같을 때만 같은 자리끼리 비교하고,
그렇지 않으면 "정답에만 있는 행 / 후보에만 있는 행"으로만 정리한다.

컬럼 구성 자체가 다르면 행끼리 견줄 기준이 없으므로 값 비교를 하지 않는다. 컬럼 문제를 먼저 풀어야 한다.
컬럼 **순서만** 다른 경우는 정답 순서에 맞춰 재배열한 뒤 값을 비교하므로, 순서 때문에 값이 전부
어긋난 것처럼 보이지 않는다.

## 값 비교를 건너뛰는 경우

정답과 후보는 각각 최대 `--answer-rows`(기본 50) 행까지만 저장된다. 저장된 행이 전체가 아니거나
행수 상한(`--fetch-rows`)에 걸린 케이스는 값 비교를 건너뛰고 그 사실을 표시한다.

```
- 행 값 비교: 저장된 행이 전체가 아니라 값 비교를 생략했다 (정답 5000행 중 50행 저장 / 후보 5000행 중 50행 저장)
```

목록형 케이스까지 값 단위로 보고 싶으면 정답을 만들 때와 채점할 때 모두 `--row-limit 200` 처럼
같은 상한을 걸어 행 수를 줄여 두는 편이 좋다.

## 주의

- 이 스크립트는 채점 결과만 읽고 SQL을 실행하지 않는다. Athena 접속도 자격증명도 필요 없다.
- `agent_error` / `no_sql` 은 기본 대상이 아니다. 필요하면 `--verdicts` 에 추가하면 되고,
  `no_sql` 은 오류 문구 대신 `param_stage` 같은 에이전트 상태를 오류 칸에 채운다.
- 대표 원인은 진단의 출발점이지 결론이 아니다. `row_count` 와 `tables` 가 같이 잡혔다면
  테이블을 잘못 골라 행 수가 틀어진 것일 수 있으므로 원인 목록 전체를 함께 봐야 한다.

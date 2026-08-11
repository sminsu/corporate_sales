# 골든셋 채점 (`scripts/run_goldenset_eval.py`)

골든셋의 **정답 SQL·정답 결과**와, 같은 질의를 이 프로젝트의 에이전트에 태워 나온
**후보 SQL·후보 결과**를 비교해 채점하는 러너다.

SQL 문자열이 정답과 달라도 실행 결과가 같으면 정답으로 친다(execution accuracy).
같은 답을 만드는 SQL은 여러 가지이므로, 문자열 일치로 채점하면 멀쩡한 SQL이 오답이 된다.

```
질의 ──▶ 에이전트 ──▶ 후보 SQL ──▶ 실행 ──▶ 후보 결과 ┐
                                                      ├─▶ 값 비교 ─▶ 판정
골든셋 ─────────────▶ 정답 SQL ──▶ 실행 ──▶ 정답 결과 ┘   (run_goldenset_answers.py가 미리 채움)
```

## 전제

먼저 [`run_goldenset_answers.py`](goldenset-answer-runner.md)로 정답 결과를 채워야 한다.
정답 결과(`expected_answer`)가 없는 케이스는 채점 대상에서 빠진다(`no_gold`).

```bash
python scripts/run_goldenset_answers.py            # 정답 결과 채우기
python scripts/run_goldenset_eval.py               # 채점
```

정답·후보의 값 정규화와 해시는 `run_goldenset_answers.py` 의 함수를 그대로 재사용하므로
양쪽이 완전히 같은 기준(NULL 표기·Decimal·실수 반올림 자릿수·날짜 ISO)으로 비교된다.
반올림 자릿수는 정답에 기록된 `float_digits` 를 따라간다.

## 실행

```bash
# 0) 에이전트·Athena 없이 채점 로직만 점검
python scripts/run_goldenset_eval.py --self-test

# 1) 무엇이 채점될지만 확인
python scripts/run_goldenset_eval.py --dry-run --limit 20

# 2) 앞의 20건만 시험 채점
python scripts/run_goldenset_eval.py --limit 20

# 3) 저장된 결과는 건너뛰고 나머지 전체 채점 (나눠 돌려도 됨)
python scripts/run_goldenset_eval.py

# 4) 실패 케이스만 다시 채점
python scripts/run_goldenset_eval.py --retry-errors

# 5) 채점 없이 리포트만 다시 생성
python scripts/run_goldenset_eval.py --report-only
```

케이스마다 결과를 즉시 flush·fsync 하므로 중단해도 안전하고, 같은 명령을 다시 실행하면
채점된 케이스를 건너뛰고 남은 건부터 이어간다.

## 판정 기준

| 판정 | 뜻 | 정답 처리 |
|---|---|:---:|
| `exact` | 행 순서까지 완전히 같음 | O |
| `match` | 행 집합이 같음(`ORDER BY` 동점 구간 순서 차이 허용) | O |
| `value_match` | 컬럼 순서·이름만 다르고 값 집합은 같음 | O |
| `near` | 스칼라가 허용오차 안이거나, 행수·컬럼수만 같음 | X |
| `mismatch` | 실행은 됐지만 결과가 다름 | X |
| `sql_error` | 후보 SQL 실행 실패 | X |
| `no_sql` | 에이전트가 SQL을 만들지 못함(툴 응답·거절·파라미터 미해결 등) | X |
| `agent_error` | 에이전트 호출 자체가 실패 | X |
| `no_gold` | 정답 결과가 없어 채점 불가 | 집계 제외 |

`near`는 정답으로 세지 않지만 따로 보이게 남긴다. "숫자는 맞는데 컬럼 구성이 다르다",
"행수만 우연히 같다"를 완전한 오답과 구분해 원인을 빨리 좁히기 위한 구간이다.

`sql_error` 와 `mismatch` 를 원인별로 파고들려면 [오답 정리 스크립트](goldenset-failure-report.md)를 이어서 돌린다.

```bash
python scripts/report_goldenset_failures.py
```

집계 지표는 다음과 같다.

- **실행 정확도**: `exact + match + value_match` / 채점 대상 — 기본 성능 지표다.
- **엄격 정확도**: `exact` / 채점 대상 — 정렬까지 맞아야 하는 순위형 질의를 볼 때 쓴다.
- **SQL 생성률**: 에이전트가 SQL을 만든 비율. `no_sql` 이 많으면 채점 이전에 라우팅·파라미터 문제다.
- **생성 SQL 실행 성공률**: 만든 SQL 중 실제로 돌아간 비율.
- **테이블 재현율**: 골든셋 `expected_tables` 중 후보 SQL이 실제로 참조한 비율. CTE 별칭은 세지 않는다.

## 산출물

| 경로 | 내용 |
|---|---|
| `reports/goldenset-v2-eval-results.jsonl` | 케이스별 채점 이력(원본). 재개 판단의 기준 |
| `reports/goldenset-v2-eval-summary.md` | 지표·판정 분포·도메인/질의형태/난이도별 표·오답 목록 |
| `reports/goldenset-v2-eval.json` | 같은 내용의 기계 판독용 집계 |
| `reports/goldenset-v2-eval-review.csv` | 현업 검수용. 질의 / 판정 / 기대·실제 값 / 누락 테이블 / 후보 SQL |

케이스별 레코드에는 판정 근거가 함께 남는다.

```json
"verdict": "mismatch",
"correct": false,
"detail": {
  "result_match": false,
  "ordered_match": false,
  "value_match": false,
  "row_count_match": true,
  "column_count_match": false,
  "column_name_overlap": 0.5,
  "scalar_match": false,
  "expected_row_count": 7, "actual_row_count": 7,
  "expected_scalar": null, "actual_scalar": null,
  "tables": {"expected": ["tbdaa1d12"], "actual": ["tbdaa1d12", "tbdaadb17"],
             "recall": 1.0, "precision": 0.5, "missing": [], "extra": ["tbdaadb17"]},
  "sql_token_overlap": 0.62,
  "notes": []
}
```

`notes`에 `row_limit_truncated`가 있으면 정답·후보 중 한쪽이 행수 상한에 걸렸다는 뜻이고,
그 경우 해시 비교는 상한 이후 행을 반영하지 못한다. 목록형 케이스는 정답을 만들 때와
채점할 때 **같은 `--row-limit`** 으로 돌려야 비교가 성립한다.

## 후보 SQL을 어디서 가져올지

| 옵션 | 동작 |
|---|---|
| 기본 (`--predictor agent`) | 서비스와 같은 LangGraph 그래프를 태워 `final_sql`(없으면 `generated_sql`)을 얻는다 |
| `--predictions <jsonl>` | 이미 뽑아둔 SQL을 읽어 채점만 한다. 에이전트를 돌리지 않는다 |
| `--predictor stub` | 에이전트 없이 파이프라인만 점검하는 더미 |

에이전트가 파라미터를 더 물으면(`param_stage == "need_params"`) `run_semantic_golden_live.py` 의
기본값 채우기를 재사용해 사람 입력 없이 같은 케이스를 이어서 진행한다(`--max-param-rounds`,
기본 3회). 적용한 기본값은 결과의 `agent.meta.default_params_applied` 에 남는다.

`--predictions` 파일은 한 줄에 한 케이스이고, `id` 와 SQL만 있으면 된다.

```json
{"id": "cs-golden-v2-0001", "sql": "SELECT ..."}
{"id": "cs-golden-v2-0002", "sql": "SELECT ...", "columns": ["c"], "rows": [[123]]}
```

`columns`/`rows` 까지 있고 `--reuse-agent-rows` 를 주면 후보 SQL을 다시 실행하지 않고
그 결과로 바로 채점한다. 빠르고 스캔 비용이 들지 않지만, 에이전트가 표시용으로 잘라둔
결과라면 행수가 달라 오답으로 잡히므로 상한을 확인하고 쓰는 편이 좋다. 기본값은 재실행이다.

## 실행량·비용 조절

후보 SQL도 Athena에서 실행되므로 정답을 채울 때와 같은 방식으로 범위를 나눈다.

```bash
python scripts/run_goldenset_eval.py --domain merchant_sales --limit 50
python scripts/run_goldenset_eval.py --shape business_case
python scripts/run_goldenset_eval.py --difficulty hard
python scripts/run_goldenset_eval.py --ids cs-golden-v2-0007 cs-golden-v2-0042
python scripts/run_goldenset_eval.py --max-scanned-gb 20      # 누적 스캔량 도달 시 중단
python scripts/run_goldenset_eval.py --stop-after-errors 5    # 연속 실패 시 중단
```

`--executor`, `--schema`, `--fetch-rows`, `--row-limit`, `--timeout-ms` 는 정답 러너와 같은 뜻이며
같은 값을 쓰는 것이 원칙이다. 정답과 후보를 다른 상한으로 실행하면 비교가 성립하지 않는다.

## CI에서 쓰기

```bash
python scripts/run_goldenset_eval.py --domain corporate_sales_targeting --fail-under 0.8
```

실행 정확도가 기준 미만이면 종료 코드 1을 반환한다. `--report-only` 로 리포트만 다시 그릴 때도
같은 기준이 적용되므로, 채점과 판정을 분리해서 돌릴 수 있다.

## 주의

- 정답 결과는 실행 시점 데이터에 종속된다. 원천이 바뀌었는데 채점 결과가 나빠졌다면
  `run_goldenset_answers.py --verify` 로 "데이터가 바뀐 것"과 "에이전트가 틀린 것"을 먼저 갈라야 한다.
- 정답이 0행(`empty`)인 케이스는 후보도 0행이면 정답 처리된다. 브랜드명·기업명 리터럴이
  실제 데이터와 맞지 않아 양쪽 다 0행이 나오는 경우가 있으므로, `empty` 비중이 높은 도메인은
  정답 자체를 먼저 손보는 편이 좋다.
- `no_gold`는 오답이 아니라 채점 불가다. 지표 분모에 들어가지 않으며 리포트에 건수로만 남는다.

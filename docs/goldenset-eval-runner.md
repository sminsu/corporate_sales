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
| `subset_match` | 정답 컬럼의 값을 그대로 담고 있고, 후보가 컬럼을 더 냈을 뿐 | O |
| `required_match` | 질의가 요구한 컬럼은 다 맞고, 정답에만 있던 부가 컬럼이 빠졌을 뿐 | O |
| `scalar_near` | 단일 수치가 허용오차 안에서 같음(반올림 차이) | O |
| `rollup_match` | 측정값 총합은 같은데 집계 축(행 단위)이 다름 | X |
| `shape_only` | 행수·컬럼수만 같고 값은 다름 | X |
| `mismatch` | 실행은 됐지만 결과가 다름 | X |
| `sql_error` | 후보 SQL 실행 실패 | X |
| `no_sql` | 에이전트가 SQL을 만들지 못함(툴 응답·거절·파라미터 미해결 등) | X |
| `agent_error` | 에이전트 호출 자체가 실패 | X |
| `no_gold` | 정답 결과가 없어 채점 불가 | 집계 제외 |

`subset_match`는 SELECT 목록만 다른 경우를 구제하는 판정이다. 예를 들어 정답이
`SELECT sum(매출)` 인데 후보가 `SELECT 가맹점명, sum(매출), 비중` 을 냈다면, 정답 컬럼의 값이
행 단위로 그대로 들어 있으므로 정답으로 친다. 판정 조건은 다음과 같다.

- 행 수가 같고, 정답의 각 컬럼에 대응하는 후보 컬럼이 **1:1로** 존재한다(한 컬럼 재사용 불가).
- 그 컬럼들만 뽑아낸 후보 결과의 **행 집합**이 정답 행 집합과 같다. 컬럼별 값 집합만 우연히
  같고 행 조합이 뒤바뀐 결과(`A-20, B-10` vs `A-10, B-20`)는 걸러진다.
- 값 대조는 저장된 행으로 하므로 정답·후보 중 한쪽이라도 행이 잘려 있으면 판정을 보류하고
  `notes` 에 `subset_check_skipped` 를 남긴다(`--answer-rows` 상한, `row_count_truncated`).
  행이 많은 목록형 케이스까지 이 완화를 적용하려면 정답 러너와 채점 러너를 **같은
  `--answer-rows`** 로 넉넉히 잡아 돌려야 한다(기본 50행).
- 날짜는 표기 차이를 흡수한다(`2026-07-01` = `20260701` = `2026-07-01 00:00:00`, `2026-07` = `202607`).
  해시 비교는 그대로 엄격하고, 이 관대한 컬럼 대조에서만 적용된다.

`required_match`는 반대쪽, 즉 **정답 SQL이 질문에 없는 부가 컬럼까지 낸 경우**를 구제한다.
예를 들어 `2026년 7월 법인카드 일별 매출금액을 보여줘`(cs-golden-v2-0074)의 정답은
`전표매출년월일 / 일매출금액 / 매출건수` 를 내지만, 질문이 요구한 것은 날짜와 매출금액이다.
후보가 `기준년월일 / 일별매출금액` 만 냈다면 `매출건수` 가 없다는 이유로 깎지 않는다.

무엇이 "요구된 컬럼"인지는 이렇게 정한다.

- **질의 어휘에 걸리는 컬럼**: 질문 + `category` + `metrics` 를 붙여 쓴 문자열과 컬럼명을
  3-gram 겹침으로 대조한다(한글 컬럼명은 `일매출금액` 처럼 붙여 쓰므로 공백 토큰이 안 맞는다).
  위 예에서 `일매출금액` 은 질문의 `매출금액` 에 걸리고 `매출건수` 는 걸리지 않는다.
- **정답 SQL의 `GROUP BY` 키**: 답의 축이라 질문에 이름이 안 나와도 필수로 본다.
  `전표매출년월일` 이 여기 해당한다(후보의 `기준년월일` 과 이름은 달라도 값으로 대응된다).
- 다만 `GROUP BY` 키만 걸리고 **질의 어휘에 걸린 컬럼이 하나도 없으면** 관대한 판정을 쓰지 않는다.
  축만 맞고 측정값을 통째로 빠뜨린 후보가 통과해 버리기 때문이다.
- 요구 컬럼을 하나라도 빠뜨리면 정답이 아니다(`detail.column_subset` 에
  `gold_contains_candidate`, `notes` 에 `candidate_columns_missing` 이 남는다).

판정에 쓰인 요구 컬럼은 `detail.required_columns` 에, 후보에서 대응된 컬럼은
`detail.subset_columns` 에 남으므로 관대한 판정이 타당했는지 사후에 확인할 수 있다.
골든셋 v2 기준으로 이 완화가 열리는 케이스는 약 200건(컬럼 2개 이상 케이스의 22%)이고,
나머지는 정답 컬럼을 모두 맞춰야 한다.

`rollup_match`는 **집계 축이 다른 경우**를 갈라 두는 판정이다. 정답이 총합 1행인데 후보가
일별 31행을 내고 그 합이 같으면 여기 걸린다. 필터·기간은 맞는데 그룹핑 단위가 다르다는 신호다.

- 행 수가 **다를 때만** 본다. 행 수가 같은데 값이 어긋난 건 집계 축 문제가 아니므로 `shape_only`/`mismatch` 로 남는다.
- 합계는 더해서 의미가 있는 수치 컬럼만 낸다. `20260701`·`202607` 같은 연월일 표기는 축으로 보고 뺀다.
- 총합은 `detail.expected_totals` / `detail.actual_totals` 에 남는다.
- **정답으로 세지 않는다.** "일별로 보여줘"에 총합만 낸 답은 질의에 맞는 답이 아니기 때문이다.
  다만 판정 분포에 건수가 따로 잡히므로, 실제 케이스를 보고 골든셋 쪽 집계 단위를 손볼지
  에이전트를 고칠지 판단하면 된다.

SQL 문자열이나 WHERE 절이 비슷하다는 이유로 정답 처리하지는 않는다. 골든셋에서 재보면
조회월만 7월→6월로 바꾼 오답도 SQL 토큰 겹침이 0.85 이고, `현재 기준` 스냅샷 계열은 WHERE 가
`기준년월일 = (SELECT max ...)` 상용구뿐이라 **서로 다른 질문끼리 WHERE 겹침이 1.0** 인 쌍이
같은 도메인 안에서 6%나 된다. 오답을 걸러낼 만큼 문턱을 올리면 멀쩡한 패러프레이즈가 죽고,
패러프레이즈를 살리면 기간이 틀린 쿼리가 통과한다. 그래서 완화는 전부 **값 기준**으로만 한다.

`scalar_near`는 1행 1열끼리 상대오차 `--rel-tol`(기본 1e-6) 안에서 같은 경우다. 값 정규화가
소수점 6자리 **절대** 반올림이라 비율·평균처럼 자릿수가 긴 수치에서만 갈리는데, 그 정도 차이는
같은 답으로 본다(금액은 정수라 애초에 해당이 없다). 정답으로 센다.

`shape_only`는 행수·컬럼수만 같고 값이 다른 경우다. 오답이지만 완전한 `mismatch`와 구분해 둔다.
구조는 맞고 값만 틀렸다는 뜻이라 조인·필터 쪽을 먼저 보면 된다. 1행 1열 결과는 모양이 항상
같으므로, 스칼라 오답은 허용오차를 벗어나면 전부 여기로 떨어진다.

`sql_error` 와 `mismatch` 를 원인별로 파고들려면 [오답 정리 스크립트](goldenset-failure-report.md)를 이어서 돌린다.

```bash
python scripts/report_goldenset_failures.py
python scripts/report_goldenset_failures.py --verdicts rollup_match   # 집계 축만 다른 건 따로
```

집계 지표는 다음과 같다.

- **실행 정확도**: `exact + match + value_match + subset_match + required_match + scalar_near` /
  채점 대상 — 기본 성능 지표다. 엄격하게 보고 싶으면 판정 분포에서 완화 판정을 빼고 다시 세면 된다.
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
  "column_subset": null,
  "subset_columns": [],
  "required_columns": [],
  "rollup_match": false,
  "expected_totals": [1250000], "actual_totals": [1250000, 7],
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

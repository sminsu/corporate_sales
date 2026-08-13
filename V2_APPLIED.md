# 이 폴더는 v2가 전부 적용된 사본이다

`corporate_sales_fable`(v1)의 복사본에 goldenset v2 SQL 오류 400건 대응을 **제자리로
적용**했다. 환경변수나 monkeypatch 없이 그대로 실행된다.

원인 분석은 [docs/goldenset-v2-sql-error-analysis.md](docs/goldenset-v2-sql-error-analysis.md),
변경 요약은 [docs/v2-README.md](docs/v2-README.md)를 본다.

## 무엇이 적용돼 있나

**데이터**

| 파일 | 변경 |
|---|---|
| `semantic_layer.yaml` | 버전 `2026-08-11.2-v2`. 되묻기 지시 제거, 같은 이름 컬럼 동의어 통합(107) + 유도(3,243), 0811 코드북 7종을 컬럼 44곳에 적용, Athena 방언 규칙 보강, 프롬프트 렌더 한도(12개) 정리 |
| `text2sql_agent/tools/sql_verified_queries.yaml` | `special_debt_by_asset_quality` 원천 테이블 `tmdaaus01`→`tbmaisd06`(오류 22건 원인), `merchant_risk_combined_customer_month` `tbmaisd06`→`tbdaaus01`, `::FLOAT` 8곳 → `CAST(... AS DOUBLE)` |
| `codebooks/column_codebooks.yaml` | 0811 xlsx 8종에서 만든 코드북 7종(단일 출처). 유효종료 코드도 이력 해석용으로 보존 |
| `tests/fixtures/semantic_layer_golden_v1.jsonl` | semantic layer 버전 변경에 맞춰 재생성 |

**코드** (v1 대비 이 4곳만 다르다)

| 위치 | 변경 |
|---|---|
| `text2sql_agent/v2/` | 신규 패키지. 동의어 유도·매처, 방언 가드, 계약 문장, 참고 SQL 감사기, **참고 SQL 출력 계약 가드** |
| `text2sql_agent/schema.py` `_phrase_in_text()` | 구분자·중첩 조사에 둔감한 v2 매처로 교체. 프롬프트에 어떤 컬럼이 들어가는지를 결정하는 지점 |
| `text2sql_agent/schema.py` `find_relevant_queries()` | 질문에 답 못 하는 참고 SQL은 프롬프트 예시로 붙이지 않는다 |
| `text2sql_agent/workflow.py` `generate_sql()` | 되묻기 응답이면 재시도로 전환, `expr::type` 자동 교정, `max_tokens` 2048 → 4096, 기업영업 스코프 절 추가 |
| `text2sql_agent/workflow.py` `validate_sql()` | 실행 전 캐스트 교정 + QUALIFY·WHERE 윈도함수·잘린 SQL 정적 감사 |
| `text2sql_agent/workflow.py` `_verified_query_matches_intent()` | 기간·축·지표·대상 개념을 못 내놓는 VQ는 매칭 거부 |
| `text2sql_agent/workflow.py` `_rule_rank_tables()` | authoritative 계약은 required 엔티티가 있을 때만, 정확 컬럼명 일치에 가중치 |

**medium mismatch 130건 대응** (4차)

기계 분류로 원인을 가른 뒤, 현재 코드로 아직 재현되는 것만 고쳤다. 새 모듈 없이
기존 함수만 손봤다.

| 원인 | 건수 | 상태 |
|---|---|---|
| 기업영업 스코프 누락 | 21 | 3차 프롬프트 절로 **21/21 해결** |
| 기간 어구가 가맹점명으로 추출 | 2 | `_extract_merchant_name_by_rule()` 에 기간 어구 추가 |
| 개수 질문에 목록형 VQ 매칭 | — | `_asks_scalar_count()` + `_vq_lists_entities()` 로 VQ 경로에도 게이트 |
| 테이블 오선택 | 50 | 소수 보유 컬럼 가중치로 29/50 (측정 기준 상세는 아래) |

정답 테이블 커버리지(452건 전체, 정답 테이블 전부가 후보에 오른 비율):
easy 57→59, medium 83→84, hard 192→193, 합계 332→336. 어느 난이도도 내려가지 않았다.

**SQL 실행 실패 40건 대응** (3차)

`column_not_found` 27 · `guard` 9 · `group_by` 3 · `syntax` 1.
원인과 남은 과제는
[docs/goldenset-v2-sql-execution-failures.md](docs/goldenset-v2-sql-execution-failures.md).

| 위치 | 변경 |
|---|---|
| `text2sql_agent/v2/column_repair.py` | 신규. 실행 전 컬럼 해석 — 축약·오타 자동 교정, 못 고치면 "그 컬럼은 어느 테이블에 있다"를 재시도 메시지로 |
| `text2sql_agent/v2/sql_dialect_guard.py` | `rewrite_cross_join_scalars()` — GROUP BY 쿼리의 CROSS JOIN 스칼라를 `MAX()` 로 감싼다 |
| `text2sql_agent/workflow.py` `validate_sql()` | 컬럼 교정·검증 연결 |
| `text2sql_agent/workflow.py` `_extract_merchant_name_by_rule()` | "기준 가맹점" 같은 시점 수식어를 이름으로 잡지 않는다 |
| `scripts/run_goldenset_answers.py` `assert_read_only()` | 주석·문자열 안의 세미콜론을 다중 문장으로 오판하던 것을 운영 가드와 동일 기준으로 교체 |

**easy mismatch 63건 대응** (2차)

원인 분석과 남은 과제는
[docs/goldenset-v2-easy-mismatch-analysis.md](docs/goldenset-v2-easy-mismatch-analysis.md).
정답 테이블 커버리지가 452건 기준 40% → 51%(부분 포함 78% → 92%)로 올랐고,
`_rule_rank_tables()` 한 번이 18초 → 11ms 가 됐다(`phrase_in_text` 패턴 캐시).

`text2sql_agent/v2/` 를 패키지 안에 둔 이유는 `Dockerfile` 이 `COPY text2sql_agent`
로만 코드를 가져가기 때문이다. 최상위에 두면 이미지에서 빠져 import 가 깨진다.

## 확인

```bash
cd corporate_sales_fable_v2
python -m pytest
```

기존 테스트 전체 + v2 회귀 테스트가 함께 돈다.

```bash
python -c "
import text2sql_agent.workflow as W
q='2026년 7월 기준 가맹점 상태구분별 가맹점 수를 알려줘'
t=W._rule_rank_tables(q)
print('가맹점상태구분코드 in prompt:', '가맹점상태구분코드' in W._table_details(t,q))
"
```

v1에서는 `False`(그래서 모델이 "컬럼을 알려주세요"로 답했다), 여기서는 `True` 다.

## v1 과 다른 점

- `fable.zip`(44MB, v1 시점 스냅샷)은 복사하지 않았다.
- `output/`, `reports/`, `tmp/`, `__pycache__` 등 산출물·캐시는 제외했다.
- `.git` 이 없다. 형상관리 이력은 v1 저장소의 `fix/goldenset-v2-sql-errors`
  브랜치에 있다.

## 재생성

원본 xlsx·CSV가 갱신되면 v1 저장소를 원본으로 다시 만든다. 빌드 스크립트의 기본
입력 경로는 `../corporate_sales_fable/`, 출력은 이 폴더 제자리다.

```bash
python scripts/v2_build/build_column_codebooks.py --source ~/Downloads/goldenset_error/0811
python scripts/v2_build/build_semantic_layer_v2.py
python scripts/v2_build/build_verified_queries_v2.py
python scripts/generate_semantic_golden_set.py
python -m pytest
```

빌드는 `ruamel.yaml`(주석 보존)이 필요하고 런타임은 `pyyaml` 로 읽는다.
`requirements.docker.txt` 는 그대로다.

## 남은 것

- 참고 SQL이 참조하는 미등록 테이블 5개(`tbdaaat14`, `tmdaa2e17`, `tbdaaaf23`,
  `tbdaaha97`, 런타임 관계 `managed_scope`). 이번 오류 400건에는 없었다.
- `EXPRESSION_NOT_AGGREGATE` 1건은 정적 감사에서 제외했다(정상 쿼리 오탐).
  프롬프트 규칙으로만 예방한다.
- `"가맹점매출금액"` 처럼 지표 합성이 필요한 8건은 `canonical_metrics` 사안이라
  범위에서 제외했다.
- **테이블 선택이 다음 병목이다.** 회귀 픽스처 271건 중 190건(70%)이 프롬프트에
  도달한다(v1 기준 59건, 22%). 남은 81건은 정답 테이블이 `_rule_rank_tables()` 후보에
  아예 오르지 못한 경우라, 컬럼 예산을 늘려도 달라지지 않는다. 근거는
  `docs/integration.md` 의 "컬럼 예산은 지금 병목이 아니다" 절에 있다.

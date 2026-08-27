# 골든셋 정답 결과 채우기 (`scripts/run_goldenset_answers.py`)

골든셋 v2의 정답 SQL을 Amazon Athena에서 실제로 실행해 **정답 결과(denotation)** 를 채워 넣는 러너다.
질의와 SQL만 있던 골든셋에 실행 결과가 붙으면, 에이전트가 만든 SQL을 실행한 결과와 값 단위로 비교하는
채점이 가능해진다.

이 스크립트는 사내 AWS 자격증명이 있는 환경에서 실행해야 한다. 인증은 표준 AWS 자격증명 체인
(환경변수 / `AWS_PROFILE` / EC2·ECS IAM 역할)을 그대로 쓰고, 접속 설정은 서비스와 같은 환경변수를 읽는다.

```bash
ATHENA_REGION=ap-northeast-2
ATHENA_DATABASE=card_system
ATHENA_WORKGROUP=primary
ATHENA_CATALOG=AwsDataCatalog
ATHENA_S3_STAGING_DIR=s3://your-athena-results-bucket/path/   # 워크그룹에 있으면 생략 가능
# ATHENA_ENDPOINT_URL=https://vpce-xxxx.athena.ap-northeast-2.vpce.amazonaws.com
```

## 실행

```bash
# 0) Athena 없이 내부 로직만 점검 (정규화·해시·가드)
python scripts/run_goldenset_answers.py --self-test

# 1) 무엇이 실행될지만 확인
python scripts/run_goldenset_answers.py --dry-run --limit 20

# 2) 앞의 20건만 시험 실행
python scripts/run_goldenset_answers.py --limit 20

# 3) 저장된 결과는 건너뛰고 나머지 전체 실행 (여러 번 나눠 돌려도 됨)
python scripts/run_goldenset_answers.py

# 4) 실패·0행 케이스만 재시도
python scripts/run_goldenset_answers.py --retry-errors

# 5) 실행 없이 병합본·리포트만 다시 생성
python scripts/run_goldenset_answers.py --merge-only
```

케이스마다 결과를 즉시 flush 하고 fsync 하므로 중단해도 안전하다. 같은 명령을 다시 실행하면
저장된 케이스를 건너뛰고 남은 건부터 이어서 처리한다.

## 산출물

| 경로 | 내용 |
|---|---|
| `reports/goldenset-v2-answers.jsonl` | 케이스별 실행 이력(원본). 재개 판단의 기준 |
| `tests/fixtures/corporate_sales_text2sql_goldenset_v2_answered.jsonl` | 골든셋에 `expected_answer`를 병합한 최종본 |
| `reports/goldenset-v2-answers-summary.md` | 성공·0행·실패·미실행 집계, 도메인별 표, 실패 사유 |
| `reports/goldenset-v2-answers-preview.csv` | 현업 검수용. 질의 / 행수 / 스칼라 정답 / 첫 행 / 해시 |

병합본에 붙는 정답은 다음 형태다.

```json
"answer_status": "ok",
"expected_answer": {
  "columns": ["기준년월", "월이용금액"],
  "row_count": 7,
  "row_count_truncated": false,
  "rows": [["202601", 123456789]],
  "rows_stored": 7,
  "scalar": null,
  "result_hash": "sha256 (행 순서 무시)",
  "ordered_hash": "sha256 (행 순서 포함)",
  "float_digits": 6
}
```

- `scalar`: 1행 1열 결과일 때만 채워진다. "유실적 업체수", "가맹점 수" 같은 단일 수치 정답의 채점 키다.
- `result_hash`: 값을 정규화(NULL·Decimal·실수 반올림·날짜 ISO)한 뒤 행을 정렬해 만든 멀티셋 해시다.
  `ORDER BY` 동점 구간에서 순서가 흔들려도 채점이 깨지지 않는다. 정렬까지 검증하려면 `ordered_hash`를 쓴다.
- `rows`: 기본 50행까지만 저장한다(`--answer-rows`). 전체 행 수는 `row_count`에 남는다.

## 실행량·비용 조절

Athena는 스캔한 데이터량으로 과금되므로 한 번에 1,000건을 다 돌리기 전에 표본으로 감을 잡는 편이 안전하다.

```bash
# 도메인별로 나눠 실행
python scripts/run_goldenset_answers.py --domain merchant_sales --limit 50

# 특정 질의형태만
python scripts/run_goldenset_answers.py --shape business_case

# 누적 스캔량 20GB에 도달하면 중단
python scripts/run_goldenset_answers.py --max-scanned-gb 20

# 연속 5회 실패하면 중단 (권한·스키마 문제 조기 감지)
python scripts/run_goldenset_answers.py --stop-after-errors 5

# 특정 케이스만
python scripts/run_goldenset_answers.py --ids cs-golden-v2-0007 cs-golden-v2-0042
```

`--fetch-rows`(기본 5000)는 행 수와 해시 계산을 위해 실제로 가져오는 최대 행 수다. 상세 목록형 케이스는
원본 SQL이 `LIMIT 1000000`이라 이 상한에 걸려 `row_count_truncated: true`로 저장된다. 목록형까지 해시로
채점하려면 골든과 후보를 **같은 상한**으로 실행해야 하므로 `--row-limit 200`처럼 명시적으로 감싸서 돌리는 편이 좋다.

## 스키마 접두사

골든셋 SQL은 스키마 접두사 없이 테이블명만 쓴다(`FROM tbdaa1d12 a`). pyathena 접속의
`ATHENA_DATABASE`(기본 `card_system`)가 기본 스키마가 되므로 별도 설정 없이 그대로 실행된다.

database-qualified 이름을 반드시 써야 하는 환경에서만 실행 시점에 접두사를 붙인다.

```bash
python scripts/run_goldenset_answers.py --schema kbcard_db   # FROM kbcard_db.tbdaa1d12
python scripts/run_goldenset_answers.py --schema none        # 기존 접두사가 있으면 제거
```

미지정 시 `DB_SCHEMA` 환경변수를 따르고, 그것도 비어 있으면 원본 그대로 실행한다. 접두사 부여·제거는
골든셋의 `expected_tables` 에서 모은 물리 테이블 이름에만 적용되므로 `base`, `ranked`, `agg` 같은
CTE 이름은 건드리지 않는다.

## 실행기 선택

| 값 | 동작 |
|---|---|
| `auto` (기본) | pyathena로 직접 실행. 초기화 실패 시 서비스 경로로 폴백 |
| `pyathena` | pyathena DB-API 직접 실행. query id·스캔 바이트·엔진 실행시간을 함께 기록 |
| `agent` | `text2sql_agent.db.execute_sql` 경유. 서비스와 완전히 같은 읽기 전용 가드·한글 식별자 처리·TBD→TMD 폴백이 적용되지만 Athena 실행 통계는 얻지 못함 |
| `stub` | Athena 없이 파이프라인만 점검하는 더미 |

어느 경로든 실행 전에 단일 문장·`SELECT`/`WITH` 시작·쓰기 키워드 없음을 다시 확인하고, 위반하면 실행하지 않는다.

## 정답 재검증

원천 데이터가 바뀌면 저장된 정답도 낡는다. `--verify`는 저장된 정답을 다시 실행해 해시가 달라진 케이스를 찾는다.

```bash
python scripts/run_goldenset_answers.py --verify --domain credit_risk
```

불일치가 있으면 목록을 출력하고 종료 코드 1을 반환한다. 스냅샷 테이블 적재가 끝난 뒤 정기적으로 돌리면
"데이터가 바뀌어서 틀린 것"과 "에이전트가 틀린 것"을 구분할 수 있다.

## 주의

- 종료 코드는 실패 케이스가 하나라도 있으면 1이다. CI에서 `&&`로 이어 붙일 때 유의한다.
- 정답 결과는 실행 시점의 데이터에 종속된다. 병합본의 `answer_executed_at`을 함께 보관하고,
  벤치마크로 동결할 때는 실행 시점과 스냅샷 적재일을 같이 기록하는 편이 좋다.
- 0행(`empty`)은 SQL 오류가 아니라 조건에 맞는 데이터가 없다는 뜻이다. 다만 브랜드명·기업명 리터럴이
  실제 데이터와 맞지 않아 0행이 나오는 경우도 있으므로, `empty` 비중이 높으면 해당 리터럴을 실제 값으로
  교체한 뒤 다시 돌리는 것을 권한다.

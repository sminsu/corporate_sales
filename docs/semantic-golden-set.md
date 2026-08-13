# 시맨틱 레이어 골든셋 v1

`tests/fixtures/semantic_layer_golden_v1.jsonl`은 `semantic_layer.yaml`의 현재 버전에서 생성한 한국어 Text2SQL 시맨틱 골든셋이다. 기존 `nl2sql_quality_cases.json`은 파서·안전성 회귀용이므로 그대로 두고, 이 데이터는 별도 계약 테스트로 검증한다.

## 구성

| 구분 | sql | clarify | unsupported | 합계 |
|---|---:|---:|---:|---:|
| 핵심 semantic query contract | 600 | 0 | 0 | 600 |
| canonical metric·curated join 조합 | 300 | 0 | 0 | 300 |
| 필수값 누락·모호성 | 0 | 60 | 0 | 60 |
| 원천 부재·민감정보·미지원 의미 | 0 | 0 | 40 | 40 |
| 합계 | 900 | 60 | 40 | 1,000 |

지원 가능·추가 질문 960건의 도메인별 구성은 `corporate_sales_targeting` 273, `merchant_sales` 213, `customer_card_portfolio` 169, `card_usage` 151, `credit_risk` 154건이다. `unsupported` 40건은 특정 기업 도메인의 정답이 아니므로 `domain=null`이다. 상대 날짜 평가 기준일은 모든 케이스에서 `2026-08-04`로 고정했다.

## 제외 범위

현재 골든셋은 다음 질문을 생성하지 않는다.

- `공공기업` 또는 `공기업`을 대상으로 하는 질문
- 로그인 아이디, 내 계정, 사용자 권한이나 담당·관리 기업 범위에 의존하는 질문
- 카드 상품별 또는 특정 카드 상품의 유효카드 수 질문
- 현행 업무에 없는 `1~5번 영업대상` 분류를 전제로 한 질문

카드 브랜드별 유효 좌수, 카드 상품별 이용금액, 특정 상품 신규 발급은 위 상품별 유효카드 수와 서로 다른 분석이므로 유지한다. 생성기는 제외 도메인·source ID·질문 문구를 마지막 단계에서 다시 검사하며, 알려진 조사 오류와 중복 표현도 함께 차단한다.

## 지원 불가 범위

`unsupported` 40건은 다음 네 유형을 각 10건씩 포함한다.

- 기업카드 분석이 아닌 개인카드·개인회원 서비스 질문
- 시멘틱 레이어에 정의되지 않은 용어·지표 질문
- 주민등록번호·개인 연락처·계좌번호·자택 주소 등 개인정보 요청
- 날씨·음식·여행·스포츠·코딩 등 기업영업 데이터와 무관한 질문

이들은 SQL이나 수치 결과를 내지 않고 지원 불가 안내로 종료해야 한다. 이전 요청에서 제외한 로그인·내 계정·담당 기업 의존 질문은 이 집합에도 다시 추가하지 않았다.

## 레코드 계약

- `question_ko`: 사용자 질문
- `expected_action`: `sql`, `clarify`, `unsupported` 중 하나
- `source`: contract·metric·attribute·join path·verified query lineage
- `parameters.required`: SQL 실행 전에 질문에서 확보되어야 하는 값
- `expected_sql`: 정답 SQL의 평가 방식
  - `verified_query`: `sql_verified_queries.yaml`의 이름과 SHA-256으로 템플릿을 고정
  - `semantic_generation`: semantic query contract의 테이블·필터·계산식으로 평가
  - `semantic_spec`: canonical metric expression, aggregation behavior, required filter, curated join condition으로 평가
- `expected_tables`, `expected_grain`: 물리 테이블과 결과 grain
- `expected_missing_parameters`, `reason_code`: 추가 질문·미지원 정답

SQL 문자열 완전일치 대신 query id/hash 또는 구조적 테이블·지표·필터·join·grain을 비교한다. 이 방식은 schema prefix와 포맷 차이를 허용하면서 스냅샷 중복 제거, ratio-of-sums, 기간 필터 같은 의미 계약은 고정한다.

활성 VQ라도 현재 schema validator를 통과하지 못하면 `verified_query` 정답으로 고정하지 않고 `semantic_spec`으로 내린다. 따라서 단순히 활성 플래그만 보고 잘못된 SQL을 골든으로 승인하지 않는다.

## 생성·실행

```bash
python scripts/generate_semantic_golden_set.py
python scripts/generate_semantic_golden_set.py --check
python scripts/run_semantic_golden_eval.py
pytest -q tests/test_semantic_golden_set.py
pytest -q tests/test_semantic_golden_runner.py
```

평가 러너는 외부 LLM·DB 없이 1,000건을 모두 순회한 뒤 다음 파일을 만든다.

- `reports/semantic-golden-eval.json`: 케이스별 계약 검사와 런타임 관측 전체 결과
- `reports/semantic-golden-eval.md`: 종합·검사별·도메인별 결과와 불일치 예시

계약 검사는 정확히 1,000건인지, NFKC·공백·문장부호 정규화 후 중복이 없는지, 포함된 5개 도메인/35개 실행 가능 계약/canonical metric/curated join/VQ hash가 현재 시맨틱 레이어와 일치하는지, SQL 케이스가 restricted `tbdaaat18`을 사용하지 않는지 확인한다. 실패가 있어도 두 리포트를 먼저 저장한 뒤 종료 코드 1을 반환하므로 CI에서 원인을 확인할 수 있다.

도메인, semantic contract, verified query, authoritative core의 테이블 선택은 현재 코드의 결정적 규칙을 실제로 호출해 별도 지표로 기록한다. LLM adjudication이 필요한 도메인 질문은 미결정으로 남기며, verified query 지표는 골든 정답이 VQ인 케이스의 재현율이다. 이 값은 embedding·LLM 폴백 전 단계의 개선 지표이므로 기본 합격 여부에는 반영하지 않는다. 비-authoritative composition의 테이블 선택은 계산량이 매우 커 기본 실행에서 제외하며 꼭 필요할 때만 다음처럼 실행한다.

```bash
python scripts/run_semantic_golden_eval.py --include-table-runtime
```

입력·출력 위치를 바꾸려면 `--cases`, `--json-out`, `--markdown-out`을 사용한다. 레코드 수 검사를 끄고 부분 데이터로 진단할 때는 `--expected-count 0`을 지정한다.

## 실제 에이전트 실행

실제 LLM·DB를 포함한 LangGraph 전체 경로로 질문을 실행하려면 별도 라이브 러너를 사용한다. 먼저 일부만 확인하고, 같은 명령을 다시 실행해 나머지를 이어서 처리하는 방식이 안전하다.

```bash
# 아직 실행하지 않은 앞 10건만 실행
python scripts/run_semantic_golden_live.py --limit 10

# 저장된 케이스를 건너뛰고 나머지 전체 실행
python scripts/run_semantic_golden_live.py

# 최근 실행이 오류 또는 빈 답변인 케이스만 재시도
python scripts/run_semantic_golden_live.py --retry-errors
```

각 케이스가 끝날 때마다 결과를 즉시 flush하여 중단 후 재개할 수 있다. 결과는 다음 파일에 생성된다.

- `reports/semantic-golden-live-results.jsonl`: 질문, 기대값, 실제 답변, SQL, 라우팅, 실행시간, 오류를 포함한 케이스별 최신 실행 이력
- `reports/semantic-golden-live-summary.md`: 실행·저장 완료, 답변 저장, 오류, 추가 파라미터 요청, 미실행 건수 요약

에이전트가 추가 파라미터를 요청하면 라이브 러너가 각 케이스의 `parameters.reference_date`를 기준으로 날짜·기간 기본값을 만들고, 기업명·가맹점명·상품명·금액·분류축 등에도 고정된 테스트 기본값을 넣어 자동으로 이어서 실행한다. 적용한 값과 재호출 횟수는 결과의 `default_params_applied`, `default_param_rounds`에 저장한다. 같은 파라미터를 다시 요청하면 무한 반복하지 않고 `requires_params`로 남기며 `--retry-errors` 실행 때 재시도한다. 이 기능 도입 전에 저장된 `requires_params` 결과는 다음 기본 실행에서 한 번 자동 재시도한다.

상태는 `answered`, `unsupported`, `action_mismatch`, `requires_params`, `agent_error`, `execution_error`, `missing_answer`로 구분한다. `unsupported`는 에이전트가 `reject`로 종료하고 SQL·DB 결과를 만들지 않은 채 지원 불가 사유를 안내했을 때만 성공이다. 안내문도 응답 문자열이므로 `answer_saved=true`이지만 `data_answer_saved=false`이다. SQL·수치 답변을 시도하면 `action_mismatch`로 실패 처리한다. DB 원본 행은 저장하지 않고 답변·SQL·반환 행 수·오류 메타데이터만 저장한다.

전체 1,000건은 실제 모델 비용과 DB 부하가 발생하며 오래 걸릴 수 있어 기본 동시성은 1이다. 결과를 완전히 지우고 처음부터 실행할 때만 명시적으로 `--restart`를 사용한다.

`labels.review_status=semantic_contract_verified`는 시맨틱 SSOT와 자동 계약 검증을 통과했다는 뜻이다. 실제 DB 결과(denotation)와 사람의 문장별 승인까지 마친 상태를 뜻하지 않으므로, 외부 벤치마크로 동결하기 전에는 현업 표본 검수를 추가한다.

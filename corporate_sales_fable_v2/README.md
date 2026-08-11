# corporate_sales_fable v2

goldenset v2 테스트에서 나온 SQL 오류 400건의 원인을 없앤 버전이다.
v1 소스(`../semantic_layer.yaml`, `../text2sql_agent/`)는 수정하지 않았다.

## 무엇이 문제였나

| error_kind | 건수 | 실제 원인 |
|---|---:|---|
| guard | 332 | semantic layer 계약이 모델에게 **되묻도록 지시**하고 있었다 + 질문 표면형이 컬럼과 연결되지 않았다 |
| column_not_found | 57 | 참고 SQL 1건의 원천 테이블 오류가 22건 + 컬럼 매칭 실패 |
| syntax | 10 | 참고 SQL의 `::FLOAT` 전파, `QUALIFY`, WHERE 안의 윈도함수, `max_tokens` 부족 |
| group_by | 1 | CROSS JOIN 스칼라를 집계 없이 SELECT |

정답 SQL이 쓰는 컬럼은 **전부 semantic layer 에 있었다**. 스키마가 부족한 게 아니라,
그 컬럼이 프롬프트까지 전달되지 않은 것이 문제였다.

자세한 분석: [docs/goldenset-v2-sql-error-analysis.md](docs/goldenset-v2-sql-error-analysis.md)

## 무엇이 바뀌었나

**semantic_layer.yaml** (v1과 동일 스키마, 드롭인 교체 가능)
- `sql_generation_contract.ambiguity_rules` 의 "추가 입력을 요청한다" 삭제 →
  항상 SQL만 내보내는 `output_contract` + 기본값 해석 `resolution_defaults` 로 교체
- 같은 이름의 컬럼은 테이블을 넘어 동의어를 공유 (107건 전파)
- 컬럼명에서 질문 표면형 동의어 유도 (3,243건)
- 0811 상세 컬럼 코드북 7종을 같은 이름의 컬럼 44곳에 `value_semantics` 로 적용
- Athena 방언 규칙에 `QUALIFY`·`expr::type`·윈도함수 WHERE 금지 명시
- 프롬프트 렌더 한도(12개)를 넘겨 잘리던 규칙 리스트 정리

**sql_verified_queries.yaml** (v1과 동일 스키마)
- `special_debt_by_asset_quality`: `tmdaaus01` → `tbmaisd06` (오류 22건의 원인)
- `merchant_risk_combined_customer_month`: `tbmaisd06` → `tbdaaus01`
- `::FLOAT` 8곳 → `CAST(... AS DOUBLE)`

**text2sql_v2/** (선택 적용 모듈)
- `column_synonyms` — 동의어 유도 + 띄어쓰기·중첩 조사에 둔감한 매처
- `sql_dialect_guard` — 캐스트 자동 교정, QUALIFY·윈도함수 WHERE·잘림 검출, 되묻기 판정
- `sql_contract` — 프롬프트 계약 문장의 단일 출처
- `verified_query_audit` — 참고 SQL이 스키마와 맞는지 검사

## 효과

질문↔컬럼 매칭 실패 조합 (primary key 제외, 오류 400건에서 추출):

| 구성 | 실패 | 감소 |
|---|---:|---:|
| v1 스키마 + v1 매처 | 481 | — |
| v2 스키마 + v1 매처 (1단계) | 132 | 73% |
| **v2 스키마 + v2 매처 (2단계)** | **46** | **90%** |

여기에 계약 변경으로 되묻기 지시가 사라지고, 참고 SQL 오류 22건과 `::FLOAT` 2건은
원천에서 제거됐다. 실제 통과율은 Athena 실행이 필요하므로 goldenset 재실행으로
확인해야 한다.

## 적용 — 2단계까지 해야 한다

**1단계 (코드 변경 없음)** — 되묻기 지시 제거, 참고 SQL 오류, 코드북, 방언 규칙

```bash
export SEMANTIC_SCHEMA_PATH=corporate_sales_fable_v2/semantic_layer.yaml
export VERIFIED_QUERY_FILE_PATH=corporate_sales_fable_v2/sql_verified_queries.yaml
```

두 경로는 v1 코드가 이미 환경변수로 열어둔 지점이다.

**2단계 (v1 코드 1줄)** — 질문↔컬럼 매칭. 1단계만으로는 부족하다.

프롬프트에 넣을 컬럼을 고르는 `_table_details()` 가 v1 매처를 쓰기 때문에, 동의어를
늘려도 v1 매처가 못 잡는 표면형은 여전히 잘려나간다. 대표 사례
(`2026년 7월 기준 가맹점 상태구분별 가맹점 수를 알려줘`)에서 `가맹점상태구분코드` 는
1단계만 적용했을 때 **여전히 프롬프트에 들어가지 못했고**, 2단계에서 들어갔다.

```python
import text2sql_agent.schema as schema, text2sql_agent.workflow as workflow
from corporate_sales_fable_v2.text2sql_v2 import phrase_in_text
schema._phrase_in_text = workflow._phrase_in_text = phrase_in_text
```

자세한 적용 지점과 선택 항목(캐스트 자동 교정, 되묻기 재시도, `max_tokens`)은
[docs/integration.md](docs/integration.md) 참고.

## 테스트

```bash
cd corporate_sales_fable_v2 && python -m pytest
```

89개 테스트. 실제 오류 400건에서 뽑은 회귀 픽스처
(`tests/fixtures/goldenset_v2_sql_errors.json`)로 검증한다.

## 구성

```
semantic_layer.yaml                v2 스키마 (생성물)
sql_verified_queries.yaml          v2 참고 SQL (생성물)
codebooks/column_codebooks.yaml    0811 xlsx 8종 → 코드북 7종 (생성물, 단일 출처)
text2sql_v2/                       적용 모듈
scripts/build_*.py                 생성 스크립트 (모두 재실행 가능)
tests/                             89개 테스트 + 회귀 픽스처
docs/                              원인 분석 / 적용 가이드
```

생성물은 스크립트로 재현된다. 원본이 갱신되면 `docs/integration.md#재생성` 순서대로
다시 만든다. 빌드는 `ruamel.yaml`(주석 보존)을 쓰고 런타임은 `pyyaml` 로 읽는다.

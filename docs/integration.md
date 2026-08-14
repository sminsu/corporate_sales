# v2 적용 가이드

v1 소스는 수정하지 않았다. 적용은 두 단계이고, **두 단계가 서로 다른 오류를 담당한다.**

- **1단계 (데이터 교체, 코드 변경 없음)** — 되묻기 지시 제거, 참고 SQL의 원천 테이블
  오류와 `::FLOAT`, 코드북, 방언 규칙. guard 332건의 직접 원인과 column_not_found 22건,
  syntax 2건이 여기서 해결된다.
- **2단계 (매처 교체, v1 코드 1줄)** — 질문↔컬럼 매칭. **1단계만으로는 부족하다.**
  프롬프트에 넣을 컬럼을 고르는 `_table_details()` 가 v1 매처를 쓰기 때문에, 동의어를
  늘려도 v1 매처가 못 잡는 표면형은 여전히 잘려나간다.

대표 사례로 확인한 결과 (`2026년 7월 기준 가맹점 상태구분별 가맹점 수를 알려줘`):

| 구성 | `가맹점상태구분코드` 가 프롬프트에 포함 |
|---|---|
| v1 스키마 + v1 매처 | 안 됨 |
| v2 스키마 + v1 매처 (1단계만) | **안 됨** |
| v2 스키마 + v2 매처 (2단계) | 됨 (코드값 `{"1":"정상","2":"거래정지","3":"해지"}` 포함) |

v1 매처는 동의어에 적힌 토큰 사이에서만 공백을 허용한다. `'가맹점상태구분'` 은 질문의
`"가맹점 상태구분별"`(가맹점과 상태 사이 공백)을 잡지 못하고, 띄어쓰기 변형을 아무리
넣어도 조합이 다르면 또 빗나간다. 2단계 매처는 글자 사이마다 구분자를 허용해 이 문제를
근본적으로 없앤다.

**즉 컬럼 매칭 오류를 없애려면 2단계까지 해야 한다.**

## 1단계 — 데이터 교체 (코드 변경 없음)

v2 의 두 YAML은 v1과 동일한 스키마이므로 경로만 바꿔주면 된다. 두 경로 모두 이미
환경변수로 열려 있다(`text2sql_agent/config.py:30`, `text2sql_agent/tools/verified_queries.py:75`).

```bash
export SEMANTIC_SCHEMA_PATH=corporate_sales_fable_v2/semantic_layer.yaml
export VERIFIED_QUERY_FILE_PATH=corporate_sales_fable_v2/sql_verified_queries.yaml
```

이것만으로 반영되는 것:

| 오류 | 대응 |
|---|---|
| guard 332건 (되묻기) | `sql_generation_contract.ambiguity_rules` 의 "추가 입력을 요청한다" 삭제 → `output_contract` 로 교체 |
| guard·column_not_found (컬럼 미발견) | 같은 이름 컬럼 동의어 통합 107건 + 컬럼명 유도 동의어 3,243건 |
| column_not_found 22건 | `special_debt_by_asset_quality` 의 원천 테이블 `tmdaaus01` → `tbmaisd06` |
| syntax 2건 | 참고 SQL의 `::FLOAT` 8곳 → `CAST(... AS DOUBLE)` |
| syntax 4+3건 | `athena_rules` 에 QUALIFY·윈도함수 WHERE 금지 명시 |
| 코드값 해석 | 0811 코드북 7종을 같은 이름의 컬럼 44곳에 `value_semantics` 로 적용 |

`semantic_layer_metadata.version` 이 `2026-08-14.3-v2` 면 v2 가 로드된 것이다.

## 2단계 — 매처·가드 연결 (컬럼 매칭에는 필수)

1단계 뒤에도 v1 매처의 한계 때문에 질문↔컬럼 매칭 실패가 132건 남는다(v1 기준 481건).
v2 매처를 쓰면 46건으로 줄어든다. (1)이 핵심이고 (2)(3)은 방어 장치다.

### (1) 질문↔컬럼 매칭 — 필수

`text2sql_agent/schema.py:367` 의 `_phrase_in_text()` 를 교체한다. v1 매처는 phrase 에
적힌 토큰 사이에서만 구분자를 허용하고 조사를 한 개만 허용해서, `"회원자격별로"`(조사
`별`+`로`)와 `"기업카드 신용한도금액"`(컬럼명 중간 공백)을 놓친다.

```python
from corporate_sales_fable_v2.text2sql_v2 import phrase_in_text as _phrase_in_text
```

`workflow.py:43` 이 이 함수를 import 해두므로 그쪽 심볼도 함께 바꿔야 한다.

```python
import text2sql_agent.schema as schema, text2sql_agent.workflow as workflow
from corporate_sales_fable_v2.text2sql_v2 import phrase_in_text
schema._phrase_in_text = workflow._phrase_in_text = phrase_in_text
```

이 하나로 `_rule_rank_tables()` 의 테이블 선택과 `_table_details()` 의 컬럼 선택이
함께 개선된다. 두 곳 모두 같은 매처를 쓴다. 실측에서 테이블 선택도 나아졌다.
`가맹점 상태구분` 질의의 선택 테이블이
`['tmdaa1d12','tbdaadt01']` → `['tbdaadt01','tmdaa1d12','tbdaaus01','tmdaa5d01']` 로,
`자산건전성 분류` 질의는 `tbmaisd06`(정답 테이블)이 1순위로 올라왔다.

### (2) 실행 전 캐스트 교정과 정적 감사

`text2sql_agent/workflow.py` 의 `validate_sql()` 에서 기존 검증 앞에 끼운다.

```python
from corporate_sales_fable_v2.text2sql_v2 import audit_sql, normalize_sql

sql = normalize_sql(sql)          # expr::TYPE → CAST(expr AS TYPE)
issues.extend(audit_sql(sql))     # QUALIFY, WHERE 윈도함수, 잘림
```

`audit_sql()` 이 돌려주는 문장은 재시도 프롬프트(`validation_result`)에 그대로 넣도록
작성했다. 오탐이 재시도 루프를 만들지 않는 것을 우선했다.

### (3) 되묻기 응답 재시도

`generate_sql()` 이 프로즈를 반환하면 그대로 실행 단계로 넘어가 읽기 전용 가드에서
`ValueError: SELECT/WITH로 시작하지 않는 SQL` 로 죽는다. 재시도로 돌린다.

```python
from corporate_sales_fable_v2.text2sql_v2 import looks_like_prose, prose_reason

sql = _extract_sql_from_llm(_call_llm(prompt, max_tokens=4096))
if looks_like_prose(sql):
    return {"generated_sql": sql, "validation_result": prose_reason(sql), "is_valid": False,
            "retry_count": state.get("retry_count", 0) + 1}
```

### `max_tokens` 상향

`generate_sql()` 의 `max_tokens=2048` 때문에 복잡한 질의 3건이 문장 중간에서 잘렸다
(`mismatched input '<EOF>'`). 4096 이상을 권한다. `audit_sql()` 의 괄호 검사가
잘림을 잡아 재시도시키지만, 애초에 잘리지 않는 게 낫다.

### 컬럼 예산은 지금 병목이 아니다

`_table_details(max_columns=16, max_total_columns=48)` 이라 테이블 4개가 선택되면
테이블당 12개만 들어간다. 처음에는 이 예산을 올리라고 적었는데, 실측해 보니 아니었다.

2단계 적용 후에도 프롬프트에 못 들어간 81건 중 최다 사례를 추적한 결과:

```
질문: 현재 기준 상품중분류별 평균 기업카드 신용한도금액 상위 15개를 알려줘
정답 테이블: tbdaaat05
선택 테이블: ['tbdaa1d12']      ← tbdaaat05 가 후보에 오르지 못했다
max_total_columns 48 → 96: 결과 동일 (테이블이 없으니 예산과 무관)
```

병목은 컬럼 예산이 아니라 `_rule_rank_tables()` 의 **테이블 선택**이다. 점수 3점
이상인 테이블만 최대 4개 고르는데, 정답 테이블이 canonical_metrics·
semantic_attributes·query_references 어디에도 걸리지 않으면 후보에 오르지 못한다.
예산을 올리면 프롬프트만 길어지고 도달률은 그대로다.

**따라서 다음 개선은 테이블 랭킹이다.** 이번 작업은 선택된 테이블 안에서 컬럼이
살아남는 문제를 고쳤고, 랭킹은 손대지 않았다. `tbdaaat05`(기업카드 원천)처럼 자주
쓰이는 테이블을 canonical_metrics 나 semantic_attributes 에 연결하는 쪽이
예산 조정보다 효과가 클 것으로 보인다.

## 재생성

원본 xlsx·CSV가 갱신되면 순서대로 다시 만든다.

```bash
python corporate_sales_fable_v2/scripts/build_column_codebooks.py --source ~/Downloads/goldenset_error/0811
python corporate_sales_fable_v2/scripts/build_semantic_layer_v2.py
python corporate_sales_fable_v2/scripts/build_verified_queries_v2.py
python corporate_sales_fable_v2/scripts/build_goldenset_fixture.py --csv ~/Downloads/goldenset_error/goldenset-v2-sql-errors.csv
cd corporate_sales_fable_v2 && python -m pytest
```

빌드 스크립트는 `ruamel.yaml` 을 쓴다(주석 보존용, 개발 시에만 필요). 런타임은
`pyyaml` 로 읽으므로 `requirements.docker.txt` 는 그대로 둬도 된다.

## 남은 것

- **미등록 테이블 5개** — 참고 SQL이 `tbdaaat14`, `tmdaa2e17`, `tbdaaaf23`,
  `tbdaaha97` 와 런타임 관계 `managed_scope` 를 참조하는데 semantic layer 에 정의가
  없다. 컬럼 검증이 불가능하고, 모델에게 미등록 테이블을 가르칠 수 있다. 이번 오류
  400건에는 나타나지 않아 그대로 뒀다. 원천 정의서가 있으면 등록해야 한다.
  `tests/test_verified_queries_v2.py::test_unregistered_table_references_are_known`
  이 목록을 고정한다.
- **EXPRESSION_NOT_AGGREGATE 1건** — GROUP BY 쿼리에서 CROSS JOIN 스칼라를 집계 없이
  SELECT 한 경우. CTE별 SELECT 블록을 나눠 봐야 정확히 판정되고, 블록 구분 없이
  검사하면 정상 참고 쿼리 4개를 오탐했다. `athena_rules` 의 프롬프트 규칙으로만 예방한다.
- **지표 합성** — `"가맹점매출금액"` 처럼 질문이 부르는 이름이 컬럼이 아니라
  `가맹점일시불매출금액 + 가맹점할부매출금액` 인 경우가 8건 남는다. 동의어가 아니라
  `canonical_metrics` 에 지표를 추가해야 하는 사안이라 이번 범위에서 제외했다.

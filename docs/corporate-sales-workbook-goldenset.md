# 현업 워크북 질의 23건 Athena 정답 SQL

`통합 문서.xlsx`(Sheet1 A2:A24)의 현업 질의 23건에 대한 Athena engine v3 정답 SQL이다.
근거는 `semantic_layer.yaml`(테이블 grain·canonical metric·semantic query contract·`sql_generation_contract`)과
`text2sql_agent/tools/sql_verified_queries.yaml`이며, 기준일은 기존 골든셋과 같은 **2026-08-10** 으로 고정했다.

## 파일

| 경로 | 내용 |
|---|---|
| `scripts/generate_workbook_goldenset.py` | 정답 SQL 원본 + 검증 + 산출물 생성기 |
| `tests/fixtures/corporate_sales_workbook_goldenset_v1.jsonl` | 평가 러너 입력용 |
| `tests/fixtures/corporate_sales_workbook_goldenset_v1.csv` | 현업 검수용(UTF-8 BOM) |

```bash
python scripts/generate_workbook_goldenset.py --check
```

레코드 스키마는 골든셋 v2와 같고 `workbook_row`, `authoring_status`, `assumptions` 세 필드를 추가했다.
(`answer_status`는 `run_goldenset_answers.py`가 실행 결과로 덮어쓰는 예약 필드라 작성 상태는 `authoring_status`에 둔다.)
SQL은 v2와 마찬가지로 **스키마 접두사 없이 테이블명만** 쓴다(`FROM tbdaa1d12 a`). Athena 접속의
`ATHENA_DATABASE`가 기본 스키마이므로 그대로 실행되고, database-qualified 이름이 필요하면 실행 시점에
`card_system.` 을 붙인다.

## 질의별 정답 요약

| # | 행 | 질의 요지 | 원천 | 상태 |
|---:|---:|---|---|---|
| 1 | 2 | 26년 상반기 신규 모집 기업회원 이용금액(신용·체크) | tbdaaat03 + tmdaa3e16 | ok |
| 2 | 3 | 쿠팡 작년·올해 반기별 이용금액 | tbdaa1d12 | ok |
| 3 | 4 | (3행과 동일 질의) | tbdaa1d12 | 중복 |
| 4 | 5 | 기업카드 이용금액 상위 10개사 2026 분기별 | tbdaabt30 + tbdaa1d12 | ok |
| 5 | 6 | 위 결과에서 국세·지방세·4대보험·기업오토·KB계열사·상품권 제외 | tbdaabt30 + tbdaada72 | 가정 포함 |
| 6 | 7 | 반기 이용금액 하락 기업회원 수 + 리스트 | tbdaa1d12 | ok |
| 7 | 8 | 같은 조건 상위 100개사 | tbdaa1d12 | ok |
| 8 | 9 | 26년 6월 쿠팡 공헌이익율·대손비용률 | tbmewcm94 + tbmaisd06 | **부분 차단** |
| 9 | 10 | 체크카드만 보유 회원의 신용카드 발급·이용 건/금액 | tmdaa3e16 + tbdaaat05 | 가정 포함 |
| 10 | 11 | 상반기 전기요금전용 체크카드 신규 발급 좌수·리스트 | tbdaaat05 + tbdaada72 | ok |
| 11 | 12 | 가맹점번호 59593113 월별 매출 | tmdaa5e11 | ok |
| 12 | 13 | 현재기준 KB아시아나 기업카드 유효카드수 | tbdaaat05 + tbdaada72 | ok |
| 13 | 14 | 기업카드 보유 가맹점 매출 상위 10 | tmdaaus01 + tmdaa5e11 | 가정 포함 |
| 14 | 15 | 26년 6월 기업카드 브랜드별 유효좌수 | tbdaaat05 | ok |
| 15 | 16 | 26년 6월 법인회원 유실적 업체수 | tbdaa1d12 | ok |
| 16 | 17 | 26년 6월 브랜드별 신용·체크 유효좌수 | tbdaaat05 | ok |
| 17 | 18 | cpc08201 상품 상반기 이용금액 상위 10개사 | tmdaa3e16 + tbdaa1d12 | ok |
| 18 | 19 | (18행과 동일 질의) | tmdaa3e16 + tbdaa1d12 | 중복 |
| 19 | 20 | (카드명 누락) 26년 1~6월 이용금액 | tmdaa3e16 + tbdaada72 | **추가 입력 필요** |
| 20 | 21 | 2026년 기업카드 발급 이력 업체수·좌수 | tbdaaat05 + tbdaaat03 | ok |
| 21 | 22 | 26년 1~3월 발급 후 4~6월 해지 좌수·업체수 | tbdaaat05 + tbdaaat03 | ok |
| 22 | 23 | 2026년 해외 최다 이용월과 금액 | tbdaabt08 + tbdaaat03 | 가정 포함 |
| 23 | 24 | 26년 6월 vs 5월 법인회원 평균 이용금액 | tbdaa1d12 | 가정 포함 |

23건 중 12건은 기존 semantic query contract·verified query에 그대로 대응하고, 11건은 이번에 새로 작성했다.
질의 원문 기준으로 3·4행, 18·19행은 표현만 다른 같은 질문이라 같은 SQL을 쓴다(고유 SQL 21종).

## 판단이 갈릴 수 있는 지점

### 워크북 5·6행 — 원천을 기업 월 스냅샷이 아니라 매출전표로 잡은 이유

5행만 보면 `tbdaa1d12`의 금월신용/체크카드이용금액으로 업체별 분기 합계를 내는 편이 싸다.
그런데 6행이 이어서 **결제처 업종(국세·지방세·4대보험·상품권)과 카드 상품(기업오토·KB계열사)** 을
제외하라고 요구한다. 이 축은 기업 월 스냅샷에 없고 전표에만 있으므로, 두 질의가 같은 골격을 쓰도록
`tbdaabt30`(매출전표) 기준으로 통일하고 `기업고객식별자`로 집계했다.

제외 코드는 원천 정의서에 명시된 값만 하드코딩했다.

| 제외 대상 | 가맹점업종코드 | 근거 |
|---|---|---|
| 국세 | 8219 | `tbdaa1d12.금월국세이용금액` 정의 |
| 지방세 | 8220 | `tbdaa1d12.금월지방세이용금액` 정의 |
| 4대보험 | 8099 | `tbdaa1d12.금월4대보험이용금액` 정의 |
| 상품권·전사상품권 | 4200, 4124, 4125, 4126 | `tmdaaus01.가맹점상품권업종여부` 정의 |

**기업오토와 KB계열사 기업카드는 코드북이 없다.** semantic layer 31개 테이블에 해당 상품군을 식별하는
코드나 플래그가 없어서, 상품 마스터(`tbdaada72`)의 상품명 4종에 `'오토'`, `'계열사'` 부분일치를 적용한
`excluded_products` CTE로 처리했다. 운영에서 상품코드 목록을 받으면 이 CTE를 코드 `IN` 조건으로
바꾸는 것이 정확하다. 검수 시 이 부분을 먼저 확인해 달라.

취소 전표 순액 처리는 canonical metric `카드매출금액`(= `SUM(tbdaabt30.매출금액)`, required_filters 없음)을
따라 적용하지 않았다. 순액 기준이 필요하면 전표취소구분코드 규칙을 업무에서 확정해야 한다.

### 워크북 9행 — 공헌이익율은 SQL을 만들지 않았다

대손비용률은 만들었다. ambiguity_rules가 "대손비용률을 명시하면 월초/월말 충당금과 상각 기반으로
분리한다"고 규정하므로, 다음 식으로 계산한다.

```
대손비용   = 월말 기대대손충당금(202606) - 월초 기대대손충당금(202605) + 당월 상각액(202606 특수채권편입원금)
대손비용률 = 대손비용 / ((월말 원화대출잔액 + 월초 원화대출잔액) / 2)
```

참고로 월말 충당금 / 원화대출잔액인 **대손충당금률** 도 같이 반환해 두 지표를 구분할 수 있게 했다.

**공헌이익율은 원천이 없어 생성하지 않았다.** 기업고객 단위의 수익·비용·경제적이익 금액 컬럼이
31개 테이블에 존재하지 않는다. `tbdaa1d12.사업자여전법규제1위반여부`는 "총수익 < 총비용" 판정 결과의
Y/N 플래그일 뿐 금액이 아니어서 비율 계산에 쓸 수 없다. 산출하려면 다음이 추가로 필요하다.

- 기업고객별 카드수익(가맹점수수료 배분·할부수수료·CA수수료) 집계 원천
- 법인회원 경제적이익금액(캐시백·포인트·마일리지) 월별 원천
- 기업고객별 변동비·마케팅비 배분 규칙

임의 대체 공식을 만들면 정답 세트가 오염되므로 비워 두고 요건으로 남겼다.

### 워크북 15·17행 — 카드브랜드 코드북

원천 정의서에는 1~4만 있었으나 현업 확인으로 **5 UPI&글로벌, 6 아맥스** 를 추가해 6종 전체를 매핑했다.
질문이 열거한 "브랜드, 비자, 마스터, UPI, Amex, JCB"가 모두 코드값을 갖는다.

| 코드 | 브랜드 | 출처 |
|---|---|---|
| 1 | 로칼(국민) | 원천 정의서 |
| 2 | 마스터 | 원천 정의서 |
| 3 | 비자 | 원천 정의서 |
| 4 | JCB | 원천 정의서 |
| 5 | UPI&글로벌 | 현업 확인 |
| 6 | 아맥스 | 현업 확인 |

이 코드북은 골든셋뿐 아니라 `semantic_layer.yaml`에도 반영했다(아래 "semantic layer 반영" 참고).
코드북에 없는 값이 나오면 `ELSE`로 원본 코드를 노출한다.
"KB국민"은 당사 기준을 뜻한다고 보고 `KB카드BC카드구분코드` 필터는 넣지 않았다(질문에 BC 제외 조건 없음).

### 워크북 20행 — 카드명이 비어 있다

원문이 `" 카드의 2026년 1월부터 6월까지의 이용금액을 알려줘"` 로, 상품명이 누락됐다.
ambiguity_rules에 따라 임의 상품을 가정하지 않고 `<상품명>` 플레이스홀더가 든 템플릿을 정답으로 뒀다.
상품명을 받으면 문자열만 치환해 실행하면 된다. 워크북 원본에서 셀이 잘린 것으로 보이므로
원 질의를 확인해 달라.

### 그 밖의 해석 결정

- **3·4행 "당사 관리기업"**: 관리기업 범위는 로그인 사용자 권한에 종속돼 SQL로 재현할 수 없다.
  기업명 부분일치만 적용했고, 범위 제한이 필요하면 사업자등록번호 목록을 VALUES CTE로 추가한다.
- **2행 "신규로 모집한"**: `tbdaaat03.회원최초신규발급년월일`이 20260101~20260630인 기업회원으로 정의했다.
  모집 채널·모집인 기준(`tddaa3e21.최초모집회원여부`)이 필요하면 원천을 바꿔야 한다.
- **2행 신용·체크 구분**: `tbdaa1d12`의 금월신용/체크카드이용금액 정의(체크 = 상품중분류구분코드 CP53·CP54)를
  `tmdaa3e16.금월이용합계금액`에 그대로 적용해 나눴다.
- **10행 "체크카드만 보유"**: 상반기 시작월인 202601 카드 월실적에서 유효신용카드 0좌·유효체크카드 1좌 이상인
  회원으로 판정했다. `tbdaaat05`는 최신 스냅샷만 있어 과거 시점 보유 판정에 쓸 수 없다.
- **14행 "매출액이 가장 높은"**: 기준월이 없어 `tmdaa5e11`의 최신 기준년월을 쓰고 결과에 기준년월을 함께 반환한다.
- **23행 "해외업종"**: canonical metric 해외매출금액(`tbdaabt08.매출금액`)을 쓰되, 이 테이블에는
  개인기업구분코드가 없어 `tbdaaat03`과 회원일련번호로 조인해 기업회원으로 한정했다.
- **24행 "평균 이용금액"**: 회원 1개사당 평균으로 해석하고, 분모 해석이 갈리므로 전체 법인회원 기준 평균과
  유실적 회원 기준 평균을 함께 반환한다.

## semantic layer 반영

카드브랜드 코드북은 이 골든셋에만 두면 다음 질의에서 또 틀리므로 semantic layer에 SSOT로 넣었다.

| 위치 | 변경 |
|---|---|
| `semantic_layer.yaml` 테이블 컬럼 8곳 | `카드브랜드구분코드`에 `value_semantics` 6종 + `value_semantics_provenance: user_provided_business_codebook` 추가. 대상 테이블은 tbdaaat05, tmdaa3e16, tddaa3d01, tddaa3e21, tddaa3e23, tddaa3l01, tbdaabt30, tbdaabt08 |
| `semantic_layer.yaml` `semantic_attributes` | `card_brand` 속성 신설. aliases에 로칼·로컬·UPI·글로벌·아맥스·Amex를 넣어 브랜드 표현이 코드로 해석되게 하고, 8개 테이블 source_mappings와 semantic_cautions를 함께 정의 |
| `semantic_layer.yaml` `corporate_valid_card_count_by_brand_at_month` | 계약의 `codebook`을 6종으로 확장 |
| `sql_verified_queries.yaml` 같은 이름의 verified query | CASE에 `WHEN '5' THEN 'UPI&글로벌'`, `WHEN '6' THEN '아맥스'` 추가 |
| `tests/test_card_product_vqs.py` | 생성 SQL이 5·6 매핑을 포함하는지 검사하는 assert 2줄 추가 |
| `tests/fixtures/semantic_layer_golden_v1.jsonl` | 해당 verified query를 참조하는 24개 레코드의 `template_sha256`을 새 SQL 해시로 갱신 |

`semantic_layer_golden_v1.jsonl`은 verified query 템플릿의 SHA256을 고정해 두고 테스트가 대조하므로,
verified query SQL을 바꾸면 이 해시도 함께 갱신해야 한다. 갱신하지 않으면
`test_semantic_golden_set.py`와 `test_semantic_golden_runner.py`가 해시 불일치로 실패한다.

`card_brand` 속성을 둔 이유는 브랜드별 좌수 계약 하나만 고치면 브랜드별 이용금액·연체 같은 다른 질의에서
같은 코드북이 다시 필요해지기 때문이다. 이제 카드 보유(tbdaaat05), 이용금액(tbdaabt30·tbdaabt08·tmdaa3e16),
연체(tddaa3l01)까지 같은 코드북을 참조한다.

## 공통 규칙

- 기간은 절대 연월 리터럴만 쓴다(`'202601'`, `'20260630'`). "현재·최신"은 데이터의 `MAX(기준년월일)`·
  `MAX(실적기준년월일)`로 표현하고 조회기준일을 결과에 포함한다.
- 일·월 스냅샷(`tbdaa1d12`, `tmdaaus01`, `tbdaaat05`)은 고객·가맹점×기간 최신행 1건으로 축소한 뒤 집계한다.
- 신용+체크(카드 종류 축)와 개별+공용(소유 형태 축)을 함께 더하지 않는다.
- 가맹점 월매출은 `tmdaa5e11`의 일시불+할부이며 `tmdaaus01.가맹점총지급금액`으로 대체하지 않는다.
- 상세 목록은 요청 건수가 없으면 `LIMIT 1000000`을 적용한다.

## 검증 범위

`python scripts/generate_workbook_goldenset.py --check`가 검사하는 것:

- 23건 전부 단일 읽기 전용 SELECT/WITH, 쓰기 키워드 0건, 스마트 따옴표 0건
- `sqlglot` 파서로 athena·trino·presto 3개 방언 전건 파싱 성공
- SQL이 참조하는 물리 테이블이 semantic layer에 존재하고 `expected_tables`와 정확히 일치
- SQL의 모든 한글 식별자가 참조 테이블의 실제 컬럼이거나 쿼리 안에서 정의한 alias
- `별칭."컬럼"` 형태는 별칭을 물리 테이블로 되돌려 **그 테이블에 실제 있는 컬럼인지** 개별 확인
  (이 검사가 성립하도록 한 쿼리 안에서 같은 별칭을 CTE와 물리 테이블에 겹쳐 쓰지 않는다)
- restricted 테이블(`tbdaaat18`) 미참조

**문법·스키마 검증이지 결과값 검증이 아니다.** 실제 행 수와 금액은 Athena 실행으로 확인해야 한다.
기존 러너를 그대로 쓸 수 있다.

```bash
python scripts/run_goldenset_answers.py \
  --cases tests/fixtures/corporate_sales_workbook_goldenset_v1.jsonl \
  --results reports/workbook-goldenset-answers.jsonl \
  --merged tests/fixtures/corporate_sales_workbook_goldenset_v1_answered.jsonl \
  --summary reports/workbook-goldenset-answers-summary.md \
  --preview-csv reports/workbook-goldenset-answers-preview.csv \
  --dry-run
```

실행 전에 검수가 필요한 항목은 6행(기업오토·KB계열사 상품코드), 9행(공헌이익율 원천),
20행(누락된 카드명) 세 가지다. 17행의 UPI·아맥스 코드값은 현업 확인으로 해소해 semantic layer에 반영했다.

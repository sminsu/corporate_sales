#!/usr/bin/env python3
"""통합 문서.xlsx 현업 질의 23건에 대한 Athena 정답 SQL 골든셋 생성기.

입력 질의는 `통합 문서.xlsx`(Sheet1 A2:A24)의 원문이고, 정답 SQL은
`semantic_layer.yaml`(테이블 grain·canonical metric·semantic query contract·
sql_generation_contract)과 `text2sql_agent/tools/sql_verified_queries.yaml`을
근거로 작성했다.

산출물
  - tests/fixtures/corporate_sales_workbook_goldenset_v1.jsonl  평가 러너 입력용
  - tests/fixtures/corporate_sales_workbook_goldenset_v1.csv    현업 검수용(UTF-8 BOM)

검증
  - sqlglot Trino 파서로 전건 파싱
  - 단일 읽기 전용 SELECT/WITH (쓰기 키워드 0건)
  - SQL의 모든 한글 식별자가 참조 테이블의 실제 컬럼이거나 쿼리 내 alias
  - 참조 테이블이 semantic_layer.yaml에 존재하고 restricted 테이블(tbdaaat18) 미사용

실행
  python scripts/generate_workbook_goldenset.py            # 생성 + 검증
  python scripts/generate_workbook_goldenset.py --check    # 검증만 (파일 미변경)
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_LAYER = ROOT / "semantic_layer.yaml"
OUT_JSONL = ROOT / "tests/fixtures/corporate_sales_workbook_goldenset_v1.jsonl"
OUT_CSV = ROOT / "tests/fixtures/corporate_sales_workbook_goldenset_v1.csv"

REFERENCE_DATE = "2026-08-10"
SQL_DIALECT = "athena_v3_trino"
RESTRICTED_TABLES = {"tbdaaat18"}

# =============================================================================
# 정답 SQL
# =============================================================================

Q01_NEW_MEMBER_USAGE = """
WITH new_members AS (
  SELECT DISTINCT m."회원일련번호"
  FROM tbdaaat03 m
  WHERE m."개인기업구분코드" = '2'
    AND m."회원최초신규발급년월일" BETWEEN '20260101' AND '20260630'
),
new_member_count AS (
  SELECT COUNT(*) AS "신규모집기업회원수"
  FROM new_members
),
monthly_usage AS (
  SELECT
    c."회원일련번호",
    CASE
      WHEN c."상품중분류구분코드" IN ('CP53', 'CP54') THEN 0
      ELSE COALESCE(c."금월이용합계금액", 0)
    END AS "신용카드이용금액",
    CASE
      WHEN c."상품중분류구분코드" IN ('CP53', 'CP54') THEN COALESCE(c."금월이용합계금액", 0)
      ELSE 0
    END AS "체크카드이용금액"
  FROM tmdaa3e16 c
  JOIN new_members n
    ON c."회원일련번호" = n."회원일련번호"
  WHERE c."개인기업구분코드" = '2'
    AND c."기준년월" BETWEEN '202601' AND '202606'
),
member_usage AS (
  SELECT
    u."회원일련번호",
    SUM(u."신용카드이용금액") AS "신용카드이용금액",
    SUM(u."체크카드이용금액") AS "체크카드이용금액",
    SUM(u."신용카드이용금액" + u."체크카드이용금액") AS "전체이용금액"
  FROM monthly_usage u
  GROUP BY u."회원일련번호"
),
usage_total AS (
  SELECT
    COUNT(*) FILTER (WHERE g."전체이용금액" > 0) AS "이용회원수",
    SUM(g."신용카드이용금액") AS "신용카드이용금액",
    SUM(g."체크카드이용금액") AS "체크카드이용금액",
    SUM(g."전체이용금액") AS "전체이용금액"
  FROM member_usage g
)
SELECT
  '202601' AS "조회시작월",
  '202606' AS "조회종료월",
  c."신규모집기업회원수",
  COALESCE(t."이용회원수", 0) AS "이용회원수",
  COALESCE(t."신용카드이용금액", 0) AS "신용카드이용금액",
  COALESCE(t."체크카드이용금액", 0) AS "체크카드이용금액",
  COALESCE(t."전체이용금액", 0) AS "전체이용금액"
FROM new_member_count c
CROSS JOIN usage_total t
""".strip()

Q02_NAMED_HALF_YEAR_USAGE = """
WITH monthly_latest AS (
  SELECT
    SUBSTR(a."기준년월일", 1, 6) AS "기준년월",
    a."고객식별자",
    a."사업자등록번호",
    a."기업명",
    a."금월신용카드이용금액",
    a."금월체크카드이용금액",
    ROW_NUMBER() OVER (
      PARTITION BY SUBSTR(a."기준년월일", 1, 6), a."고객식별자"
      ORDER BY a."기준년월일" DESC
    ) AS rn
  FROM tbdaa1d12 a
  WHERE LOWER(COALESCE(a."기업명", '')) LIKE LOWER('%쿠팡%')
    AND SUBSTR(a."기준년월일", 1, 6) BETWEEN '202501' AND '202608'
)
SELECT
  SUBSTR(m."기준년월", 1, 4) AS "기준연도",
  CASE
    WHEN SUBSTR(m."기준년월", 5, 2) BETWEEN '01' AND '06' THEN '상반기'
    ELSE '하반기'
  END AS "반기",
  MAX(m."기준년월") AS "최종반영년월",
  COUNT(DISTINCT m."고객식별자") AS "기업수",
  SUM(COALESCE(m."금월신용카드이용금액", 0)) AS "신용카드이용금액",
  SUM(COALESCE(m."금월체크카드이용금액", 0)) AS "체크카드이용금액",
  SUM(
    COALESCE(m."금월신용카드이용금액", 0)
    + COALESCE(m."금월체크카드이용금액", 0)
  ) AS "총이용금액"
FROM monthly_latest m
WHERE m.rn = 1
GROUP BY
  SUBSTR(m."기준년월", 1, 4),
  CASE
    WHEN SUBSTR(m."기준년월", 5, 2) BETWEEN '01' AND '06' THEN '상반기'
    ELSE '하반기'
  END
ORDER BY 1, 2
""".strip()

Q04_TOP10_QUARTERLY = """
WITH corporate_slips AS (
  SELECT
    t."기업고객식별자" AS "고객식별자",
    t."기준년월",
    COALESCE(t."매출금액", 0) AS "매출금액"
  FROM tbdaabt30 t
  WHERE t."개인기업구분코드" = '2'
    AND t."기준년월" BETWEEN '202601' AND '202612'
    AND t."기업고객식별자" IS NOT NULL
),
company_quarter AS (
  SELECT
    s."고객식별자",
    CASE
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('01', '02', '03') THEN 1
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('04', '05', '06') THEN 2
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('07', '08', '09') THEN 3
      ELSE 4
    END AS "분기",
    SUM(s."매출금액") AS "이용금액"
  FROM corporate_slips s
  GROUP BY
    s."고객식별자",
    CASE
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('01', '02', '03') THEN 1
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('04', '05', '06') THEN 2
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('07', '08', '09') THEN 3
      ELSE 4
    END
),
company_total AS (
  SELECT
    q."고객식별자",
    SUM(q."이용금액") AS "연간이용금액",
    ROW_NUMBER() OVER (ORDER BY SUM(q."이용금액") DESC, q."고객식별자") AS "순위"
  FROM company_quarter q
  GROUP BY q."고객식별자"
),
company_profile AS (
  SELECT
    a."고객식별자",
    a."사업자등록번호",
    a."기업명",
    ROW_NUMBER() OVER (
      PARTITION BY a."고객식별자"
      ORDER BY a."기준년월일" DESC
    ) AS rn
  FROM tbdaa1d12 a
  WHERE SUBSTR(a."기준년월일", 1, 6) BETWEEN '202601' AND '202612'
)
SELECT
  t."순위",
  t."고객식별자",
  cp."사업자등록번호",
  cp."기업명",
  SUM(CASE WHEN q."분기" = 1 THEN q."이용금액" ELSE 0 END) AS "1분기이용금액",
  SUM(CASE WHEN q."분기" = 2 THEN q."이용금액" ELSE 0 END) AS "2분기이용금액",
  SUM(CASE WHEN q."분기" = 3 THEN q."이용금액" ELSE 0 END) AS "3분기이용금액",
  SUM(CASE WHEN q."분기" = 4 THEN q."이용금액" ELSE 0 END) AS "4분기이용금액",
  MAX(t."연간이용금액") AS "연간이용금액"
FROM company_total t
JOIN company_quarter q
  ON t."고객식별자" = q."고객식별자"
LEFT JOIN company_profile cp
  ON t."고객식별자" = cp."고객식별자"
  AND cp.rn = 1
WHERE t."순위" <= 10
GROUP BY
  t."순위",
  t."고객식별자",
  cp."사업자등록번호",
  cp."기업명"
ORDER BY t."순위"
""".strip()

Q05_TOP10_QUARTERLY_EXCLUDED = r"""
WITH excluded_products AS (
  SELECT DISTINCT p."상품코드"
  FROM tbdaada72 p
  WHERE REGEXP_REPLACE(LOWER(COALESCE(p."한글상품명", '')), '\s+', '') LIKE '%오토%'
     OR REGEXP_REPLACE(LOWER(COALESCE(p."대표카드상품명", '')), '\s+', '') LIKE '%오토%'
     OR REGEXP_REPLACE(LOWER(COALESCE(p."고객안내상품명", '')), '\s+', '') LIKE '%오토%'
     OR REGEXP_REPLACE(LOWER(COALESCE(p."한글상품명", '')), '\s+', '') LIKE '%계열사%'
     OR REGEXP_REPLACE(LOWER(COALESCE(p."대표카드상품명", '')), '\s+', '') LIKE '%계열사%'
     OR REGEXP_REPLACE(LOWER(COALESCE(p."고객안내상품명", '')), '\s+', '') LIKE '%계열사%'
),
corporate_slips AS (
  SELECT
    t."기업고객식별자" AS "고객식별자",
    t."기준년월",
    COALESCE(t."매출금액", 0) AS "매출금액"
  FROM tbdaabt30 t
  WHERE t."개인기업구분코드" = '2'
    AND t."기준년월" BETWEEN '202601' AND '202612'
    AND t."기업고객식별자" IS NOT NULL
    AND COALESCE(t."가맹점업종코드", '') NOT IN ('8219', '8220', '8099', '4200', '4124', '4125', '4126')
    AND NOT EXISTS (
      SELECT 1
      FROM excluded_products e
      WHERE e."상품코드" = t."상품코드"
    )
),
company_quarter AS (
  SELECT
    s."고객식별자",
    CASE
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('01', '02', '03') THEN 1
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('04', '05', '06') THEN 2
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('07', '08', '09') THEN 3
      ELSE 4
    END AS "분기",
    SUM(s."매출금액") AS "이용금액"
  FROM corporate_slips s
  GROUP BY
    s."고객식별자",
    CASE
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('01', '02', '03') THEN 1
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('04', '05', '06') THEN 2
      WHEN SUBSTR(s."기준년월", 5, 2) IN ('07', '08', '09') THEN 3
      ELSE 4
    END
),
company_total AS (
  SELECT
    q."고객식별자",
    SUM(q."이용금액") AS "연간이용금액",
    ROW_NUMBER() OVER (ORDER BY SUM(q."이용금액") DESC, q."고객식별자") AS "순위"
  FROM company_quarter q
  GROUP BY q."고객식별자"
),
company_profile AS (
  SELECT
    a."고객식별자",
    a."사업자등록번호",
    a."기업명",
    ROW_NUMBER() OVER (
      PARTITION BY a."고객식별자"
      ORDER BY a."기준년월일" DESC
    ) AS rn
  FROM tbdaa1d12 a
  WHERE SUBSTR(a."기준년월일", 1, 6) BETWEEN '202601' AND '202612'
)
SELECT
  t."순위",
  t."고객식별자",
  cp."사업자등록번호",
  cp."기업명",
  SUM(CASE WHEN q."분기" = 1 THEN q."이용금액" ELSE 0 END) AS "1분기이용금액",
  SUM(CASE WHEN q."분기" = 2 THEN q."이용금액" ELSE 0 END) AS "2분기이용금액",
  SUM(CASE WHEN q."분기" = 3 THEN q."이용금액" ELSE 0 END) AS "3분기이용금액",
  SUM(CASE WHEN q."분기" = 4 THEN q."이용금액" ELSE 0 END) AS "4분기이용금액",
  MAX(t."연간이용금액") AS "연간이용금액"
FROM company_total t
JOIN company_quarter q
  ON t."고객식별자" = q."고객식별자"
LEFT JOIN company_profile cp
  ON t."고객식별자" = cp."고객식별자"
  AND cp.rn = 1
WHERE t."순위" <= 10
GROUP BY
  t."순위",
  t."고객식별자",
  cp."사업자등록번호",
  cp."기업명"
ORDER BY t."순위"
""".strip()


def half_year_decline_sql(limit: int | None) -> str:
    """현재 유효 기업카드 보유회원의 2025 상반기 대비 2026 상반기 이용금액 하락 명단."""
    tail = "" if limit is None else f"\nLIMIT {limit}"
    return f"""
WITH latest_month AS (
  SELECT MAX(SUBSTR(a."기준년월일", 1, 6)) AS "현재기준년월"
  FROM tbdaa1d12 a
),
current_ranked AS (
  SELECT
    a."기준년월일" AS "현재기준일",
    a."고객식별자",
    a."사업자등록번호",
    a."기업명",
    a."유효기업신용카드수",
    a."유효기업체크카드수",
    ROW_NUMBER() OVER (
      PARTITION BY a."고객식별자"
      ORDER BY a."기준년월일" DESC
    ) AS rn
  FROM tbdaa1d12 a
  CROSS JOIN latest_month l
  WHERE SUBSTR(a."기준년월일", 1, 6) = l."현재기준년월"
),
current_members AS (
  SELECT
    c."현재기준일",
    c."고객식별자",
    c."사업자등록번호",
    c."기업명"
  FROM current_ranked c
  WHERE c.rn = 1
    AND (
      COALESCE(c."유효기업신용카드수", 0)
      + COALESCE(c."유효기업체크카드수", 0)
    ) > 0
),
usage_ranked AS (
  SELECT
    SUBSTR(a."기준년월일", 1, 6) AS "기준년월",
    a."고객식별자",
    a."금월신용카드이용금액",
    a."금월체크카드이용금액",
    ROW_NUMBER() OVER (
      PARTITION BY a."고객식별자", SUBSTR(a."기준년월일", 1, 6)
      ORDER BY a."기준년월일" DESC
    ) AS rn
  FROM tbdaa1d12 a
  WHERE SUBSTR(a."기준년월일", 1, 6) BETWEEN '202501' AND '202506'
     OR SUBSTR(a."기준년월일", 1, 6) BETWEEN '202601' AND '202606'
),
monthly_usage AS (
  SELECT
    u."기준년월",
    u."고객식별자",
    COALESCE(u."금월신용카드이용금액", 0)
      + COALESCE(u."금월체크카드이용금액", 0) AS "전체이용금액"
  FROM usage_ranked u
  WHERE u.rn = 1
),
usage_totals AS (
  SELECT
    u."고객식별자",
    SUM(CASE WHEN u."기준년월" BETWEEN '202501' AND '202506' THEN u."전체이용금액" ELSE 0 END)
      AS "기준기간이용금액",
    SUM(CASE WHEN u."기준년월" BETWEEN '202601' AND '202606' THEN u."전체이용금액" ELSE 0 END)
      AS "대상기간이용금액"
  FROM monthly_usage u
  GROUP BY u."고객식별자"
),
declined AS (
  SELECT
    c."현재기준일",
    c."고객식별자",
    c."사업자등록번호",
    c."기업명",
    u."기준기간이용금액",
    u."대상기간이용금액",
    u."기준기간이용금액" - u."대상기간이용금액" AS "하락금액"
  FROM current_members c
  JOIN usage_totals u
    ON c."고객식별자" = u."고객식별자"
  WHERE u."대상기간이용금액" < u."기준기간이용금액"
)
SELECT
  COUNT(*) OVER () AS "하락기업회원수",
  d."현재기준일",
  d."고객식별자",
  d."사업자등록번호",
  d."기업명",
  d."기준기간이용금액" AS "2025년상반기이용금액",
  d."대상기간이용금액" AS "2026년상반기이용금액",
  d."하락금액"
FROM declined d
ORDER BY d."하락금액" DESC, d."기업명", d."고객식별자"{tail}
""".strip()


Q08_LOSS_COST_RATE = """
WITH company AS (
  SELECT
    r."고객식별자",
    r."사업자등록번호",
    r."기업명"
  FROM (
    SELECT
      a."고객식별자",
      a."사업자등록번호",
      a."기업명",
      ROW_NUMBER() OVER (
        PARTITION BY a."고객식별자"
        ORDER BY a."기준년월일" DESC
      ) AS rn
    FROM tbdaa1d12 a
    WHERE SUBSTR(a."기준년월일", 1, 6) = '202606'
      AND LOWER(COALESCE(a."기업명", '')) LIKE LOWER('%쿠팡%')
  ) r
  WHERE r.rn = 1
),
allowance_end AS (
  SELECT
    w."고객식별자",
    SUM(COALESCE(w."기대대손충당금", 0)) AS "월말기대대손충당금",
    SUM(COALESCE(w."원화대출잔액", 0)) AS "월말원화대출잔액"
  FROM tbmewcm94 w
  JOIN company c
    ON w."고객식별자" = c."고객식별자"
  WHERE w."기준년월" = '202606'
    AND w."개인기업구분코드" = '2'
  GROUP BY w."고객식별자"
),
allowance_begin AS (
  SELECT
    w."고객식별자",
    SUM(COALESCE(w."기대대손충당금", 0)) AS "월초기대대손충당금",
    SUM(COALESCE(w."원화대출잔액", 0)) AS "월초원화대출잔액"
  FROM tbmewcm94 w
  JOIN company c
    ON w."고객식별자" = c."고객식별자"
  WHERE w."기준년월" = '202605'
    AND w."개인기업구분코드" = '2'
  GROUP BY w."고객식별자"
),
write_off AS (
  SELECT
    s."고객식별자",
    SUM(COALESCE(s."특수채권편입원금", 0)) AS "당월상각원금"
  FROM tbmaisd06 s
  JOIN company c
    ON s."고객식별자" = c."고객식별자"
  WHERE s."특수채권편입기준년월일" BETWEEN '20260601' AND '20260630'
    AND s."개인기업구분코드" = '2'
  GROUP BY s."고객식별자"
)
SELECT
  '202606' AS "기준년월",
  c."고객식별자",
  c."사업자등록번호",
  c."기업명",
  COALESCE(b."월초기대대손충당금", 0) AS "월초기대대손충당금",
  COALESCE(e."월말기대대손충당금", 0) AS "월말기대대손충당금",
  COALESCE(w."당월상각원금", 0) AS "당월상각원금",
  COALESCE(b."월초원화대출잔액", 0) AS "월초원화대출잔액",
  COALESCE(e."월말원화대출잔액", 0) AS "월말원화대출잔액",
  COALESCE(e."월말기대대손충당금", 0)
    - COALESCE(b."월초기대대손충당금", 0)
    + COALESCE(w."당월상각원금", 0) AS "대손비용",
  ROUND(
    CAST(
      COALESCE(e."월말기대대손충당금", 0)
      - COALESCE(b."월초기대대손충당금", 0)
      + COALESCE(w."당월상각원금", 0) AS DOUBLE
    )
    / NULLIF(
      (CAST(COALESCE(e."월말원화대출잔액", 0) AS DOUBLE)
       + CAST(COALESCE(b."월초원화대출잔액", 0) AS DOUBLE)) / 2.0,
      0.0
    ) * 100,
    4
  ) AS "대손비용률_퍼센트",
  ROUND(
    CAST(COALESCE(e."월말기대대손충당금", 0) AS DOUBLE)
    / NULLIF(CAST(COALESCE(e."월말원화대출잔액", 0) AS DOUBLE), 0.0) * 100,
    4
  ) AS "대손충당금률_퍼센트"
FROM company c
LEFT JOIN allowance_end e
  ON c."고객식별자" = e."고객식별자"
LEFT JOIN allowance_begin b
  ON c."고객식별자" = b."고객식별자"
LEFT JOIN write_off w
  ON c."고객식별자" = w."고객식별자"
ORDER BY c."고객식별자"
""".strip()

Q09_CHECK_ONLY_TO_CREDIT = """
WITH check_only_members AS (
  SELECT c."회원일련번호"
  FROM tmdaa3e16 c
  WHERE c."개인기업구분코드" = '2'
    AND c."기준년월" = '202601'
  GROUP BY c."회원일련번호"
  HAVING SUM(CASE WHEN c."유효신용카드여부" = '1' THEN 1 ELSE 0 END) = 0
     AND SUM(CASE WHEN c."유효체크카드여부" = '1' THEN 1 ELSE 0 END) > 0
),
latest_snapshot AS (
  SELECT MAX(k."실적기준년월일") AS "조회기준일"
  FROM tbdaaat05 k
),
new_credit_cards AS (
  SELECT DISTINCT
    k."회원일련번호",
    k."카드구분키번호",
    k."카드신규발급년월일"
  FROM tbdaaat05 k
  JOIN check_only_members m
    ON k."회원일련번호" = m."회원일련번호"
  CROSS JOIN latest_snapshot s
  WHERE k."실적기준년월일" = s."조회기준일"
    AND k."개인기업구분코드" = '2'
    AND k."상품중분류구분코드" NOT IN ('CP53', 'CP54')
    AND k."카드신규발급년월일" BETWEEN '20260101' AND '20260630'
),
credit_usage AS (
  SELECT
    n."회원일련번호",
    n."카드구분키번호",
    SUM(COALESCE(p."금월이용합계건수", 0)) AS "이용건수",
    SUM(COALESCE(p."금월이용합계금액", 0)) AS "이용금액"
  FROM new_credit_cards n
  JOIN tmdaa3e16 p
    ON p."회원일련번호" = n."회원일련번호"
    AND p."카드구분키번호" = n."카드구분키번호"
  WHERE p."기준년월" BETWEEN '202601' AND '202606'
  GROUP BY n."회원일련번호", n."카드구분키번호"
)
SELECT
  '202601' AS "조회시작월",
  '202606' AS "조회종료월",
  COUNT(DISTINCT u."회원일련번호") AS "신규발급회원수",
  COUNT(*) AS "신규발급신용카드좌수",
  COUNT(DISTINCT CASE WHEN u."이용금액" > 0 THEN u."회원일련번호" END) AS "이용회원수",
  COUNT(*) FILTER (WHERE u."이용금액" > 0) AS "이용신용카드좌수",
  SUM(CASE WHEN u."이용금액" > 0 THEN u."이용건수" ELSE 0 END) AS "이용건수",
  SUM(CASE WHEN u."이용금액" > 0 THEN u."이용금액" ELSE 0 END) AS "이용금액"
FROM credit_usage u
""".strip()

Q10_NEW_CHECK_CARD_ISSUANCE = r"""
WITH latest_card_snapshot AS (
  SELECT MAX(c."실적기준년월일") AS "조회기준일"
  FROM tbdaaat05 c
),
current_corporate_members AS (
  SELECT
    m."회원일련번호",
    m."고객식별자"
  FROM tbdaaat03 m
  WHERE m."개인기업구분코드" = '2'
    AND (
      COALESCE(m."유효신용카드수", 0)
      + COALESCE(m."유효체크카드수", 0)
    ) > 0
),
company_ranked AS (
  SELECT
    a."고객식별자",
    a."기업명",
    a."사업자등록번호",
    ROW_NUMBER() OVER (
      PARTITION BY a."고객식별자"
      ORDER BY a."기준년월일" DESC
    ) AS rn
  FROM tbdaa1d12 a
  WHERE SUBSTR(a."기준년월일", 1, 6) BETWEEN '202601' AND '202608'
),
issued_cards AS (
  SELECT DISTINCT
    s."조회기준일",
    m."고객식별자",
    c."회원일련번호",
    c."카드구분키번호",
    c."상품코드",
    COALESCE(p."한글상품명", p."대표카드상품명", p."고객안내상품명") AS "상품명",
    c."카드신규발급년월일"
  FROM tbdaaat05 c
  JOIN current_corporate_members m
    ON c."회원일련번호" = m."회원일련번호"
  JOIN tbdaada72 p
    ON c."상품코드" = p."상품코드"
  CROSS JOIN latest_card_snapshot s
  WHERE c."실적기준년월일" = s."조회기준일"
    AND c."개인기업구분코드" = '2'
    AND c."상품중분류구분코드" IN ('CP53', 'CP54')
    AND c."카드신규발급년월일" BETWEEN '20260101' AND '20260630'
    AND (
      REGEXP_REPLACE(LOWER(COALESCE(p."한글상품명", '')), '\s+', '') LIKE '%전기요금%'
      OR REGEXP_REPLACE(LOWER(COALESCE(p."대표카드상품명", '')), '\s+', '') LIKE '%전기요금%'
      OR REGEXP_REPLACE(LOWER(COALESCE(p."고객안내상품명", '')), '\s+', '') LIKE '%전기요금%'
      OR REGEXP_REPLACE(LOWER(COALESCE(p."통장장표인자상품명", '')), '\s+', '') LIKE '%전기요금%'
    )
)
SELECT
  COUNT(*) OVER () AS "신규발급좌수",
  i."조회기준일",
  i."고객식별자",
  co."기업명",
  co."사업자등록번호",
  i."회원일련번호",
  i."카드구분키번호",
  i."상품코드",
  i."상품명",
  i."카드신규발급년월일"
FROM issued_cards i
LEFT JOIN company_ranked co
  ON i."고객식별자" = co."고객식별자"
  AND co.rn = 1
ORDER BY
  i."카드신규발급년월일",
  co."기업명",
  i."회원일련번호",
  i."카드구분키번호"
LIMIT 1000000
""".strip()

Q11_MERCHANT_MONTHLY_SALES = """
SELECT
  a."기준년월",
  a."가맹점번호",
  a."가맹점명",
  COALESCE(a."가맹점일시불매출금액", 0) AS "일시불매출금액",
  COALESCE(a."가맹점할부매출금액", 0) AS "할부매출금액",
  COALESCE(a."가맹점일시불매출금액", 0)
    + COALESCE(a."가맹점할부매출금액", 0) AS "월별매출금액"
FROM tmdaa5e11 a
WHERE a."가맹점번호" = '59593113'
ORDER BY a."기준년월"
""".strip()

Q12_PRODUCT_VALID_CARD_COUNT = r"""
WITH latest_snapshot AS (
  SELECT MAX(c."실적기준년월일") AS "조회기준일"
  FROM tbdaaat05 c
),
matched_cards AS (
  SELECT DISTINCT
    c."회원일련번호",
    c."카드구분키번호"
  FROM tbdaaat05 c
  JOIN tbdaada72 p
    ON c."상품코드" = p."상품코드"
  CROSS JOIN latest_snapshot s
  WHERE c."실적기준년월일" = s."조회기준일"
    AND c."개인기업구분코드" = '2'
    AND (
      c."유효신용카드여부" = '1'
      OR c."유효체크카드여부" = '1'
    )
    AND (
      REGEXP_REPLACE(LOWER(COALESCE(p."한글상품명", '')), '\s+', '') LIKE '%kb아시아나%'
      OR REGEXP_REPLACE(LOWER(COALESCE(p."대표카드상품명", '')), '\s+', '') LIKE '%kb아시아나%'
      OR REGEXP_REPLACE(LOWER(COALESCE(p."고객안내상품명", '')), '\s+', '') LIKE '%kb아시아나%'
      OR REGEXP_REPLACE(LOWER(COALESCE(p."통장장표인자상품명", '')), '\s+', '') LIKE '%kb아시아나%'
    )
)
SELECT
  s."조회기준일",
  COUNT(m."카드구분키번호") AS "유효카드수"
FROM latest_snapshot s
LEFT JOIN matched_cards m
  ON TRUE
GROUP BY s."조회기준일"
""".strip()

Q13_TOP_MERCHANTS_WITH_CORPORATE_CARD = """
WITH latest_month AS (
  SELECT MAX(s."기준년월") AS "기준년월"
  FROM tmdaa5e11 s
),
merchant_ranked AS (
  SELECT
    a."가맹점번호",
    a."가맹점명",
    a."기업고객식별자",
    a."사업자등록번호",
    a."한글시도명",
    a."한글시군구명",
    a."가맹점업종코드",
    a."유효기업신용카드수",
    a."유효기업체크카드수",
    ROW_NUMBER() OVER (
      PARTITION BY a."기준년월", a."가맹점번호"
      ORDER BY a."기준년월일" DESC
    ) AS rn
  FROM tmdaaus01 a
  CROSS JOIN latest_month l
  WHERE a."기준년월" = l."기준년월"
),
merchant_month AS (
  SELECT
    r."가맹점번호",
    r."가맹점명",
    r."기업고객식별자",
    r."사업자등록번호",
    r."한글시도명",
    r."한글시군구명",
    r."가맹점업종코드",
    r."유효기업신용카드수",
    r."유효기업체크카드수"
  FROM merchant_ranked r
  WHERE r.rn = 1
),
merchant_sales AS (
  SELECT
    s."가맹점번호",
    SUM(
      COALESCE(s."가맹점일시불매출금액", 0)
      + COALESCE(s."가맹점할부매출금액", 0)
    ) AS "월매출액"
  FROM tmdaa5e11 s
  CROSS JOIN latest_month l
  WHERE s."기준년월" = l."기준년월"
  GROUP BY s."가맹점번호"
)
SELECT
  l."기준년월",
  m."가맹점번호",
  m."가맹점명",
  m."기업고객식별자",
  m."사업자등록번호",
  m."한글시도명",
  m."한글시군구명",
  m."가맹점업종코드",
  COALESCE(m."유효기업신용카드수", 0) AS "유효기업신용카드수",
  COALESCE(m."유효기업체크카드수", 0) AS "유효기업체크카드수",
  s."월매출액"
FROM merchant_month m
JOIN merchant_sales s
  ON m."가맹점번호" = s."가맹점번호"
CROSS JOIN latest_month l
WHERE (
    COALESCE(m."유효기업신용카드수", 0)
    + COALESCE(m."유효기업체크카드수", 0)
  ) > 0
ORDER BY s."월매출액" DESC, m."가맹점번호"
LIMIT 10
""".strip()

Q14_BRAND_VALID_CARD_COUNT = """
WITH target_snapshot AS (
  SELECT MAX(c."실적기준년월일") AS "조회기준일"
  FROM tbdaaat05 c
  WHERE c."실적기준년월일" BETWEEN '20260601' AND '20260630'
),
valid_cards AS (
  SELECT DISTINCT
    c."회원일련번호",
    c."카드구분키번호",
    c."카드브랜드구분코드"
  FROM tbdaaat05 c
  CROSS JOIN target_snapshot s
  WHERE c."실적기준년월일" = s."조회기준일"
    AND c."개인기업구분코드" = '2'
    AND (
      c."유효신용카드여부" = '1'
      OR c."유효체크카드여부" = '1'
    )
)
SELECT
  s."조회기준일",
  v."카드브랜드구분코드",
  CASE v."카드브랜드구분코드"
    WHEN '1' THEN '로칼(국민)'
    WHEN '2' THEN '마스터'
    WHEN '3' THEN '비자'
    WHEN '4' THEN 'JCB'
    WHEN '5' THEN 'UPI&글로벌'
    WHEN '6' THEN '아맥스'
    ELSE COALESCE(v."카드브랜드구분코드", '미분류')
  END AS "카드브랜드명",
  COUNT(*) AS "유효좌수"
FROM target_snapshot s
JOIN valid_cards v
  ON TRUE
GROUP BY
  s."조회기준일",
  v."카드브랜드구분코드"
ORDER BY v."카드브랜드구분코드"
""".strip()

Q15_CORPORATE_MEMBER_WITH_USAGE = """
WITH ranked AS (
  SELECT
    a."고객식별자",
    a."금월신용카드이용금액",
    a."금월체크카드이용금액",
    ROW_NUMBER() OVER (
      PARTITION BY a."고객식별자"
      ORDER BY a."기준년월일" DESC
    ) AS rn
  FROM tbdaa1d12 a
  WHERE a."기준년월일" BETWEEN '20260601' AND '20260630'
)
SELECT
  COUNT(DISTINCT r."고객식별자") AS "유실적업체수"
FROM ranked r
WHERE r.rn = 1
  AND (
    COALESCE(r."금월신용카드이용금액", 0)
    + COALESCE(r."금월체크카드이용금액", 0)
  ) > 0
""".strip()

Q16_BRAND_VALID_CARD_COUNT_BY_TYPE = """
WITH target_snapshot AS (
  SELECT MAX(c."실적기준년월일") AS "조회기준일"
  FROM tbdaaat05 c
  WHERE c."실적기준년월일" BETWEEN '20260601' AND '20260630'
),
valid_cards AS (
  SELECT DISTINCT
    c."회원일련번호",
    c."카드구분키번호",
    c."카드브랜드구분코드",
    c."유효신용카드여부",
    c."유효체크카드여부"
  FROM tbdaaat05 c
  CROSS JOIN target_snapshot s
  WHERE c."실적기준년월일" = s."조회기준일"
    AND c."개인기업구분코드" = '2'
    AND (
      c."유효신용카드여부" = '1'
      OR c."유효체크카드여부" = '1'
    )
)
SELECT
  s."조회기준일",
  v."카드브랜드구분코드",
  CASE v."카드브랜드구분코드"
    WHEN '1' THEN '로칼(국민)'
    WHEN '2' THEN '마스터'
    WHEN '3' THEN '비자'
    WHEN '4' THEN 'JCB'
    WHEN '5' THEN 'UPI&글로벌'
    WHEN '6' THEN '아맥스'
    ELSE COALESCE(v."카드브랜드구분코드", '미분류')
  END AS "카드브랜드명",
  SUM(CASE WHEN v."유효신용카드여부" = '1' THEN 1 ELSE 0 END) AS "유효신용카드좌수",
  SUM(CASE WHEN v."유효체크카드여부" = '1' THEN 1 ELSE 0 END) AS "유효체크카드좌수",
  COUNT(*) AS "유효좌수"
FROM target_snapshot s
JOIN valid_cards v
  ON TRUE
GROUP BY
  s."조회기준일",
  v."카드브랜드구분코드"
ORDER BY v."카드브랜드구분코드"
""".strip()

Q17_PRODUCT_TOP10_COMPANIES = """
WITH product_cards AS (
  SELECT DISTINCT p."상품코드"
  FROM tbdaada72 p
  WHERE UPPER(p."상품코드") = UPPER('cpc08201')
),
company_usage AS (
  SELECT
    c."고객식별자",
    COUNT(DISTINCT CONCAT(c."회원일련번호", '-', c."카드구분키번호")) AS "이용카드좌수",
    SUM(COALESCE(c."금월이용합계건수", 0)) AS "이용건수",
    SUM(COALESCE(c."금월이용합계금액", 0)) AS "이용금액"
  FROM tmdaa3e16 c
  JOIN product_cards p
    ON c."상품코드" = p."상품코드"
  WHERE c."개인기업구분코드" = '2'
    AND c."기준년월" BETWEEN '202601' AND '202606'
  GROUP BY c."고객식별자"
),
company_profile AS (
  SELECT
    a."고객식별자",
    a."사업자등록번호",
    a."기업명",
    ROW_NUMBER() OVER (
      PARTITION BY a."고객식별자"
      ORDER BY a."기준년월일" DESC
    ) AS rn
  FROM tbdaa1d12 a
  WHERE SUBSTR(a."기준년월일", 1, 6) BETWEEN '202601' AND '202606'
)
SELECT
  u."고객식별자",
  cp."사업자등록번호",
  cp."기업명",
  u."이용카드좌수",
  u."이용건수",
  u."이용금액"
FROM company_usage u
LEFT JOIN company_profile cp
  ON u."고객식별자" = cp."고객식별자"
  AND cp.rn = 1
ORDER BY u."이용금액" DESC, u."고객식별자"
LIMIT 10
""".strip()

Q19_PRODUCT_USAGE_TEMPLATE = r"""
WITH product_cards AS (
  SELECT DISTINCT p."상품코드"
  FROM tbdaada72 p
  WHERE REGEXP_REPLACE(LOWER(COALESCE(p."한글상품명", '')), '\s+', '')
      LIKE REGEXP_REPLACE(LOWER('%<상품명>%'), '\s+', '')
    OR REGEXP_REPLACE(LOWER(COALESCE(p."대표카드상품명", '')), '\s+', '')
      LIKE REGEXP_REPLACE(LOWER('%<상품명>%'), '\s+', '')
    OR REGEXP_REPLACE(LOWER(COALESCE(p."고객안내상품명", '')), '\s+', '')
      LIKE REGEXP_REPLACE(LOWER('%<상품명>%'), '\s+', '')
    OR UPPER(p."상품코드") = UPPER('<상품명>')
)
SELECT
  c."기준년월",
  COUNT(DISTINCT c."고객식별자") AS "이용업체수",
  COUNT(DISTINCT CONCAT(c."회원일련번호", '-', c."카드구분키번호")) AS "이용카드좌수",
  SUM(COALESCE(c."금월이용합계건수", 0)) AS "이용건수",
  SUM(COALESCE(c."금월이용합계금액", 0)) AS "이용금액"
FROM tmdaa3e16 c
JOIN product_cards p
  ON c."상품코드" = p."상품코드"
WHERE c."개인기업구분코드" = '2'
  AND c."기준년월" BETWEEN '202601' AND '202606'
GROUP BY c."기준년월"
ORDER BY c."기준년월"
""".strip()

Q20_ISSUED_IN_YEAR = """
WITH latest_snapshot AS (
  SELECT MAX(c."실적기준년월일") AS "조회기준일"
  FROM tbdaaat05 c
),
issued_cards AS (
  SELECT DISTINCT
    m."고객식별자",
    c."회원일련번호",
    c."카드구분키번호"
  FROM tbdaaat05 c
  JOIN tbdaaat03 m
    ON c."회원일련번호" = m."회원일련번호"
  CROSS JOIN latest_snapshot s
  WHERE c."실적기준년월일" = s."조회기준일"
    AND c."개인기업구분코드" = '2'
    AND m."개인기업구분코드" = '2'
    AND c."카드신규발급년월일" BETWEEN '20260101' AND '20261231'
)
SELECT
  s."조회기준일",
  COUNT(i."카드구분키번호") AS "카드좌수",
  COUNT(DISTINCT i."고객식별자") AS "업체수"
FROM latest_snapshot s
LEFT JOIN issued_cards i
  ON TRUE
GROUP BY s."조회기준일"
""".strip()

Q21_ISSUED_THEN_CANCELLED = """
WITH latest_snapshot AS (
  SELECT MAX(c."실적기준년월일") AS "조회기준일"
  FROM tbdaaat05 c
),
matched_cards AS (
  SELECT DISTINCT
    m."고객식별자",
    c."회원일련번호",
    c."카드구분키번호"
  FROM tbdaaat05 c
  JOIN tbdaaat03 m
    ON c."회원일련번호" = m."회원일련번호"
  CROSS JOIN latest_snapshot s
  WHERE c."실적기준년월일" = s."조회기준일"
    AND c."개인기업구분코드" = '2'
    AND m."개인기업구분코드" = '2'
    AND c."카드신규발급년월일" BETWEEN '20260101' AND '20260331'
    AND c."해지년월일" BETWEEN '20260401' AND '20260630'
)
SELECT
  s."조회기준일",
  COUNT(mc."카드구분키번호") AS "카드좌수",
  COUNT(DISTINCT mc."고객식별자") AS "업체수"
FROM latest_snapshot s
LEFT JOIN matched_cards mc
  ON TRUE
GROUP BY s."조회기준일"
""".strip()

Q22_OVERSEAS_PEAK_MONTH = """
WITH corporate_overseas AS (
  SELECT
    t."기준년월",
    COALESCE(t."매출금액", 0) AS "매출금액"
  FROM tbdaabt08 t
  JOIN tbdaaat03 m
    ON t."회원일련번호" = m."회원일련번호"
  WHERE m."개인기업구분코드" = '2'
    AND t."기준년월" BETWEEN '202601' AND '202612'
)
SELECT
  o."기준년월" AS "최다이용월",
  COUNT(*) AS "이용건수",
  SUM(o."매출금액") AS "이용금액"
FROM corporate_overseas o
GROUP BY o."기준년월"
ORDER BY SUM(o."매출금액") DESC, o."기준년월"
LIMIT 1
""".strip()

Q23_MONTHLY_AVERAGE_COMPARE = """
WITH ranked AS (
  SELECT
    SUBSTR(a."기준년월일", 1, 6) AS "기준년월",
    a."고객식별자",
    COALESCE(a."금월신용카드이용금액", 0)
      + COALESCE(a."금월체크카드이용금액", 0) AS "전체이용금액",
    ROW_NUMBER() OVER (
      PARTITION BY a."고객식별자", SUBSTR(a."기준년월일", 1, 6)
      ORDER BY a."기준년월일" DESC
    ) AS rn
  FROM tbdaa1d12 a
  WHERE SUBSTR(a."기준년월일", 1, 6) IN ('202605', '202606')
),
monthly AS (
  SELECT
    r."기준년월",
    r."고객식별자",
    r."전체이용금액"
  FROM ranked r
  WHERE r.rn = 1
),
monthly_stats AS (
  SELECT
    m."기준년월",
    COUNT(DISTINCT m."고객식별자") AS "법인회원수",
    COUNT(DISTINCT CASE WHEN m."전체이용금액" > 0 THEN m."고객식별자" END) AS "유실적회원수",
    SUM(m."전체이용금액") AS "총이용금액",
    CAST(SUM(m."전체이용금액") AS DOUBLE)
      / NULLIF(CAST(COUNT(DISTINCT m."고객식별자") AS DOUBLE), 0.0) AS "전체회원평균이용금액",
    CAST(SUM(m."전체이용금액") AS DOUBLE)
      / NULLIF(
        CAST(COUNT(DISTINCT CASE WHEN m."전체이용금액" > 0 THEN m."고객식별자" END) AS DOUBLE),
        0.0
      ) AS "유실적회원평균이용금액"
  FROM monthly m
  GROUP BY m."기준년월"
)
SELECT
  s."기준년월",
  s."법인회원수",
  s."유실적회원수",
  s."총이용금액",
  ROUND(s."전체회원평균이용금액", 0) AS "전체회원평균이용금액",
  ROUND(s."유실적회원평균이용금액", 0) AS "유실적회원평균이용금액",
  ROUND(
    s."전체회원평균이용금액"
    - LAG(s."전체회원평균이용금액") OVER (ORDER BY s."기준년월"),
    0
  ) AS "전월대비증감금액",
  ROUND(
    (s."전체회원평균이용금액"
     - LAG(s."전체회원평균이용금액") OVER (ORDER BY s."기준년월"))
    / NULLIF(LAG(s."전체회원평균이용금액") OVER (ORDER BY s."기준년월"), 0.0) * 100,
    2
  ) AS "전월대비증감률_퍼센트"
FROM monthly_stats s
ORDER BY s."기준년월"
""".strip()


# =============================================================================
# 케이스 정의 (workbook Sheet1 A2:A24 순서)
# =============================================================================

CASES: list[dict] = [
    {
        "row": 2,
        "question": "2026년 1월~6월까지 신규로 모집한 기업회원의 전체 이용금액(신용, 체크)을 알려줘.",
        "sql": Q01_NEW_MEMBER_USAGE,
        "domain": "customer_card_portfolio",
        "category": "신규 모집 기업회원 이용금액",
        "expected_tables": ["tbdaaat03", "tmdaa3e16"],
        "metrics": ["카드별월이용금액"],
        "semantic_contract": "",
        "difficulty": "hard",
        "authoring_status": "ok",
        "assumptions": (
            "신규 모집은 회원기본(tbdaaat03)의 회원최초신규발급년월일이 20260101~20260630인 기업회원으로 정의했다. "
            "신용·체크 구분은 tbdaa1d12 금월신용/체크카드이용금액의 원천 정의(상품중분류구분코드 CP53·CP54가 체크)를 "
            "tmdaa3e16 금월이용합계금액에 적용해 나눴다."
        ),
    },
    {
        "row": 3,
        "question": "당사 관리기업인 쿠팡의 작년과 올해 반기별 이용금액 실적을 알려줘.",
        "sql": Q02_NAMED_HALF_YEAR_USAGE,
        "domain": "corporate_sales_targeting",
        "category": "특정 기업 반기별 이용금액",
        "expected_tables": ["tbdaa1d12"],
        "metrics": ["기업월카드이용금액"],
        "semantic_contract": "named_corporate_half_year_usage",
        "difficulty": "medium",
        "authoring_status": "ok",
        "assumptions": (
            "기준일 2026-08-10 기준으로 작년은 202501~202512, 올해는 202601~202608이다. "
            "관리기업 범위는 사용자 권한에 종속되므로 SQL에는 반영하지 않고 기업명 부분일치만 적용했다. "
            "관리기업 목록으로 제한해야 하면 사업자등록번호 목록 파라미터를 VALUES CTE로 추가한다."
        ),
    },
    {
        "row": 4,
        "question": "당사 관리기업인 쿠팡의 작년과 올해 반기별 이용금액 실적을 알려줘.",
        "sql": Q02_NAMED_HALF_YEAR_USAGE,
        "domain": "corporate_sales_targeting",
        "category": "특정 기업 반기별 이용금액",
        "expected_tables": ["tbdaa1d12"],
        "metrics": ["기업월카드이용금액"],
        "semantic_contract": "named_corporate_half_year_usage",
        "difficulty": "medium",
        "authoring_status": "duplicate_of_row3",
        "assumptions": "워크북 3행과 동일한 질의이므로 같은 정답 SQL을 사용한다.",
    },
    {
        "row": 5,
        "question": "당사 기업카드 이용금액 상위 10개의 2026년 분기별 이용금액을 업체별로 추출해줘",
        "sql": Q04_TOP10_QUARTERLY,
        "domain": "card_usage",
        "category": "상위 기업 분기별 이용금액",
        "expected_tables": ["tbdaabt30", "tbdaa1d12"],
        "metrics": ["카드매출금액"],
        "semantic_contract": "",
        "difficulty": "hard",
        "authoring_status": "ok",
        "assumptions": (
            "업체 순위는 2026년(202601~202612) 전체 이용금액 기준 상위 10개다. "
            "다음 행의 업종·상품 제외 조건을 이어서 적용할 수 있도록 기업 월 스냅샷 대신 매출전표(tbdaabt30)를 "
            "원천으로 쓰고 기업고객식별자로 집계했다. 취소 전표 순액 규칙은 원천 정의에 없어 적용하지 않았다."
        ),
    },
    {
        "row": 6,
        "question": "(위 질문과 이어서) 지방세 국세 4대보험, 기업오토, kb계열사 기업카드, 상품권업종은 제거해줘",
        "sql": Q05_TOP10_QUARTERLY_EXCLUDED,
        "domain": "card_usage",
        "category": "상위 기업 분기별 이용금액(특화매출·상품 제외)",
        "expected_tables": ["tbdaabt30", "tbdaa1d12", "tbdaada72"],
        "metrics": ["카드매출금액"],
        "semantic_contract": "",
        "difficulty": "hard",
        "authoring_status": "ok_with_assumption",
        "assumptions": (
            "업종 제외는 원천 정의에 기재된 코드를 사용했다. 국세 8219, 지방세 8220, 4대보험 8099, "
            "상품권 4200과 전사상품권 4124·4125·4126. "
            "기업오토와 KB계열사 기업카드는 semantic layer에 코드북이 없어 상품명 부분일치('오토', '계열사')로 "
            "제외했다. 운영 상품코드 목록이 확정되면 excluded_products CTE를 코드 IN 조건으로 교체해야 한다."
        ),
    },
    {
        "row": 7,
        "question": (
            "현재기준 유효한 기업카드를 보유한 기업 회원중 2025년 상반기 전체 이용금액 대비 "
            "2026년 상반기 이용금액이 하락한 기업회원은 몇개이고, 리스트를 뽑아줘"
        ),
        "sql": half_year_decline_sql(None),
        "domain": "corporate_sales_targeting",
        "category": "반기 이용금액 하락 기업회원",
        "expected_tables": ["tbdaa1d12"],
        "metrics": ["기업월카드이용금액", "기업유효카드수"],
        "semantic_contract": "current_corporate_half_year_usage_decline",
        "difficulty": "hard",
        "authoring_status": "ok",
        "assumptions": (
            "현재 기준은 tbdaa1d12의 최신 기준년월에서 고객별 최신 스냅샷 1건이다. "
            "업체 수와 리스트를 한 번에 답하도록 COUNT(*) OVER ()로 하락 기업회원 수를 모든 행에 붙였다."
        ),
    },
    {
        "row": 8,
        "question": (
            "현재기준 유효한 기업카드를 보유한 기업 회원중 2025년 상반기 전체 이용금액 대비 "
            "2026년 상반기 이용금액이 하락한 기업회원은 상위 100개와 이용금액을 뽑아줘"
        ),
        "sql": half_year_decline_sql(100),
        "domain": "corporate_sales_targeting",
        "category": "반기 이용금액 하락 기업회원 상위 100",
        "expected_tables": ["tbdaa1d12"],
        "metrics": ["기업월카드이용금액", "기업유효카드수"],
        "semantic_contract": "current_corporate_half_year_usage_decline",
        "difficulty": "hard",
        "authoring_status": "ok",
        "assumptions": "상위 100개는 하락금액 내림차순 100건으로 해석했다.",
    },
    {
        "row": 9,
        "question": "2026년 6월 쿠팡의 공헌이익율과 대손비용률을 분석해줘",
        "sql": Q08_LOSS_COST_RATE,
        "domain": "credit_risk",
        "category": "기업 대손비용률",
        "expected_tables": ["tbdaa1d12", "tbmewcm94", "tbmaisd06"],
        "metrics": ["기대대손충당금", "특수채권편입원금"],
        "semantic_contract": "",
        "difficulty": "hard",
        "authoring_status": "partial_blocked",
        "assumptions": (
            "대손비용률 = (월말 기대대손충당금 - 월초 기대대손충당금 + 당월 상각액) / 월평균 원화대출잔액이며 "
            "월초는 202605 충당금, 상각액은 tbmaisd06의 202606 특수채권편입원금을 사용했다. "
            "공헌이익율은 기업고객별 수익·비용·경제적이익 금액 컬럼이 31개 테이블에 없어 SQL을 생성하지 않았다. "
            "산출에는 법인회원 경제적이익금액과 수익·원가 배분 원천이 추가로 필요하다."
        ),
    },
    {
        "row": 10,
        "question": (
            "kb카드 기업카드 보유회원중에 2026년 상반기 기준으로 체크카드만 보유한 회원이 "
            "신용카드를 발급해 이용한 건과 이용금액을 알려줘"
        ),
        "sql": Q09_CHECK_ONLY_TO_CREDIT,
        "domain": "customer_card_portfolio",
        "category": "체크카드 전용 회원의 신용카드 교차판매 실적",
        "expected_tables": ["tmdaa3e16", "tbdaaat05"],
        "metrics": ["카드별월이용금액"],
        "semantic_contract": "",
        "difficulty": "hard",
        "authoring_status": "ok_with_assumption",
        "assumptions": (
            "체크카드만 보유한 회원은 상반기 시작월인 202601 카드 월실적에서 유효신용카드가 0좌이고 "
            "유효체크카드가 1좌 이상인 기업회원으로 판정했다. "
            "신용카드 발급은 카드신규발급년월일이 20260101~20260630이고 상품중분류구분코드가 CP53·CP54가 아닌 카드이며, "
            "건수와 금액은 해당 카드의 202601~202606 금월이용합계건수·금월이용합계금액 합계다."
        ),
    },
    {
        "row": 11,
        "question": "현재 kb카드 기업카드 보유회원중에 상반기 전기요금전용 체크카드를 신규로 몇좌 발급했는지와 리스트를 뽑아줘.",
        "sql": Q10_NEW_CHECK_CARD_ISSUANCE,
        "domain": "customer_card_portfolio",
        "category": "반기 체크카드 신규 발급 좌수·목록",
        "expected_tables": ["tbdaaat03", "tbdaaat05", "tbdaada72", "tbdaa1d12"],
        "metrics": ["유효신용카드수"],
        "semantic_contract": "current_corporate_member_half_year_new_check_card_issuance",
        "difficulty": "hard",
        "authoring_status": "ok",
        "assumptions": (
            "기준일 2026-08-10 기준 상반기는 202601~202606이다. "
            "전기요금전용 체크카드는 상품명 4종에 '전기요금' 부분일치와 체크카드 상품중분류(CP53·CP54)를 함께 적용했다."
        ),
    },
    {
        "row": 12,
        "question": "가맹점번호 59593113의 월별 매출금액을 알려줘",
        "sql": Q11_MERCHANT_MONTHLY_SALES,
        "domain": "merchant_sales",
        "category": "가맹점번호 월별 매출",
        "expected_tables": ["tmdaa5e11"],
        "metrics": ["가맹점월매출금액"],
        "semantic_contract": "",
        "difficulty": "easy",
        "authoring_status": "ok",
        "assumptions": "가맹점 월매출은 tmdaa5e11의 일시불+할부 매출금액이며 총지급금액으로 대체하지 않는다.",
    },
    {
        "row": 13,
        "question": "현재기준 KB아시아나 기업카드 유효카드수 알려줘",
        "sql": Q12_PRODUCT_VALID_CARD_COUNT,
        "domain": "customer_card_portfolio",
        "category": "상품별 현재 유효 기업카드 좌수",
        "expected_tables": ["tbdaaat05", "tbdaada72"],
        "metrics": ["유효신용카드수"],
        "semantic_contract": "card_product_current_valid_corporate_count",
        "difficulty": "medium",
        "authoring_status": "ok",
        "assumptions": "현재 기준은 tbdaaat05 전체 데이터의 MAX(실적기준년월일) 스냅샷이며 상품명은 공백 제거 후 부분일치다.",
    },
    {
        "row": 14,
        "question": "기업카드를 보유하고 있는 당사 가맹점중에 매출액이 가장 높은 상위10개 명단을 알려줘",
        "sql": Q13_TOP_MERCHANTS_WITH_CORPORATE_CARD,
        "domain": "merchant_sales",
        "category": "기업카드 보유 가맹점 매출 상위 10",
        "expected_tables": ["tmdaaus01", "tmdaa5e11"],
        "metrics": ["가맹점월매출금액", "기업유효카드수"],
        "semantic_contract": "",
        "difficulty": "hard",
        "authoring_status": "ok_with_assumption",
        "assumptions": (
            "질문에 기준월이 없어 tmdaa5e11의 최신 기준년월을 사용하고 결과에 기준년월을 함께 반환한다. "
            "기업카드 보유는 가맹점 월말요약의 유효기업신용카드수+유효기업체크카드수 > 0이며, "
            "개별·공용 카드수는 다른 분류 축이라 합산하지 않았다."
        ),
    },
    {
        "row": 15,
        "question": "2026년 6월 기준 기업카드의 브랜드별 유효 좌수를 알고 싶어",
        "sql": Q14_BRAND_VALID_CARD_COUNT,
        "domain": "customer_card_portfolio",
        "category": "브랜드별 유효 기업카드 좌수",
        "expected_tables": ["tbdaaat05"],
        "metrics": ["유효신용카드수"],
        "semantic_contract": "corporate_valid_card_count_by_brand_at_month",
        "difficulty": "medium",
        "authoring_status": "ok",
        "assumptions": (
            "202606 안의 MAX(실적기준년월일) 스냅샷에서 회원일련번호×카드구분키번호 DISTINCT 좌수를 센다. "
            "브랜드명은 코드북 1 로칼(국민), 2 마스터, 3 비자, 4 JCB, 5 UPI&글로벌, 6 아맥스로 매핑한다."
        ),
    },
    {
        "row": 16,
        "question": "2026년 6월 기준 법인회원의 유실적 업체수를 알려주세요",
        "sql": Q15_CORPORATE_MEMBER_WITH_USAGE,
        "domain": "corporate_sales_targeting",
        "category": "유실적 업체수",
        "expected_tables": ["tbdaa1d12"],
        "metrics": ["기업월카드이용금액"],
        "semantic_contract": "corporate_member_with_monthly_usage_count",
        "difficulty": "easy",
        "authoring_status": "ok",
        "assumptions": "고객별 월 최신 스냅샷 1건에서 신용+체크 이용금액이 0보다 큰 고객식별자를 DISTINCT로 센다.",
    },
    {
        "row": 17,
        "question": (
            "2026년 6월 기준 KB국민 기업 신용, 체크카드의 브랜드별"
            "(브랜드, 비자, 마스터, UPI, Amex, JCB) 유효좌수를 알려줘"
        ),
        "sql": Q16_BRAND_VALID_CARD_COUNT_BY_TYPE,
        "domain": "customer_card_portfolio",
        "category": "브랜드별 신용·체크 유효 기업카드 좌수",
        "expected_tables": ["tbdaaat05"],
        "metrics": ["유효신용카드수"],
        "semantic_contract": "corporate_valid_card_count_by_brand_at_month",
        "difficulty": "medium",
        "authoring_status": "ok",
        "assumptions": (
            "카드브랜드구분코드는 현업 확인 코드북 1 로칼(국민), 2 마스터, 3 비자, 4 JCB, 5 UPI&글로벌, 6 아맥스를 "
            "그대로 매핑했다. 질문이 열거한 브랜드 6종이 모두 코드값을 갖는다. "
            "코드북에 없는 값이 나오면 ELSE로 원본 코드를 노출한다. "
            "질문에 BC 제외 조건이 없어 KB카드BC카드구분코드 필터는 넣지 않았다."
        ),
    },
    {
        "row": 18,
        "question": (
            "KB국민 비씨전철보충수수료 결제전용기업카드(cpc08201) 카드의 2026년 상반기 "
            "이용금액 상위 10개 업체의 사업자번호와 이용금액을 알려줘"
        ),
        "sql": Q17_PRODUCT_TOP10_COMPANIES,
        "domain": "card_usage",
        "category": "상품별 이용금액 상위 업체",
        "expected_tables": ["tbdaada72", "tmdaa3e16", "tbdaa1d12"],
        "metrics": ["카드별월이용금액"],
        "semantic_contract": "",
        "difficulty": "hard",
        "authoring_status": "ok",
        "assumptions": (
            "질문의 cpc08201은 상품코드로 해석해 대문자 정규화 후 정확 일치시켰다. "
            "업체는 카드 월실적의 고객식별자이며 사업자등록번호·기업명은 상반기 고객별 최신 기업 스냅샷에서 가져온다."
        ),
    },
    {
        "row": 19,
        "question": (
            "KB국민 비씨전철보충수수료 결제전용기업카드(cpc08201) 카드의 2026년 상반기 "
            "이용금액 상위 10개 회사의 사업자번호와 이용금액을 알려줘"
        ),
        "sql": Q17_PRODUCT_TOP10_COMPANIES,
        "domain": "card_usage",
        "category": "상품별 이용금액 상위 업체",
        "expected_tables": ["tbdaada72", "tmdaa3e16", "tbdaa1d12"],
        "metrics": ["카드별월이용금액"],
        "semantic_contract": "",
        "difficulty": "hard",
        "authoring_status": "duplicate_of_row18",
        "assumptions": "18행과 '업체'/'회사' 표현만 다른 동일 질의여서 같은 정답 SQL을 사용한다.",
    },
    {
        "row": 20,
        "question": " 카드의 2026년 1월부터 6월까지의 이용금액을 알려줘",
        "sql": Q19_PRODUCT_USAGE_TEMPLATE,
        "domain": "card_usage",
        "category": "상품별 기간 이용금액",
        "expected_tables": ["tbdaada72", "tmdaa3e16"],
        "metrics": ["카드별월이용금액"],
        "semantic_contract": "",
        "difficulty": "medium",
        "authoring_status": "clarification_required",
        "assumptions": (
            "질문에서 카드명이 비어 있어 대상 상품을 확정할 수 없다. "
            "ambiguity_rules에 따라 임의 상품을 가정하지 않고 상품명·상품코드 자리에 <상품명> 플레이스홀더를 둔 "
            "템플릿을 정답으로 둔다. 상품명을 받으면 그대로 치환해 실행한다."
        ),
    },
    {
        "row": 21,
        "question": "2026년에 기업카드를 발급한 이력이 있는 업체수, 좌수를 알려줘",
        "sql": Q20_ISSUED_IN_YEAR,
        "domain": "customer_card_portfolio",
        "category": "연간 기업카드 발급 좌수·업체수",
        "expected_tables": ["tbdaaat05", "tbdaaat03"],
        "metrics": ["유효신용카드수"],
        "semantic_contract": "",
        "difficulty": "medium",
        "authoring_status": "ok",
        "assumptions": (
            "발급 이력은 최신 회원카드 스냅샷에서 카드신규발급년월일이 20260101~20261231인 카드다. "
            "좌수는 회원일련번호×카드구분키번호 DISTINCT, 업체수는 회원기본의 고객식별자 DISTINCT다."
        ),
    },
    {
        "row": 22,
        "question": "기업카드 발급을 26년 1,2,3월에 하고 4,5,6월 안에 해지한 경우의 카드좌수와 업체수를 알려줘",
        "sql": Q21_ISSUED_THEN_CANCELLED,
        "domain": "customer_card_portfolio",
        "category": "발급 후 해지 좌수·업체수",
        "expected_tables": ["tbdaaat05", "tbdaaat03"],
        "metrics": ["유효신용카드수"],
        "semantic_contract": "corporate_cards_issued_then_cancelled_count",
        "difficulty": "hard",
        "authoring_status": "ok",
        "assumptions": "26년은 2026년으로 확장했고 발급기간은 20260101~20260331, 해지기간은 20260401~20260630이다.",
    },
    {
        "row": 23,
        "question": "2026년에 해외업종에서 가장 많이 사용한 달과 이용금액을 알려줘",
        "sql": Q22_OVERSEAS_PEAK_MONTH,
        "domain": "card_usage",
        "category": "해외 이용 최다월",
        "expected_tables": ["tbdaabt08", "tbdaaat03"],
        "metrics": ["해외매출금액"],
        "semantic_contract": "",
        "difficulty": "medium",
        "authoring_status": "ok_with_assumption",
        "assumptions": (
            "해외 이용금액은 canonical metric인 해외매출전표(tbdaabt08) 매출금액이다. "
            "tbdaabt08에는 개인기업구분코드가 없어 회원기본(tbdaaat03)과 회원일련번호로 조인해 기업회원으로 한정했다. "
            "법인·개인 구분 없이 전사 기준이 필요하면 조인을 제거한다."
        ),
    },
    {
        "row": 24,
        "question": "2026년 6월과 2026년 5월의 법인회원 평균 이용금액을 비교해주세요",
        "sql": Q23_MONTHLY_AVERAGE_COMPARE,
        "domain": "card_usage",
        "category": "법인회원 월 평균 이용금액 비교",
        "expected_tables": ["tbdaa1d12"],
        "metrics": ["기업월카드이용금액"],
        "semantic_contract": "",
        "difficulty": "hard",
        "authoring_status": "ok_with_assumption",
        "assumptions": (
            "평균 이용금액은 회원 1개사당 평균으로 해석했다. 해당 월 스냅샷이 있는 전체 법인회원 기준 평균과 "
            "이용금액이 0보다 큰 유실적 회원 기준 평균을 함께 반환해 분모 해석 차이를 드러냈다. "
            "월 스냅샷은 고객×월 최신 기준년월일 1건으로 축소한 뒤 집계한다."
        ),
    },
]

# 워크북 9행의 공헌이익율처럼 원천이 없어 SQL을 만들지 않은 항목
BLOCKED_NOTES = [
    {
        "row": 9,
        "measure": "공헌이익율",
        "reason": (
            "기업고객 단위 수익·비용·법인회원 경제적이익 금액 컬럼이 semantic layer의 31개 테이블에 없다. "
            "tbdaa1d12의 사업자여전법규제1·2위반여부는 총수익·총비용 비교 결과의 Y/N 플래그일 뿐 금액이 아니다."
        ),
        "required_sources": [
            "기업고객별 카드수익(가맹점수수료 배분·할부·CA 수수료) 집계 원천",
            "법인회원 경제적이익금액(캐시백·포인트·마일리지) 월별 원천",
            "기업고객별 변동비·마케팅비 배분 규칙",
        ],
    },
]


# =============================================================================
# 검증
# =============================================================================

WRITE_KEYWORDS = (
    "insert",
    "update",
    "delete",
    "merge",
    "create",
    "drop",
    "alter",
    "truncate",
    "grant",
    "revoke",
)

KOREAN_IDENT = re.compile(r'"([^"]+)"')
TABLE_REF = re.compile(r"\b(?:FROM|JOIN)\s+([a-z][a-z0-9_]*)", re.IGNORECASE)
ALIASED_REF = re.compile(
    r"\b(?:FROM|JOIN)\s+([a-z][a-z0-9_]*)\s+(?:AS\s+)?([a-z][a-z0-9_]*)\b",
    re.IGNORECASE,
)
QUALIFIED_IDENT = re.compile(r'\b([a-z][a-z0-9_]*)\."([^"]+)"', re.IGNORECASE)


def load_columns() -> dict[str, set[str]]:
    doc = yaml.safe_load(SEMANTIC_LAYER.read_text(encoding="utf-8"))
    columns: dict[str, set[str]] = {}
    for table in doc["tables"]:
        names: set[str] = set()
        for group in ("dimensions", "measures", "time_dimensions"):
            for column in table.get(group) or []:
                names.add(column["name"])
        columns[table["name"]] = names
    return columns


def validate(cases: list[dict], columns: dict[str, set[str]]) -> list[str]:
    errors: list[str] = []
    try:
        import sqlglot
    except ImportError:  # pragma: no cover - 선택적 의존성
        sqlglot = None
        errors.append("warn: sqlglot 미설치로 파서 검증을 건너뜀 (pip install sqlglot)")

    for case in cases:
        cid = case["id"]
        sql = case["sql"]

        if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
            errors.append(f"{cid}: SELECT/WITH로 시작하지 않음")
        if ";" in sql.rstrip().rstrip(";"):
            errors.append(f"{cid}: 복수 문장 의심 (세미콜론 포함)")
        lowered = re.sub(r"'[^']*'", "''", sql.lower())
        for keyword in WRITE_KEYWORDS:
            if re.search(rf"\b{keyword}\b", lowered):
                errors.append(f"{cid}: 쓰기 키워드 '{keyword}' 포함")
        if "'" in sql and ("‘" in sql or "’" in sql or "“" in sql):
            errors.append(f"{cid}: 스마트 따옴표 포함")

        referenced = {t.lower() for t in TABLE_REF.findall(sql)}
        cte_names = {
            m.group(1).lower()
            for m in re.finditer(r"(?:WITH|,)\s*([a-z][a-z0-9_]*)\s+AS\s*\(", sql, re.IGNORECASE)
        }
        physical = {t for t in referenced if t not in cte_names}
        for table in physical:
            if table not in columns:
                errors.append(f"{cid}: semantic layer에 없는 테이블 {table}")
            if table in RESTRICTED_TABLES:
                errors.append(f"{cid}: restricted 테이블 {table} 참조")
        declared = set(case["expected_tables"])
        if declared != physical:
            errors.append(
                f"{cid}: expected_tables {sorted(declared)} != SQL 참조 {sorted(physical)}"
            )

        allowed = set()
        for table in physical:
            allowed |= columns.get(table, set())
        aliases = {
            m.group(1)
            for m in re.finditer(r'AS\s+"([^"]+)"', sql, re.IGNORECASE)
        }
        allowed |= aliases
        for ident in KOREAN_IDENT.findall(sql):
            if ident not in allowed:
                errors.append(f"{cid}: 미확인 식별자 \"{ident}\"")

        # alias -> 물리 테이블로 정규화한 뒤 컬럼이 그 테이블에 실제로 있는지 확인한다.
        # CTE alias는 컬럼 목록을 정적으로 알 수 없으므로 건너뛴다.
        alias_to_table: dict[str, str] = {}
        for source, alias in ALIASED_REF.findall(sql):
            source_lower = source.lower()
            if source_lower in columns:
                alias_to_table[alias.lower()] = source_lower
        for alias, ident in QUALIFIED_IDENT.findall(sql):
            table = alias_to_table.get(alias.lower())
            if table is None:
                continue
            if ident in aliases:
                continue
            if ident not in columns[table]:
                errors.append(f'{cid}: {table} 에 없는 컬럼 {alias}."{ident}"')

        if sqlglot is not None:
            try:
                parsed = sqlglot.parse(sql, read="trino")
            except Exception as exc:  # noqa: BLE001 - 파서 오류 메시지를 그대로 보고
                errors.append(f"{cid}: sqlglot 파싱 실패 {exc}")
            else:
                if len(parsed) != 1:
                    errors.append(f"{cid}: 단일 문장이 아님 ({len(parsed)}개)")

    return errors


# =============================================================================
# 산출물
# =============================================================================


def build_records() -> list[dict]:
    records = []
    for index, case in enumerate(CASES, start=1):
        records.append(
            {
                "id": f"cs-workbook-{index:03d}",
                "workbook_row": case["row"],
                "question": case["question"],
                "sql": case["sql"],
                "sql_dialect": SQL_DIALECT,
                "domain": case["domain"],
                "category": case["category"],
                "expected_tables": case["expected_tables"],
                "metrics": case["metrics"],
                "semantic_contract": case["semantic_contract"],
                "difficulty": case["difficulty"],
                "authoring_status": case["authoring_status"],
                "assumptions": case["assumptions"],
                "reference_date": REFERENCE_DATE,
            }
        )
    return records


def write_outputs(records: list[dict]) -> None:
    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with OUT_JSONL.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "id",
                "workbook_row",
                "question",
                "sql",
                "domain",
                "category",
                "expected_tables",
                "metrics",
                "semantic_contract",
                "difficulty",
                "authoring_status",
                "assumptions",
                "reference_date",
            ]
        )
        for record in records:
            writer.writerow(
                [
                    record["id"],
                    record["workbook_row"],
                    record["question"],
                    record["sql"],
                    record["domain"],
                    record["category"],
                    ", ".join(record["expected_tables"]),
                    ", ".join(record["metrics"]),
                    record["semantic_contract"],
                    record["difficulty"],
                    record["authoring_status"],
                    record["assumptions"],
                    record["reference_date"],
                ]
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="검증만 수행하고 파일은 쓰지 않는다")
    args = parser.parse_args()

    columns = load_columns()
    records = build_records()
    errors = validate(records, columns)

    fatal = [e for e in errors if not e.startswith("warn:")]
    for error in errors:
        print(("WARN  " if error.startswith("warn:") else "FAIL  ") + error, file=sys.stderr)

    if fatal:
        print(f"\n검증 실패 {len(fatal)}건", file=sys.stderr)
        return 1

    unique_sql = {record["sql"] for record in records}
    print(f"케이스 {len(records)}건 / 고유 SQL {len(unique_sql)}종 / 검증 통과")
    print(f"SQL 없는 지표: {len(BLOCKED_NOTES)}건 (워크북 {', '.join(str(n['row']) for n in BLOCKED_NOTES)}행)")

    if args.check:
        print("--check 모드: 파일을 쓰지 않았다")
        return 0

    write_outputs(records)
    print(f"생성 {OUT_JSONL.relative_to(ROOT)}")
    print(f"생성 {OUT_CSV.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

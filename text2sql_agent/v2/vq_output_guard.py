"""Verified Query 출력 계약 가드.

참고 SQL(Verified Query)은 컬럼이 고정된 완제품이다. 매칭되면 그대로 실행되므로,
"이 VQ가 질문이 요구한 값을 실제로 내놓는가"가 매칭의 전제여야 한다. 그런데 v2의
매칭 게이트는 어휘 겹침(태그 +3점)과 임베딩 유사도만 본다. 그래서 질문이 같은
업무 영역의 단어를 쓰기만 하면 전혀 다른 답이 나갔다.

goldenset v2 mismatch 의 difficulty=easy 63건 중 29건이 이 한 가지 원인이었다.

    질문: "2026년 특수채권편입연체료를 연체채권 종류 기준으로 집계해줘"
    매칭: special_debt_by_asset_quality  (태그 '특수채권' +3, '연체' +3 → 6점)
    실행: 기준년 × 자산건전성분류 별 편입원금·편입잔액·평균연체회차 33행
    정답: 연체채권종류구분코드 별 특수채권편입연체료 3행

    질문: "카드등급별로 2025년 6월 30일 기준 할부연체원금을 보여줘"
    매칭: card_delinquency_by_grade      (태그 '카드등급'·'연체'·'등급별' → 9점)
    실행: 카드등급별 카드수·총이용금액·총연체원금·연체율
    정답: 카드등급별 tddaa3l01."할부연체원금"

세 가지를 본다. 어느 하나라도 "질문은 요구하는데 VQ는 못 낸다"이면 거부한다.
거부는 곧 LLM 생성 경로로 내려보낸다는 뜻이지, 답을 포기하는 게 아니다.

    1. 기간   질문에 연·월·일이 박혀 있는데 VQ에 기간 파라미터도 시간 조건도 없다.
              → special_debt_by_asset_quality(전 기간 집계), daily_sales_amount
    2. 축     질문이 "X별 / X 기준으로" 라고 축을 지목했는데 VQ의 GROUP BY에 없다.
              → 연체채권 종류별, 회원자격별, CA요율그룹별
    3. 지표   질문이 스키마 컬럼명을 그대로 불렀는데 VQ SQL에 그 컬럼이 없다.
              → 할부연체원금, 금월취소이용금액, 유효체크카드수, 통합ASS평점

판정 근거를 문자열로 함께 돌려준다. 매칭 실패는 조용히 일어나면 진단이 안 된다.
"""

from __future__ import annotations

import re

from .column_synonyms import compact, phrase_in_text

# 질문이 축을 지목하는 표면형. "회원자격별로", "자산건전성 분류 기준으로",
# "CA한도별" 처럼 조사가 겹치거나 사이가 띄어져 있어도 잡아야 한다.
_AXIS_RE = re.compile(
    r"(?P<axis>[0-9A-Za-z가-힣·]+(?:\s+[0-9A-Za-z가-힣·]+){0,2}?)\s*"
    r"(?:별로|별\b|별(?=\s)|기준으로\s*(?:집계|구분|나눠|나누어)|"
    r"기준\s*(?:으로)?\s*(?:집계|구분|나눠|나누어))"
)

# 축 어구 앞에 딸려 오는 토막. "2025년 8월 금월취소이용금액을 카드브랜드 기준으로"
# 처럼 기간과 지표가 축 앞에 붙는다. 이 중 마지막 것 뒤부터가 진짜 축이다.
_AXIS_LEAD_TOKEN_RE = re.compile(
    r"(?:^|\s)(?:"
    r"\d{1,4}\s*(?:년|월|일)|(?<!\d)20\d{4,8}(?!\d)|"                     # 기간
    r"[0-9A-Za-z가-힣·]{2,}(?:을|를|은|는|의|에서|으로|까지|부터)|"        # 조사가 붙은 앞말
    r"상반기|하반기|작년|올해|전년|금년|내년|"                             # 기간 수식어
    r"기준|현재|최근|지난|이번|해당|전체|각|평균|총|누적"                  # 수식어
    r")(?=\s)"
)

# 축으로 볼 수 없는 말. 시간축과 순수 표현어는 VQ 거부 근거가 될 수 없다.
_NOT_AN_AXIS = frozenset(
    {
        "일", "월", "년", "연도", "년도", "분기", "반기", "상반기", "하반기",
        "날짜", "일자", "기간", "월별", "일별", "분기별", "반기별",
        "연", "주", "주간", "시간",
        "기업", "회사", "법인", "고객", "회원", "가맹점", "카드", "각각", "전체",
    }
)

# 질문에 기간이 박혀 있다고 볼 표면형. 상대 기간("최근 3개월")은 VQ 쪽에서
# 파라미터로 못 받아도 기본값 해석이 가능하므로 절대 기간만 본다.
_ABSOLUTE_PERIOD_RE = re.compile(
    r"20\d{2}\s*년|(?<!\d)20\d{4}(?:\d{2})?(?!\d)|"
    r"\d{1,2}\s*월\s*\d{1,2}\s*일|상반기|하반기"
)

# VQ가 기간을 표현할 수 있다고 볼 근거.
_TIME_PARAM_RE = re.compile(r"년월|년도|기준년|기간|시작|종료|일자|날짜|기준일")
_TIME_FILTER_RE = re.compile(
    r"[\"']?(?:기준년월일|기준년월|기준년|전표매출년월일|매출년월일|"
    r"[0-9A-Za-z가-힣]*년월일|[0-9A-Za-z가-힣]*년월)[\"']?\s*"
    r"(?:=|>=|<=|>|<|BETWEEN|IN)",
    re.IGNORECASE,
)

_SELECT_ALIAS_RE = re.compile(r"\bAS\s+[\"']?([0-9A-Za-z가-힣_]+)", re.IGNORECASE)

# VQ의 결과 자체를 정의하는 개념. VQ 쪽에만 있고 질문에 없으면 그 VQ는 질문보다
# 좁은 답을 낸다. "2026년 7월 기준 소기업로 분류된 기업고객이 몇 곳인지"에
# new_customers_by_month(신규 고객 수)가 붙어 최초등록년월일로 세던 사례다.
_DEFINING_CONCEPTS: tuple[tuple[str, str], ...] = (
    (r"신규|가입\s*(?:고객|회원)|등록\s*(?:고객|회원)", "신규 가입"),
    (r"무실적|미이용|이용\s*실적\s*없|한\s*번도", "무실적"),
    (r"폐업|휴폐업|해지|탈퇴|문\s*닫", "폐업·해지"),
    (r"해외", "해외"),
)

# 컬럼 검사에서 제외할 이름. 어느 집계에나 나오는 말이라 "질문이 이걸 콕
# 집었다"의 근거가 못 된다.
_GENERIC_COLUMNS = frozenset(
    {
        "매출금액", "이용금액", "매출건수", "이용건수", "고객수", "회원수",
        "카드수", "가맹점수", "건수", "금액", "잔액", "수수료율", "한도금액",
        "고객식별자", "회원일련번호", "가맹점번호", "사업자등록번호",
        "기준년월", "기준년월일", "기준년", "개인기업구분코드",
        # 이름 컬럼은 엔티티를 지목하는 질문에 늘 등장하므로 지표 근거가 못 된다.
        "고객명", "기업명", "법인명", "지점명", "직장명", "가맹점명",
        # 질문 표면의 업무 지표명과 VQ alias가 일치하면 별도 원천 컬럼 검사가
        # 필요 없다. `브랜드가맹점수`는 브랜드+가맹점수 의미이지 물리 컬럼이 아니다.
        "브랜드가맹점수",
    }
)

# 3글자 컬럼은 봉사료·선급금·연체료·예수금뿐이고 모두 고유한 지표다.
_MIN_COLUMN_LENGTH = 3


def _axis_terms(question: str) -> list[str]:
    """질문이 지목한 그룹핑 축 어구를 앞머리 제거 후 돌려준다."""
    terms: list[str] = []
    for match in _AXIS_RE.finditer(str(question or "")):
        axis = match.group("axis").strip()
        leads = list(_AXIS_LEAD_TOKEN_RE.finditer(axis))
        if leads:
            axis = axis[leads[-1].end():].strip()
        if not axis:
            continue
        if compact(axis) in _NOT_AN_AXIS or axis in _NOT_AN_AXIS:
            continue
        if len(compact(axis)) < 2:
            continue
        if axis not in terms:
            terms.append(axis)
    return terms


def _vq_expresses_period(vq: dict) -> bool:
    params = vq.get("parameters") if isinstance(vq.get("parameters"), dict) else {}
    if any(_TIME_PARAM_RE.search(str(name)) for name in params):
        return True
    return bool(_TIME_FILTER_RE.search(str(vq.get("sql") or "")))


def _vq_group_by_text(sql: str) -> str:
    """GROUP BY 절과 그 alias 를 이어 붙인 비교용 문자열.

    GROUP BY 가 서수(GROUP BY 1, 2)이거나 alias 를 참조할 수 있어서 SELECT 의
    alias 도 함께 본다. 축이 어디에도 안 보이면 그 VQ는 그 축으로 못 쪼갠다.
    """
    parts: list[str] = []
    for match in re.finditer(
        r"\bGROUP\s+BY\b(.*?)(?=\b(?:ORDER\s+BY|HAVING|LIMIT|UNION|WINDOW)\b|\)\s*(?:,|SELECT|$)|$)",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        parts.append(match.group(1))
    parts.extend(_SELECT_ALIAS_RE.findall(sql))
    return " ".join(parts)


def named_columns(question: str, column_names: object) -> list[str]:
    """질문이 스키마 컬럼명을 그대로 부른 것들을 긴 것부터 돌려준다.

    phrase_in_text() 는 글자 사이마다 구분자를 허용하는 패턴을 매번 컴파일해서
    컬럼 900여 개를 전부 돌리면 질문 하나에 12초가 걸린다. compact 부분문자열
    포함은 매칭의 필요조건이므로, 그걸로 먼저 걸러 몇 개만 넘긴다.
    """
    text = str(question or "")
    text_compact = compact(text)
    hits: list[str] = []
    candidates = sorted(
        {
            str(name)
            for name in (column_names or [])
            if _MIN_COLUMN_LENGTH <= len(compact(name))
            and compact(name) not in _GENERIC_COLUMNS
            and compact(name) in text_compact
        },
        key=len,
        reverse=True,
    )
    for name in candidates:
        if not phrase_in_text(text, name):
            continue
        # 더 긴 컬럼명에 포함되는 토막은 따로 세지 않는다.
        if any(compact(name) in compact(found) for found in hits):
            continue
        hits.append(name)
    return hits


def vq_output_gap(
    question: str,
    vq: dict,
    *,
    column_names: object = (),
) -> str:
    """VQ가 질문의 요구를 못 내놓는 첫 번째 이유. 낼 수 있으면 빈 문자열."""
    text = str(question or "")
    sql = str(vq.get("sql") or "")
    if not sql:
        return ""

    if _ABSOLUTE_PERIOD_RE.search(text) and not _vq_expresses_period(vq):
        return "질문의 기간 조건을 표현할 파라미터·시간 필터가 참고 SQL에 없습니다."

    # description 은 내부 구현 설명이라 개념어가 필터가 아닌 계산 결과로도 나온다.
    # managed_company_usage_anomalies 는 "무실적·급감·급증"을 판정 결과로 적는다.
    # 대상을 정의하는 건 name·question·tags 뿐이다.
    intent_text = " ".join(
        str(vq.get(key) or "") for key in ("name", "question")
    ) + " " + " ".join(str(tag) for tag in vq.get("tags") or [])
    for pattern, label in _DEFINING_CONCEPTS:
        if re.search(pattern, intent_text) and not re.search(pattern, text):
            return f"참고 SQL은 '{label}' 대상만 집계하는데 질문은 그 조건을 요구하지 않았습니다."

    group_by_text = _vq_group_by_text(sql)
    for axis in _axis_terms(text):
        if not phrase_in_text(group_by_text, axis) and compact(axis) not in compact(group_by_text):
            return f"질문이 요청한 '{axis}' 축이 참고 SQL의 GROUP BY에 없습니다."

    sql_compact = compact(sql)
    for column in named_columns(text, column_names):
        if compact(column) not in sql_compact:
            return f"질문이 이름을 댄 '{column}'이(가) 참고 SQL에 없습니다."

    return ""

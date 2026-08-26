"""컬럼명 → 질문 표면형 동의어 유도.

goldenset v2 의 SQL 오류 400건 중 332건이 "해당 컬럼이 스키마에 없습니다" 라는
자연어 답변(guard 차단)이었다. 원인은 컬럼이 없어서가 아니라, 프롬프트에 넣을
컬럼을 고를 때 쓰는 구문 매칭이 질문의 표면형을 컬럼명과 연결하지 못해 컬럼이
프롬프트 예산에서 잘려나갔기 때문이다.

    질문: "2026년 7월 기준 가맹점 상태구분별 가맹점 수를 알려줘"
    컬럼: tmdaaus01."가맹점상태구분코드"   (동의어 없음)  → 매칭 실패 → 프롬프트 누락

같은 컬럼이 tmdaa5e11 에는 동의어 ['가맹점상태', '가맹점 상태'] 를 갖고 있었는데
그마저도 "가맹점 상태구분별" 은 잡지 못한다. '가맹점상태' 다음에 오는 '구'가
어절 경계가 아니기 때문이다.

이 모듈은 두 가지를 제공한다.

1. derive_column_synonyms(): 컬럼명에서 접미사를 벗기고 띄어쓰기 변형을 만든다.
   생성 결과를 semantic layer 의 synonyms 에 넣으면 기존 매처로도 매칭된다.
2. phrase_in_text(): 구분자를 무시하는 매처. 코드를 함께 통합할 때 쓰면
   띄어쓰기 변형을 열거하지 않아도 된다.
"""

from __future__ import annotations

import re
from functools import lru_cache

_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_]+")
_SEPARATOR_RE = re.compile(r"[^0-9A-Za-z가-힣_]+")

# 조사. 원본 매처는 조사를 한 개만 허용해서 "회원자격별로" 처럼 조사가 겹친
# 표면형("별"+"로")을 놓쳤다. 실패 481건 중 상당수가 이 한 글자 때문이었다.
# 나열 조사가 빠져 있어 "가맹점번호랑 가맹점명", "건수랑 금액" 의 앞말이 컬럼으로
# 안 잡혔다. '이랑'은 '이'+'랑' 으로 이어 받으므로 '랑' 하나면 된다.
_PARTICLE = r"(?:으로|에서|에게|하고|의|은|는|이|가|을|를|에|로|와|과|도|만|별|인|랑)"
_PARTICLES = rf"(?:{_PARTICLE})*"

# 용언 활용과 예정 표현. 질문은 컬럼을 명사로만 부르지 않는다.
#   해지년월일         → "2026년 7월에 해지된 기업카드"
#   카드만료년월일      → "4분기에 만료 예정인 기업카드"
#   평일24시간운영여부   → "평일 24시간 운영하는 가맹점"
# 조사만 허용하던 lookahead 는 이 어미에서 전부 끊겼다. 어미 뒤에 다시 조사가
# 붙으므로("예정" + "인") 어미를 조사보다 앞에 둔다. 어미는 뒤따르는 글자가
# 어절 경계여야 하는 조건을 그대로 통과해야 해서, '카드' 가 '카드론' 에
# 걸리지 않는 성질은 유지된다.
_PREDICATE_TAIL = r"(?:되는|된|하는|한다|한|할|함|예정)?"

# 컬럼명 접미사. 긴 것부터 벗겨야 '구분코드' 가 '코드' 로 먼저 잘리지 않는다.
# 벗긴 중간 단계도 질문 표면형이 되므로 단계마다 후보로 남긴다.
_SUFFIX_CHAIN: tuple[tuple[str, ...], ...] = (
    ("구분코드명", "구분코드", "구분명", "구분"),
    ("코드명", "코드"),
    ("구분코드", "구분"),
    ("코드", ),
    ("구분명", "구분"),
    ("명", ),
)

# 복합어 안에서 질문이 띄어 쓰는 경계. 실패 질문에서 관측된 어휘만 넣었다.
#   자산건전성분류구분코드 → "자산건전성 분류별"
#   연체채권종류구분코드   → "연체채권 종류 기준으로"
#   충당금차주구분코드     → "충당금 차주 구분 기준으로"
_SEGMENT_WORDS: tuple[str, ...] = (
    "대분류",
    "중분류",
    "소분류",
    "분류",
    "종류",
    "유형",
    "구분",
    "등급",
    "그룹",
    "상태",
    "사유",
    "방법",
    "차주",
    "수준",
    "평점",
    "요율",
    "한도",
    "비중",
    "비율",
    "잔액",
    "금액",
    "건수",
    "좌수",
    "매체",
    "기관",
    "과목",
    "부점",
    "회차",
    "일수",
)

# 접미사를 벗기면 뜻이 사라지거나 너무 흔해서 오검출을 만드는 토막.
_TOO_GENERIC = frozenset(
    {
        "",
        "고객",
        "기업",
        "카드",
        "가맹점",
        "회원",
        "상품",
        "구분",
        "코드",
        "명",
        "여부",
        "금액",
        "건수",
        "번호",
        "일자",
        "년월",
        "년월일",
        "기준",
        "합계",
        "관리",
        "대상",
        "사용",
        "이용",
        "기간",
        "수량",
        "단위",
    }
)

_MIN_SYNONYM_LENGTH = 3

# 여부 컬럼은 앞의 엔티티 접두사를 떼고 부른다.
#   가맹점배달가능여부 → "배달이 가능한 가맹점이 몇 개인지"
# 엔티티 접두사를 떼면 2글자 서술어("배달")만 남으므로 여부 컬럼에서만 허용한다.
_ENTITY_PREFIXES: tuple[str, ...] = ("가맹점", "기업고객", "기업", "회원", "고객", "카드", "상품", "부점")
_MIN_BOOLEAN_STEM_LENGTH = 2

# 날짜 컬럼도 여부 컬럼과 같다. 질문은 시점을 사건으로 부른다.
#   카드신규발급년월일 → "7월에 신규 발급된 기업카드 좌수"
#   해지년월일        → "7월에 해지된 기업카드 좌수"
#   카드만료년월일     → "4분기에 만료 예정인 기업카드 좌수"
# 스키마의 날짜 컬럼 81개 중 78개가 동의어를 하나도 갖고 있지 않았다. 접미사를
# 벗기면 2글자 사건명("해지", "만료")만 남으므로 최소 길이를 따로 둔다.
_DATE_SUFFIXES: tuple[str, ...] = ("년월일", "년월", "일자", "일시")
_MIN_DATE_STEM_LENGTH = 2

# 표기 수식어. 질문은 이걸 떼고 부른다: 한글상품명 → "카드 상품명별로".
_NAME_MODIFIER_PREFIXES: tuple[str, ...] = ("한글", "영문")

# 접미사를 벗긴 어간은 2글자여도 업무 용어다: 국가코드 → "국가별",
# 한글시도명 → "시도별". 3글자 하한에 걸려 통째로 버려지던 어간이 17개였고,
# 뜻이 사라지는 토막은 _TOO_GENERIC 이 이미 걸러낸다.
_MIN_STEM_LENGTH = 2


def compact(text: str) -> str:
    """구분자를 없앤 비교용 표현."""
    return _SEPARATOR_RE.sub("", str(text or ""))


@lru_cache(maxsize=None)
def _phrase_pattern(needle: str) -> re.Pattern[str]:
    """글자 사이마다 구분자를 허용하는 매칭 패턴.

    테이블 랭킹은 질문 하나에 컬럼·동의어 수천 개를 대조하므로 패턴을 캐시한다.
    정규식 단어 문자 범주는 ASCII 식별자와 한글을 모두 처리하면서, 큰 한글 문자 범위를
    매 패턴마다 최적화하던 명시적 문자 클래스보다 컴파일이 훨씬 빠르다.
    """
    separator = r"\W*"
    body = separator.join(re.escape(char) for char in needle)
    return re.compile(
        rf"(?<!\w){body}(?={separator}{_PREDICATE_TAIL}{_PARTICLES}(?:\W|$))",
        flags=re.IGNORECASE,
    )


def phrase_in_text(text: str, phrase: object) -> bool:
    """질문의 띄어쓰기와 무관하게, 어절 경계를 지키며 구문을 찾는다.

    원본 매처는 phrase 에 적힌 토큰 사이에서만 구분자를 허용해서
    '기업카드신용한도금액' 이 "기업카드 신용한도금액" 을 놓쳤다. 여기서는 phrase 의
    글자 사이마다 구분자를 허용해 띄어쓰기 변형을 열거하지 않아도 되게 한다.

    질문 문자열 자체는 정규화하지 않으므로 조사 lookahead 가 그대로 살아 있고,
    '카드' 가 '카드론' 에 매칭되는 일은 계속 막힌다.
    """
    needle = compact(phrase)
    if not needle:
        return False
    return bool(_phrase_pattern(needle).search(str(text or "")))


# 코드 라벨의 표기 흔들림. 이 시스템의 도메인은 법인영업인데 코드북 라벨은 '기업'
# 으로만 적혀 있고("기업체크카드"), 질문은 '법인' 으로 온다("법인체크카드 매출").
# 라벨마다 별칭을 손으로 베껴 넣는 대신 한 쌍의 등가어로 처리한다. 코드북에 '기업'
# 이 붙은 라벨은 상품중분류(CP51~CP57)·개인기업구분·카드소유자구분에 걸쳐 있고,
# 코드북이 늘 때마다 목록을 다시 손봐야 하는 일을 만들지 않는다.
_LABEL_EQUIVALENTS: tuple[tuple[str, str], ...] = (("기업", "법인"),)


def label_surface_forms(label: object) -> list[str]:
    """코드 라벨을 질문이 실제로 부르는 표면형까지 넓힌다."""
    forms = [str(label or "")]
    for left, right in _LABEL_EQUIVALENTS:
        for form in list(forms):
            for swapped in (form.replace(left, right), form.replace(right, left)):
                if swapped != form and swapped not in forms:
                    forms.append(swapped)
    return forms


def _strip_suffixes(name: str) -> list[str]:
    """접미사를 단계적으로 벗긴 후보를 길이 내림차순으로 돌려준다."""
    candidates: list[str] = []
    for chain in _SUFFIX_CHAIN:
        head = chain[0]
        if not name.endswith(head):
            continue
        stem = name[: -len(head)]
        if not stem:
            continue
        # 같은 chain 안의 짧은 접미사들은 stem 에 다시 붙인 중간 표면형이 된다.
        for suffix in chain[1:]:
            candidate = stem + suffix
            if candidate != name and candidate not in candidates:
                candidates.append(candidate)
        if stem not in candidates:
            candidates.append(stem)
        break
    return candidates


def _boolean_variants(column: str) -> list[str]:
    """여부 컬럼의 서술어 표면형.

    질문은 여부 컬럼을 서술어로만 부른다.
        직영점여부         → "직영점인 가맹점이 몇 개인지"
        갱신카드여부       → "갱신 카드로 발급된"
        가맹점배달가능여부 → "배달이 가능한 가맹점"

    마지막 예처럼 엔티티 접두사까지 떼야 하는 경우가 있어 접두사 제거형도 만든다.
    남는 서술어가 2글자("배달")뿐일 수 있으므로 최소 길이를 따로 둔다.
    """
    if not column.endswith("여부"):
        return []

    variants: list[str] = []

    def offer(candidate: str) -> None:
        if len(candidate) >= _MIN_BOOLEAN_STEM_LENGTH and candidate not in variants:
            variants.append(candidate)

    offer(column.removesuffix("여부"))
    offer(column.removesuffix("가능여부"))

    for prefix in _ENTITY_PREFIXES:
        if not column.startswith(prefix) or len(column) <= len(prefix) + len("여부"):
            continue
        stripped = column[len(prefix) :]
        offer(stripped)
        offer(stripped.removesuffix("여부"))
        offer(stripped.removesuffix("가능여부"))
        break

    return [term for term in variants if term not in _TOO_GENERIC]


def _date_variants(column: str) -> list[str]:
    """날짜 컬럼의 사건 표면형.

    엔티티 접두사까지 떼야 질문의 어순과 만난다. "신규 발급된 기업카드" 는
    '카드신규발급' 이 아니라 '신규발급' 으로만 잡힌다.
    """
    suffix = next((item for item in _DATE_SUFFIXES if column.endswith(item)), "")
    if not suffix or len(column) <= len(suffix):
        return []

    variants: list[str] = []

    def offer(candidate: str) -> None:
        if len(candidate) >= _MIN_DATE_STEM_LENGTH and candidate not in variants:
            variants.append(candidate)

    stem = column[: -len(suffix)]
    offer(stem)
    for prefix in _ENTITY_PREFIXES:
        if stem.startswith(prefix) and len(stem) > len(prefix):
            offer(stem[len(prefix) :])
            break

    return [term for term in variants if term not in _TOO_GENERIC]


# 월 지표 컬럼의 기준월 접두사. 한 행이 이미 기준년월 한 달이라서 이 접두사는 그
# 행의 달을 가리키는 표기일 뿐이고, 질문은 접두사를 빼고 지표만 부른다.
#   금월체크카드이용금액   → "2025년 11월 기업카드 체크카드 이용금액을 발급유형별로"
#   금월유이자할부이용금액 → "유이자 할부 이용금액을 카드거래매체별로"
# 스키마의 금월 컬럼 75개 전부가 이 표면형을 동의어로 갖고 있지 않아서, 질문이
# 지표를 불러도 컬럼이 프롬프트 예산에서 잘려 나갔다.
# 전월·전년·금년·최근 접두사는 그 행의 달이 아닌 다른 기간을 가리키므로 벗기지 않는다.
_REFERENCE_MONTH_PREFIXES: tuple[str, ...] = ("금월", "당월")
_MIN_REFERENCE_MONTH_STEM_LENGTH = 3


def _reference_month_variants(column: str, reserved: frozenset[str]) -> list[str]:
    """기준월 접두사를 뗀 지표 표면형.

    벗긴 어간이 다른 컬럼의 이름 그대로면(금월CA수수료 → CA수수료) 그 표면형은
    이미 그 컬럼의 것이므로 동의어로 만들지 않는다.
    """
    for prefix in _REFERENCE_MONTH_PREFIXES:
        if not column.startswith(prefix):
            continue
        stem = column[len(prefix) :]
        if len(compact(stem)) < _MIN_REFERENCE_MONTH_STEM_LENGTH:
            return []
        if compact(stem) in reserved:
            return []
        return [stem]
    return []


def _name_modifier_variants(column: str) -> list[str]:
    """표기 수식어를 뗀 이름 컬럼: 한글상품명 → 상품명."""
    if not column.endswith("명"):
        return []
    for prefix in _NAME_MODIFIER_PREFIXES:
        if column.startswith(prefix) and len(column) > len(prefix) + 1:
            return [column[len(prefix) :]]
    return []


def _space_variants(term: str) -> list[str]:
    """한글↔영숫자 경계와 복합어 경계에 공백을 넣은 변형."""
    variants: list[str] = []

    # 통합BSS평점 → 통합 BSS 평점 / 기대1년PD율 → 기대 1년 PD율
    boundary = re.sub(r"(?<=[가-힣])(?=[0-9A-Za-z])", " ", term)
    boundary = re.sub(r"(?<=[0-9A-Za-z])(?=[가-힣])", " ", boundary)
    if boundary != term:
        variants.append(boundary)

    # 자산건전성분류 → 자산건전성 분류 (경계 어휘가 끝에 붙어 있을 때만)
    for word in _SEGMENT_WORDS:
        if not term.endswith(word) or len(term) <= len(word):
            continue
        stem = term[: -len(word)]
        if len(stem) < 2:
            continue
        candidate = f"{stem} {word}"
        if candidate not in variants:
            variants.append(candidate)
        # 앞부분도 영숫자 경계를 가질 수 있다: KB카드BC카드구분 → KB카드 BC카드 구분
        nested = re.sub(r"(?<=[가-힣])(?=[0-9A-Za-z])", " ", stem)
        nested = re.sub(r"(?<=[0-9A-Za-z])(?=[가-힣])", " ", nested)
        if nested != stem:
            candidate = f"{nested} {word}"
            if candidate not in variants:
                variants.append(candidate)
        break

    return variants


def derive_column_synonyms(
    name: str,
    existing: object = None,
    reserved: object = (),
) -> list[str]:
    """컬럼명에서 질문 표면형 동의어를 만든다.

    이미 있는 동의어는 결과에서 제외하고 새로 추가할 것만 돌려준다.
    ``reserved`` 는 스키마에 실제로 있는 컬럼명 모음이다. 접두사를 벗긴 어간이
    거기 있으면 그 표면형은 그 컬럼의 것이므로 동의어로 만들지 않는다.
    """
    column = str(name or "").strip()
    if not column:
        return []

    known = {compact(column)}
    for value in existing or []:
        known.add(compact(value))
    reserved_names = {compact(value) for value in reserved or ()} - {compact(column)}

    derived: list[str] = []

    def add(term: str, min_length: int = _MIN_SYNONYM_LENGTH) -> None:
        term = term.strip()
        if len(compact(term)) < min_length:
            return
        if compact(term) in _TOO_GENERIC or term in _TOO_GENERIC:
            return
        if compact(term) in known:
            return
        known.add(compact(term))
        derived.append(term)

    bases = _strip_suffixes(column)
    for term in _name_modifier_variants(column):
        add(term)
        bases = [*bases, term, *_strip_suffixes(term)]
    for base in bases:
        add(base, min_length=_MIN_STEM_LENGTH)

    for term in _boolean_variants(column):
        add(term, min_length=_MIN_BOOLEAN_STEM_LENGTH)

    for term in _date_variants(column):
        add(term, min_length=_MIN_DATE_STEM_LENGTH)

    # 질문은 분류 단위를 생략하고 부르기도 한다: CA한도등급코드 → "CA한도별",
    # 통합한도등급코드 → "통합한도 기준으로". 접미사를 벗긴 뒤 경계 어휘까지
    # 한 번 더 떼어 본다. 너무 짧거나 흔한 토막은 add() 가 걸러낸다.
    trimmed: list[str] = []
    for base in bases:
        for word in _SEGMENT_WORDS:
            if base.endswith(word) and len(base) > len(word):
                stem = base[: -len(word)]
                if stem not in trimmed:
                    trimmed.append(stem)
                break
    for stem in trimmed:
        add(stem)
    bases = [*bases, *trimmed]

    # 질문은 엔티티 접두사를 떼고 부른다: 가맹점사업주체구분코드 → "사업주체별로",
    # 가맹점해지사유구분코드 → "해지사유 기준으로". 접두사 제거가 여부·날짜 컬럼에만
    # 있어서, 코드·구분 컬럼의 동의어는 전부 물리 컬럼명의 접두사를 그대로 달고
    # 있었다. 그래서 사용자 표면형이 하나도 걸리지 않고 컬럼이 프롬프트 예산에서
    # 잘려 나갔다(날짜 컬럼 15개가 예산을 다 먹은 tmdaa5d01).
    stripped: list[str] = []
    for base in bases:
        for prefix in _ENTITY_PREFIXES:
            if not base.startswith(prefix) or len(base) <= len(prefix):
                continue
            stem = base[len(prefix) :]
            # 벗긴 어간이 다른 컬럼의 이름 그대로면 그 표면형은 그 컬럼의 것이다.
            if compact(stem) not in reserved_names and stem not in stripped:
                stripped.append(stem)
            break
    for stem in stripped:
        add(stem)
    bases = [*bases, *stripped]

    # 기준월 접두사를 뗀 지표는 경계 어휘까지 더 떼지 않는다. "체크카드이용" 까지
    # 가면 금월체크카드이용건수 같은 다른 지표 질문에도 이 컬럼이 잡힌다.
    for term in _reference_month_variants(column, frozenset(reserved_names)):
        add(term, min_length=_MIN_REFERENCE_MONTH_STEM_LENGTH)
        bases = [*bases, term]

    # 띄어쓰기 변형은 원본 컬럼명과 벗긴 후보 모두에 대해 만든다.
    for term in [column, *bases]:
        for variant in _space_variants(term):
            # 변형은 compact 가 같아도 기존 매처에는 새로운 표면형이므로
            # known 중복 검사를 우회해서 넣는다.
            candidate = variant.strip()
            if len(compact(candidate)) < _MIN_SYNONYM_LENGTH:
                continue
            if candidate not in derived and candidate != column:
                derived.append(candidate)

    return derived

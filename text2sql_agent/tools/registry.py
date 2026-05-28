"""Tool metadata registry used by the graph's tool-selection path."""

from .bad_debt import _tool_fn_대손비용률
from .sql_builders import (
    _tool_sql_가맹점매출순위,
    _tool_sql_가맹점카드소지현황,
    _tool_sql_기업별연체현황,
    _tool_sql_심사승인율,
    _tool_sql_업종별매출,
    _tool_sql_월별이용금액,
    _tool_sql_카드등급별연체,
    _tool_sql_한도사용률,
)

# ---------------------------------------------------------------------------
# 6. Tool 등록 (required 플래그 포함)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "대손비용률_분석",
        "description": "대손비용률(충당금+상각 기반) 분석을 수행합니다. 가맹점(기업)의 월초/월말 충당금, 상각금액을 종합하여 대손비용률, 보정계수, 보정계수반영 대손율을 계산합니다. 대손율/대손률 표현도 이 Tool로 처리합니다. 엑셀 보고서를 생성합니다.",
        "parameters": [
            {"name": "가맹점명", "type": "string", "description": "분석 대상 가맹점(기업) 이름", "required": True},
            {"name": "기준년월", "type": "string", "description": "분석 기준 년월 (YYYYMM)", "required": True},
            {"name": "LS", "type": "number", "description": "대출 스프레드 (LS)", "required": True},
            {"name": "IS", "type": "number", "description": "투자 스프레드 (IS)", "required": True},
        ],
        "fn": _tool_fn_대손비용률,
        "tags": ["대손률", "대손율", "대손비율", "대손비용률", "대손비용율", "충당금", "상각", "보정계수"],
    },
    {
        "name": "심사승인율_조회",
        "description": "기업카드 심사 승인율을 조회합니다. 승인율 = 승인건수 / 전체심사건수 x 100. 신용등급별 조회도 가능합니다.",
        "parameters": [
            {"name": "기간_시작", "type": "string", "description": "조회 시작 (YYYYMM 또는 YYYYMMDD)", "required": True},
            {"name": "기간_종료", "type": "string", "description": "조회 종료 (YYYYMM 또는 YYYYMMDD)", "required": True},
            {"name": "신용등급", "type": "string", "description": "특정 신용평가등급코드", "required": False},
            {"name": "등급별", "type": "boolean", "description": "true이면 신용등급별 그룹핑", "required": False},
        ],
        "fn": _tool_sql_심사승인율,
        "tags": ["심사", "승인율", "승인", "거절", "심사현황"],
    },
    {
        "name": "월별_법인카드_이용금액",
        "description": "월별 법인카드 이용금액 추이를 조회합니다. 일시불/할부/CA 세부 항목 포함.",
        "parameters": [
            {"name": "기간_시작", "type": "string", "description": "조회 시작 기준년월 (YYYYMM)", "required": True},
            {"name": "기간_종료", "type": "string", "description": "조회 종료 기준년월 (YYYYMM)", "required": True},
        ],
        "fn": _tool_sql_월별이용금액,
        "tags": ["이용금액", "월별", "법인카드", "추이", "일시불", "할부", "CA"],
    },
    {
        "name": "가맹점_매출_순위",
        "description": "매출 상위 가맹점 순위를 조회합니다. 업종 필터링, TOP-N 조회 가능.",
        "parameters": [
            {"name": "기간_시작", "type": "string", "description": "조회 시작 기준년월 (YYYYMM)", "required": True},
            {"name": "기간_종료", "type": "string", "description": "조회 종료 기준년월 (YYYYMM)", "required": True},
            {"name": "업종", "type": "string", "description": "업종 대분류명 필터", "required": False},
            {"name": "가맹점명", "type": "string", "description": "가맹점명 필터", "required": False},
            {"name": "limit", "type": "integer", "description": "상위 N개 (기본 10)", "required": False},
        ],
        "fn": _tool_sql_가맹점매출순위,
        "tags": ["가맹점", "매출", "순위", "랭킹", "TOP", "상위"],
    },
    {
        "name": "가맹점_카드소지_실적_조회",
        "description": "가맹점 기본정보, 업종명, 기준년월 실적을 결합하고 대표자 개인카드 및 기업고객 기업카드 소지 여부를 함께 조회합니다. 개인카드 미보유/기업카드 미보유 영업대상 추출에 사용합니다.",
        "parameters": [
            {"name": "기준년월", "type": "string", "description": "가맹점 실적 기준년월 (YYYYMM)", "required": True},
            {"name": "카드만료기준일", "type": "string", "description": "유효 카드 판단 기준일 (YYYYMMDD). 미입력 시 기준년월의 월말일", "required": False},
            {"name": "가맹점명", "type": "string", "description": "가맹점명 필터 (부분일치)", "required": False},
            {"name": "업종", "type": "string", "description": "가맹점업종명 또는 업종대분류코드명 필터", "required": False},
            {"name": "보유구분", "type": "string", "description": "전체, 개인카드보유, 개인카드미보유, 기업카드보유, 기업카드미보유, 둘다보유, 둘다미보유", "required": False},
            {"name": "limit", "type": "integer", "description": "결과 행수 제한 (기본 100)", "required": False},
        ],
        "fn": _tool_sql_가맹점카드소지현황,
        "tags": ["가맹점", "업종", "실적", "당월매출", "개인카드", "기업카드", "카드소지", "미보유", "영업대상", "대표자"],
    },
    {
        "name": "한도사용률_조회",
        "description": "기업별 한도 사용률을 조회합니다. 한도사용률 = 카드이용합계금액 / 총한도금액 x 100.",
        "parameters": [
            {"name": "기간_시작", "type": "string", "description": "조회 시작 기준년월 (YYYYMM)", "required": True},
            {"name": "기간_종료", "type": "string", "description": "조회 종료 기준년월 (YYYYMM)", "required": True},
            {"name": "기업명", "type": "string", "description": "특정 기업 상호명", "required": False},
            {"name": "limit", "type": "integer", "description": "결과 행수 제한 (기본 20)", "required": False},
        ],
        "fn": _tool_sql_한도사용률,
        "tags": ["한도", "사용률", "여신", "한도소진율"],
    },
    {
        "name": "업종별_매출_분석",
        "description": "업종별 매출금액과 건수를 조회합니다. 법인카드 기업 거래 기준.",
        "parameters": [
            {"name": "기간_시작", "type": "string", "description": "조회 시작 (YYYYMM 또는 YYYYMMDD)", "required": True},
            {"name": "기간_종료", "type": "string", "description": "조회 종료 (YYYYMM 또는 YYYYMMDD)", "required": True},
            {"name": "업종", "type": "string", "description": "업종 대분류명 필터", "required": False},
            {"name": "limit", "type": "integer", "description": "결과 행수 제한", "required": False},
        ],
        "fn": _tool_sql_업종별매출,
        "tags": ["업종", "매출", "업종별"],
    },
    {
        "name": "기업별_연체_현황",
        "description": "연체가 발생한 기업들의 연체 현황을 조회합니다. 연체금액, 연체율 포함.",
        "parameters": [
            {"name": "기간_시작", "type": "string", "description": "조회 시작 기준년월 (YYYYMM)", "required": True},
            {"name": "기간_종료", "type": "string", "description": "조회 종료 기준년월 (YYYYMM)", "required": True},
            {"name": "기업명", "type": "string", "description": "특정 기업 상호명", "required": False},
            {"name": "limit", "type": "integer", "description": "결과 행수 제한 (기본 20)", "required": False},
        ],
        "fn": _tool_sql_기업별연체현황,
        "tags": ["연체", "연체현황", "기업별", "리스크"],
    },
    {
        "name": "카드등급별_연체_현황",
        "description": "카드등급별 연체 현황을 조회합니다. 등급별 카드수, 이용금액, 연체원금, 연체율 포함.",
        "parameters": [
            {"name": "기간_시작", "type": "string", "description": "조회 시작 기준년월 (YYYYMM)", "required": True},
            {"name": "기간_종료", "type": "string", "description": "종료 기준년월 (YYYYMM)", "required": True},
        ],
        "fn": _tool_sql_카드등급별연체,
        "tags": ["카드등급", "연체", "등급별"],
    },
]

TOOL_MAP: dict[str, dict] = {t["name"]: t for t in TOOLS}


def _build_tool_descriptions() -> str:
    lines = []
    for i, tool in enumerate(TOOLS):
        params_strs = []
        for p in tool["parameters"]:
            req = " [필수]" if p.get("required") else ""
            params_strs.append(f"    - {p['name']} ({p['type']}){req}: {p['description']}")
        lines.append(f"[{i}] {tool['name']}: {tool['description']}")
        lines.append("  파라미터:")
        lines.extend(params_strs)
        lines.append("")
    return "\n".join(lines)

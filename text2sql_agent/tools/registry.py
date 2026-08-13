"""Tool metadata registry used by the graph's tool-selection path."""

from ..config import DEFAULT_QUERY_ROW_LIMIT

from .bad_debt import _tool_fn_대손비용률
from .sql_builders import (
    _tool_sql_corporate_card_active_no_usage_members,
    _tool_sql_corporate_card_churned_after_usage_members,
    _tool_sql_corporate_check_card_only_high_monthly_avg,
    _tool_sql_merchant_corporate_sales_target_no_corporate_card,
    _tool_sql_corporate_limit_low_utilization_members,
)

def _tool_fn_verified_query(sql_query_name: str, **kwargs):
    return {
        "sql_query_name": sql_query_name,
        "parameters": kwargs,
        "execution_mode": "athena_select_only",
    }


def _make_verified_query_tool_fn(sql_query_name: str):
    def _fn(**kwargs):
        return _tool_fn_verified_query(sql_query_name, **kwargs)
    _fn.__name__ = f"_tool_fn_{sql_query_name}"
    return _fn

EXCEL_CASE_SQL_TOOLS: list[dict] = [
        {
            'name': 'corporate_card_active_no_usage_members',
            'description': '입력 기준년월에 유효 기업카드를 보유하고 있으나 기준월 포함 최근 6개월 신용/체크 이용금액이 없는 기업회원 명단을 tmdaa1d12에서 조회합니다.',
            'parameters': [{'name': '기준년월', 'type': 'string', 'description': '현재 카드 보유 여부와 무실적 판단 기준년월(YYYYMM)', 'required': True, 'default': '202604'}, {'name': '조회개월수', 'type': 'integer', 'description': '기준월을 포함한 무실적 판정 개월 수', 'required': False, 'default': 6}, {'name': 'limit', 'type': 'integer', 'description': '상세 목록 제한 건수', 'required': False, 'default': 100}],
            'sql_query_name': 'corporate_card_active_no_usage_members',
            'execution_mode': 'athena_select_only',
            'tags': ['신규영업', '6무', '무실적', '기업회원', '기업카드', '보유', '명단', 'tmdaa1d12', 'Athena', 'SELECT_ONLY', 'AI영업비서_CASE'],
            'fn': _tool_sql_corporate_card_active_no_usage_members,
        },
        {
            'name': 'corporate_card_churned_after_usage_members',
            'description': '지정 기간 중 이용 이력이 있으나 기준월 현재 유효 기업 신용/체크카드 수가 0인 기업회원과 탈회를 하거나 해지한 기업 회원 명단을 검색할 때 사용',
            'parameters': [{'name': '기준년월', 'type': 'string', 'description': '현재 탈회/해지 여부 판단 기준년월(YYYYMM)', 'required': True, 'default': '202604'}, {'name': '조회개월수', 'type': 'integer', 'description': '기준월을 포함한 과거 이용 이력 조회 개월 수', 'required': False, 'default': 6}, {'name': 'limit', 'type': 'integer', 'description': '상세 목록 제한 건수', 'required': False, 'default': 100}],
            'sql_query_name': 'corporate_card_churned_after_usage_members',
            'execution_mode': 'athena_select_only',
            'tags': ['신규영업', '이탈회원', '탈회', '해지', '기업회원', '기업카드', '이용이력', 'tbdaa1d12', 'Athena', 'SELECT_ONLY', 'AI영업비서_CASE'],
            'fn': _tool_sql_corporate_card_churned_after_usage_members,
        },
        {
            'name': 'corporate_check_card_only_high_monthly_avg',
            'description': '지정 기간 동안 기업 체크카드만 보유하고 신용카드 이용금액이 없는 기업회원의 체크 이용금액 월평균을 계산해 기준 금액 이상인 기업회원 명단을 조회합니다. Excel CASE 3번을 Athena SELECT-only로 변환했습니다.',
            'parameters': [{'name': '기준년월', 'type': 'string', 'description': '보유·이용과 월평균을 판단할 기준년월(YYYYMM)', 'required': True, 'default': '202604'}, {'name': '월평균금액', 'type': 'integer', 'description': '월평균 체크카드 이용금액 기준', 'required': False, 'default': 50000000}, {'name': 'limit', 'type': 'integer', 'description': '상세 목록 제한 건수', 'required': False, 'default': 100}],
            'sql_query_name': 'corporate_check_card_only_high_monthly_avg',
            'execution_mode': 'athena_select_only',
            'tags': ['신규영업', '교차회원', '체크카드만', '신용카드미보유', '월평균', '5천만원', 'tbdaa1d12', 'Athena', 'SELECT_ONLY', 'AI영업비서_CASE'],
            'fn': _tool_sql_corporate_check_card_only_high_monthly_avg,
        },
        {
            'name': 'merchant_corporate_sales_target_no_corporate_card',
            'description': '기업영업 가맹점 테이블에서 기준월 월 매출액이 기준 이상이고 법인사업자이며 유효 기업 신용/체크카드를 보유하지 않은 가맹점/기업회원 후보를 조회합니다. 매출액 기준으로 법인사업자이며 KB국민 기업카드를 보유하고 있지 않은 기업 회원을 조회합니다',
            'parameters': [{'name': '기준년월', 'type': 'string', 'description': '가맹점 월 매출 판단 기준년월(YYYYMM)', 'required': True, 'default': '202604'}, {'name': '월매출금액', 'type': 'integer', 'description': '월 매출액 하한 기준', 'required': False, 'default': 100000000}, {'name': 'limit', 'type': 'integer', 'description': '상세 목록 제한 건수', 'required': False, 'default': 100}],
            'sql_query_name': 'merchant_corporate_sales_target_no_corporate_card',
            'execution_mode': 'athena_select_only',
            'tags': ['신규영업', '가맹점', '월매출', '1억원', '법인사업자', '기업카드미보유', '영업대상', '기업 회원'],
            'fn': _tool_sql_merchant_corporate_sales_target_no_corporate_card,
        },
        {
            'name': 'corporate_limit_low_utilization_members',
            'description': '기준월 현재 유효 기업카드를 보유하고 총한도금액이 기준 이상이며 한도소진율이 기준 미만인 기업회원 명단을 조회합니다. 한도소진율은 1 - 잔여한도/총한도로 계산합니다. Excel CASE 5번을 Athena SELECT-only로 변환했습니다.',
            'parameters': [{'name': '기준년월', 'type': 'string', 'description': '한도와 카드 보유 여부 판단 기준년월(YYYYMM)', 'required': True, 'default': '202604'}, {'name': '한도금액', 'type': 'integer', 'description': '기업 총한도 하한 기준', 'required': False, 'default': 20000000}, {'name': '한도소진율', 'type': 'number', 'description': '한도소진율 상한 기준. 50%는 0.5', 'required': False, 'default': 0.5}, {'name': 'limit', 'type': 'integer', 'description': '상세 목록 제한 건수', 'required': False, 'default': 100}],
            'sql_query_name': 'corporate_limit_low_utilization_members',
            'execution_mode': 'athena_select_only',
            'tags': ['신규영업', '한도', '한도소진율', '잔여한도', '2천만원', '50%미만', '기업회원', 'tbdaa1d12', 'Athena', 'SELECT_ONLY', 'AI영업비서_CASE'],
            'fn': _tool_sql_corporate_limit_low_utilization_members,
        },
        {
            'name': 'new_sales_targets_usage_amount_detail',
            'description': 'Excel CASE 6번의 대상군 산정 로직을 Athena SELECT-only로 변환했습니다. 원본 쿼리는 업종 차원 컬럼 없이 대상 구분별 이용금액 상세를 집계하므로, 본 템플릿도 대상 구분별 이용금액 상세를 반환합니다.',
            'parameters': [{'name': '기준년월', 'type': 'string', 'description': '이용금액 상세 집계 기준년월(YYYYMM)', 'required': True, 'default': '202604'}, {'name': '최근6개월_시작', 'type': 'string', 'description': '6무/이탈 산정 시작 기준년월(YYYYMM)', 'required': True, 'default': '202511'}, {'name': '교차기간_시작', 'type': 'string', 'description': '교차회원 산정 시작 기준년월(YYYYMM)', 'required': True, 'default': '202601'}, {'name': '월매출금액', 'type': 'integer', 'description': '가맹점 대상군 월 매출액 하한 기준', 'required': False, 'default': 100000000}],
            'sql_query_name': 'new_sales_targets_usage_amount_detail',
            'execution_mode': 'athena_select_only',
            'tags': ['신규영업', '이용금액상세', '대상군', '6무', '이탈', '교차', '가맹점', 'tbdaa1d12', 'tbdaaus01', 'Athena', 'SELECT_ONLY', 'AI영업비서_CASE'],
            'fn': _make_verified_query_tool_fn('new_sales_targets_usage_amount_detail'),
        },
        {
            'name': 'managed_corporate_usage_trend_monitoring',
            'description': '파라미터로 직접 입력한 요청별 관리기업 목록을 VALUES CTE로 적용해 최근 6개월 이용금액과 특이사항을 조회합니다.',
            'parameters': [{'name': '관리기업목록', 'type': 'business_number_list', 'description': '요청별 10자리 사업자등록번호 목록', 'required': True}, {'name': '기준년월', 'type': 'string', 'description': '모니터링 기준년월(YYYYMM)', 'required': True, 'default': '202604'}, {'name': '기간_시작', 'type': 'string', 'description': '최근 6개월 시작 기준년월(YYYYMM)', 'required': True, 'default': '202511'}, {'name': 'limit', 'type': 'integer', 'description': '상세 목록 제한 건수', 'required': False, 'default': 100}],
            'sql_query_name': 'managed_corporate_usage_trend_monitoring',
            'execution_mode': 'athena_select_only',
            'tags': ['관리영업', '이용금액추이', '모니터링', '특이사항', '급감', '급증', '무실적', '관리기업', 'tbdaa1d12', 'Athena', 'SELECT_ONLY', 'AI영업비서_CASE'],
            'fn': _make_verified_query_tool_fn('managed_corporate_usage_trend_monitoring'),
        },
        {
            'name': 'managed_corporate_delinquency_current',
            'description': '파라미터로 직접 입력한 요청별 관리기업 목록을 VALUES CTE로 적용해 기준월 연체 기업회원을 조회합니다.',
            'parameters': [{'name': '관리기업목록', 'type': 'business_number_list', 'description': '요청별 10자리 사업자등록번호 목록', 'required': True}, {'name': '기준년월', 'type': 'string', 'description': '연체 여부 확인 기준년월(YYYYMM)', 'required': True, 'default': '202604'}, {'name': 'limit', 'type': 'integer', 'description': '상세 목록 제한 건수', 'required': False, 'default': 100}],
            'sql_query_name': 'managed_corporate_delinquency_current',
            'execution_mode': 'athena_select_only',
            'tags': ['관리영업', '연체', '연체여부', '연체일수', '연체금액', '관리기업', 'tbdaa1d12', 'Athena', 'SELECT_ONLY', 'AI영업비서_CASE'],
            'fn': _make_verified_query_tool_fn('managed_corporate_delinquency_current'),
        },
        {
            'name': 'managed_corporate_limit_down_monitoring',
            'description': '파라미터로 직접 입력한 요청별 관리기업 목록을 VALUES CTE로 적용해 전월 대비 한도 감액 기업회원을 조회합니다.',
            'parameters': [{'name': '관리기업목록', 'type': 'business_number_list', 'description': '요청별 10자리 사업자등록번호 목록', 'required': True}, {'name': '기준년월', 'type': 'string', 'description': '한도 감액 확인 기준월(YYYYMM)', 'required': True, 'default': '202604'}, {'name': '전월기준년월', 'type': 'string', 'description': '비교 대상 전월 기준년월(YYYYMM)', 'required': True, 'default': '202603'}, {'name': 'limit', 'type': 'integer', 'description': '상세 목록 제한 건수', 'required': False, 'default': 100}],
            'sql_query_name': 'managed_corporate_limit_down_monitoring',
            'execution_mode': 'athena_select_only',
            'tags': ['관리영업', '한도감액', '한도', '감액금액', '전월대비', '관리기업', 'tbdaa1d12', 'Athena', 'SELECT_ONLY', 'AI영업비서_CASE'],
            'fn': _make_verified_query_tool_fn('managed_corporate_limit_down_monitoring'),
        }
]
for _tool in EXCEL_CASE_SQL_TOOLS:
    for _parameter in _tool.get("parameters", []):
        if _parameter.get("name") == "limit" and _parameter.get("default") == 100:
            _parameter["default"] = DEFAULT_QUERY_ROW_LIMIT
# ---------------------------------------------------------------------------
# 6. Tool 등록 (required 플래그 포함)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "대손비용률_분석",
        "description": "대손비용률(충당금+상각 기반) 분석을 수행합니다. 가맹점(기업)의 월초/월말 충당금, 상각금액을 종합하여 대손비용률, 보정계수, 보정계수반영 대손율을 계산합니다. 대손율/대손률 표현도 이 Tool로 처리합니다. 엑셀 보고서를 생성합니다.",
        "parameters": [
            {"name": "가맹점명", "type": "string", "description": "분석 대상 가맹점(기업) 이름", "required": True},
            {"name": "이름정확일치", "type": "boolean", "description": "이름 고정·이름만·정확 일치 요청일 때만 true", "required": False},
            {"name": "기준년월", "type": "string", "description": "분석 기준 년월 (YYYYMM)", "required": True},
            {"name": "LS", "type": "number", "description": "대출 스프레드 (LS)", "required": True},
            {"name": "IS", "type": "number", "description": "투자 스프레드 (IS)", "required": True},
        ],
        "fn": _tool_fn_대손비용률,
        "tags": ["대손률", "대손율", "대손비율", "대손비용률", "대손비용율", "충당금", "상각", "보정계수"],
    },
]

TOOLS.extend(EXCEL_CASE_SQL_TOOLS)

TOOL_MAP: dict[str, dict] = {t["name"]: t for t in TOOLS}


def _build_tool_descriptions(tools: list[dict] | None = None) -> str:
    lines = []
    for i, tool in enumerate(tools or TOOLS):
        params_strs = []
        for p in tool["parameters"]:
            req = " [필수]" if p.get("required") else ""
            params_strs.append(f"    - {p['name']} ({p['type']}){req}: {p['description']}")
        lines.append(f"[{i}] {tool['name']}: {tool['description']}")
        lines.append("  파라미터:")
        lines.extend(params_strs)
        lines.append("")
    return "\n".join(lines)

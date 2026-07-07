"""Tool metadata registry used by the graph's tool-selection path."""

from .bad_debt import _tool_fn_대손비용률

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
]

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

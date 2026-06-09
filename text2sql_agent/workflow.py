"""LangGraph node implementations, routing, and execution helpers."""

import json
import re
from datetime import datetime
from typing import Literal

import sqlparse
import yaml
from langgraph.graph import END, StateGraph

from .config import DB_BACKEND, DB_SCHEMA, DB_SCHEMA_PREFIX, ENABLE_EMBEDDING_PRECOMPUTE, EMBED_MATCH_THRESHOLD
from .db import execute_sql, prepare_sql_for_backend
from .exports import _get_source_label
from .llm import _call_llm, _cosine_similarity, _get_embedding, _get_embeddings_batch
from .schema import (
    SCHEMA,
    VERIFIED_QUERIES,
    _build_domain_embedding_text,
    _keyword_rule_domain_scores,
    _metric_entity_domain_scores,
    _needs_domain_adjudication,
    _adjudicate_domain_with_llm,
    _result_summary,
    _validate_sql_against_schema,
    _weighted_domain_scores,
    build_domain_context,
    build_glossary_summary,
    build_metrics_summary,
    build_semantic_contract_summary,
    build_semantic_join_context,
    build_table_summary,
    find_relevant_queries,
    find_relevant_references,
)
from .state import Text2SQLState
from .tools.registry import TOOLS, TOOL_MAP, _build_tool_descriptions
from .tools.sql_builders import VQ_PARAM_SPECS, _apply_params_to_vq, _current_date_context

# ---------------------------------------------------------------------------
# 9. 그래프 노드
# ---------------------------------------------------------------------------

EMBEDDINGS_AVAILABLE = False
VQ_EMBEDDINGS: list[list[float]] = []
DOMAIN_EMBEDDINGS_AVAILABLE = False
DOMAIN_EMBEDDINGS: dict[str, list[float]] = {}
_EMBEDDINGS_INITIALIZED = False


def _coerce_llm_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value).strip()


def _strip_llm_code_fence(value: object) -> str:
    text = _coerce_llm_text(value)
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return text.strip()


def _parse_llm_json(value: object) -> dict:
    if isinstance(value, dict):
        return value
    text = _strip_llm_code_fence(value)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _precompute_embeddings():
    global EMBEDDINGS_AVAILABLE, VQ_EMBEDDINGS, DOMAIN_EMBEDDINGS_AVAILABLE, DOMAIN_EMBEDDINGS, _EMBEDDINGS_INITIALIZED
    if _EMBEDDINGS_INITIALIZED:
        return
    _EMBEDDINGS_INITIALIZED = True

    if not ENABLE_EMBEDDING_PRECOMPUTE:
        EMBEDDINGS_AVAILABLE = False
        DOMAIN_EMBEDDINGS_AVAILABLE = False
        return

    if VERIFIED_QUERIES:
        try:
            texts = []
            for vq in VERIFIED_QUERIES:
                text = vq["question"]
                tags = vq.get("tags", [])
                if tags:
                    text += " " + " ".join(tags)
                desc = vq.get("description", "")
                if desc:
                    text += " " + desc.strip()
                texts.append(text)
            VQ_EMBEDDINGS = _get_embeddings_batch(texts)
            EMBEDDINGS_AVAILABLE = True
            print(f"[초기화] Embedding 사전 계산 완료: {len(VQ_EMBEDDINGS)}개 verified_queries")
        except Exception as e:
            print(f"[초기화] Verified Query embedding 사전 계산 실패 → LLM 매칭 폴백 사용: {e}")
            EMBEDDINGS_AVAILABLE = False

    domains = SCHEMA.get("canonical_domains", [])
    if domains:
        try:
            domain_names = [domain["name"] for domain in domains if domain.get("name")]
            domain_texts = [_build_domain_embedding_text(SCHEMA, domain) for domain in domains if domain.get("name")]
            embeddings = _get_embeddings_batch(domain_texts)
            DOMAIN_EMBEDDINGS = dict(zip(domain_names, embeddings))
            DOMAIN_EMBEDDINGS_AVAILABLE = True
            print(f"[초기화] Domain embedding 사전 계산 완료: {len(DOMAIN_EMBEDDINGS)}개 domains")
        except Exception as e:
            print(f"[초기화] Domain embedding 사전 계산 실패 → keyword/rule 라우팅 사용: {e}")
            DOMAIN_EMBEDDINGS_AVAILABLE = False


_AMBIGUOUS_TARGET_PATTERNS = [
    r"(해당|그|이|앞선|방금)\s*(기업|회사|가맹점|고객|업체|상호)",
    r"(?<!상)(?<!하)위\s*(기업|회사|가맹점|고객|업체|상호)",
    r"(기업|회사|가맹점|고객|업체|상호)\s*(그거|그것|그곳)",
]


def _missing_ambiguous_target_params(question: str) -> list[dict]:
    if any(re.search(pattern, question) for pattern in _AMBIGUOUS_TARGET_PATTERNS):
        return [{"name": "대상명", "label": "조회 대상명(기업명/가맹점명/고객명)"}]
    return []


def classify_question(state: Text2SQLState) -> dict:
    if state.get("question_type"):
        return {"question_type": state["question_type"]}

    question = state["question"]
    if _rule_classify_question(question):
        return {"question_type": "need_sql"}
    table_summary = build_table_summary(SCHEMA)
    glossary = build_glossary_summary(SCHEMA)
    metrics = build_metrics_summary(SCHEMA)
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    references = find_relevant_references(SCHEMA, question)
    prompt = f"""당신은 KB카드 법인영업 데이터베이스 시스템의 질문 분류기입니다.
사용자의 질문을 분석하여 아래 3가지 중 하나로 분류하세요.

## 분류 기준
**need_sql** - 데이터베이스 조회가 필요한 질문:
- 숫자/금액/건수/비율 등 집계 데이터를 요구하는 질문
- 특정 기간/조건의 데이터를 조회하는 질문
- 대손비용률, 매출, 연체, 한도 등 분석 질문
- 가맹점명/기업명/브랜드명 + 기간(저번달/지난달/이번달/최근/2025년 12월 등) + 매출액/이용금액/건수/가맹점수/순위/정렬/가맹점별 질문
- 예: "도미노피자 저번달 매출액 각 가맹점별로 알려줘, 매출액 높은 순으로 정렬해줘" => need_sql

**direct** - SQL 없이 바로 답변 가능한 질문:
- 용어/개념 설명, 테이블/컬럼 구조 설명, 비즈니스 규칙/로직 설명

**reject** - 답변할 수 없는 질문:
- KB카드 법인영업 데이터와 전혀 무관한 질문

## 보유 데이터 범위
{table_summary}

## 용어집
{glossary}

## 메트릭
{metrics}

## LLM Semantic Contract
{semantic_contract}

## 질의 작성 Reference
{references}

## 사용자 질문
{question}

위 질문의 분류를 다음 형식으로만 반환하세요 (한 단어만):
need_sql 또는 direct 또는 reject"""

    raw = _coerce_llm_text(_call_llm(prompt)).lower()
    if "need_sql" in raw:
        qtype = "need_sql"
    elif "direct" in raw:
        qtype = "direct"
    elif "reject" in raw:
        qtype = "reject"
    else:
        qtype = "need_sql"
    return {"question_type": qtype}


def _rule_classify_question(question: str) -> bool:
    """Deterministically route obvious data lookup/aggregation questions to SQL."""
    q = question or ""
    has_metric = bool(re.search(r"매출액|매출금액|매출|이용금액|금액|건수|가맹점\s*(수|개수)|가맹점수|승인율|연체|한도|비율|률|순위|정렬", q))
    has_entity_or_group = bool(re.search(r"가맹점|기업|회사|고객|상호|브랜드|업종|별|도미노피자", q))
    has_time_or_order = bool(re.search(r"저번\s*달|지난\s*달|전월|이번\s*달|이번\s*월|최근|기준|20\d{2}\s*년|\d{1,2}\s*월|높은\s*순|낮은\s*순|정렬", q))
    return has_metric and (has_entity_or_group or has_time_or_order)


def route_domain(state: Text2SQLState) -> dict:
    """Route need_sql questions to one canonical domain.

    Routing order:
    1. keyword/rule score from canonical_domains
    2. canonical metric/entity/synonym score
    3. optional embedding similarity score
    4. LLM adjudication only for close or low-confidence candidates
    """
    if state.get("selected_domain"):
        return {
            "selected_domain": state["selected_domain"],
            "domain_candidates": state.get("domain_candidates", []),
            "domain_routing_trace": state.get("domain_routing_trace", ""),
            "domain_context": state.get("domain_context", ""),
        }

    question = state["question"]
    domains = SCHEMA.get("canonical_domains", [])
    if not domains:
        return {
            "selected_domain": "",
            "domain_candidates": [],
            "domain_routing_trace": "canonical_domains 없음",
            "domain_context": "(선택된 도메인 없음)",
        }

    keyword_scores = _keyword_rule_domain_scores(SCHEMA, question)
    metric_entity_scores = _metric_entity_domain_scores(SCHEMA, question)
    embedding_scores: dict[str, float] = {}
    embedding_note = "embedding=OFF"
    if DOMAIN_EMBEDDINGS_AVAILABLE and DOMAIN_EMBEDDINGS:
        try:
            q_emb = _get_embedding(question)
            embedding_scores = {
                domain_name: _cosine_similarity(q_emb, emb)
                for domain_name, emb in DOMAIN_EMBEDDINGS.items()
            }
            embedding_note = "embedding=ON"
        except Exception as e:
            embedding_note = f"embedding=FAILED({e})"

    candidates = _weighted_domain_scores(keyword_scores, metric_entity_scores, embedding_scores)
    selected = candidates[0]["domain"] if candidates else ""
    adjudicated = False
    if _needs_domain_adjudication(candidates):
        selected = _adjudicate_domain_with_llm(question, candidates, SCHEMA)
        adjudicated = True

    domain_context = build_domain_context(SCHEMA, selected)
    trace_lines = [
        f"selected_domain={selected}",
        f"{embedding_note}",
        f"adjudication={'ON' if adjudicated else 'OFF'}",
        "top_candidates:",
    ]
    for candidate in candidates[:5]:
        trace_lines.append(
            "- {domain}: total={score}, keyword={keyword_score}, metric_entity={metric_entity_score}, embedding={embedding_score}".format(
                **candidate
            )
        )

    return {
        "selected_domain": selected,
        "domain_candidates": candidates[:5],
        "domain_routing_trace": "\n".join(trace_lines),
        "domain_context": domain_context,
    }


def _extract_ym_from_question(question: str) -> str:
    """질문에서 기준년월을 YYYYMM으로 뽑는다 (예: '2025년 12월' -> '202512')."""
    if re.search(r"저번\s*달|지난\s*달|전월", question):
        return _previous_ym()
    if re.search(r"최근|이번\s*달|이번\s*월|현재\s*월", question):
        return _current_ym()
    match = re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월", question)
    if match:
        return f"{match.group(1)}{int(match.group(2)):02d}"
    match = re.search(r"\b(20\d{2})(0[1-9]|1[0-2])\b", question)
    if match:
        return match.group(0)
    return ""


def _current_ym() -> str:
    now = datetime.now()
    return f"{now.year}{now.month:02d}"


def _previous_ym() -> str:
    now = datetime.now()
    year = now.year if now.month > 1 else now.year - 1
    month = now.month - 1 if now.month > 1 else 12
    return f"{year}{month:02d}"


def _relative_month_target(question: str) -> tuple[str, str]:
    if re.search(r"저번\s*달|지난\s*달|전월", question):
        return _previous_ym(), "저번달/지난달/전월"
    if re.search(r"최근|이번\s*달|이번\s*월|현재\s*월", question):
        return _current_ym(), "최근/이번달/최근 기준"
    return "", ""


def _validate_recent_month_semantics(question: str, sql: str) -> list[str]:
    target_ym, label = _relative_month_target(question)
    if not target_ym:
        return []
    normalized_sql = (sql or "").replace('"', "")
    if target_ym in normalized_sql:
        return []
    return [
        f'"{label}"은 {_current_date_context()} 기준으로 {target_ym} 월 조건을 사용해야 합니다. '
        f"SQL에 기준년월 = '{target_ym}' 또는 SUBSTRING(기준년월일, 1, 6) = '{target_ym}' 같은 월 조건을 포함하세요. "
        "전체 데이터의 MAX(기준년월/기준년월일)만 사용하면 안 됩니다."
    ]


def _apply_recent_month_sql_fix(question: str, sql: str) -> str:
    """Normalize common relative-month SQL into the app's date interpretation.

    This is a generic SQL-generation safety net, not a question-specific Tool.
    It keeps generated SQL usable when the model writes relative month as all-data MAX().
    """
    target_ym, _ = _relative_month_target(question)
    if not target_ym:
        return sql
    if target_ym in (sql or ""):
        return sql

    fixed = sql or ""

    # `col = (SELECT MAX(SUBSTRING(date_col, 1, 6)) FROM table)` -> `col = 'YYYYMM'`
    fixed = re.sub(
        r"=\s*\(\s*SELECT\s+MAX\s*\(\s*(?:SUBSTRING|SUBSTR)\s*\(\s*[^,()]+\s*,\s*1\s*,\s*6\s*\)\s*\)\s+FROM\s+[^)]+?\)",
        f"= '{target_ym}'",
        fixed,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # `ym_col = (SELECT MAX(기준년월) FROM table)` -> `ym_col = 'YYYYMM'`
    fixed = re.sub(
        r"=\s*\(\s*SELECT\s+MAX\s*\(\s*\"?기준년월\"?\s*\)\s+FROM\s+[^)]+?\)",
        f"= '{target_ym}'",
        fixed,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # `date_col = (SELECT MAX(기준년월일) FROM table)` -> latest date inside target month.
    fixed = re.sub(
        r"=\s*\(\s*SELECT\s+MAX\s*\(\s*(?P<col>\"?(?:기준년월일|실적기준년월일)\"?)\s*\)\s+FROM\s+(?P<table>[A-Za-z0-9_.\"]+)\s*\)",
        lambda m: (
            f"= (SELECT MAX({m.group('col')}) FROM {m.group('table')} "
            f"WHERE SUBSTRING({m.group('col')}, 1, 6) = '{target_ym}')"
        ),
        fixed,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # CTE/subquery: `SELECT MAX(기준년월) AS ... FROM table` -> target-month constrained MAX.
    fixed = re.sub(
        r"SELECT\s+MAX\s*\(\s*(?P<col>\"?기준년월\"?)\s*\)\s+AS\s+(?P<alias>\"?[A-Za-z가-힣_][A-Za-z0-9가-힣_]*\"?)\s+FROM\s+(?P<table>[A-Za-z0-9_.\"]+)(?=\s*\))",
        lambda m: (
            f"SELECT MAX({m.group('col')}) AS {m.group('alias')} "
            f"FROM {m.group('table')} WHERE {m.group('col')} = '{target_ym}'"
        ),
        fixed,
        flags=re.IGNORECASE,
    )

    # CTE/subquery: `SELECT MAX(기준년월일) AS ... FROM table` -> latest day within target month.
    fixed = re.sub(
        r"SELECT\s+MAX\s*\(\s*(?P<col>\"?(?:기준년월일|실적기준년월일)\"?)\s*\)\s+AS\s+(?P<alias>\"?[A-Za-z가-힣_][A-Za-z0-9가-힣_]*\"?)\s+FROM\s+(?P<table>[A-Za-z0-9_.\"]+)(?=\s*\))",
        lambda m: (
            f"SELECT MAX({m.group('col')}) AS {m.group('alias')} "
            f"FROM {m.group('table')} WHERE SUBSTRING({m.group('col')}, 1, 6) = '{target_ym}'"
        ),
        fixed,
        flags=re.IGNORECASE,
    )

    return fixed


def _extract_merchant_from_question(question: str) -> str:
    """질문에서 가맹점(기업)명을 뽑는다.

    LLM이 파라미터 추출을 통째로 누락할 때를 대비한 결정적 보강용. 확신이 낮으면
    빈 문자열을 돌려 사용자에게 되묻도록 둔다 (틀린 값으로 조용히 진행하지 않는다).
    """
    # 날짜/숫자/기간 표현과 대손 관련 키워드·동사·조사를 제거한 뒤 남는 명사구를 본다.
    text = question
    text = re.sub(r"20\d{2}\s*년\s*\d{1,2}\s*월", " ", text)
    text = re.sub(r"20\d{2}\s*년", " ", text)
    text = re.sub(r"\d{1,2}\s*월", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    stop_words = [
        "대손비용률", "대손비용율", "대손율", "대손률", "대손비율", "대손",
        "분석", "분석해줘", "구해줘", "알려줘", "보여줘", "해줘", "조회", "기업", "회사",
    ]
    for word in stop_words:
        text = text.replace(word, " ")
    # 조사로 끝나는 토큰의 조사를 떼고, 한글/영문/숫자로 된 후보만 남긴다.
    candidates = []
    for token in text.split():
        token = re.sub(r"(의|은|는|이|가|을|를|에|로|으로|와|과)$", "", token).strip()
        if token and re.fullmatch(r"[가-힣A-Za-z0-9()]+", token):
            candidates.append(token)
    # 후보가 정확히 하나일 때만 채택 (모호하면 되묻기에 맡긴다).
    return candidates[0] if len(candidates) == 1 else ""


def _augment_bad_debt_params(question: str, params: dict) -> dict:
    """대손비용률 Tool에서 LLM이 빠뜨린 가맹점명/기준년월을 질문에서 규칙으로 보강한다."""
    augmented = dict(params)
    if not augmented.get("기준년월"):
        ym = _extract_ym_from_question(question)
        if ym:
            augmented["기준년월"] = ym
    if not augmented.get("가맹점명"):
        merchant = _extract_merchant_from_question(question)
        if merchant:
            augmented["가맹점명"] = merchant
    return augmented


def _rule_match_tool(question: str) -> str:
    """질문에 Tool tags가 등장하는지로 Tool을 우선 매칭한다.

    작은/큰 모델 모두 select_tool에서 핵심 Tool(예: 대손비용률)을 간헐적으로 놓치므로,
    태그가 명확히 일치하는 Tool이 '단독 최다'일 때 LLM 판단보다 먼저 확정한다.
    동점이거나 일치가 없으면 빈 문자열을 돌려 기존 LLM 선택 경로로 넘긴다.
    """
    scores: list[tuple[int, str]] = []
    for tool in TOOLS:
        hits = sum(1 for tag in tool.get("tags", []) if tag and tag in question)
        if hits:
            scores.append((hits, tool["name"]))
    if not scores:
        return ""
    scores.sort(reverse=True)
    top_score, top_name = scores[0]
    # 최다 점수를 가진 Tool이 유일할 때만 규칙으로 확정 (모호하면 LLM에 위임).
    if sum(1 for score, _ in scores if score == top_score) == 1:
        return top_name
    return ""


def select_tool(state: Text2SQLState) -> dict:
    if state.get("selected_tool") and state.get("tool_params") is not None:
        return {"selected_tool": state["selected_tool"], "tool_params": state.get("tool_params", {})}

    question = state["question"]
    forced_tool = _rule_match_tool(question)
    tool_desc = _build_tool_descriptions()
    domain_context = state.get("domain_context") or "(도메인 라우팅 결과 없음)"
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    prompt = f"""사용자의 질문에 대해 아래 Tool 중 가장 적합한 것을 선택하고, 질문에서 파라미터를 추출하세요.

## 도메인 라우팅 결과
{domain_context}

## LLM Semantic Contract
{semantic_contract}

## 사용 가능한 Tool 목록
{tool_desc}

## 파라미터 추출 규칙 ({_current_date_context()})
1. 사용자가 명시적으로 언급한 값만 추출하세요.
2. 기간 변환: "2025년" → 기간_시작: "202501", 기간_종료: "202512"
   "올해" → 기간_시작: "{datetime.now().year}01", 기간_종료: "{datetime.now().year}{datetime.now().month:02d}"
3. "최근", "이번달", "이번 월"은 현재월 1개월로 해석 → 기간_시작/기간_종료 모두 "{datetime.now().year}{datetime.now().month:02d}"
4. "상위 N개", "톱 N" → limit: N
5. "등급별", "신용등급별" → 등급별: true
6. 언급되지 않은 파라미터는 포함하지 마세요.
7. [필수] 파라미터라도 질문에 명시되지 않았으면 추출하지 마세요 (나중에 사용자에게 물어봅니다).

## 사용자 질문
{question}

## 응답 형식
적합한 Tool이 있으면: {{"tool": "tool_name", "params": {{"param1": "value1"}}}}
적합한 Tool이 없으면: {{"tool": "NONE"}}

JSON:"""

    result = _parse_llm_json(_call_llm(prompt))
    tool_name = result.get("tool", "NONE") if result else "NONE"
    params = result.get("params", {}) if result else {}
    if not isinstance(params, dict):
        params = {}

    # 규칙으로 Tool이 확정되면 LLM이 Tool을 놓치거나 다르게 골라도 그 Tool로 강제한다.
    # (LLM 응답은 파라미터 추출 용도로만 신뢰한다.) LLM이 엉뚱한 Tool을 고른 경우라도
    # 추출한 값 중 강제 Tool이 실제로 받는 파라미터만 추려 살린다 — 가맹점명/기준년월 등이
    # 헛되이 누락 처리되는 것을 막는다.
    if forced_tool:
        valid_names = {p["name"] for p in TOOL_MAP[forced_tool]["parameters"]}
        params = {k: v for k, v in params.items() if k in valid_names}
        # LLM이 파라미터 추출을 비결정적으로 누락하므로, 핵심 값은 질문에서 직접 보강한다.
        if forced_tool == "대손비용률_분석":
            params = _augment_bad_debt_params(question, params)
        return {"selected_tool": forced_tool, "tool_params": params}

    if tool_name == "NONE" or tool_name not in TOOL_MAP:
        return {"selected_tool": "", "tool_params": {}}
    return {"selected_tool": tool_name, "tool_params": params}


def check_tool_params(state: Text2SQLState) -> dict:
    tool_name = state.get("selected_tool", "")
    if not tool_name:
        return {"param_stage": "done", "missing_params": []}
    tool = TOOL_MAP.get(tool_name)
    if not tool:
        return {"param_stage": "done", "missing_params": []}
    params = state.get("tool_params", {})
    required = [p for p in tool["parameters"] if p.get("required", False)]
    missing = [p for p in required if not params.get(p["name"])]
    if missing:
        return {
            "missing_params": [
                {
                    "name": p["name"],
                    "label": p["name"],
                    "description": p.get("description", ""),
                    "type": p.get("type", "string"),
                }
                for p in missing
            ],
            "param_stage": "need_params",
        }
    return {"missing_params": [], "param_stage": "done"}


def execute_tool(state: Text2SQLState) -> dict:
    tool_name = state["selected_tool"]
    params = state.get("tool_params", {})
    tool = TOOL_MAP.get(tool_name)
    if not tool:
        return {"final_sql": "", "query_error": f"Tool '{tool_name}' not found", "tool_completed": False}
    try:
        result = tool["fn"](params)
        if isinstance(result, dict) and result.get("is_complete"):
            return {
                "final_sql": prepare_sql_for_backend(result.get("sql", "")),
                "query_columns": result.get("columns", []),
                "query_rows": result.get("rows", []),
                "answer": result.get("answer", ""),
                "bad_debt_excel_path": result.get("excel_path", ""),
                "tool_completed": True,
            }
        sql = result
        formatted_sql = sqlparse.format(sql, reindent=True, keyword_case="upper")
        return {"final_sql": prepare_sql_for_backend(formatted_sql), "tool_completed": False}
    except Exception as e:
        return {"final_sql": "", "query_error": str(e), "tool_completed": False}


def run_tool_query(state: Text2SQLState) -> dict:
    sql = state.get("final_sql", "")
    if not sql:
        return {"query_columns": [], "query_rows": [], "query_error": "SQL이 생성되지 않았습니다.", "selected_tool": "", "final_sql": ""}
    prepared_sql = prepare_sql_for_backend(sql)
    columns, rows, error = execute_sql(prepared_sql)
    if error:
        return {"query_columns": [], "query_rows": [], "query_error": error, "selected_tool": "", "final_sql": ""}
    return {"query_columns": columns, "query_rows": rows[:100], "query_error": "", "final_sql": prepared_sql}


def _match_vq_by_embedding(question: str) -> dict | None:
    """Embedding cosine similarity로 Verified Query 매칭. 실패 시 None 반환."""
    if not EMBEDDINGS_AVAILABLE or not VQ_EMBEDDINGS:
        return None
    try:
        q_emb = _get_embedding(question)
        scores = [_cosine_similarity(q_emb, vq_emb) for vq_emb in VQ_EMBEDDINGS]
        best_idx = max(range(len(scores)), key=lambda i: scores[i])
        best_score = scores[best_idx]
        if best_score >= EMBED_MATCH_THRESHOLD:
            matched = VERIFIED_QUERIES[best_idx]
            if not _verified_query_matches_intent(question, matched):
                return None
            return {
                "matched_query_name": matched["name"],
                "matched_query_sql": matched["sql"].strip(),
                "matched_query_params": matched.get("parameters", {}),
            }
    except Exception:
        pass
    return None


def _verified_query_matches_intent(question: str, matched: dict) -> bool:
    """Reject broad semantic matches when the user's metric intent differs."""
    question_text = question or ""
    matched_text = " ".join(
        str(matched.get(key, ""))
        for key in ("name", "question", "description", "sql")
    )
    matched_text += " " + " ".join(str(tag) for tag in matched.get("tags", []))

    asks_merchant_count = bool(re.search(r"가맹점\s*(수|개수)|가맹점수|가맹점개수|몇\s*개", question_text))
    if asks_merchant_count:
        return bool(
            re.search(r"가맹점\s*(수|개수)|가맹점수|가맹점개수", matched_text)
            or re.search(r"COUNT\s*\(\s*DISTINCT\s+[^)]*가맹점번호", matched_text, re.IGNORECASE)
        )
    return True


def _match_vq_by_llm(question: str) -> dict:
    """LLM으로 Verified Query 매칭 (embedding 폴백)."""
    vqs = VERIFIED_QUERIES
    if not vqs:
        return {"matched_query_name": ""}
    vq_lines = []
    for i, vq in enumerate(vqs):
        entry = f"[{i}] {vq['question']}"
        tags = ", ".join(vq.get("tags", []))
        entry += f"  [태그: {tags}]"
        if vq.get("parameters"):
            params = [f"{k}" for k in vq["parameters"].keys()]
            entry += f"  [파라미터: {', '.join(params)}]"
        vq_lines.append(entry)
    match_prompt = f"""사용자의 질문이 아래 사전 정의된 쿼리 중 하나와 의도가 동일하거나 매우 유사한지 판단하세요.

## 사전 정의된 쿼리 목록
{chr(10).join(vq_lines)}

## 판단 규칙
1. 사용자의 질문 핵심 의도가 사전 정의된 쿼리와 동일해야 매칭입니다.
2. 추가 조건이 붙어도 기본 의도가 같으면 매칭입니다.
3. 의도가 다르면 NONE입니다.

## 사용자 질문
{question}

가장 잘 매칭되는 쿼리의 인덱스 번호(숫자)만 반환하세요. 매칭되는 쿼리가 없으면 NONE만 반환하세요:"""

    raw = _coerce_llm_text(_call_llm(match_prompt))
    if "NONE" in raw.upper():
        return {"matched_query_name": ""}
    nums = re.findall(r"^\s*\[?(\d+)\]?", raw)
    if not nums:
        nums = re.findall(r"\b(\d+)\b", raw)
    valid_indices = [int(n) for n in nums if int(n) < len(vqs)]
    if not valid_indices:
        return {"matched_query_name": ""}
    idx = valid_indices[0]
    matched = vqs[idx]
    if not _verified_query_matches_intent(question, matched):
        return {"matched_query_name": ""}
    return {
        "matched_query_name": matched["name"],
        "matched_query_sql": matched["sql"].strip(),
        "matched_query_params": matched.get("parameters", {}),
    }


def match_verified_query(state: Text2SQLState) -> dict:
    question = state["question"]
    result = _match_vq_by_embedding(question)
    if result:
        return result
    return _match_vq_by_llm(question)


def extract_and_apply_params(state: Text2SQLState) -> dict:
    question = state["question"]
    base_sql = state["matched_query_sql"]
    vq_name = state.get("matched_query_name", "")
    vq_params_def = state.get("matched_query_params", {})
    param_specs = VQ_PARAM_SPECS.get(vq_name, [
        {"name": "기간_시작", "type": "string", "description": "조회 시작 기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "조회 종료 기준년월 (YYYYMM)"},
        {"name": "기업명", "type": "string", "description": "기업/상호명 (부분일치)"},
        {"name": "가맹점명", "type": "string", "description": "가맹점명 (부분일치)"},
        {"name": "업종", "type": "string", "description": "업종명/업종 대분류 (부분일치)"},
        {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
    ])
    for pname, pinfo in vq_params_def.items():
        if not any(s["name"] == pname for s in param_specs):
            param_specs.append({"name": pname, "type": pinfo.get("type", "string"), "description": pinfo.get("description", "")})
    specs_json = json.dumps(param_specs, ensure_ascii=False, indent=2)
    extract_prompt = f"""사용자의 질문에서 SQL 조회에 필요한 파라미터 값을 추출하세요.

## 추출 가능한 파라미터
{specs_json}

## 추출 규칙 ({_current_date_context()})
1. 사용자가 명시적으로 언급한 값만 추출하세요.
2. 기간 변환: "2025년" → 기간_시작:"202501", 기간_종료:"202512"
   "올해" → 기간_시작:"{datetime.now().year}01", 기간_종료:"{datetime.now().year}{datetime.now().month:02d}"
3. "최근", "이번달", "이번 월"은 현재월 1개월로 해석 → 기간_시작/기간_종료 모두 "{datetime.now().year}{datetime.now().month:02d}"
4. "상위 N개" → limit: N
5. "개인카드 미보유" → 보유구분:"개인카드미보유", "기업카드 미보유/법인카드 미보유" → 보유구분:"기업카드미보유"
6. JSON만 반환하세요. 없으면 빈 오브젝트 {{}}.

## 사용자 질문
{question}

JSON:"""
    extracted = _parse_llm_json(_call_llm(extract_prompt))
    final_sql = _apply_params_to_vq(base_sql, extracted, vq_name, vq_params_def)
    formatted_sql = sqlparse.format(final_sql, reindent=True, keyword_case="upper")
    return {"extracted_params": extracted, "final_sql": prepare_sql_for_backend(formatted_sql)}


def run_matched_query(state: Text2SQLState) -> dict:
    sql = state.get("final_sql", "")
    prepared_sql = prepare_sql_for_backend(sql)
    columns, rows, error = execute_sql(prepared_sql)
    if error:
        return {"query_columns": [], "query_rows": [], "query_error": error, "matched_query_name": "", "final_sql": ""}
    return {"query_columns": columns, "query_rows": rows[:100], "query_error": "", "final_sql": prepared_sql}


def direct_answer(state: Text2SQLState) -> dict:
    question = state["question"]
    glossary = build_glossary_summary(SCHEMA)
    table_summary = build_table_summary(SCHEMA)
    metrics = build_metrics_summary(SCHEMA)
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    references = find_relevant_references(SCHEMA, question)
    prompt = f"""당신은 KB카드 법인영업 데이터베이스 전문가입니다.
사용자의 질문에 대해 아래 정보를 바탕으로 명확한 한국어 답변을 작성하세요.

## 테이블 정보
{table_summary}

## 비즈니스 용어집
{glossary}

## 메트릭 정의
{metrics}

## LLM Semantic Contract
{semantic_contract}

## 질의 작성 Reference
{references}

## 사용자 질문
{question}

답변:"""
    return {"answer": _coerce_llm_text(_call_llm(prompt))}


def reject_answer(state: Text2SQLState) -> dict:
    return {"answer": "죄송합니다. 해당 질문에 대해서는 답변을 드리지 못합니다.\n\n저는 KB카드 법인영업 데이터 분석을 도와드리는 에이전트입니다.\n다음과 같은 질문을 해주시면 답변드릴 수 있습니다:\n- 매출/이용 분석, 가맹점 분석, 기업 심사, 여신/연체, 대손비용률\n- 대손비용률 분석: \"한빛테크놀로지의 2025년 12월 대손비용률 구해줘\"\n\n결과 저장: 조회 후 '저장' | 'word' | 'text' | 'csv' 입력"}


def analyze_question(state: Text2SQLState) -> dict:
    if state.get("selected_tables") and state.get("table_details"):
        return {"selected_tables": state["selected_tables"], "table_details": state["table_details"]}

    question = state["question"]
    domain_context = state.get("domain_context") or build_domain_context(SCHEMA, state.get("selected_domain", ""))
    domain_trace = state.get("domain_routing_trace", "")
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    join_context = build_semantic_join_context(SCHEMA, state.get("selected_domain", ""), question)
    prompt = f"""당신은 KB카드 법인영업 데이터베이스 전문가입니다.
사용자의 질문을 분석하여 필요한 테이블을 선택하세요.

## 도메인 라우팅 결과
{domain_context}

## 도메인 라우팅 근거
{domain_trace}

## LLM Semantic Contract
{semantic_contract}

## 안전 조인 그래프
{join_context}

## 사용 가능한 테이블
{build_table_summary(SCHEMA)}

## 비즈니스 용어집
{build_glossary_summary(SCHEMA)}

## 사전 정의된 메트릭
{build_metrics_summary(SCHEMA)}

## 질문과 가까운 질의 작성 Reference
{find_relevant_references(SCHEMA, question)}

## 규칙
1. 필요한 테이블만 선택. 2. JOIN 필요 시 관련 테이블 포함.
3. 질의 작성 Reference와 맞는 intent가 있으면 primary_table과 join_tables를 우선 포함.
4. 도메인 라우팅 결과의 default_fact_table과 primary_entities를 우선 검토.
5. JOIN이 필요하면 안전 조인 그래프의 from_entity/to_entity에 해당하는 테이블을 우선 포함.
6. 테이블명만 쉼표 구분 반환.

## 사용자 질문
{question}

필요한 테이블명 (쉼표 구분):"""

    raw_table_names = [t.strip() for t in _coerce_llm_text(_call_llm(prompt)).split(",")]
    table_index = {}
    for table in SCHEMA.get("tables", []):
        logical = str(table.get("name", "")).strip()
        physical = str(table.get("physical_table", "")).strip()
        physical_short = physical.rsplit(".", 1)[-1] if physical else ""
        for candidate in (logical, physical, physical_short):
            if candidate:
                table_index[candidate.lower()] = logical
    table_names = []
    for raw_name in raw_table_names:
        normalized_raw = raw_name.strip().strip("`\"'").lower()
        normalized = table_index.get(normalized_raw)
        if normalized and normalized not in table_names:
            table_names.append(normalized)
    details = []
    for t in SCHEMA.get("tables", []):
        if t["name"] in table_names:
            details.append(yaml.dump(t, allow_unicode=True, default_flow_style=False))
    rel_lines = []
    for r in SCHEMA.get("relationships", []):
        if r["from"] in table_names or r["to"] in table_names:
            rel_lines.append(f"- {r['from']} -> {r['to']}: {r['join_expr']}")
    table_details = "\n---\n".join(details)
    if rel_lines:
        table_details += "\n\n## Relationships\n" + "\n".join(rel_lines)
    return {"selected_tables": table_names, "table_details": table_details}


def check_sql_gen_params(state: Text2SQLState) -> dict:
    if state.get("user_provided_params"):
        return {"param_stage": "done", "missing_params": []}

    question = state["question"]
    needed = _missing_ambiguous_target_params(question)

    if needed:
        return {
            "missing_params": needed,
            "param_stage": "need_params",
        }
    return {"missing_params": [], "param_stage": "done"}


def _sql_dialect_name() -> str:
    """LLM 프롬프트에 명시할 SQL 방언 이름."""
    return "Trino/Presto (Amazon Athena)" if DB_BACKEND == "athena" else "PostgreSQL"


def _schema_prefix_rule() -> str:
    """테이블 참조 시 붙일 스키마 prefix 규칙 (스키마가 비면 prefix 없이 안내)."""
    if DB_SCHEMA_PREFIX:
        return f"모든 테이블은 {DB_SCHEMA_PREFIX} prefix를 붙여 참조 (예: {DB_SCHEMA_PREFIX}테이블명)."
    return "테이블은 스키마 prefix 없이 테이블명만 사용."


def _sql_dialect_rules() -> str:
    """백엔드별 SQL 작성 주의사항. Athena(Trino)는 PostgreSQL과 방언이 다르다."""
    if DB_BACKEND == "athena":
        return (
            "16. 이 쿼리는 Amazon Athena(Trino/Presto)에서 실행됩니다. 다음 방언 규칙을 지키세요:\n"
            "    - 타입 캐스트는 CAST(expr AS type)만 사용 (PostgreSQL의 expr::type 금지).\n"
            "    - 실수 나눗셈은 CAST(... AS DOUBLE), 정수는 CAST(... AS INTEGER).\n"
            "    - 대소문자 무시 검색은 LOWER(col) LIKE LOWER('%값%') 사용 (ILIKE 금지).\n"
            "    - 문자열 부분추출은 SUBSTR(col, start, length) 사용 (LEFT/RIGHT 대신).\n"
            "    - 날짜/문자 함수는 Trino 표준만 사용 (TO_CHAR, DATE_TRUNC 등 PG 전용 함수 금지).\n"
            "    - 한글/비ASCII 컬럼명과 alias는 반드시 double quote로 감싸기 (예: \"기준년월\", a.\"가맹점명\", AS \"총매출금액\").\n"
            "    - 테이블 상세에 athena_partition이 있으면 업무 날짜 조건(예: \"기준년월\" = '202512')과 함께 파티션 조건도 반드시 추가 (예: \"year\" = '2025' AND \"month\" = '12')."
        )
    return ""


def generate_sql(state: Text2SQLState) -> dict:
    question = state["question"]
    table_details = state["table_details"]
    retry_count = state.get("retry_count", 0)
    validation_result = state.get("validation_result", "")
    relevant_queries = find_relevant_queries(SCHEMA, question)
    relevant_references = find_relevant_references(SCHEMA, question)
    domain_context = state.get("domain_context") or build_domain_context(SCHEMA, state.get("selected_domain", ""))
    domain_trace = state.get("domain_routing_trace", "")
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    join_context = build_semantic_join_context(SCHEMA, state.get("selected_domain", ""), question)
    current_ym = _current_ym()
    previous_ym = _previous_ym()
    retry_context = ""
    if retry_count > 0 and validation_result:
        retry_context = f"\n## 이전 시도 실패\n{validation_result}\n\n이전 SQL:\n{state.get('generated_sql', '')}\n\n위 문제를 수정한 SQL을 생성하세요.\n"

    user_params = state.get("user_provided_params", {})
    user_params_context = ""
    if user_params:
        params_text = "\n".join(f"- {k}: {v}" for k, v in user_params.items())
        user_params_context = f"\n## 사용자가 제공한 추가 정보\n{params_text}\n위 값을 SQL의 WHERE 조건이나 파라미터에 반영하세요.\n"

    prompt = f"""당신은 KB카드 법인영업 데이터베이스의 SQL 전문가입니다.
사용자의 자연어 질문을 {_sql_dialect_name()} SQL로 변환하세요.

## 도메인 라우팅 결과
{domain_context}

## 도메인 라우팅 근거
{domain_trace}

## LLM Semantic Contract
{semantic_contract}

## 안전 조인 그래프
{join_context}

## 테이블 상세 정보
{table_details}

## 사전 정의된 메트릭
{build_metrics_summary(SCHEMA)}

## 비즈니스 용어집
{build_glossary_summary(SCHEMA)}

## 질문과 가까운 질의 작성 Reference
{relevant_references}

## 참고 SQL 예시
{relevant_queries}

{retry_context}{user_params_context}

## SQL 작성 규칙
1. 위 테이블 정보에 있는 컬럼만 사용. 2. 메트릭 정의된 경우 해당 SQL 공식 사용.
3. 도메인 라우팅 결과의 canonical_metrics, required_filters, default_time_dimension을 우선 반영.
4. JOIN이 필요하면 안전 조인 그래프 또는 Relationships에 있는 경로만 사용.
5. 법인카드: 개인기업구분코드 = '2'. 6. 매출전표: 취소 건 제외.
7. 기준년월 'YYYYMM', 기준년월일 'YYYYMMDD'. 8. {_schema_prefix_rule()}
9. 질의 작성 Reference가 질문과 맞으면 reference의 primary_table, filter, join_rule을 우선 적용.
10. 질문에 없는 테이블명은 절대 만들지 말고, 위 테이블 상세/Reference/도메인 컨텍스트에 있는 실테이블만 사용.
11. "신규/가입/등록 고객"은 tbdaaat01.최초등록년월일 기준으로 해석.
12. "여성 고객"은 성별구분코드 = '2'를 기본값으로 사용.
13. 상세 목록 조회는 LIMIT 100 이하를 기본으로 둡니다. 집계 결과는 의미있는 순서로 정렬합니다.
14. 읽기 쉬운 alias. 15. 순수 SQL만 반환.
16. 날짜 해석 ({_current_date_context()}):
    - "최근", "최근 기준", "이번달", "이번 월"은 현재월 1개월 기준으로 해석.
    - 기준년월 컬럼은 "{current_ym}" 조건을 사용.
    - 기준년월일 컬럼은 SUBSTRING(기준년월일, 1, 6) = "{current_ym}" 범위 안의 최신 기준년월일을 사용.
    - "저번달", "지난달", "전월"은 지난달 1개월 기준으로 해석하고, 기준년월 컬럼은 "{previous_ym}" 조건을 사용.
    - "가맹점별/각 가맹점별"은 가맹점번호와 가맹점명을 SELECT/GROUP BY에 포함.
    - "매출액 높은 순"은 매출액 집계 alias 기준 DESC 정렬.
{_sql_dialect_rules()}

## 사용자 질문
{question}

SQL:"""
    sql = _strip_llm_code_fence(_call_llm(prompt))
    sql = _apply_recent_month_sql_fix(question, sql)
    return {"generated_sql": sql}


def validate_sql(state: Text2SQLState) -> dict:
    question = state["question"]
    sql = _apply_recent_month_sql_fix(question, state["generated_sql"])
    sql = prepare_sql_for_backend(sql)
    selected_tables = state["selected_tables"]
    issues = _validate_sql_against_schema(sql, selected_tables)
    issues.extend(_validate_recent_month_semantics(question, sql))
    current_ym = _current_ym()
    try:
        parsed = sqlparse.parse(sql)
        if not parsed or not parsed[0].tokens:
            issues.append("SQL 파싱 실패: 빈 쿼리입니다.")
    except Exception as e:
        issues.append(f"SQL 파싱 오류: {e}")
    validation_prompt = f"""SQL 검증 전문가로서, 아래 SQL이 사용자 질문에 정확히 답하는지 검증하세요.

사용자 질문: {question}
SQL:
{sql}
사용 테이블: {', '.join(selected_tables)}
검증 기준:
- 이 앱에서 "최근", "최근 기준", "이번달", "이번 월"은 반드시 현재월({current_ym}) 기준입니다.
- 위 표현이 있는 질문에서 SQL이 현재월({current_ym}) 조건을 포함하면 날짜 해석은 올바릅니다.
- 위 표현이 있는 질문에서 전체 데이터의 MAX(기준년월/기준년월일)만 사용하면 잘못입니다.
- "저번달", "지난달", "전월"은 {_current_date_context()}의 지난달 기준으로 해석합니다.
메트릭 정의:
{build_metrics_summary(SCHEMA)}

스키마 기반 사전 검증 결과:
{chr(10).join(issues) if issues else "사전 검증 이슈 없음"}

문제가 없으면 "VALID"만 반환. 문제가 있으면 구체적 목록을 반환."""
    llm_result = _coerce_llm_text(_call_llm(validation_prompt))
    if llm_result.upper().startswith("VALID") and not issues:
        formatted_sql = sqlparse.format(sql, reindent=True, keyword_case="upper")
        return {"validation_result": "VALID", "is_valid": True, "final_sql": prepare_sql_for_backend(formatted_sql)}
    else:
        all_issues = "\n".join(issues)
        if not llm_result.upper().startswith("VALID"):
            all_issues += "\n" + llm_result if all_issues else llm_result
        return {"validation_result": all_issues, "is_valid": False, "retry_count": state.get("retry_count", 0) + 1}


def run_query(state: Text2SQLState) -> dict:
    sql = state.get("final_sql", state.get("generated_sql", ""))
    prepared_sql = prepare_sql_for_backend(sql)
    columns, rows, error = execute_sql(prepared_sql)
    if error:
        retry = state.get("retry_count", 0) + 1
        return {
            "query_columns": [],
            "query_rows": [],
            "query_error": error,
            "validation_result": f"DB 실행 오류: {error}",
            "is_valid": False,
            "retry_count": retry,
            "final_sql": prepared_sql,
        }
    return {"query_columns": columns, "query_rows": rows[:100], "query_error": "", "final_sql": prepared_sql}


def generate_answer(state: Text2SQLState) -> dict:
    if state.get("answer"):
        return {"answer": state["answer"]}

    question = state["question"]
    sql = state.get("final_sql", "")
    columns = state.get("query_columns", [])
    rows = state.get("query_rows", [])
    if not rows:
        return {"answer": "쿼리 결과가 없습니다. 조건을 확인해주세요."}
    result_text = " | ".join(columns) + "\n" + "-" * 40 + "\n"
    for row in rows[:50]:
        result_text += " | ".join(str(v) for v in row) + "\n"
    if len(rows) > 50:
        result_text += f"\n... 외 {len(rows) - 50}건 더 있음"
    source_label = _get_source_label(state)
    summary_text = _result_summary(columns, rows)
    prompt = f"""당신은 KB카드 법인영업 데이터 분석가입니다.
사용자의 질문과 SQL 쿼리 결과를 바탕으로 짧고 직관적인 한국어 답변을 작성하세요.

## 사용자 질문
{question}

## 실행된 SQL ({source_label})
{sql}

## 쿼리 결과 ({len(rows)}건)
{result_text}

## 계산 요약
{summary_text}

## 답변 형식
아래 형식을 반드시 지키세요. 전체 답변은 10줄 이내로 제한하세요.

### 핵심 요약
| 항목 | 값 |
|---|---:|
| 주요 지표 | ... |
| 비교/순위 | ... |
| 특이사항 | ... |

### 해석
- 데이터에서 바로 확인되는 내용만 1~2개 bullet로 작성하세요.

## 답변 규칙
1. 긴 문단을 쓰지 말고 표와 짧은 bullet 중심으로 작성하세요.
2. 금액은 억원/만원 단위로 변환하세요.
3. 비율은 %로 표시하세요.
4. 상위/하위 결과는 가장 중요한 3개까지만 언급하세요.
5. 데이터에 없는 원인 추정이나 영업 제안은 쓰지 마세요.
6. 계산 요약의 row_count, 합계, 평균, 최소, 최대를 우선 활용하세요.

답변:"""
    return {"answer": _coerce_llm_text(_call_llm(prompt))}


def handle_error(state: Text2SQLState) -> dict:
    return {
        "error_message": f"SQL 생성/실행에 실패했습니다 (재시도 {state.get('retry_count', 0)}회).\n마지막 검증 결과:\n{state.get('validation_result', '알 수 없는 오류')}\n\n마지막 SQL:\n{state.get('generated_sql', '없음')}",
        "answer": "죄송합니다. 질문에 대한 SQL을 생성하지 못했습니다. 질문을 다시 표현해주세요.",
    }


# ---------------------------------------------------------------------------
# 10. 라우팅
# ---------------------------------------------------------------------------

def route_by_question_type(state: Text2SQLState) -> Literal["route_domain", "direct_answer", "reject_answer"]:
    qtype = state.get("question_type", "need_sql")
    if qtype == "direct":
        return "direct_answer"
    if qtype == "reject":
        return "reject_answer"
    return "route_domain"


def after_tool_selection(state: Text2SQLState) -> Literal["check_tool_params", "match_verified_query"]:
    return "check_tool_params" if state.get("selected_tool") else "match_verified_query"


def after_check_params(state: Text2SQLState) -> Literal["execute_tool", "__end__"]:
    if state.get("param_stage") == "need_params":
        return "__end__"
    return "execute_tool"


def after_execute_tool(state: Text2SQLState) -> Literal["generate_answer", "run_tool_query"]:
    if state.get("tool_completed"):
        return "generate_answer"
    return "run_tool_query"


def after_tool_query(state: Text2SQLState) -> Literal["generate_answer", "match_verified_query"]:
    return "match_verified_query" if state.get("query_error") else "generate_answer"


def after_query_match(state: Text2SQLState) -> Literal["extract_and_apply_params", "analyze_question"]:
    return "extract_and_apply_params" if state.get("matched_query_name") else "analyze_question"


def after_matched_query(state: Text2SQLState) -> Literal["generate_answer", "analyze_question"]:
    return "analyze_question" if state.get("query_error") else "generate_answer"


def after_check_sql_gen_params(state: Text2SQLState) -> Literal["generate_sql", "__end__"]:
    if state.get("param_stage") == "need_params":
        return "__end__"
    return "generate_sql"


def after_validate(state: Text2SQLState) -> Literal["run_query", "generate_sql", "handle_error"]:
    if state.get("is_valid", False):
        return "run_query"
    return "handle_error" if state.get("retry_count", 0) >= 3 else "generate_sql"


def after_query(state: Text2SQLState) -> Literal["generate_answer", "generate_sql", "handle_error"]:
    if state.get("query_error"):
        return "handle_error" if state.get("retry_count", 0) >= 3 else "generate_sql"
    return "generate_answer"


# ---------------------------------------------------------------------------
# 11. 그래프 구성
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    graph = StateGraph(Text2SQLState)

    graph.add_node("classify_question", classify_question)
    graph.add_node("route_domain", route_domain)
    graph.add_node("select_tool", select_tool)
    graph.add_node("check_tool_params", check_tool_params)
    graph.add_node("execute_tool", execute_tool)
    graph.add_node("run_tool_query", run_tool_query)
    graph.add_node("match_verified_query", match_verified_query)
    graph.add_node("extract_and_apply_params", extract_and_apply_params)
    graph.add_node("run_matched_query", run_matched_query)
    graph.add_node("direct_answer", direct_answer)
    graph.add_node("reject_answer", reject_answer)
    graph.add_node("analyze_question", analyze_question)
    graph.add_node("check_sql_gen_params", check_sql_gen_params)
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("validate_sql", validate_sql)
    graph.add_node("run_query", run_query)
    graph.add_node("generate_answer", generate_answer)
    graph.add_node("handle_error", handle_error)

    graph.set_entry_point("classify_question")
    graph.add_conditional_edges("classify_question", route_by_question_type)
    graph.add_edge("route_domain", "select_tool")

    graph.add_edge("direct_answer", END)
    graph.add_edge("reject_answer", END)

    graph.add_conditional_edges("select_tool", after_tool_selection)
    graph.add_conditional_edges("check_tool_params", after_check_params)
    graph.add_conditional_edges("execute_tool", after_execute_tool)
    graph.add_conditional_edges("run_tool_query", after_tool_query)

    graph.add_conditional_edges("match_verified_query", after_query_match)
    graph.add_edge("extract_and_apply_params", "run_matched_query")
    graph.add_conditional_edges("run_matched_query", after_matched_query)

    graph.add_edge("analyze_question", "check_sql_gen_params")
    graph.add_conditional_edges("check_sql_gen_params", after_check_sql_gen_params)
    graph.add_edge("generate_sql", "validate_sql")
    graph.add_conditional_edges("validate_sql", after_validate)
    graph.add_conditional_edges("run_query", after_query)

    graph.add_edge("generate_answer", END)
    graph.add_edge("handle_error", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# 12. 실행 인터페이스
# ---------------------------------------------------------------------------

def _new_initial_state(question: str) -> Text2SQLState:
    return {
        "question": question, "question_type": "",
        "selected_domain": "", "domain_candidates": [], "domain_routing_trace": "", "domain_context": "",
        "selected_tool": "", "tool_params": {}, "tool_completed": False,
        "missing_params": [], "param_stage": "", "user_provided_params": {},
        "matched_query_name": "", "matched_query_sql": "", "matched_query_params": {}, "extracted_params": {},
        "selected_tables": [], "table_details": "",
        "generated_sql": "", "validation_result": "", "is_valid": False, "retry_count": 0, "final_sql": "",
        "query_columns": [], "query_rows": [], "query_error": "",
        "answer": "", "error_message": "",
        "bad_debt_excel_path": "",
    }


_COMPILED_APP = None

def _get_app():
    global _COMPILED_APP
    if _COMPILED_APP is None:
        _precompute_embeddings()
        _COMPILED_APP = build_graph()
    return _COMPILED_APP

def run_agent_with_prompts(question: str) -> dict:
    """모든 질문에 대해 그래프를 실행하고, 파라미터 누락 시 사용자에게 입력 요청 루프를 수행합니다."""
    app = _get_app()
    state = _new_initial_state(question)
    result = app.invoke(state)

    while result.get("param_stage") == "need_params":
        missing = result.get("missing_params", [])
        missing_names = ", ".join(p["label"] for p in missing)
        print(f"\n  다음 파라미터가 필요합니다: {missing_names}")
        print("  추가로 입력해주세요!")
        print()

        current_params = {}
        tool_name = result.get("selected_tool", "")

        if tool_name:
            current_params = dict(result.get("tool_params", {}))
        else:
            current_params = dict(result.get("user_provided_params", {}))

        for p in missing:
            raw = input(f"  -> {p['label']}: ").strip()
            if raw:
                current_params[p["name"]] = raw

        state = _new_initial_state(question)
        state["question_type"] = "need_sql"
        state["selected_domain"] = result.get("selected_domain", "")
        state["domain_candidates"] = result.get("domain_candidates", [])
        state["domain_routing_trace"] = result.get("domain_routing_trace", "")
        state["domain_context"] = result.get("domain_context", "")

        if tool_name:
            state["selected_tool"] = tool_name
            state["tool_params"] = current_params

            tool = TOOL_MAP.get(tool_name, {})
            required = [p for p in tool.get("parameters", []) if p.get("required", False)]
            still_missing = [p for p in required if not current_params.get(p["name"])]
            if still_missing:
                result["tool_params"] = current_params
                result["missing_params"] = [
                    {
                        "name": p["name"],
                        "label": p["name"],
                        "description": p.get("description", ""),
                        "type": p.get("type", "string"),
                    }
                    for p in still_missing
                ]
                continue
        else:
            state["user_provided_params"] = current_params
            state["selected_tables"] = result.get("selected_tables", [])
            state["table_details"] = result.get("table_details", "")

        result = app.invoke(state)

    return result

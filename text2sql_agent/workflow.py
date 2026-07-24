"""LangGraph node implementations, routing, and execution helpers."""

import ast
import json
import re
from calendar import monthrange
from datetime import datetime
from typing import Literal

import sqlparse
from langgraph.graph import END, StateGraph

from .config import (
    DB_BACKEND,
    DB_SCHEMA_PREFIX,
    EMBED_MATCH_THRESHOLD,
    ENABLE_EMBEDDING_PRECOMPUTE,
    ENABLE_VERIFIED_QUERY_LLM_FALLBACK,
    ENABLE_VERIFIED_QUERY_MATCHING,
    VERIFIED_QUERY_LLM_CANDIDATE_LIMIT,
    VERIFIED_QUERY_MIN_LEXICAL_SCORE,
    VERIFIED_QUERY_RULE_MATCH_MARGIN,
    VERIFIED_QUERY_RULE_MATCH_THRESHOLD,
)
from .db import execute_sql, prepare_sql_for_backend
from .exports import _get_source_label
from .llm import _call_llm, _cosine_similarity, _get_embedding, _get_embeddings_batch
from .managed_scope import ManagedScopeParseError, parse_business_number_list
from .query_frame import build_result_scope, query_frame_prompt
from .schema import (
    SCHEMA,
    VERIFIED_QUERIES,
    _build_domain_embedding_text,
    _keyword_rule_domain_scores,
    _metric_entity_domain_scores,
    _needs_domain_adjudication,
    _adjudicate_domain_with_llm,
    _result_summary,
    _extract_schema_tables,
    _validate_sql_against_schema,
    _weighted_domain_scores,
    build_domain_context,
    build_glossary_summary,
    build_metrics_summary,
    build_semantic_attributes_summary,
    build_semantic_contract_summary,
    build_semantic_join_context,
    find_relevant_queries,
    find_relevant_references,
    find_relevant_semantic_query_contracts,
    resolve_semantic_attribute_value,
    semantic_attribute_candidates,
    semantic_query_contract_candidates,
    semantic_join_paths_for_tables,
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
_DISPLAY_ROW_LIMIT = 100


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
    fenced = re.findall(r"```(?:sql|json|postgresql|trino|presto)?\s*\n?(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if fenced:
        # Prefer the block that actually looks like SQL.  For JSON tasks the
        # first block is normally the only one and is returned unchanged.
        for block in fenced:
            if re.search(r"^\s*(?:SELECT|WITH)\b", block, re.IGNORECASE):
                return block.strip()
        return fenced[0].strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text, count=1)
        text = re.sub(r"\s*```\s*$", "", text, count=1)
    return text.strip()


def _extract_balanced_json_object(text: str) -> str:
    """Extract the first balanced JSON object while respecting quoted braces."""
    start = text.find("{")
    while start >= 0:
        depth = 0
        quote = ""
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = ""
                continue
            if char in {'"', "'"}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1]
        start = text.find("{", start + 1)
    return ""


def _parse_llm_json(value: object) -> dict:
    if isinstance(value, dict):
        return value
    text = re.sub(
        r"<(reasoning|think|analysis)>.*?</\1>\s*",
        "",
        _coerce_llm_text(value),
        flags=re.IGNORECASE | re.DOTALL,
    )
    candidates = [_strip_llm_code_fence(text), _extract_balanced_json_object(text)]
    for candidate in dict.fromkeys(candidate for candidate in candidates if candidate):
        normalized = re.sub(r",\s*([}\]])", r"\1", candidate.strip())
        # 한국어 IME/소형 모델이 곧은따옴표 대신 둥근따옴표(“ ” ‘ ’)를 내는 경우가
        # 있어, 원문 파싱이 실패했을 때만 치환본을 추가로 시도한다.
        straightened = normalized.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
        attempts = [normalized] if straightened == normalized else [normalized, straightened]
        for attempt in attempts:
            try:
                parsed = json.loads(attempt)
            except json.JSONDecodeError:
                try:
                    parsed = ast.literal_eval(attempt)
                except (SyntaxError, ValueError):
                    continue
            if isinstance(parsed, dict):
                return parsed
    return {}


def _truncate_after_sql_terminator(text: str) -> str:
    """Cut trailing prose a small model appends after the statement's ``;``.

    The semicolon itself is kept. Semicolons inside string literals, quoted
    identifiers, and comments are ignored, and nothing changes when no
    terminator exists.
    """
    quote = ""
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "-" and text[index : index + 2] == "--":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline
            continue
        elif char == "/" and text[index : index + 2] == "/*":
            end = text.find("*/", index + 2)
            index = length if end < 0 else end + 2
            continue
        elif char == ";":
            return text[: index + 1]
        index += 1
    return text


def _extract_sql_from_llm(value: object) -> str:
    """Recover SQL from common small-model wrappers without changing its meaning."""
    raw = re.sub(
        r"<(reasoning|think|analysis)>.*?</\1>\s*",
        "",
        _coerce_llm_text(value),
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    text = _strip_llm_code_fence(raw)
    if text != raw:
        return _truncate_after_sql_terminator(text.strip()).strip()
    match = re.search(r"(?im)^\s*(SELECT|WITH)\b", text)
    if match:
        text = text[match.start() :]
    return _truncate_after_sql_terminator(text.strip()).strip()


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
    if _looks_like_direct_sql(question):
        return {"question_type": "direct_sql"}
    if _looks_like_schema_question(question):
        return {"question_type": "direct"}
    if _rule_classify_question(question):
        return {"question_type": "need_sql"}
    domain_lines = []
    for domain in SCHEMA.get("canonical_domains", []):
        domain_lines.append(
            f"- {domain.get('name')}: {domain.get('business_scope', '')} "
            f"(keywords: {', '.join(domain.get('keywords', [])[:8])})"
        )
    glossary_terms = ", ".join(
        str(item.get("term") or "") for item in SCHEMA.get("glossary", []) if item.get("term")
    )
    prompt = f"""당신은 KB카드 기업영업 데이터베이스 시스템의 질문 분류기입니다.
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
- KB카드 기업영업 데이터와 전혀 무관한 질문

## 보유 데이터 도메인
{chr(10).join(domain_lines)}

## 주요 업무 용어
{glossary_terms}

## 사용자 질문
{question}

위 질문의 분류를 다음 형식으로만 반환하세요 (한 단어만):
need_sql 또는 direct 또는 reject"""

    raw = _coerce_llm_text(_call_llm(prompt, max_tokens=128)).lower()
    if "need_sql" in raw:
        qtype = "need_sql"
    elif "direct" in raw:
        qtype = "direct"
    elif "reject" in raw:
        qtype = "reject"
    else:
        qtype = "need_sql"
    return {"question_type": qtype}


def _looks_like_direct_sql(question: str) -> bool:
    """Recognize a user-authored read query without treating surrounding prose as SQL."""
    stripped = _strip_llm_code_fence(question)
    return bool(re.match(r"^\s*(?:SELECT|WITH)\b", stripped, flags=re.IGNORECASE))


def _looks_like_schema_question(question: str) -> bool:
    """Recognize metadata questions that the semantic layer can answer directly."""
    text = question or ""
    return bool(
        re.search(
            r"(?:어느|어떤)\s*테이블|"
            r"(?:테이블|컬럼|스키마).{0,16}(?:구조|정의|설명|목록|용도|의미|자료형|데이터\s*타입|알려|뭐|무엇)|"
            r"(?:grain|그레인|primary\s*key|기본키|주키|PK)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _rule_classify_question(question: str) -> bool:
    """Deterministically route obvious data lookup/aggregation questions to SQL."""
    q = question or ""
    has_metric = bool(re.search(r"매출액|매출금액|매출|이용금액|금액|건수|가맹점\s*(수|개수)|가맹점수|승인율|연체|한도|비율|률|순위|정렬", q))
    has_entity_or_group = bool(re.search(r"가맹점|기업|회사|고객|상호|브랜드|업종|별|도미노피자", q))
    has_time_or_order = bool(re.search(r"저번\s*달|지난\s*달|전월|이번\s*달|이번\s*월|최근|기준|20\d{2}\s*년|\d{1,2}\s*월|높은\s*순|낮은\s*순|정렬", q))
    return has_metric and (has_entity_or_group or has_time_or_order)


def _reference_domain_by_rule(question: str) -> str:
    """Return a curated domain for an exact intent or an unambiguous business phrase."""
    compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", (question or "").lower())
    matches: list[tuple[int, str]] = []
    known_domains = {str(domain.get("name") or "") for domain in SCHEMA.get("canonical_domains", [])}
    for ref in SCHEMA.get("query_references", []):
        domain = str(ref.get("domain") or "")
        if domain not in known_domains:
            continue
        for phrase in ref.get("when_user_says", []):
            normalized = re.sub(r"[^0-9A-Za-z가-힣_]", "", str(phrase or "").lower())
            if len(normalized) >= 4 and normalized in compact:
                matches.append((len(normalized), domain))
    if matches:
        return max(matches)[1]

    # Reusable semantic contracts describe compositional intents (metric +
    # grain + time + entity binding) without depending on a full example
    # sentence.  High-strength contracts are authoritative routing evidence.
    semantic_routes = semantic_query_contract_candidates(
        SCHEMA,
        question,
        max_count=1,
        routing_only=True,
    )
    if semantic_routes:
        domain = str(semantic_routes[0].get("domain") or "")
        if domain in known_domains:
            return domain

    # A merchant/brand name may be written without the generic word
    # "가맹점" (for example, "도미노 피자 최근 6개월 매출액").  Treat a
    # safely extracted name + period + sales metric as merchant performance,
    # before broad metric scoring can mistake "최근 6개월" for a corporate
    # sales-targeting intent.
    if _is_recent_closed_named_merchant_count_question(question):
        return "merchant_sales"
    if _is_brand_merchant_owner_corporate_card_count_question(question):
        return "corporate_sales_targeting"
    if _is_named_merchant_sales_question(question):
        return "merchant_sales"

    # Small local models often over-weight the word "기업" in merchant-sales
    # questions. Keep clear merchant/brand performance questions in the
    # merchant domain unless the user explicitly asks for card ownership or a
    # sales-target population.
    merchant_subject = re.search(r"가맹점|브랜드|점포|매장|상호", question or "")
    merchant_analysis = re.search(r"매출|가맹점\s*(?:수|개수)|수수료|업종|순위", question or "")
    targeting_intent = re.search(
        r"기업카드|법인카드|미보유|보유|소지|영업\s*대상|신규\s*영업|교차판매",
        question or "",
    )
    if merchant_subject and merchant_analysis and not targeting_intent:
        return "merchant_sales"
    return ""


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
    reference_domain = _reference_domain_by_rule(question)
    selected = reference_domain or (candidates[0]["domain"] if candidates else "")
    adjudicated = False
    if not reference_domain and _needs_domain_adjudication(candidates):
        selected = _adjudicate_domain_with_llm(question, candidates, SCHEMA)
        adjudicated = True

    domain_context = build_domain_context(SCHEMA, selected)
    trace_lines = [
        f"selected_domain={selected}",
        f"{embedding_note}",
        f"reference_domain={'ON(' + reference_domain + ')' if reference_domain else 'OFF'}",
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
    match = re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월", question)
    if match:
        return f"{match.group(1)}{int(match.group(2)):02d}"
    match = re.search(r"\b(20\d{2})(0[1-9]|1[0-2])\b", question)
    if match:
        return match.group(0)
    match = re.search(
        r"(?<!\d)(\d{2})(0[1-9]|1[0-2])\s*(?=(?:기준|월))(?!\d)",
        question,
    )
    if match:
        return f"20{match.group(1)}{match.group(2)}"
    match = re.fullmatch(r"\s*(\d{2})(0[1-9]|1[0-2])\s*", question or "")
    if match:
        return f"20{match.group(1)}{match.group(2)}"
    if _has_previous_month_lookup(question):
        return _previous_ym()
    if re.search(r"최근|이번\s*달|이번\s*월|현재\s*월", question):
        return _current_ym()
    return ""


_PREVIOUS_MONTH_COMPARISON_RE = re.compile(
    r"전월\s*(?:대비|비|과(?:의)?\s*비교)"
)


def _has_previous_month_lookup(question: str) -> bool:
    """Return whether ``전월`` is a requested period, not a comparison basis.

    Phrases such as ``전월 대비 증감률`` describe an analytic operation over
    the user's requested period.  Treating that ``전월`` as a relative date
    silently replaces an explicit year with the system's previous month.
    """
    lookup_text = _PREVIOUS_MONTH_COMPARISON_RE.sub(" ", question or "")
    return bool(re.search(r"저번\s*달|지난\s*달|전월", lookup_text))


_ENTITY_NAME_PARAM_NAMES = {
    "가맹점명",
    "기업명",
    "기업검색명",
    "상호명",
    "회사명",
    "업체명",
    "고객명",
    "브랜드명",
    "대상명",
}
_ENTITY_NAME_LABEL_RE = r"(?:가맹점명|기업명|기업검색명|상호명|회사명|업체명|고객명|브랜드명|대상명|이름)"


def _is_exact_name_match_requested(question: str) -> bool:
    """Return whether the user explicitly requested an exact entity-name match."""
    text = re.sub(r"\s+", " ", question or "").strip()
    patterns = (
        rf"{_ENTITY_NAME_LABEL_RE}\s*(?:만으로|으로만)",
        rf"{_ENTITY_NAME_LABEL_RE}\s*(?:을|를|은|는|으로|로)?\s*고정",
        rf"{_ENTITY_NAME_LABEL_RE}(?:(?!기간|날짜|기준)[^.!?]){{0,24}}고정",
        rf"{_ENTITY_NAME_LABEL_RE}[^.!?]{{0,16}}(?:정확히|정확하게|완전\s*일치|정확\s*일치)",
        rf"(?:정확히|정확하게|완전\s*일치|정확\s*일치)[^.!?]{{0,16}}{_ENTITY_NAME_LABEL_RE}",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


_ENTITY_NAME_SQL_COLUMN = (
    r'(?:(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)\.)?"?'
    r'(?:가맹점명|기업명|기업검색명|상호명|회사명|업체명|고객명|브랜드명|대상명)"?'
)
_SQL_STRING_CONTENT = r"(?:''|[^'])*"


def _apply_name_filter_mode(question: str, sql: str) -> str:
    """Normalize simple generated name predicates to contains/exact semantics.

    Default name searches use ``%value%``. Only an explicit exact-name request
    removes the surrounding wildcards. JOIN key comparisons and CASE clauses
    are left untouched because the rewrite is restricted to WHERE/AND/OR
    predicates with string literals.
    """
    exact = _is_exact_name_match_requested(question)
    prefix = r"(?P<prefix>\b(?:WHERE|AND|OR)\s+\(*\s*)"
    column = rf"(?P<column>{_ENTITY_NAME_SQL_COLUMN})"
    value = rf"(?P<value>{_SQL_STRING_CONTENT})"

    def adjusted(raw: str) -> str:
        if exact:
            return raw[1:-1] if len(raw) >= 2 and raw.startswith("%") and raw.endswith("%") else raw
        return raw if raw.startswith("%") and raw.endswith("%") else f"%{raw}%"

    if not exact:
        sql = re.sub(
            rf"{prefix}{column}\s*=\s*'{value}'",
            lambda m: f"{m.group('prefix')}{m.group('column')} LIKE '%{m.group('value')}%'",
            sql,
            flags=re.IGNORECASE,
        )

    sql = re.sub(
        rf"{prefix}{column}\s+LIKE\s+'{value}'(?P<escape>\s+ESCAPE\s+'{_SQL_STRING_CONTENT}')?",
        lambda m: (
            f"{m.group('prefix')}{m.group('column')} LIKE '{adjusted(m.group('value'))}'"
            f"{m.group('escape') or ''}"
        ),
        sql,
        flags=re.IGNORECASE,
    )

    lower_expr = rf"LOWER\(\s*(?P<lower_column>(?:{_ENTITY_NAME_SQL_COLUMN}|COALESCE\(\s*{_ENTITY_NAME_SQL_COLUMN}\s*,\s*''\s*\)))\s*\)"
    if not exact:
        sql = re.sub(
            rf"{prefix}{lower_expr}\s*=\s*LOWER\(\s*'{value}'\s*\)",
            lambda m: (
                f"{m.group('prefix')}LOWER({m.group('lower_column')}) "
                f"LIKE LOWER('%{m.group('value')}%')"
            ),
            sql,
            flags=re.IGNORECASE,
        )
    sql = re.sub(
        rf"{prefix}{lower_expr}\s+LIKE\s+LOWER\(\s*'{value}'\s*\)(?P<lower_escape>\s+ESCAPE\s+'{_SQL_STRING_CONTENT}')?",
        lambda m: (
            f"{m.group('prefix')}LOWER({m.group('lower_column')}) "
            f"LIKE LOWER('{adjusted(m.group('value'))}')"
            f"{m.group('lower_escape') or ''}"
        ),
        sql,
        flags=re.IGNORECASE,
    )
    return sql


_TIME_EXPRESSION_RE = re.compile(
    r"("
    r"20\d{2}\s*년|20\d{2}(?:0[1-9]|1[0-2])(?:[0-3]\d)?|"
    r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])\s*(?:기준|월)(?!\d)|"
    r"\d{1,2}\s*월|\d{4}-\d{1,2}|\d{4}\.\d{1,2}|"
    r"(?:최근|지난)\s*(?:\d{1,3}\s*(?:개월|달)|(?:\d{1,2}|일|반)\s*년)|"
    r"오늘|어제|내일|현재|최근|이번\s*(?:달|월|년)|저번\s*달|지난\s*(?:달|월|해|년)|전월|전년|작년|올해|"
    r"기준(?:일|월|년월|시점)?|기간|월별|일별|연도별|분기|상반기|하반기"
    r")"
)

_SNAPSHOT_STATUS_RE = re.compile(
    r"소지|보유|미보유|가지고\s*있|갖고\s*있|현황|현재|유효|만료|해지|거래정지|활성"
)

_TIME_FILTER_COLUMNS = (
    "기준년월",
    "작업기준년월",
    "기준년월일",
    "실적기준년월일",
    "전표매출년월일",
    "최종심사년월일",
    "최초등록년월일",
)


def _has_time_expression(question: str) -> bool:
    return bool(_TIME_EXPRESSION_RE.search(question or ""))


def _is_snapshot_status_question(question: str) -> bool:
    return bool(_SNAPSHOT_STATUS_RE.search(question or ""))


def _time_resolution_instruction(question: str) -> str:
    current_ym = _current_ym()
    previous_ym = _previous_ym()
    if _has_time_expression(question):
        return f"""17. 날짜 해석 ({_current_date_context()}):
    - 사용자가 명시한 월/기간/상대시점만 날짜 조건으로 반영합니다.
    - "2605 기준" 같은 YYMM 축약은 2000년대 YYYYMM인 "202605"로 해석합니다.
    - "최근", "최근 기준", "이번달", "이번 월"은 현재월 1개월 기준으로 해석합니다.
    - 이때 기준년월 컬럼은 "{current_ym}" 조건을 사용합니다.
    - 이때 기준년월일/실적기준년월일 컬럼은 SUBSTR(일자컬럼, 1, 6) = "{current_ym}" 범위 안의 최신 기준일을 사용합니다.
    - "저번달", "지난달", 조회시점을 뜻하는 단독 "전월"은 지난달 1개월 기준으로 해석하고, 기준년월 컬럼은 "{previous_ym}" 조건을 사용합니다.
    - "전월 대비", "전월비", "전월과 비교"의 전월은 조회기간이 아니라 비교 기준입니다. 명시된 연/월 기간을 유지하고 LAG 등으로 전월 값을 계산합니다.
    - "가맹점별/각 가맹점별"은 가맹점번호와 가맹점명을 SELECT/GROUP BY에 포함합니다.
    - "매출액 높은 순"은 매출액 집계 alias 기준 DESC 정렬합니다."""

    if _is_snapshot_status_question(question):
        return f"""17. 날짜 해석 ({_current_date_context()}):
    - 사용자 질문에 특정 월/기간/상대시점이 없어도 조회 실패로 처리하지 말고, 시스템이 기준시점을 정해 조회합니다.
    - "소지/보유/현황/유효" 같은 스냅샷성 질문의 기본 기준시점은 데이터 최신 스냅샷입니다.
    - 기준년월 컬럼은 해당 테이블의 MAX(기준년월)을 서브쿼리/CTE로 사용합니다.
    - 기준년월일/실적기준년월일 컬럼은 해당 테이블의 MAX(기준년월일/실적기준년월일)을 서브쿼리/CTE로 사용합니다.
    - 가능하면 SELECT 결과에 조회 기준시점 컬럼을 포함하세요(예: latest."실적기준년월일" AS "조회기준일").
    - 카드 유효성 판단은 질문에 기준일이 없으면 현재 실행일({datetime.now().strftime("%Y%m%d")}) 기준으로 실제카드만료년월일 > 현재일 조건을 사용합니다.
    - 예시 SQL의 sample_values/default 값은 형식 참고용입니다. 불가피하게 고정 기준시점을 쓰면 답변에서 그 기준시점을 반드시 명시할 수 있게 SQL에 드러내세요."""

    return f"""17. 날짜 해석 ({_current_date_context()}):
    - 사용자 질문에 특정 월/기간/상대시점이 없어도 조회 실패로 처리하지 말고, 기준시점이 필요한 경우 시스템이 기준시점을 정해 조회합니다.
    - 기간 집계 질문은 시간 조건 없이 전체 기간 기준으로 집계하거나, 스키마 규칙상 스냅샷이 필수인 지표만 MAX(기준시점) 서브쿼리를 사용합니다.
    - 시스템이 기준시점을 정해 조회하면 SELECT 결과나 답변에서 그 기준시점을 명시할 수 있게 SQL에 드러내세요.
    - "가맹점별/각 가맹점별"은 가맹점번호와 가맹점명을 SELECT/GROUP BY에 포함합니다.
    - "매출액 높은 순"은 매출액 집계 alias 기준 DESC 정렬합니다."""


def _extract_unrequested_time_literals(question: str, sql: str) -> list[str]:
    if _has_time_expression(question):
        return []

    current_ym = _current_ym()
    literal_hits: set[str] = set()
    for col in _TIME_FILTER_COLUMNS:
        quoted_col = rf'(?:(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)\.)?"?{re.escape(col)}"?'
        patterns = [
            rf'(?:SUBSTR|SUBSTRING)\s*\([^)]*{quoted_col}[^)]*\)\s*(?:=|<>|!=|>|>=|<|<=)\s*\'(20\d{{4}}(?:\d{{2}})?)\'',
            rf'{quoted_col}\s*(?:=|<>|!=|>|>=|<|<=)\s*\'(20\d{{4}}(?:\d{{2}})?)\'',
            rf'{quoted_col}\s+BETWEEN\s+\'(20\d{{4}}(?:\d{{2}})?)\'\s+AND\s+\'(20\d{{4}}(?:\d{{2}})?)\'',
            rf'(?:SUBSTR|SUBSTRING)\s*\([^)]*{quoted_col}[^)]*\)\s+BETWEEN\s+\'(20\d{{4}}(?:\d{{2}})?)\'\s+AND\s+\'(20\d{{4}}(?:\d{{2}})?)\'',
            rf'{quoted_col}\s+IN\s*\(([^)]*20\d{{4}}(?:\d{{2}})?[^)]*)\)',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, sql or "", flags=re.IGNORECASE):
                for group in match.groups():
                    for value in re.findall(r"20\d{4}(?:\d{2})?", group or ""):
                        if not value.startswith(current_ym):
                            literal_hits.add(value)

    return sorted(literal_hits)


def _implicit_time_basis_note(question: str, sql: str) -> str:
    if _has_time_expression(question):
        return ""

    sql_text = sql or ""
    max_cols = []
    for col in _TIME_FILTER_COLUMNS:
        quoted_col = rf'(?:(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)\.)?"?{re.escape(col)}"?'
        if re.search(rf"\bMAX\s*\(\s*{quoted_col}\s*\)", sql_text, flags=re.IGNORECASE):
            max_cols.append(col)
    if max_cols:
        unique_cols = ", ".join(dict.fromkeys(max_cols))
        return f"질문에 기준시점이 없어 데이터 최신 스냅샷(MAX {unique_cols}) 기준으로 조회했습니다."

    literals = _extract_unrequested_time_literals(question, sql_text)
    if literals:
        return f"질문에 기준시점이 없어 SQL의 기준시점 조건({', '.join(literals)})으로 조회했습니다."

    if _is_snapshot_status_question(question):
        today = datetime.now().strftime("%Y%m%d")
        return f"질문에 기준시점이 없어 현재 실행일({today}) 기준으로 조회했습니다."
    return ""


def _current_ym() -> str:
    now = datetime.now()
    return f"{now.year}{now.month:02d}"


def _previous_ym() -> str:
    now = datetime.now()
    year = now.year if now.month > 1 else now.year - 1
    month = now.month - 1 if now.month > 1 else 12
    return f"{year}{month:02d}"


def _months_back_ym(yyyymm: str, months_back: int) -> str:
    year = int(yyyymm[:4])
    month = int(yyyymm[4:6]) - months_back
    while month <= 0:
        month += 12
        year -= 1
    return f"{year:04d}{month:02d}"


def _month_end(yyyymm: str) -> str:
    year, month = int(yyyymm[:4]), int(yyyymm[4:6])
    return f"{yyyymm}{monthrange(year, month)[1]:02d}"


def _extract_period_by_rule(question: str) -> tuple[str, str, str]:
    """Return ``(start_ym, end_ym, explicit_yyyymmdd)`` from Korean date text."""
    text = question or ""
    day = re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text)
    explicit_day = ""
    if day:
        try:
            explicit_day = f"{day.group(1)}{int(day.group(2)):02d}{int(day.group(3)):02d}"
            datetime.strptime(explicit_day, "%Y%m%d")
        except ValueError:
            explicit_day = ""

    months = [
        f"{match.group(1)}{int(match.group(2)):02d}"
        for match in re.finditer(r"(20\d{2})\s*년\s*(\d{1,2})\s*월", text)
        if 1 <= int(match.group(2)) <= 12
    ]
    months.extend(re.findall(r"(?<!\d)(20\d{2}(?:0[1-9]|1[0-2]))(?!\d)", text))
    months.extend(
        f"20{match.group(1)}{match.group(2)}"
        for match in re.finditer(
            r"(?<!\d)(\d{2})(0[1-9]|1[0-2])\s*(?=(?:기준|월))(?!\d)",
            text,
        )
    )
    shorthand_only = re.fullmatch(r"\s*(\d{2})(0[1-9]|1[0-2])\s*", text)
    if shorthand_only:
        months.append(f"20{shorthand_only.group(1)}{shorthand_only.group(2)}")
    months = list(dict.fromkeys(months))
    if months:
        return months[0], months[-1], explicit_day

    now = datetime.now()
    current = f"{now.year}{now.month:02d}"
    recent = re.search(r"(?:최근|지난)\s*(\d+)\s*(?:개월|달)", text)
    if recent:
        span = max(1, min(int(recent.group(1)), 120))
        return _months_back_ym(current, span - 1), current, explicit_day
    recent_years = re.search(
        r"(?:최근|지난)\s*(\d{1,2}|일)\s*년(?:\s*(?:이내|내|간|동안))?",
        text,
    )
    if recent_years:
        raw_years = recent_years.group(1)
        years = 1 if raw_years == "일" else int(raw_years)
        span = max(1, min(years * 12, 120))
        return _months_back_ym(current, span - 1), current, explicit_day
    year_match = re.search(r"(20\d{2})\s*년", text)
    if year_match:
        year = year_match.group(1)
        return f"{year}01", f"{year}12", explicit_day
    if "작년" in text or "지난해" in text:
        year = now.year - 1
        return f"{year}01", f"{year}12", explicit_day
    if "올해" in text:
        return f"{now.year}01", current, explicit_day
    if _has_previous_month_lookup(text):
        previous = _previous_ym()
        return previous, previous, explicit_day
    if re.search(r"이번\s*(?:달|월)|현재(?:\s*(?:월|시점))?|최근", text):
        return current, current, explicit_day
    return "", "", explicit_day


_AMOUNT_UNIT = {
    "억": 100_000_000,
    "천만": 10_000_000,
    "백만": 1_000_000,
    "십만": 100_000,
    "만": 10_000,
    "천": 1_000,
}


def _parse_korean_number(raw_number: str, unit: str = "") -> int | float:
    number = float(raw_number.replace(",", "")) * _AMOUNT_UNIT.get(unit, 1)
    return int(number) if number.is_integer() else number


def _find_amount_near(question: str, labels: tuple[str, ...]) -> int | float | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    amount_pattern = r"(\d+(?:,\d{3})*(?:\.\d+)?)\s*(천만|백만|십만|억|만|천)?\s*원?"
    patterns = [
        rf"(?:{label_pattern})[^\d]{{0,14}}{amount_pattern}",
        rf"{amount_pattern}[^가-힣A-Za-z0-9]{{0,8}}(?:이상|이하|초과|미만)?[^가-힣A-Za-z0-9]{{0,8}}(?:{label_pattern})",
    ]
    for pattern in patterns:
        match = re.search(pattern, question or "", re.IGNORECASE)
        if match:
            groups = [group for group in match.groups() if group is not None]
            number = next((group for group in groups if re.fullmatch(r"\d+(?:,\d{3})*(?:\.\d+)?", group)), "")
            unit = next((group for group in groups if group in _AMOUNT_UNIT), "")
            if number:
                return _parse_korean_number(number, unit)
    return None


_RECENT_CLOSURE_PERIOD_PATTERN = (
    r"(?:최근|지난)\s*"
    r"(?:(?:\d{1,3}\s*(?:개월|달))|(?:(?:\d{1,2}|일)\s*년)|반\s*년)"
    r"\s*(?:이내|내|간|동안)?"
)


def _extract_recent_period_months_by_rule(question: str) -> int | None:
    """Return the explicit relative period as months for closure queries."""
    text = question or ""
    month_match = re.search(
        r"(?:최근|지난)\s*(\d{1,3})\s*(?:개월|달)\s*(?:이내|내|간|동안)?",
        text,
    )
    if month_match:
        return max(1, min(int(month_match.group(1)), 120))
    if re.search(r"(?:최근|지난)\s*반\s*년\s*(?:이내|내|간|동안)?", text):
        return 6
    year_match = re.search(
        r"(?:최근|지난)\s*(\d{1,2}|일)\s*년\s*(?:이내|내|간|동안)?",
        text,
    )
    if year_match:
        years = 1 if year_match.group(1) == "일" else int(year_match.group(1))
        return max(1, min(years * 12, 120))
    return None


def _extract_recent_closed_merchant_name_by_rule(question: str) -> str:
    """Extract the named merchant from a recent-period closure question."""
    text = question or ""
    token = r"[0-9A-Za-z가-힣&()._-]+"
    closure = r"(?:(?:폐업|휴폐업|해지)(?:한|된)?|문\s*닫(?:은|았던|힌)?)"
    merchant_subject = r"(?:가맹점|점포|매장)"
    patterns = [
        (
            rf"{_RECENT_CLOSURE_PERIOD_PATTERN}\s*(?:에\s*)?{closure}\s+"
            rf"(?P<name>{token}(?:\s+{token}){{0,4}})\s+{merchant_subject}"
        ),
        (
            rf"(?P<name>{token}(?:\s+{token}){{0,4}})\s+{merchant_subject}(?:\s*중)?"
            rf"[^?!.]{{0,40}}?{_RECENT_CLOSURE_PERIOD_PATTERN}[^?!.]{{0,20}}?{closure}"
        ),
    ]
    generic = {
        "당사", "해당", "특정", "전체", "모든", "가맹점", "브랜드",
        "기업", "법인", "개인사업자",
    }
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = re.sub(r"\s+", " ", match.group("name")).strip()
        candidate = re.sub(r"^(?:현재|우리|KB국민)\s+", "", candidate).strip()
        if candidate and candidate not in generic and len(candidate) <= 80:
            return candidate
    return ""


def _extract_merchant_name_by_rule(question: str) -> str:
    """Extract a merchant/brand name from high-confidence question shapes.

    Besides an explicit ``이름 + 브랜드 가맹점`` shape, this supports a name
    next to a period and sales metric, such as ``도미노 피자 최근 6개월
    매출액``. Generic subjects remain for the LLM/clarification path.
    """
    text = question or ""
    recent_closed_name = _extract_recent_closed_merchant_name_by_rule(text)
    if recent_closed_name:
        return recent_closed_name
    text = re.sub(r"20\d{2}\s*년\s*\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?", " ", text)
    text = re.sub(r"(?<!\d)20\d{4}(?:\d{2})?(?!\d)", " ", text)
    text = re.sub(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])\s*(?=기준|월)(?!\d)", " ", text)
    token = r"[0-9A-Za-z가-힣&()._-]+"
    patterns = [
        rf"({token}(?:\s+{token}){{0,4}}?)\s+(?:가맹점\s*주|가맹점주|점주|대표자)",
        rf"({token}(?:\s+{token}){{0,4}})\s+브랜드\s+가맹점",
        rf"({token}(?:\s+{token}){{0,4}})\s+가맹점",
    ]
    generic = {
        "당사", "해당", "특정", "전체", "모든", "사용중인", "사용 중인",
        "신규", "활성", "기업", "법인", "개인사업자", "관리하고 있는",
    }
    invalid_candidate_context = re.compile(
        r"최근|지난|이번|저번|전월|매출|업종별|가맹점별|법인카드|기업카드|"
        r"폐업|휴폐업|해지|일년|\d+\s*년|\d+\s*(?:개월|달)|반년"
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = re.sub(r"\s+", " ", match.group(1)).strip()
        candidate = re.sub(r"^(?:내가|현재|우리|KB국민)\s+", "", candidate).strip()
        compact = re.sub(r"\s+", "", candidate)
        if (
            candidate
            and candidate not in generic
            and len(candidate) <= 80
            and not invalid_candidate_context.search(compact)
        ):
            return candidate

    if not re.search(r"매출(?:액|금액)?|매출\s*추이", text) or not _has_time_expression(text):
        return ""

    time_phrase = (
        r"(?:최근|지난)\s*\d{1,3}\s*개월(?:간|동안)?|"
        r"(?:최근|지난)\s*(?:\d{1,2}|일)\s*년(?:\s*(?:이내|내|간|동안))?|"
        r"이번\s*(?:달|월)|저번\s*달|지난\s*달|전월|"
        r"20\d{2}\s*년(?:\s*\d{1,2}\s*월)?|"
        r"20\d{4}"
    )
    sales_phrase = r"(?:가맹점(?:별)?\s*)?(?:월별\s*)?매출(?:액|금액)?(?:\s*추이)?"
    fallback_patterns = [
        rf"^\s*(?P<name>{token}(?:\s+{token}){{0,4}}?)\s*(?:의\s*)?(?={time_phrase})",
        rf"(?:{time_phrase})\s+(?P<name>{token}(?:\s+{token}){{0,4}}?)\s+(?={sales_phrase})",
        rf"^\s*(?P<name>{token}(?:\s+{token}){{0,4}}?)\s+(?={sales_phrase})",
    ]
    generic_compact = {
        "당사", "해당", "특정", "전체", "모든", "기업", "법인", "개인사업자",
        "가맹점", "브랜드", "업종", "법인카드", "기업카드", "카드",
    }
    for pattern in fallback_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = re.sub(r"\s+", " ", match.group("name")).strip()
        candidate = re.sub(r"^(?:현재|우리|KB국민)\s+", "", candidate).strip()
        candidate = re.sub(r"(?:의|에서)$", "", candidate).strip()
        compact = re.sub(r"\s+", "", candidate)
        if (
            candidate
            and len(candidate) <= 80
            and compact not in generic_compact
            and not invalid_candidate_context.search(compact)
        ):
            return candidate
    return ""


def _is_named_merchant_sales_question(question: str) -> bool:
    """Return whether a question unambiguously asks named merchant sales."""
    return bool(
        _has_time_expression(question)
        and re.search(r"매출(?:액|금액)?|매출\s*추이", question or "")
        and _extract_merchant_name_by_rule(question)
    )


def _is_recent_closed_named_merchant_count_question(question: str) -> bool:
    """Detect a named merchant count whose closure was observed in a recent period."""
    text = question or ""
    return bool(
        _extract_recent_closed_merchant_name_by_rule(text)
        and _extract_recent_period_months_by_rule(text) is not None
        and re.search(r"폐업|휴폐업|해지|문\s*닫", text)
        and re.search(
            r"가맹점\s*(?:수|개수)|몇\s*(?:개|곳)|곳\s*수|점포\s*수",
            text,
        )
    )


def _is_brand_merchant_owner_corporate_card_count_question(question: str) -> bool:
    """Detect a named brand's distinct merchant-owner corporate-card count."""
    text = question or ""
    return bool(
        _extract_merchant_name_by_rule(text)
        and re.search(r"가맹점\s*주|가맹점주|대표자", text)
        and re.search(r"(?:KB국민카드\s*)?(?:기업|법인)카드", text)
        and re.search(r"몇\s*명|명수|인원|사람\s*(?:수|은|이)|몇\s*사람", text)
    )


def _extract_params_by_rule(question: str, param_specs: list[dict]) -> dict:
    """Deterministically extract high-confidence common parameters.

    This intentionally handles only values whose surface form is unambiguous;
    unknown entity names remain for the LLM or the missing-parameter prompt.
    """
    names = {str(spec.get("name") or "") for spec in param_specs if spec.get("name")}
    params: dict = {}
    for spec in param_specs:
        name = str(spec.get("name") or "")
        if not name:
            continue
        attribute_name = str(spec.get("semantic_attribute") or name)
        resolved = resolve_semantic_attribute_value(SCHEMA, attribute_name, question)
        if resolved:
            params[name] = resolved
    recent_period_months = _extract_recent_period_months_by_rule(question)
    for name in {"조회기간개월수", "기간개월수"} & names:
        if recent_period_months is not None:
            params[name] = recent_period_months
    business_number_params = {
        str(spec.get("name") or "")
        for spec in param_specs
        if str(spec.get("type") or "").lower() == "business_number_list" and spec.get("name")
    }
    for name in business_number_params:
        try:
            params[name] = parse_business_number_list(question)
        except ManagedScopeParseError:
            pass
    start_ym, end_ym, explicit_day = _extract_period_by_rule(question)
    for name in names:
        if not start_ym and not explicit_day:
            break
        if name in {"기준년월일", "카드만료기준일"}:
            params[name] = explicit_day or _month_end(end_ym)
        elif name == "전월기준년월":
            params[name] = _months_back_ym(end_ym, 1)
        elif "최근6개월_시작" == name:
            params[name] = _months_back_ym(end_ym, 5)
        elif "시작" in name:
            params[name] = start_ym
        elif "종료" in name:
            params[name] = end_ym
        elif "기준년월" in name or name == "등록기준년월":
            params[name] = end_ym
        elif name == "기준년":
            params[name] = end_ym[:4]

    limit_match = re.search(r"(?:상위|하위|TOP|톱)\s*(\d+)", question or "", re.IGNORECASE)
    if not limit_match:
        limit_match = re.search(r"(\d+)\s*(?:개|곳|건|명)(?!월)", question or "")
    if "limit" in names and limit_match:
        params["limit"] = min(max(int(limit_match.group(1)), 1), 500)

    amount_specs = {
        "월평균금액": ("월평균", "평균 이용금액", "평균금액"),
        "월매출금액": ("월매출", "월 매출", "매출액", "매출금액"),
        "한도금액": ("총한도", "한도금액", "한도"),
    }
    for name, labels in amount_specs.items():
        if name in names and (value := _find_amount_near(question, labels)) is not None:
            params[name] = int(value)

    if "한도소진율" in names:
        ratio = re.search(r"(?:한도\s*)?소진율[^\d]{0,10}(\d+(?:\.\d+)?)\s*%", question or "")
        if ratio:
            params["한도소진율"] = float(ratio.group(1)) / 100
    if "조회개월수" in names:
        month_span = re.search(r"(?:최근|현재\s*시점\s*기준)?\s*(\d{1,3})\s*개월", question or "")
        if month_span:
            params["조회개월수"] = max(1, min(int(month_span.group(1)), 120))
    if "가맹점명" in names:
        merchant_name = _extract_merchant_name_by_rule(question)
        if merchant_name:
            params["가맹점명"] = merchant_name
    for name in ("LS", "IS"):
        if name in names:
            match = re.search(rf"(?<![A-Za-z]){name}\s*(?:[:=은는])?\s*(-?\d+(?:\.\d+)?)", question or "", re.IGNORECASE)
            if match:
                params[name] = float(match.group(1))
    if "등급별" in names and "등급별" in (question or ""):
        params["등급별"] = True
    if "보유구분" in names:
        compact = re.sub(r"\s+", "", question or "")
        for label in ("둘다미보유", "둘다보유", "개인카드미보유", "기업카드미보유", "법인카드미보유", "개인카드보유", "기업카드보유"):
            if label in compact:
                params["보유구분"] = label
                break
    if "이름정확일치" in names and _is_exact_name_match_requested(question):
        params["이름정확일치"] = True
    return params


def _normalize_params(params: dict, param_specs: list[dict]) -> dict:
    """Drop hallucinated keys and reject malformed typed values from LLM JSON."""
    spec_by_name = {str(spec.get("name") or ""): spec for spec in param_specs if spec.get("name")}
    normalized: dict = {}
    for name, value in (params or {}).items():
        spec = spec_by_name.get(str(name))
        if spec is None or value in (None, ""):
            continue
        param_type = str(spec.get("type") or "string").lower()
        try:
            if param_type == "integer":
                raw = str(value).replace(",", "").strip()
                if not re.fullmatch(r"[+-]?\d+", raw):
                    continue
                number = int(raw)
                normalized[name] = min(max(number, 1), 500) if name == "limit" else number
            elif param_type in {"number", "float", "decimal"}:
                raw = str(value).replace(",", "").strip().removesuffix("%")
                if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw):
                    continue
                normalized[name] = float(raw)
            elif param_type == "boolean":
                normalized[name] = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "y"}
            elif param_type == "business_number_list":
                normalized[name] = parse_business_number_list(value)
            else:
                text = str(value).strip()
                if len(text) <= 200:
                    normalized[name] = text
        except (TypeError, ValueError):
            continue
    return normalized


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


_SINGLE_TAG_FORCE_TOOLS = {"대손비용률_분석"}
_TOOL_SCHEMA_COMPATIBILITY: dict[str, bool] = {}


def _tool_schema_compatible(tool: dict) -> bool:
    """Disable deterministic SQL tools whose physical tables are not deployed."""
    name = str(tool.get("name") or "")
    if name in _TOOL_SCHEMA_COMPATIBILITY:
        return _TOOL_SCHEMA_COMPATIBILITY[name]
    if name == "대손비용률_분석":
        _TOOL_SCHEMA_COMPATIBILITY[name] = True
        return True
    params = {
        spec["name"]: spec.get("default")
        for spec in tool.get("parameters", [])
        if spec.get("name") and spec.get("default") not in (None, "")
    }
    try:
        preview = tool["fn"](params)
        if isinstance(preview, dict):
            enabled_names = {query.get("name") for query in VERIFIED_QUERIES}
            compatible = preview.get("sql_query_name") in enabled_names
        elif isinstance(preview, str):
            issues = _validate_sql_against_schema(prepare_sql_for_backend(preview), [])
            compatible = not any("스키마에 없는 테이블" in issue for issue in issues)
        else:
            compatible = False
    except Exception:
        compatible = False
    _TOOL_SCHEMA_COMPATIBILITY[name] = compatible
    return compatible


def _tag_hits(question: str, tags: list[str]) -> int:
    q_lower = (question or "").lower()
    q_compact = re.sub(r"\s+", "", q_lower)
    hits = 0
    for tag in tags:
        tag_lower = str(tag or "").lower().strip()
        if tag_lower and (tag_lower in q_lower or tag_lower in q_compact):
            hits += 1
    return hits


def _rank_tool_candidates(question: str) -> list[tuple[int, dict]]:
    scores: list[tuple[int, dict]] = []
    for tool in TOOLS:
        if not _tool_schema_compatible(tool):
            continue
        hits = _tag_hits(question, tool.get("tags", []))
        if hits:
            scores.append((hits, tool))
    scores.sort(key=lambda item: (item[0], item[1]["name"]), reverse=True)
    return scores


def _tool_candidates(question: str) -> list[dict]:
    candidates = []
    for hits, tool in _rank_tool_candidates(question):
        if hits >= 2 or (hits >= 1 and tool["name"] in _SINGLE_TAG_FORCE_TOOLS):
            candidates.append(tool)
    return candidates


def _empty_capability_selection() -> dict:
    return {
        "selected_capability_type": "",
        "selected_capability_name": "",
        "selected_tool": "",
        "tool_params": {},
        "matched_query_name": "",
        "matched_query_sql": "",
        "matched_query_params": {},
    }


def _tool_capability_result(tool_name: str, params: dict | None = None) -> dict:
    return {
        "selected_capability_type": "tool",
        "selected_capability_name": tool_name,
        "selected_tool": tool_name,
        "tool_params": params or {},
        "matched_query_name": "",
        "matched_query_sql": "",
        "matched_query_params": {},
    }


def _verified_query_capability_result(match: dict) -> dict:
    name = match.get("matched_query_name", "")
    if not name:
        return _empty_capability_selection()
    return {
        "selected_capability_type": "verified_query",
        "selected_capability_name": name,
        "selected_tool": "",
        "tool_params": {},
        "matched_query_name": name,
        "matched_query_sql": match.get("matched_query_sql", ""),
        "matched_query_params": match.get("matched_query_params", {}),
    }


def _rule_match_tool(question: str) -> str:
    """질문에 Tool tags가 등장하는지로 Tool을 우선 매칭한다.

    작은/큰 모델 모두 select_tool에서 핵심 Tool(예: 대손비용률)을 간헐적으로 놓치므로,
    태그가 명확히 일치하는 Tool이 '단독 최다'일 때 LLM 판단보다 먼저 확정한다.
    동점이거나 일치가 없으면 빈 문자열을 돌려 기존 LLM 선택 경로로 넘긴다.
    """
    scores = [(hits, tool["name"]) for hits, tool in _rank_tool_candidates(question)]
    if not scores:
        return ""
    scores.sort(reverse=True)
    top_score, top_name = scores[0]
    if top_score < 2 and top_name not in _SINGLE_TAG_FORCE_TOOLS:
        return ""
    # 최다 점수를 가진 Tool이 유일할 때만 규칙으로 확정 (모호하면 LLM에 위임).
    if sum(1 for score, _ in scores if score == top_score) == 1:
        return top_name
    return ""


def _extract_tool_selection_with_llm(question: str, candidate_tools: list[dict], domain_context: str = "") -> tuple[str, dict]:
    if not candidate_tools:
        return "NONE", {}

    tool_desc = _build_tool_descriptions(candidate_tools)
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    prompt = f"""사용자의 질문에 대해 아래 Tool 중 가장 적합한 것을 선택하고, 질문에서 파라미터를 추출하세요.

## 도메인 라우팅 결과
{domain_context or "(도메인 라우팅 결과 없음)"}

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

    result = _parse_llm_json(_call_llm(prompt, max_tokens=384))
    tool_name = result.get("tool", "NONE") if result else "NONE"
    params = result.get("params", {}) if result else {}
    if not isinstance(params, dict):
        params = {}
    return tool_name, params


def _select_tool_capability(
    question: str,
    candidate_tools: list[dict],
    forced_tool: str = "",
    domain_context: str = "",
) -> dict:
    if forced_tool:
        candidate_tools = [TOOL_MAP[forced_tool]]

    try:
        tool_name, params = _extract_tool_selection_with_llm(question, candidate_tools, domain_context)
    except Exception:
        if not forced_tool:
            raise
        # A high-confidence deterministic route should remain usable even when
        # the small model fails to emit selection JSON.
        tool_name, params = forced_tool, {}
    if forced_tool:
        tool_name = forced_tool
    if tool_name == "NONE" or tool_name not in TOOL_MAP:
        return _empty_capability_selection()

    param_specs = TOOL_MAP[tool_name]["parameters"]
    rule_params = _extract_params_by_rule(question, param_specs)
    params = _normalize_params({**params, **rule_params}, param_specs)
    if any(str(spec.get("name") or "") in _ENTITY_NAME_PARAM_NAMES for spec in param_specs):
        if _is_exact_name_match_requested(question):
            params["이름정확일치"] = True
        else:
            params.pop("이름정확일치", None)
    if tool_name == "대손비용률_분석":
        params = _normalize_params(_augment_bad_debt_params(question, params), param_specs)
    return _tool_capability_result(tool_name, params)


def select_tool(state: Text2SQLState) -> dict:
    if state.get("skip_tool_selection"):
        return _empty_capability_selection()

    if state.get("selected_tool") and state.get("tool_params") is not None:
        selected = TOOL_MAP.get(state["selected_tool"])
        params = state.get("tool_params", {})
        if selected:
            params = _normalize_params(params, selected.get("parameters", []))
        return _tool_capability_result(state["selected_tool"], params)
    if state.get("matched_query_name") and state.get("matched_query_sql"):
        return _verified_query_capability_result(
            {
                "matched_query_name": state["matched_query_name"],
                "matched_query_sql": state.get("matched_query_sql", ""),
                "matched_query_params": state.get("matched_query_params", {}),
            }
        )

    question = state["question"]
    domain_context = state.get("domain_context", "")
    semantic_contracts = semantic_query_contract_candidates(SCHEMA, question, max_count=1)
    if semantic_contracts:
        contract = semantic_contracts[0]
        support_status = str(contract.get("support_status") or "supported").lower()
        if support_status.startswith("blocked"):
            reasons = contract.get("ambiguity_policy", [])
            if isinstance(reasons, str):
                reasons = [reasons]
            details = "\n".join(f"- {reason}" for reason in reasons if reason)
            result = _empty_capability_selection()
            result["answer"] = (
                "현재 semantic layer만으로는 이 지표를 안전하게 계산할 수 없습니다.\n\n"
                f"{details or '- 필요한 원천 컬럼 또는 코드 의미가 정의되지 않았습니다.'}"
            )
            return result
        if str(contract.get("execution_mode") or "").lower() == "semantic_generation":
            result = _empty_capability_selection()
            result["selected_capability_type"] = "semantic_generation"
            result["selected_capability_name"] = str(contract.get("name") or "")
            return result
    # An exact/rule VQ is more specific than a broad tag-based tool.  In
    # particular, "관리기업 한도 감액" must not be captured by the generic
    # low-utilization tool simply because both mention 기업 and 한도.
    verified_query = _select_verified_query_capability(question, state)
    if verified_query:
        return verified_query

    forced_tool = _rule_match_tool(question)
    if forced_tool:
        return _select_tool_capability(question, [], forced_tool, domain_context)

    candidate_tools = _tool_candidates(question)
    if candidate_tools:
        return _select_tool_capability(question, candidate_tools, domain_context=domain_context)
    return _empty_capability_selection()


def check_tool_params(state: Text2SQLState) -> dict:
    tool_name = state.get("selected_tool", "")
    if not tool_name:
        return {"param_stage": "done", "missing_params": []}
    tool = TOOL_MAP.get(tool_name)
    if not tool:
        return {"param_stage": "done", "missing_params": []}
    params = state.get("tool_params", {})
    required = [p for p in tool["parameters"] if p.get("required", False)]
    missing = [p for p in required if params.get(p["name"]) in (None, "")]
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
            final_sql = prepare_sql_for_backend(
                _apply_name_filter_mode(state.get("question", ""), result.get("sql", ""))
            )
            rows = list(result.get("rows", []) or [])
            visible_rows = rows[:_DISPLAY_ROW_LIMIT]
            return {
                "final_sql": final_sql,
                "query_columns": result.get("columns", []),
                "query_rows": visible_rows,
                "result_scope": build_result_scope(
                    final_sql,
                    fetched_row_count=len(rows),
                    displayed_row_count=len(visible_rows),
                ),
                "answer": result.get("answer", ""),
                "bad_debt_excel_path": result.get("excel_path", ""),
                "tool_completed": True,
            }
        sql = _apply_name_filter_mode(state.get("question", ""), result)
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
        return {
            "query_columns": [],
            "query_rows": [],
            "query_error": error,
            "selected_tool": "",
            "final_sql": prepared_sql,
        }
    visible_rows = rows[:_DISPLAY_ROW_LIMIT]
    return {
        "query_columns": columns,
        "query_rows": visible_rows,
        "query_error": "",
        "final_sql": prepared_sql,
        "result_scope": build_result_scope(
            prepared_sql,
            fetched_row_count=len(rows),
            displayed_row_count=len(visible_rows),
        ),
    }


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
    if not _verified_query_is_executable(matched):
        return False
    question_text = question or ""
    matched_text = " ".join(
        str(matched.get(key, ""))
        for key in ("name", "question", "description", "sql")
    )
    matched_text += " " + " ".join(str(tag) for tag in matched.get("tags", []))

    matched_name = str(matched.get("name") or "")
    bound_contracts = [
        contract
        for contract in SCHEMA.get("semantic_query_contracts", [])
        if str(contract.get("verified_query") or "") == matched_name
    ]
    if bound_contracts:
        matched_contract_names = {
            str(contract.get("name") or "")
            for contract in semantic_query_contract_candidates(
                SCHEMA,
                question_text,
                max_count=max(1, len(SCHEMA.get("semantic_query_contracts", []))),
            )
        }
        compatible_contracts = [
            contract
            for contract in bound_contracts
            if str(contract.get("name") or "") in matched_contract_names
            and _semantic_contract_bindings_satisfied(question_text, contract, matched)
        ]
        if not compatible_contracts:
            return False

    asks_sales = bool(re.search(r"매출(?:액|금액|건수)?|판매(?:액|금액|건수)", question_text))
    if asks_sales and not re.search(r"매출|판매", matched_text):
        return False

    asks_merchant_count = bool(re.search(r"가맹점\s*(수|개수)|가맹점수|가맹점개수|몇\s*개", question_text))
    if asks_merchant_count:
        return bool(
            re.search(r"가맹점\s*(수|개수)|가맹점수|가맹점개수", matched_text)
            or re.search(r"COUNT\s*\(\s*DISTINCT\s+[^)]*가맹점번호", matched_text, re.IGNORECASE)
        )
    return True


def _verified_query_is_executable(query: dict) -> bool:
    """Keep migration/reference SQL available as documentation, not runtime templates."""
    return str(query.get("runtime_mode") or "executable").lower() != "reference_only"


def _semantic_contract_bindings_satisfied(question: str, contract: dict, vq: dict) -> bool:
    """Check declarative required entity bindings with the common parameter extractor."""
    raw_bindings = contract.get("entity_binding")
    bindings = raw_bindings if isinstance(raw_bindings, list) else [raw_bindings]
    required = [
        binding
        for binding in bindings
        if isinstance(binding, dict) and binding.get("required") and binding.get("parameter")
    ]
    if not required:
        return True

    param_defs = vq.get("parameters") if isinstance(vq.get("parameters"), dict) else {}
    specs = []
    for binding in required:
        name = str(binding["parameter"])
        info = param_defs.get(name) if isinstance(param_defs.get(name), dict) else {}
        specs.append(
            {
                "name": name,
                "type": info.get("type", "string"),
                "description": info.get("description", ""),
            }
        )
    extracted = _extract_params_by_rule(question, specs)
    return all(extracted.get(str(binding["parameter"])) not in (None, "") for binding in required)


def _match_vq_by_semantic_contract(question: str) -> dict | None:
    """Select a VQ through reusable semantic-layer intent contracts."""
    contracts = semantic_query_contract_candidates(
        SCHEMA,
        question,
        max_count=max(1, len(SCHEMA.get("semantic_query_contracts", []))),
    )
    queries_by_name = {str(vq.get("name") or ""): vq for vq in VERIFIED_QUERIES}
    for contract in contracts:
        if str(contract.get("support_status") or "supported").lower().startswith("blocked"):
            continue
        matched = queries_by_name.get(str(contract.get("verified_query") or ""))
        if not matched or not _verified_query_matches_intent(question, matched):
            continue
        return {
            "matched_query_name": matched["name"],
            "matched_query_sql": matched["sql"].strip(),
            "matched_query_params": matched.get("parameters", {}),
        }
    return None


_VQ_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_]+")


def _score_vq_candidate(question: str, vq: dict) -> int:
    q_lower = (question or "").lower()
    q_tokens = {token for token in _VQ_TOKEN_RE.findall(q_lower) if len(token) >= 2}
    vq_text = " ".join(
        str(value or "")
        for value in (
            vq.get("name"),
            vq.get("question"),
            vq.get("description"),
            " ".join(str(tag) for tag in vq.get("tags", [])),
        )
    ).lower()
    vq_tokens = {token for token in _VQ_TOKEN_RE.findall(vq_text) if len(token) >= 2}
    score = len(q_tokens & vq_tokens)
    q_compact = re.sub(r"\s+", "", q_lower)
    for tag in vq.get("tags", []):
        tag_lower = str(tag or "").lower().strip()
        if tag_lower and (tag_lower in q_lower or tag_lower in q_compact):
            score += 3
    return score


def _rank_vq_candidate_scores(question: str) -> list[tuple[int, dict]]:
    scored = [
        (score, vq)
        for vq in VERIFIED_QUERIES
        if _verified_query_is_executable(vq)
        if (score := _score_vq_candidate(question, vq)) >= VERIFIED_QUERY_MIN_LEXICAL_SCORE
    ]
    scored.sort(key=lambda item: (item[0], item[1].get("name", "")), reverse=True)
    return scored


def _rank_vq_candidates(question: str) -> list[dict]:
    scored = _rank_vq_candidate_scores(question)
    return [vq for _, vq in scored[:VERIFIED_QUERY_LLM_CANDIDATE_LIMIT]]


def _match_vq_by_rules(question: str) -> dict | None:
    scored = _rank_vq_candidate_scores(question)
    if not scored:
        return None
    top_score, matched = scored[0]
    if top_score < VERIFIED_QUERY_RULE_MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and top_score - scored[1][0] < VERIFIED_QUERY_RULE_MATCH_MARGIN:
        return None
    if not _verified_query_matches_intent(question, matched):
        return None
    return {
        "matched_query_name": matched["name"],
        "matched_query_sql": matched["sql"].strip(),
        "matched_query_params": matched.get("parameters", {}),
    }


def _match_vq_by_llm(question: str) -> dict:
    """LLM으로 Verified Query 매칭 (embedding 폴백)."""
    vqs = _rank_vq_candidates(question)
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

    raw = _coerce_llm_text(_call_llm(match_prompt, max_tokens=128))
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


def _select_verified_query_capability(question: str, state: Text2SQLState | dict) -> dict | None:
    if not ENABLE_VERIFIED_QUERY_MATCHING or state.get("skip_verified_query_matching"):
        return None
    contracts = semantic_query_contract_candidates(SCHEMA, question, max_count=1)
    if contracts and str(contracts[0].get("execution_mode") or "").lower() == "semantic_generation":
        return None

    result = _match_vq_by_semantic_contract(question)
    if result:
        return _verified_query_capability_result(result)

    result = _match_vq_by_embedding(question)
    if result:
        return _verified_query_capability_result(result)

    result = _match_vq_by_rules(question)
    if result:
        return _verified_query_capability_result(result)

    if ENABLE_VERIFIED_QUERY_LLM_FALLBACK:
        result = _match_vq_by_llm(question)
        if result.get("matched_query_name"):
            return _verified_query_capability_result(result)
    return None


def _missing_vq_required_params(vq_params_def: dict, params: dict) -> list[dict]:
    missing = []
    for name, info in (vq_params_def or {}).items():
        if not isinstance(info, dict) or not info.get("required"):
            continue
        value = params.get(name)
        if value not in (None, ""):
            continue
        is_time_param = bool(re.search(r"(?:년월|기간|시작|종료|기준일|기준년)", str(name)))
        # Static sample defaults such as 202604 become silently stale in
        # production. Required time values must come from the question, a
        # deterministic relative-date rule, or an explicit continuation.
        if info.get("default") not in (None, "") and not is_time_param:
            continue
        missing.append(
            {
                "name": name,
                "label": name,
                "description": info.get("description", ""),
                "type": info.get("type", "string"),
            }
        )
    return missing


def extract_and_apply_params(state: Text2SQLState) -> dict:
    question = state["question"]
    base_sql = state["matched_query_sql"]
    vq_name = state.get("matched_query_name", "")
    vq_params_def = state.get("matched_query_params", {})
    base_specs = VQ_PARAM_SPECS.get(vq_name, [
        {"name": "기간_시작", "type": "string", "description": "조회 시작 기준년월 (YYYYMM)"},
        {"name": "기간_종료", "type": "string", "description": "조회 종료 기준년월 (YYYYMM)"},
        {"name": "기업명", "type": "string", "description": "기업/상호명 (부분일치)"},
        {"name": "가맹점명", "type": "string", "description": "가맹점명 (부분일치)"},
        {"name": "업종", "type": "string", "description": "업종명/업종 대분류 (부분일치)"},
        {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
    ])
    param_specs = [dict(spec) for spec in base_specs]
    for pname, pinfo in vq_params_def.items():
        info = pinfo if isinstance(pinfo, dict) else {}
        existing = next((spec for spec in param_specs if spec["name"] == pname), None)
        if existing is None:
            param_specs.append({"name": pname, **info})
        else:
            for key, value in info.items():
                existing.setdefault(key, value)
    if (
        any(str(spec.get("name") or "") in _ENTITY_NAME_PARAM_NAMES for spec in param_specs)
        and not any(spec.get("name") == "이름정확일치" for spec in param_specs)
    ):
        param_specs.append(
            {
                "name": "이름정확일치",
                "type": "boolean",
                "description": "사용자가 이름 고정·이름만·정확 일치를 명시한 경우에만 true",
            }
        )
    specs_json = json.dumps(param_specs, ensure_ascii=False, indent=2)
    extract_prompt = f"""사용자의 질문에서 SQL 조회에 필요한 파라미터 값을 추출하세요.

## 추출 가능한 파라미터
{specs_json}

## 추출 규칙 ({_current_date_context()})
1. 사용자가 명시적으로 언급한 값만 추출하세요.
2. 기간 변환: "2025년" → 기간_시작:"202501", 기간_종료:"202512"
   "올해" → 기간_시작:"{datetime.now().year}01", 기간_종료:"{datetime.now().year}{datetime.now().month:02d}"
3. "최근", "이번달", "이번 월"은 현재월 1개월로 해석 → 기준년월/기간_시작/기간_종료 모두 "{_current_ym()}"
4. "저번달", "지난달", 조회시점을 뜻하는 단독 "전월"은 지난달 1개월로 해석 → 기준년월/기간_시작/기간_종료 모두 "{_previous_ym()}"
   단, "전월 대비", "전월비", "전월과 비교"의 전월은 비교 기준이므로 조회기간 파라미터로 추출하지 마세요.
5. "최근 N개월/N달"은 기준년월을 종료월로 보고 N개월 구간으로 해석합니다.
6. "상위 N개" → limit: N
7. "개인카드 미보유" → 보유구분:"개인카드미보유", "기업카드 미보유/법인카드 미보유" → 보유구분:"기업카드미보유"
8. 이름 검색은 기본 부분일치입니다. "이름 고정", "이름만으로", "정확 일치"를 명시한 경우에만 이름정확일치:true로 추출하세요.
9. JSON만 반환하세요. 없으면 빈 오브젝트 {{}}.

## 사용자 질문
{question}

JSON:"""
    rule_params = _extract_params_by_rule(question, param_specs)
    try:
        llm_params = _parse_llm_json(_call_llm(extract_prompt, max_tokens=512))
    except Exception:
        # Parameter extraction is recoverable when deterministic rules and/or
        # explicit continuation values satisfy the template.
        llm_params = {}
    extracted = _normalize_params(
        {
            **(llm_params if isinstance(llm_params, dict) else {}),
            **rule_params,
            **(state.get("user_provided_params", {}) or {}),
        },
        param_specs,
    )
    if any(str(spec.get("name") or "") in _ENTITY_NAME_PARAM_NAMES for spec in param_specs):
        if _is_exact_name_match_requested(question):
            extracted["이름정확일치"] = True
        else:
            extracted.pop("이름정확일치", None)
    if "기준년월" in vq_params_def and not extracted.get("기준년월"):
        ym = _extract_ym_from_question(question) or extracted.get("기간_종료")
        if ym:
            extracted["기준년월"] = ym
    recent_months = re.search(r"최근\s*(\d+)\s*(?:개월|달)", question)
    period_param_names = {str(spec.get("name") or "") for spec in param_specs}
    if recent_months and period_param_names.intersection({"기간_시작", "기간_종료", "기준년월"}):
        end_ym = extracted.get("기간_종료") or extracted.get("기준년월") or _current_ym()
        if re.fullmatch(r"20\d{4}", str(end_ym)):
            span = max(int(recent_months.group(1)), 1)
            extracted.setdefault("기간_종료", end_ym)
            extracted.setdefault("기간_시작", _months_back_ym(end_ym, span - 1))
            if "기준년월" in vq_params_def:
                extracted.setdefault("기준년월", end_ym)
    missing = _missing_vq_required_params(vq_params_def, extracted)
    if missing:
        return {
            "extracted_params": extracted,
            "user_provided_params": extracted,
            "param_stage": "need_params",
            "missing_params": missing,
        }
    final_sql = _apply_params_to_vq(base_sql, extracted, vq_name, vq_params_def)
    final_sql = _apply_name_filter_mode(question, final_sql)
    formatted_sql = sqlparse.format(final_sql, reindent=True, keyword_case="upper")
    return {"extracted_params": extracted, "param_stage": "done", "missing_params": [], "final_sql": prepare_sql_for_backend(formatted_sql)}


def run_matched_query(state: Text2SQLState) -> dict:
    sql = state.get("final_sql", "")
    prepared_sql = prepare_sql_for_backend(sql)
    columns, rows, error = execute_sql(prepared_sql)
    if error:
        return {
            "query_columns": [],
            "query_rows": [],
            "query_error": error,
            "matched_query_name": "",
            "final_sql": prepared_sql,
        }
    visible_rows = rows[:_DISPLAY_ROW_LIMIT]
    return {
        "query_columns": columns,
        "query_rows": visible_rows,
        "query_error": "",
        "final_sql": prepared_sql,
        "result_scope": build_result_scope(
            prepared_sql,
            fetched_row_count=len(rows),
            displayed_row_count=len(visible_rows),
        ),
    }


def direct_answer(state: Text2SQLState) -> dict:
    question = state["question"]
    selected_domain = state.get("selected_domain", "")
    glossary = build_glossary_summary(SCHEMA, question, selected_domain)
    rule_tables = _rule_rank_tables(question)
    schema_question = _looks_like_schema_question(question)
    table_summary = (
        _table_details(rule_tables, question, max_columns=24, max_total_columns=48)
        if schema_question and rule_tables
        else _compact_table_catalog(rule_tables)
    )
    metrics = build_metrics_summary(SCHEMA, question, selected_domain)
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    references = find_relevant_references(SCHEMA, question, domain_name=selected_domain)
    prompt = f"""당신은 KB카드 기업영업 데이터베이스 전문가입니다.
사용자의 질문에 대해 아래 정보를 바탕으로 명확한 한국어 답변을 작성하세요.

## 테이블 정보
{table_summary}

## 비즈니스 용어집
{glossary}

## 재사용 가능한 Semantic Attribute
{build_semantic_attributes_summary(SCHEMA, question, selected_domain)}

## 메트릭 정의
{metrics}

## LLM Semantic Contract
{semantic_contract}

## 질의 작성 Reference
{references}

## 사용자 질문
{question}

답변:"""
    try:
        answer = _coerce_llm_text(_call_llm(prompt, max_tokens=1000))
    except Exception:
        answer = ""
    if answer:
        return {"answer": answer}
    if schema_question and table_summary and "사용 가능한 테이블 없음" not in table_summary:
        return {"answer": table_summary}

    q_compact = re.sub(r"\s+", "", question.lower())
    for glossary_item in SCHEMA.get("glossary", []):
        terms = [glossary_item.get("term", ""), *(glossary_item.get("aliases") or [])]
        if any(re.sub(r"\s+", "", str(term).lower()) in q_compact for term in terms if term):
            lines = [f"### {glossary_item.get('term', '용어 설명')}"]
            if glossary_item.get("description"):
                lines.append(str(glossary_item["description"]))
            if glossary_item.get("canonical"):
                lines.append(f"- 기준: {glossary_item['canonical']}")
            if glossary_item.get("sql_hint"):
                lines.append(f"- 조회 규칙: {glossary_item['sql_hint']}")
            return {"answer": "\n".join(lines)}
    return {"answer": "요청하신 개념을 자동 설명하지 못했습니다. 용어 또는 지표명을 조금 더 구체적으로 입력해주세요."}


def reject_answer(state: Text2SQLState) -> dict:
    return {"answer": "죄송합니다. 현재 기업영업 데이터 범위에서는 답변하기 어려운 질문입니다.\n\n다음처럼 질문해 주세요:\n- 카드 매출·이용금액과 월별 추이\n- 가맹점·업종별 매출과 순위\n- 기업고객의 카드 보유, 여신한도, 연체, 특수채권, 대손충당금\n- 예: `2025년 12월 가맹점별 매출 상위 10곳을 보여줘`"}


_GENERIC_TABLE_TERMS = {"기준년월", "기준년월일", "고객식별자", "회원일련번호", "금액", "건수"}


def _is_semantic_table_visible(table: dict) -> bool:
    return str(table.get("semantic_visibility") or "default").lower() != "restricted"


_DEFAULT_RESTRICTED_COLUMN_RE = re.compile(
    r"(?:주민등록번호|고객고유번호|계좌번호|카드번호|이메일|전자주소|전화번호|상세주소)"
)


def _visible_table_columns(table: dict, section: str) -> list[dict]:
    restricted = {str(name) for name in table.get("restricted_columns", [])}
    return [
        column
        for column in table.get(section, [])
        if str(column.get("name") or "") not in restricted
        and not _DEFAULT_RESTRICTED_COLUMN_RE.search(str(column.get("name") or ""))
    ]


def _phrase_in_question(question_compact: str, phrase: object) -> bool:
    normalized = re.sub(r"[^0-9A-Za-z가-힣_]", "", str(phrase or "").lower())
    return len(normalized) >= 2 and normalized in question_compact


def _contract_source_tables(question: str, contract: dict) -> list[str]:
    policy = contract.get("source_table_policy")
    if not isinstance(policy, dict):
        return list(contract.get("source_tables", []))

    q_compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", (question or "").lower())
    current_snapshot_terms = policy.get("current_snapshot_when", [])
    use_current_snapshot = any(
        _phrase_in_question(q_compact, term) for term in current_snapshot_terms
    )
    key = "current_snapshot" if use_current_snapshot else "period_snapshot"
    return list(policy.get(key) or policy.get("default") or contract.get("source_tables", []))


def _rule_rank_tables(question: str, max_tables: int = 4) -> list[str]:
    """Rank schema tables using explicit semantic-layer evidence only."""
    q_compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", (question or "").lower())
    matched_contracts = semantic_query_contract_candidates(SCHEMA, question, max_count=2)
    for contract in matched_contracts:
        if str(contract.get("table_selection_mode") or "").lower() != "authoritative":
            continue
        selected = []
        for table_name in _contract_source_tables(question, contract):
            name = str(table_name or "").rsplit(".", 1)[-1]
            if name and name not in selected:
                selected.append(name)
        if selected:
            return selected[:max_tables]

    scores: dict[str, int] = {}
    order: dict[str, int] = {}

    def add(table_name: object, score: int) -> None:
        name = str(table_name or "").rsplit(".", 1)[-1]
        if not name:
            return
        scores[name] = scores.get(name, 0) + score
        order.setdefault(name, len(order))

    for metric in SCHEMA.get("canonical_metrics", []):
        terms = [metric.get("name", ""), *metric.get("synonyms", [])]
        if any(_phrase_in_question(q_compact, term) for term in terms):
            add(metric.get("source_table"), 20)

    for attribute in semantic_attribute_candidates(SCHEMA, question, max_count=6):
        for mapping in attribute.get("source_mappings", []):
            add(mapping.get("table"), 18)

    for contract in matched_contracts:
        for table_name in _contract_source_tables(question, contract):
            add(table_name, 28)

    references: list[tuple[int, int, dict]] = []
    for ref in SCHEMA.get("query_references", []):
        evidence = [ref.get("intent", ""), *ref.get("when_user_says", [])]
        tokens = {
            token.lower()
            for value in evidence
            for token in re.findall(r"[0-9A-Za-z가-힣_]+", str(value))
            if len(token) >= 2
        }
        exact_matches = sum(1 for phrase in ref.get("when_user_says", []) if _phrase_in_question(q_compact, phrase))
        score = sum(1 for token in tokens if token in q_compact) + 4 * exact_matches
        references.append((exact_matches, score, ref))
    references.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if references and references[0][1] >= 2:
        exact_matches, best_score, best_ref = references[0]
        # The workbook reference questions are curated, high-confidence intent
        # anchors.  When one of their normalized phrases is present, keep its
        # required tables ahead of broad metric/table-name hits such as "회원".
        primary_weight = 120 + best_score if exact_matches else 12 + best_score
        join_weight = 100 + min(best_score, 12) if exact_matches else 4 + min(best_score, 4)
        add(best_ref.get("primary_table"), primary_weight)
        for table_name in best_ref.get("join_tables", []):
            add(table_name, join_weight)

    for index, table in enumerate(SCHEMA.get("tables", [])):
        if not _is_semantic_table_visible(table):
            continue
        logical = str(table.get("name") or "")
        physical = str(table.get("physical_table") or logical).rsplit(".", 1)[-1]
        order.setdefault(logical, index)
        identity_terms = [logical, physical, table.get("korean_name", ""), *table.get("synonyms", [])]
        if any(_phrase_in_question(q_compact, term) for term in identity_terms):
            add(logical, 14)
        column_hits = 0
        for section in ("dimensions", "measures", "time_dimensions"):
            for column in _visible_table_columns(table, section):
                terms = [column.get("name", ""), *column.get("synonyms", [])]
                if any(
                    str(term or "") not in _GENERIC_TABLE_TERMS and _phrase_in_question(q_compact, term)
                    for term in terms
                ):
                    column_hits += 1
        if column_hits:
            add(logical, min(column_hits, 4) * 3)

    known_by_physical = {
        str(table.get("physical_table") or table.get("name") or "").rsplit(".", 1)[-1]: str(table.get("name") or "")
        for table in SCHEMA.get("tables", [])
        if _is_semantic_table_visible(table)
    }
    normalized_scores: dict[str, int] = {}
    for name, score in scores.items():
        logical = known_by_physical.get(name, name)
        if logical:
            normalized_scores[logical] = normalized_scores.get(logical, 0) + score
    ranked = sorted(normalized_scores, key=lambda name: (-normalized_scores[name], order.get(name, 10_000), name))
    return [name for name in ranked if normalized_scores[name] >= 3][:max_tables]


def _parse_table_selection(raw: object) -> list[str]:
    """Parse comma, newline, bullet, and explanatory LLM table selections."""
    text = _coerce_llm_text(raw)
    table_index: dict[str, str] = {}
    for table in SCHEMA.get("tables", []):
        if not _is_semantic_table_visible(table):
            continue
        logical = str(table.get("name", "")).strip()
        physical = str(table.get("physical_table", "")).strip()
        physical_short = physical.rsplit(".", 1)[-1] if physical else ""
        for candidate in (logical, physical, physical_short, str(table.get("korean_name", "")).strip()):
            if candidate:
                table_index[candidate.lower()] = logical

    selected: list[str] = []
    for fragment in re.split(r"[,;\n]", text):
        cleaned = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", fragment).strip().strip("`\"'")
        cleaned = re.sub(r"^(?:테이블(?:명)?|table)\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE)
        normalized = table_index.get(cleaned.lower())
        if normalized and normalized not in selected:
            selected.append(normalized)
    # Explanatory responses often contain valid physical names in prose.
    lowered = text.lower()
    for alias, logical in table_index.items():
        if re.fullmatch(r"[a-z_][a-z0-9_.]*", alias) and re.search(rf"(?<![a-z0-9_]){re.escape(alias)}(?![a-z0-9_])", lowered):
            if logical not in selected:
                selected.append(logical)
    return selected


def _compact_table_catalog(rule_tables: list[str]) -> str:
    """Build a small selection catalog instead of serializing every column."""
    candidate_names = set(rule_tables)
    if candidate_names:
        for path in semantic_join_paths_for_tables(SCHEMA, candidate_names, require_both=False):
            candidate_names.update(
                name for name in (path.get("from_table"), path.get("to_table")) if name
            )
    tables = [
        table
        for table in SCHEMA.get("tables", [])
        if _is_semantic_table_visible(table) and (not candidate_names or table.get("name") in candidate_names)
    ]
    # Direct-neighbour expansion can be large around customer master tables.
    if candidate_names and len(tables) > 10:
        priority = {name: index for index, name in enumerate(rule_tables)}
        tables.sort(key=lambda table: priority.get(str(table.get("name")), 100))
        tables = tables[:10]

    lines = []
    for table in tables:
        name = str(table.get("name") or "")
        korean_name = str(table.get("korean_name") or "")
        description = re.sub(r"\s+", " ", str(table.get("description") or ""))[:140]
        dimensions = [str(column.get("name")) for column in _visible_table_columns(table, "dimensions")[:5]]
        measures = [str(column.get("name")) for column in _visible_table_columns(table, "measures")[:4]]
        time_columns = [str(column.get("name")) for column in _visible_table_columns(table, "time_dimensions")[:3]]
        lines.append(f"- {name} ({korean_name}): {description}")
        if dimensions:
            lines.append("  주요 차원: " + ", ".join(dimensions))
        if measures:
            lines.append("  주요 지표: " + ", ".join(measures))
        if time_columns:
            lines.append("  시간축: " + ", ".join(time_columns))
    return "\n".join(lines) or "(사용 가능한 테이블 없음)"


def _column_evidence(question: str, table_names: list[str]) -> str:
    evidence = [question]
    q_compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", (question or "").lower())
    for metric in SCHEMA.get("canonical_metrics", []):
        terms = [metric.get("name", ""), *metric.get("synonyms", [])]
        if metric.get("source_table") in table_names and any(_phrase_in_question(q_compact, term) for term in terms):
            evidence.extend(
                [
                    metric.get("name", ""),
                    metric.get("business_definition", metric.get("description", "")),
                    metric.get("expression", ""),
                    metric.get("window_expression", ""),
                    metric.get("numerator_expression", ""),
                    metric.get("denominator_expression", ""),
                    json.dumps(metric.get("time_policy", {}), ensure_ascii=False),
                    json.dumps(metric.get("name_filter", {}), ensure_ascii=False),
                    " ".join(str(value) for value in metric.get("required_filters", [])),
                ]
            )
    for attribute in semantic_attribute_candidates(SCHEMA, question, max_count=6):
        mapped_tables = {
            str(mapping.get("table") or "").rsplit(".", 1)[-1]
            for mapping in attribute.get("source_mappings", [])
        }
        if mapped_tables.intersection(table_names):
            evidence.extend(
                [
                    attribute.get("name", ""),
                    attribute.get("korean_name", ""),
                    attribute.get("business_definition", ""),
                    json.dumps(attribute.get("source_mappings", []), ensure_ascii=False),
                    json.dumps(attribute.get("value_semantics", {}), ensure_ascii=False),
                    " ".join(str(value) for value in attribute.get("semantic_cautions", [])),
                ]
            )
    for contract in semantic_query_contract_candidates(SCHEMA, question, max_count=2):
        source_tables = {str(value).rsplit(".", 1)[-1] for value in contract.get("source_tables", [])}
        if source_tables.intersection(table_names):
            evidence.extend(
                [
                    contract.get("description", ""),
                    json.dumps(contract.get("dimensions", []), ensure_ascii=False),
                    json.dumps(contract.get("entity_binding", {}), ensure_ascii=False),
                    json.dumps(contract.get("name_filter", {}), ensure_ascii=False),
                    json.dumps(contract.get("time_policy", {}), ensure_ascii=False),
                    json.dumps(contract.get("deduplication", {}), ensure_ascii=False),
                    json.dumps(contract.get("filters", []), ensure_ascii=False),
                    json.dumps(contract.get("calculation", {}), ensure_ascii=False),
                ]
            )
    for ref in SCHEMA.get("query_references", []):
        phrases = [ref.get("intent", ""), *ref.get("when_user_says", [])]
        token_hits = sum(
            1
            for phrase in phrases
            for token in re.findall(r"[0-9A-Za-z가-힣_]+", str(phrase).lower())
            if len(token) >= 2 and token in q_compact
        )
        if token_hits >= 2 and (ref.get("primary_table") in table_names or set(ref.get("join_tables", [])) & set(table_names)):
            evidence.extend(
                [
                    ref.get("join_rule", ""),
                    json.dumps(ref.get("recommended_columns", {}), ensure_ascii=False),
                    ref.get("sql_pattern", ""),
                ]
            )
    return " ".join(str(item or "") for item in evidence)


def _table_details(
    table_names: list[str],
    question: str = "",
    max_columns: int = 16,
    max_total_columns: int = 48,
) -> str:
    """Return high-signal, question-focused metadata within one global budget."""
    selected = {str(name or "").rsplit(".", 1)[-1] for name in table_names}
    selected_tables = [
        table
        for table in SCHEMA.get("tables", [])
        if _is_semantic_table_visible(table)
        and (
            str(table.get("name") or "") in selected
            or str(table.get("physical_table") or "").rsplit(".", 1)[-1] in selected
        )
    ]
    if not selected_tables:
        return ""

    selected_names = [str(table.get("name") or "") for table in selected_tables]
    evidence = _column_evidence(question, selected_names)
    evidence_lower = evidence.lower()
    question_compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", (question or "").lower())
    q_tokens = {
        token.lower()
        for token in re.findall(r"[0-9A-Za-z가-힣_]+", question or "")
        if len(token) >= 2 and token not in {"알려줘", "보여줘", "조회", "분석"}
    }

    paths = semantic_join_paths_for_tables(SCHEMA, selected_names, require_both=True)
    relationship_lines = []
    join_text = ""
    for path in paths:
        line = (
            f"- {path.get('from_table')} -> {path.get('to_table')} "
            f"[{path.get('join_type', '')}]: {path.get('sql', '')}"
        )
        if path.get("caution"):
            line += f"; 주의={path.get('caution')}"
        relationship_lines.append(line)
        join_text += " " + str(path.get("sql") or "")

    per_table_limit = min(
        max_columns,
        max(8, max_total_columns // max(len(selected_tables), 1)),
    )
    blocks = []
    for table in selected_tables:
        primary_keys = [str(value) for value in table.get("primary_key", [])]
        primary_key_set = set(primary_keys)
        partition = table.get("athena_partition") if isinstance(table.get("athena_partition"), dict) else {}
        partition_keys = {
            str(key if isinstance(key, str) else key.get("name", ""))
            for key in partition.get("keys", [])
        }
        source_time = str(partition.get("source_time_dimension") or "")
        if source_time:
            partition_keys.add(source_time)
        primary_time = str(table.get("primary_time_dimension") or "")

        ranked: list[tuple[int, int, str, dict]] = []
        position = 0
        for section, semantic_role in (("dimensions", "차원"), ("measures", "지표"), ("time_dimensions", "시간")):
            for section_index, column in enumerate(_visible_table_columns(table, section)):
                name = str(column.get("name") or "")
                score = max(0, 8 - section_index) if section_index < 5 else 0
                if name in primary_key_set:
                    score += 100
                if name == primary_time:
                    score += 95
                if name in partition_keys:
                    score += 90
                if name and name in join_text:
                    score += 80
                if semantic_role == "시간":
                    score += 35 if _has_time_expression(question) else 18
                terms = [name, *column.get("synonyms", [])]
                if any(_phrase_in_question(question_compact, term) for term in terms):
                    score += 70
                if name and name.lower() in evidence_lower:
                    score += 45
                searchable = (
                    f"{name} {column.get('description', '')} "
                    f"{column.get('aggregation', '')} {column.get('role', '')}"
                ).lower()
                score += min(sum(1 for token in q_tokens if token in searchable), 5) * 4
                if name == "개인기업구분코드" and re.search(r"법인|기업", question or ""):
                    score += 60
                ranked.append((score, position, semantic_role, column))
                position += 1
        ranked.sort(key=lambda item: (-item[0], item[1]))
        chosen = sorted(ranked[:per_table_limit], key=lambda item: item[1])

        description = re.sub(r"\s+", " ", str(table.get("description") or ""))[:240]
        source_definition = re.sub(r"\s+", " ", str(table.get("source_definition") or ""))[:280]
        lines = [
            f"### {table.get('name')} ({table.get('physical_table')}) / {table.get('korean_name', '')}",
            f"- purpose: {description}",
            f"- table_kind: {table.get('table_kind', '')}",
            f"- grain: {table.get('grain', '')}",
            f"- primary_key: {', '.join(primary_keys) if primary_keys else '(원천 PK 미선언)'}",
        ]
        if source_definition:
            lines.append(f"- source_definition: {source_definition}")
        if primary_time:
            lines.append(f"- primary_time_dimension: {primary_time}")

        policy = table.get("aggregation_policy") if isinstance(table.get("aggregation_policy"), dict) else {}
        if policy:
            lines.append(
                "- aggregation_policy: "
                f"default={policy.get('default_measure_behavior', '')}, "
                f"across_time={policy.get('aggregate_across_time', False)}, "
                f"deduplicate={policy.get('deduplicate_before_aggregation', False)}"
            )
            for key in (
                "snapshot_deduplication",
                "canonical_formulas",
                "forbidden_as_monthly_sales",
                "forbidden_metric_aliases",
                "overlap_groups",
            ):
                if policy.get(key):
                    rendered = json.dumps(policy.get(key), ensure_ascii=False)
                    lines.append(f"  {key}: {rendered[:900]}")

        if table.get("runtime_scope"):
            lines.append(
                "- runtime_relation: "
                f"type={table.get('relation_type')}, materialized={table.get('materialized')}, "
                f"required_parameter={table.get('required_parameter')}; schema prefix 금지"
            )

        lines.append("- columns:")
        for _, _, semantic_role, column in chosen:
            column_description = re.sub(r"\s+", " ", str(column.get("description") or ""))[:135]
            metadata = []
            if column.get("required"):
                metadata.append("required")
            if column.get("aggregation"):
                metadata.append("aggregation=" + str(column.get("aggregation")))
            if column.get("unit"):
                metadata.append("unit=" + str(column.get("unit")))
            if column.get("format"):
                metadata.append("format=" + str(column.get("format")))
            if column.get("role"):
                metadata.append("time_role=" + str(column.get("role")))
            if column.get("value_semantics"):
                value_text = json.dumps(column.get("value_semantics"), ensure_ascii=False)
                provenance = str(column.get("value_semantics_provenance") or "")
                metadata.append(f"values={value_text}; provenance={provenance}")
            suffix = f" ({'; '.join(metadata)})" if metadata else ""
            lines.append(
                f"  - {column.get('name')} [{semantic_role}, {column.get('data_type', 'UNKNOWN')}]: "
                f"{column_description}{suffix}"
            )
        if partition_keys:
            lines.append("- athena_partition: " + ", ".join(sorted(key for key in partition_keys if key)))
        filters = table.get("filters", [])
        if filters:
            lines.append("- filters: " + "; ".join(f"{item.get('name', '')}: {item.get('expr', '')}" for item in filters[:5]))
        blocks.append("\n".join(lines))

    result = "\n\n".join(blocks)
    if relationship_lines:
        result += "\n\n## Safe joins\n" + "\n".join(relationship_lines)
    return result


def analyze_question(state: Text2SQLState) -> dict:
    if state.get("selected_tables") and state.get("table_details"):
        return {"selected_tables": state["selected_tables"], "table_details": state["table_details"]}

    question = state["question"]
    selected_domain = state.get("selected_domain", "")
    domain_context = state.get("domain_context") or build_domain_context(SCHEMA, selected_domain)
    domain_trace = state.get("domain_routing_trace", "")
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    join_context = build_semantic_join_context(SCHEMA, selected_domain, question, max_paths=5)
    rule_tables = _rule_rank_tables(question)
    prompt = f"""당신은 KB카드 기업영업 데이터베이스 전문가입니다.
사용자의 질문을 분석하여 필요한 테이블을 선택하세요.

## 도메인 라우팅 결과
{domain_context}

## 도메인 라우팅 근거
{domain_trace}

## LLM Semantic Contract
{semantic_contract}

## 안전 조인 그래프
{join_context}

## 규칙 기반 우선 후보
{', '.join(rule_tables) if rule_tables else "(명확한 후보 없음)"}

## 사용 가능한 테이블 (후보 및 직접 연결 테이블)
{_compact_table_catalog(rule_tables)}

## 비즈니스 용어집
{build_glossary_summary(SCHEMA, question, selected_domain)}

## 재사용 가능한 Semantic Attribute
{build_semantic_attributes_summary(SCHEMA, question, selected_domain)}

## 사전 정의된 메트릭
{build_metrics_summary(SCHEMA, question, selected_domain)}

## 재사용 가능한 Semantic Query Contract
{find_relevant_semantic_query_contracts(SCHEMA, question, selected_domain)}

## 질문과 가까운 질의 작성 Reference
{find_relevant_references(SCHEMA, question, domain_name=selected_domain)}

## 규칙
1. 필요한 테이블만 선택. 2. JOIN 필요 시 관련 테이블 포함.
3. 질의 작성 Reference와 맞는 intent가 있으면 primary_table과 join_tables를 우선 포함.
4. 도메인 라우팅 결과의 default_fact_table과 primary_entities를 우선 검토.
5. JOIN이 필요하면 안전 조인 그래프의 from_entity/to_entity에 해당하는 테이블을 우선 포함.
6. 테이블명만 쉼표 구분 반환.

## 사용자 질문
{question}

필요한 테이블명 (쉼표 구분):"""
    try:
        llm_tables = _parse_table_selection(_call_llm(prompt, max_tokens=192))
    except Exception:
        if not rule_tables:
            raise
        llm_tables = []
    # A parseable explicit selection is the model's adjudication; rules are a
    # deterministic fallback for malformed/empty small-model output.
    table_names = llm_tables[:4] if llm_tables else rule_tables[:4]
    return {"selected_tables": table_names, "table_details": _table_details(table_names, question)}


def check_sql_gen_params(state: Text2SQLState) -> dict:
    if state.get("user_provided_params"):
        return {"param_stage": "done", "missing_params": []}

    question = state["question"]
    needed = _missing_ambiguous_target_params(question)
    query_frame = state.get("query_frame") or {}
    if needed and query_frame.get("entities"):
        needed = []

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
            "18. 이 쿼리는 Amazon Athena(Trino/Presto)에서 실행됩니다. 다음 방언 규칙을 지키세요:\n"
            "    - 타입 캐스트는 CAST(expr AS type)만 사용 (PostgreSQL의 expr::type 금지).\n"
            "    - 실수 나눗셈은 CAST(... AS DOUBLE), 정수는 CAST(... AS INTEGER).\n"
            "    - 대소문자 무시 이름 검색은 기본적으로 LOWER(col) LIKE LOWER('%값%') 사용 (ILIKE 금지). 이름 고정·이름만·정확 일치를 명시한 경우만 %를 제거.\n"
            "    - 문자열 부분추출은 SUBSTR(col, start, length) 사용 (LEFT/RIGHT 대신).\n"
            "    - 날짜/문자 함수는 Trino 표준만 사용 (TO_CHAR, DATE_TRUNC 등 PG 전용 함수 금지).\n"
            "    - 한글/비ASCII 컬럼명과 alias는 반드시 double quote로 감싸기 (예: \"기준년월\", a.\"가맹점명\", AS \"총매출금액\").\n"
            "    - 테이블 상세에 athena_partition이 있으면 업무 날짜 조건(예: \"기준년월\" = '202512')과 함께 파티션 조건도 반드시 추가 (예: \"year\" = '2025' AND \"month\" = '12')."
        )
    return ""


def _multiturn_sql_context(state: Text2SQLState | dict) -> str:
    previous_question = str(state.get("previous_question") or "").strip()
    previous_sql = str(state.get("previous_sql") or "").strip()
    previous_answer = str(state.get("previous_answer") or "").strip()
    followup_question = str(state.get("followup_question") or "").strip()
    query_frame = state.get("query_frame") or {}
    if not any((previous_question, previous_sql, previous_answer, followup_question, query_frame)):
        return ""
    return """## 멀티턴 문맥
- 이전 질문: {previous_question}
- 실제 후속 질문: {followup_question}
- 이전 답변 요약: {previous_answer}
- 구조화된 조회 상태:
{query_frame}
- 이전 실행 SQL:
{previous_sql}

구조화된 조회 상태를 조건 상속의 기준으로 사용하세요. 이전 SQL은 보조 참고입니다. 실제 후속 질문에서 변경한 기간·지표·정렬·비교 조건을 우선 적용하고, 새 질문에 없는 대상 조건은 유지하세요. "소스 재탐색 필요: 예"이면 이전 도메인·테이블은 상속하지 말고 현재 도메인 라우팅과 테이블 상세를 사용하세요.
""".format(
        previous_question=previous_question[:1000] or "(없음)",
        followup_question=followup_question[:1000] or "(없음)",
        previous_answer=previous_answer[:1200] or "(없음)",
        query_frame=query_frame_prompt(query_frame),
        previous_sql=previous_sql[:5000] or "(없음)",
    )


def generate_sql(state: Text2SQLState) -> dict:
    question = state["question"]
    table_details = state["table_details"]
    retry_count = state.get("retry_count", 0)
    validation_result = state.get("validation_result", "")
    selected_domain = state.get("selected_domain", "")
    selected_tables = state.get("selected_tables", [])
    relevant_queries = find_relevant_queries(SCHEMA, question, domain_name=selected_domain)
    relevant_references = find_relevant_references(SCHEMA, question, domain_name=selected_domain)
    relevant_semantic_contracts = find_relevant_semantic_query_contracts(
        SCHEMA,
        question,
        selected_domain,
    )
    domain_context = state.get("domain_context") or build_domain_context(SCHEMA, selected_domain)
    domain_trace = state.get("domain_routing_trace", "")
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    join_context = build_semantic_join_context(
        SCHEMA,
        selected_domain,
        question,
        max_paths=5,
        table_names=selected_tables,
    )
    multiturn_context = _multiturn_sql_context(state)
    retry_context = ""
    if retry_count > 0 and validation_result:
        retry_context = f"\n## 이전 시도 실패\n{validation_result}\n\n이전 SQL:\n{state.get('generated_sql', '')}\n\n위 문제를 수정한 SQL을 생성하세요.\n"

    user_params = state.get("user_provided_params", {})
    user_params_context = ""
    if user_params:
        params_text = "\n".join(f"- {k}: {v}" for k, v in user_params.items())
        user_params_context = f"\n## 사용자가 제공한 추가 정보\n{params_text}\n위 값을 SQL의 WHERE 조건이나 파라미터에 반영하세요.\n"

    prompt = f"""당신은 KB카드 기업영업 데이터베이스의 SQL 전문가입니다.
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
{build_metrics_summary(SCHEMA, question, selected_domain)}

## 재사용 가능한 Semantic Attribute
{build_semantic_attributes_summary(SCHEMA, question, selected_domain)}

## 재사용 가능한 Semantic Query Contract
{relevant_semantic_contracts}

## 비즈니스 용어집
{build_glossary_summary(SCHEMA, question, selected_domain)}

## 질문과 가까운 질의 작성 Reference
{relevant_references}

## 참고 SQL 예시
{relevant_queries}

{multiturn_context}
{retry_context}{user_params_context}

## SQL 작성 규칙
1. 위 테이블 정보에 있는 컬럼만 사용. 2. 메트릭 정의된 경우 해당 SQL 공식 사용.
3. 도메인 라우팅 결과의 canonical_metrics, required_filters, default_time_dimension을 우선 반영.
4. JOIN이 필요하면 안전 조인 그래프에 있는 경로만 사용.
5. 코드값은 컬럼의 value semantics와 provenance 또는 정확히 일치하는 Reference가 제공할 때만 사용.
6. 질문에 없는 취소·활성·상태 필터를 임의로 추가하지 말고, snapshot/aggregation policy를 먼저 적용.
7. 기준년월 'YYYYMM', 기준년월일 'YYYYMMDD'. 8. {_schema_prefix_rule()}
9. 질의 작성 Reference가 질문과 맞으면 reference의 primary_table, filter, join_rule을 우선 적용.
10. 질문에 없는 테이블명은 절대 만들지 말고, 위 테이블 상세/Reference/도메인 컨텍스트에 있는 실테이블만 사용.
11. "신규/가입/등록 고객"은 tbdaaat01.최초등록년월일 기준으로 해석.
12. "여성 고객"은 성별구분코드 = '2'를 기본값으로 사용.
13. 상세 목록 조회는 LIMIT 100 이하를 기본으로 둡니다. 집계 결과는 의미있는 순서로 정렬합니다.
14. 가맹점명·기업명·상호명·브랜드명 등 이름 필터는 기본적으로 LIKE '%이름%' 부분일치를 사용합니다. 사용자가 "이름 고정", "이름만으로", "정확 일치"를 명시한 경우에만 앞뒤 % 없이 정확히 비교합니다.
15. 읽기 쉬운 alias. 16. 순수 SQL만 반환.
{_time_resolution_instruction(question)}
{_sql_dialect_rules()}

## 사용자 질문
{question}

SQL:"""
    sql = _extract_sql_from_llm(_call_llm(prompt, max_tokens=2048))
    sql = _apply_recent_month_sql_fix(question, sql)
    sql = _apply_name_filter_mode(question, sql)
    return {"generated_sql": sql}


def _validate_required_semantic_tables(
    question: str,
    sql: str,
    selected_tables: list[str],
) -> list[str]:
    contracts = semantic_query_contract_candidates(SCHEMA, question, max_count=1)
    if not contracts or not contracts[0].get("require_all_selected_tables"):
        return []
    used_tables = _extract_schema_tables(sql)
    required_tables = {
        str(table).rsplit(".", 1)[-1].lower()
        for table in selected_tables
        if table
    }
    missing = sorted(required_tables - used_tables)
    if not missing:
        return []
    return [
        "Semantic Query Contract 필수 테이블 누락: "
        + ", ".join(missing)
        + ". 선택된 현재/과거 기업규모 원천과 실적 테이블을 모두 사용하세요."
    ]


def validate_sql(state: Text2SQLState) -> dict:
    question = state["question"]
    sql = _apply_recent_month_sql_fix(question, state["generated_sql"])
    sql = _apply_name_filter_mode(question, sql)
    sql = prepare_sql_for_backend(sql)
    selected_tables = state["selected_tables"]
    issues = _validate_sql_against_schema(sql, selected_tables)
    issues.extend(_validate_required_semantic_tables(question, sql, selected_tables))
    issues.extend(_validate_recent_month_semantics(question, sql))
    implicit_time_basis = _implicit_time_basis_note(question, sql)
    current_ym = _current_ym()
    try:
        parsed = sqlparse.parse(sql)
        if not parsed or not parsed[0].tokens:
            issues.append("SQL 파싱 실패: 빈 쿼리입니다.")
    except Exception as e:
        issues.append(f"SQL 파싱 오류: {e}")
    def valid_result(note: str = "VALID") -> dict:
        formatted_sql = sqlparse.format(sql, reindent=True, keyword_case="upper")
        return {
            "validation_result": note,
            "is_valid": True,
            "final_sql": prepare_sql_for_backend(formatted_sql),
            "implicit_time_basis": implicit_time_basis,
        }

    def invalid_result(messages: list[str]) -> dict:
        return {
            "validation_result": "\n".join(message for message in messages if message),
            "is_valid": False,
            "retry_count": state.get("retry_count", 0) + 1,
            "implicit_time_basis": implicit_time_basis,
        }

    # Static safety/schema checks are authoritative and do not need another
    # model round trip to reject the SQL.
    if issues:
        return invalid_result(issues)

    if state.get("question_type") == "direct_sql":
        return valid_result("VALID (사용자 입력 SQL 정적 검증 통과)")

    validation_prompt = f"""SQL 검증 전문가로서, 아래 SQL이 사용자 질문에 정확히 답하는지 검증하세요.

사용자 질문: {question}
SQL:
{sql}
사용 테이블: {', '.join(selected_tables)}
검증 기준:
- 이 앱에서 "최근", "최근 기준", "이번달", "이번 월"은 반드시 현재월({current_ym}) 기준입니다.
- 위 표현이 있는 질문에서 SQL이 현재월({current_ym}) 조건을 포함하면 날짜 해석은 올바릅니다.
- 위 표현이 있는 질문에서 전체 데이터의 MAX(기준년월/기준년월일)만 사용하면 잘못입니다.
- "저번달", "지난달", 조회시점을 뜻하는 단독 "전월"은 {_current_date_context()}의 지난달 기준으로 해석합니다.
- "전월 대비", "전월비", "전월과 비교"는 조회기간이 아닌 비교 연산입니다. 질문의 명시 기간을 유지한 SQL이어야 합니다.
- 가맹점명·기업명·상호명·브랜드명 등 이름 필터는 기본 LIKE '%이름%'여야 합니다. "이름 고정", "이름만으로", "정확 일치"가 명시된 경우에만 앞뒤 %가 없어야 합니다.
- 질문에 특정 시점/기간 표현이 없으면 시스템이 기준시점을 정해 조회할 수 있으며, 이것만으로 SQL을 실패 처리하지 않습니다.
- 질문에 특정 시점/기간 표현이 없고 "소지/보유/현황/유효" 같은 스냅샷성 질문이면 데이터 최신 MAX(기준년월/기준년월일) 또는 현재 실행일 기준 유효성 조건을 사용할 수 있습니다.
- 시스템이 정한 기준시점이 SQL에 있으면 결과 답변에서 그 기준시점을 명시하면 됩니다.
메트릭 정의:
{build_metrics_summary(SCHEMA, question, state.get('selected_domain', ''))}

Semantic Query Contract:
{find_relevant_semantic_query_contracts(SCHEMA, question, state.get('selected_domain', ''))}

스키마 기반 사전 검증 결과:
{chr(10).join(issues) if issues else "사전 검증 이슈 없음"}

문제가 없으면 "VALID"만 반환. 문제가 있으면 구체적 목록을 반환."""
    try:
        llm_result = _coerce_llm_text(_call_llm(validation_prompt, max_tokens=512)).strip()
    except Exception as exc:
        return valid_result(f"VALID (정적 검증 통과, LLM 검증 생략: {type(exc).__name__})")

    # Exact token matching avoids treating "VALIDATION FAILED" as VALID.
    if re.fullmatch(r"VALID\s*[.!]?", llm_result, flags=re.IGNORECASE):
        return valid_result()
    if re.search(
        r"(?:VALIDATION\s+FAILED|\bINVALID\b|검증\s*실패|문제|오류|누락|잘못|위반|불일치)",
        llm_result,
        flags=re.IGNORECASE,
    ):
        return invalid_result([llm_result])
    # Empty or structurally unusable verifier output must not turn a statically
    # safe query into a service failure on compact models.
    return valid_result("VALID (정적 검증 통과, LLM 판정 형식 오류로 fallback)")


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
    visible_rows = rows[:_DISPLAY_ROW_LIMIT]
    return {
        "query_columns": columns,
        "query_rows": visible_rows,
        "query_error": "",
        "final_sql": prepared_sql,
        "result_scope": build_result_scope(
            prepared_sql,
            fetched_row_count=len(rows),
            displayed_row_count=len(visible_rows),
        ),
    }


def _markdown_cell(value: object, max_length: int = 80) -> str:
    text = str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


def _deterministic_result_answer(columns: list, rows: list[tuple], implicit_time_basis: str = "") -> str:
    """Render a truthful result summary when the answer model is unavailable."""
    visible_columns = [str(column) for column in columns[:6]]
    lines = ["### 핵심 요약", f"- 조회 결과: {len(rows):,}건"]
    if implicit_time_basis:
        lines.append(f"- 조회 기준: {implicit_time_basis}")
    if len(columns) > len(visible_columns):
        lines.append(f"- 표에는 전체 {len(columns)}개 컬럼 중 앞 {len(visible_columns)}개를 표시합니다.")
    if visible_columns:
        lines.extend(
            [
                "",
                "### 주요 결과",
                "| " + " | ".join(_markdown_cell(column) for column in visible_columns) + " |",
                "| " + " | ".join("---" for _ in visible_columns) + " |",
            ]
        )
        for row in rows[:3]:
            cells = [row[index] if index < len(row) else None for index in range(len(visible_columns))]
            lines.append("| " + " | ".join(_markdown_cell(value) for value in cells) + " |")
        if len(rows) > 3:
            lines.append(f"\n상위 3건을 표시했습니다. 전체 {len(rows):,}건은 결과 표에서 확인할 수 있습니다.")
    return "\n".join(lines)


def _mask_business_numbers_for_llm(value: object) -> str:
    """Keep request-scope identifiers out of answer-generation prompts."""

    return re.sub(
        r"(?<!\d)(?:\d{3}\s*-\s*\d{2}\s*-\s*\d{5}|\d{10})(?!\d)",
        "[사업자등록번호]",
        str(value or ""),
    )


def generate_answer(state: Text2SQLState) -> dict:
    if state.get("answer"):
        return {"answer": state["answer"]}

    question = state["question"]
    sql = state.get("final_sql", "")
    columns = state.get("query_columns", [])
    rows = state.get("query_rows", [])
    if not rows:
        lines = ["조회 결과가 0건입니다."]
        basis = state.get("implicit_time_basis", "") or _implicit_time_basis_note(question, sql)
        if basis:
            lines.append(f"- 조회 기준: {basis}")
        lines.append("- 기간을 넓히거나 이름·업종 등의 검색어를 줄여서 다시 시도해 보세요.")
        return {"answer": "\n".join(lines)}
    implicit_time_basis = state.get("implicit_time_basis", "") or _implicit_time_basis_note(question, sql)
    fallback_answer = _deterministic_result_answer(columns, rows, implicit_time_basis)
    if state.get("question_type") == "direct_sql":
        return {"answer": fallback_answer}
    result_text = " | ".join(columns) + "\n" + "-" * 40 + "\n"
    for row in rows[:20]:
        result_text += " | ".join(_mask_business_numbers_for_llm(v) for v in row) + "\n"
    if len(rows) > 20:
        result_text += f"\n... 외 {len(rows) - 20}건 더 있음"
    source_label = _get_source_label(state)
    summary_text = _result_summary(columns, rows)
    prompt = f"""당신은 KB카드 기업영업 데이터 분석가입니다.
사용자의 질문과 SQL 쿼리 결과를 바탕으로 짧고 직관적인 한국어 답변을 작성하세요.

## 사용자 질문
{_mask_business_numbers_for_llm(question)}

## 실행된 SQL ({source_label})
{_mask_business_numbers_for_llm(sql[:4000])}

## 쿼리 결과 ({len(rows)}건)
{result_text}

## 계산 요약
{_mask_business_numbers_for_llm(summary_text)}

## 기준시점 안내
{implicit_time_basis or "(사용자 질문 또는 SQL에서 별도 기준시점 안내 없음)"}

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
7. 기준시점 안내가 있으면 핵심 요약 또는 해석에 "조회 기준"으로 반드시 명시하세요.

답변:"""
    try:
        answer = _coerce_llm_text(_call_llm(prompt, max_tokens=1200))
    except Exception:
        answer = ""
    return {"answer": answer or fallback_answer}


def handle_error(state: Text2SQLState) -> dict:
    failed_sql = state.get("final_sql") or state.get("generated_sql") or ""
    direct_sql = state.get("question_type") == "direct_sql"
    return {
        "error_message": (
            f"SQL 생성/실행에 실패했습니다 (시도 {state.get('retry_count', 0)}회).\n"
            f"마지막 검증 결과:\n{state.get('validation_result', '알 수 없는 오류')}\n\n"
            f"마지막 SQL:\n{failed_sql or '없음'}"
        ),
        "answer": (
            "입력한 SQL을 실행하지 못했습니다. 실패 원인 분석과 SQL을 확인해 수정해 주세요."
            if direct_sql
            else (
                "질문에 대한 SQL을 생성하거나 실행하지 못했습니다.\n\n"
                "실패 원인 분석과 마지막 SQL을 확인하거나, 조회 대상과 기간을 더 구체적으로 입력해 주세요."
            )
        ),
    }


# ---------------------------------------------------------------------------
# 10. 라우팅
# ---------------------------------------------------------------------------

def prepare_direct_sql(state: Text2SQLState) -> dict:
    """Prepare user-authored SQL for the same read-only validation/execution path."""
    sql = _extract_sql_from_llm(state.get("question", ""))
    used_tables = _extract_schema_tables(sql)
    selected_tables = []
    for table in SCHEMA.get("tables", []):
        logical = str(table.get("name") or "")
        physical = str(table.get("physical_table") or "")
        candidates = {logical.lower(), physical.lower(), physical.rsplit(".", 1)[-1].lower()}
        if candidates.intersection(used_tables):
            selected_tables.append(logical)
    return {
        "selected_domain": "direct_sql",
        "selected_capability_type": "direct_sql",
        "selected_capability_name": "사용자 입력 SQL",
        "selected_tables": selected_tables,
        "table_details": _table_details(selected_tables, state.get("question", "")),
        "generated_sql": sql,
    }


def route_by_question_type(
    state: Text2SQLState,
) -> Literal["route_domain", "prepare_direct_sql", "direct_answer", "reject_answer"]:
    qtype = state.get("question_type", "need_sql")
    if qtype == "direct_sql":
        return "prepare_direct_sql"
    if qtype == "direct":
        return "direct_answer"
    if qtype == "reject":
        return "reject_answer"
    return "route_domain"


def after_tool_selection(state: Text2SQLState) -> Literal["check_tool_params", "extract_and_apply_params", "analyze_question", "__end__"]:
    if state.get("answer"):
        return "__end__"
    if state.get("selected_tool"):
        return "check_tool_params"
    if state.get("matched_query_name"):
        return "extract_and_apply_params"
    return "analyze_question"


def after_check_params(state: Text2SQLState) -> Literal["execute_tool", "__end__"]:
    if state.get("param_stage") == "need_params":
        return "__end__"
    return "execute_tool"


def after_execute_tool(state: Text2SQLState) -> Literal["generate_answer", "run_tool_query"]:
    if state.get("tool_completed"):
        return "generate_answer"
    return "run_tool_query"


def after_tool_query(state: Text2SQLState) -> Literal["generate_answer", "analyze_question"]:
    return "analyze_question" if state.get("query_error") else "generate_answer"


def after_matched_query(state: Text2SQLState) -> Literal["generate_answer", "analyze_question"]:
    return "analyze_question" if state.get("query_error") else "generate_answer"


def after_extract_params(state: Text2SQLState) -> Literal["run_matched_query", "__end__"]:
    if state.get("param_stage") == "need_params":
        return "__end__"
    return "run_matched_query"


def after_check_sql_gen_params(state: Text2SQLState) -> Literal["generate_sql", "__end__"]:
    if state.get("param_stage") == "need_params":
        return "__end__"
    return "generate_sql"


def after_validate(state: Text2SQLState) -> Literal["run_query", "generate_sql", "handle_error"]:
    if state.get("is_valid", False):
        return "run_query"
    if state.get("question_type") == "direct_sql":
        return "handle_error"
    return "handle_error" if state.get("retry_count", 0) >= 3 else "generate_sql"


def after_query(state: Text2SQLState) -> Literal["generate_answer", "generate_sql", "handle_error"]:
    if state.get("query_error"):
        if state.get("question_type") == "direct_sql":
            return "handle_error"
        return "handle_error" if state.get("retry_count", 0) >= 3 else "generate_sql"
    return "generate_answer"


# ---------------------------------------------------------------------------
# 11. 그래프 구성
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    graph = StateGraph(Text2SQLState)

    graph.add_node("classify_question", classify_question)
    graph.add_node("prepare_direct_sql", prepare_direct_sql)
    graph.add_node("route_domain", route_domain)
    graph.add_node("select_tool", select_tool)
    graph.add_node("check_tool_params", check_tool_params)
    graph.add_node("execute_tool", execute_tool)
    graph.add_node("run_tool_query", run_tool_query)
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
    graph.add_edge("prepare_direct_sql", "validate_sql")
    graph.add_edge("route_domain", "select_tool")

    graph.add_edge("direct_answer", END)
    graph.add_edge("reject_answer", END)

    graph.add_conditional_edges("select_tool", after_tool_selection)
    graph.add_conditional_edges("check_tool_params", after_check_params)
    graph.add_conditional_edges("execute_tool", after_execute_tool)
    graph.add_conditional_edges("run_tool_query", after_tool_query)

    graph.add_conditional_edges("extract_and_apply_params", after_extract_params)
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
        "previous_question": "", "previous_sql": "", "previous_answer": "", "followup_question": "",
        "query_frame": {},
        "selected_domain": "", "domain_candidates": [], "domain_routing_trace": "", "domain_context": "",
        "selected_tool": "", "tool_params": {}, "tool_completed": False, "skip_tool_selection": False,
        "selected_capability_type": "", "selected_capability_name": "",
        "missing_params": [], "param_stage": "", "user_provided_params": {},
        "matched_query_name": "", "matched_query_sql": "", "matched_query_params": {}, "extracted_params": {},
        "skip_verified_query_matching": False,
        "selected_tables": [], "table_details": "",
        "generated_sql": "", "validation_result": "", "is_valid": False, "retry_count": 0, "final_sql": "",
        "implicit_time_basis": "",
        "query_columns": [], "query_rows": [], "query_error": "", "result_scope": {},
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
        matched_query_name = result.get("matched_query_name", "")

        if tool_name:
            current_params = dict(result.get("tool_params", {}))
        elif matched_query_name:
            current_params = dict(result.get("extracted_params", {}))
            current_params.update(result.get("user_provided_params", {}) or {})
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
        elif matched_query_name:
            state["matched_query_name"] = matched_query_name
            state["matched_query_sql"] = result.get("matched_query_sql", "")
            state["matched_query_params"] = result.get("matched_query_params", {})
            state["user_provided_params"] = current_params
        else:
            state["user_provided_params"] = current_params
            state["selected_tables"] = result.get("selected_tables", [])
            state["table_details"] = result.get("table_details", "")

        result = app.invoke(state)

    return result

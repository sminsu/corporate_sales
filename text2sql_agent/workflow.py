"""LangGraph node implementations, routing, and execution helpers."""

import ast
import json
import re
from calendar import monthrange
from datetime import date, datetime
from functools import lru_cache
from typing import Literal

import sqlparse
from langgraph.graph import END, StateGraph

from .config import (
    DB_BACKEND,
    DB_SCHEMA_PREFIX,
    DEFAULT_QUERY_ROW_LIMIT,
    DISPLAY_ROW_LIMIT,
    EMBED_MATCH_THRESHOLD,
    ENABLE_EMBEDDING_PRECOMPUTE,
    ENABLE_VERIFIED_QUERY_LLM_FALLBACK,
    ENABLE_VERIFIED_QUERY_MATCHING,
    VERIFIED_QUERY_LLM_CANDIDATE_LIMIT,
    VERIFIED_QUERY_MIN_LEXICAL_SCORE,
    VERIFIED_QUERY_RULE_MATCH_MARGIN,
    VERIFIED_QUERY_RULE_MATCH_THRESHOLD,
    MAX_QUERY_ROW_LIMIT,
)
from .db import count_result_rows, execute_sql, loaded_period_range, prepare_sql_for_backend
from .exports import _get_source_label
from .llm import _call_llm, _cosine_similarity, _get_embedding, _get_embeddings_batch
from .managed_scope import ManagedScopeParseError, parse_business_number_list
from .query_frame import DEFAULT_FETCH_ROW_LIMIT, build_result_scope, query_frame_prompt
from .row_constraints import (
    RowRequest,
    first_order_key,
    is_scalar_aggregate_query,
    outer_limit,
    outer_select_list,
    parse_row_request,
)
from .safety import SAFETY_REFUSAL, check_content_safety
from .schema import (
    SCHEMA,
    VERIFIED_QUERIES,
    _build_domain_embedding_text,
    _has_historical_period_expression,
    _keyword_rule_domain_scores,
    _metric_entity_domain_scores,
    _needs_domain_adjudication,
    _adjudicate_domain_with_llm,
    _result_summary,
    _phrase_in_text,
    _schema_table_index,
    _extract_cte_names,
    _extract_schema_tables,
    _entry_matches_domain,
    _is_restricted_column,
    _sql_table_names,
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
    resolve_query_reference_for_question,
    semantic_attribute_candidates,
    semantic_query_contract_candidates,
    semantic_join_paths_for_tables,
    source_tables_for_question,
)
from .state import Text2SQLState
from .time_policy import (
    TABLE_ACCUMULATION_POLICIES,
    accumulation_policy_for,
    format_accumulation_policy,
    kst_today,
    live_source_for,
    load_axis_window_ymd,
    previous_day_ymd,
    recent_window_ymd,
)
from .tools.registry import TOOLS, TOOL_MAP, _build_tool_descriptions
from .tools.sql_builders import (
    VQ_PARAM_SPECS,
    _apply_params_to_vq,
    _current_date_context,
    _name_like_pattern,
)
from .v2.sql_dialect_guard import (
    audit_sql as _v2_audit_sql,
    looks_like_prose as _v2_looks_like_prose,
    normalize_sql as _v2_normalize_sql,
    prose_reason as _v2_prose_reason,
)
from .v2.column_repair import repair_columns as _v2_repair_columns
from .v2.column_synonyms import compact as _v2_compact
from .v2.vq_output_guard import (
    absolute_period_requested as _v2_absolute_period_requested,
    named_columns as _v2_named_columns,
    vq_output_gap as _v2_vq_output_gap,
)

# v2: 2048 토큰에서는 CTE 여러 개를 쓰는 질의가 문장 중간에서 끊겼다.
SQL_GENERATION_MAX_TOKENS = 4096

# SQL 생성 재시도 한도. validate_sql/run_query 의 실패가 이 횟수에 닿으면 더 고칠
# 기회가 없으므로 handle_error 로 끝낸다.
SQL_RETRY_LIMIT = 3

# ---------------------------------------------------------------------------
# 9. 그래프 노드
# ---------------------------------------------------------------------------

EMBEDDINGS_AVAILABLE = False
VQ_EMBEDDINGS: list[list[float]] = []
DOMAIN_EMBEDDINGS_AVAILABLE = False
DOMAIN_EMBEDDINGS: dict[str, list[float]] = {}
_EMBEDDINGS_INITIALIZED = False
_DISPLAY_ROW_LIMIT = DISPLAY_ROW_LIMIT


def _executed_query_state(sql: str, columns: list, rows: list[tuple]) -> dict:
    """Keep the display slice of a result together with the original row count.

    상태에 남는 행은 화면 한도까지뿐이다. "몇 건이냐"에 그 행 수로 답하면 결과가
    한도 크기(100건)로 보이므로, 조회 한도에 잘린 결과는 전체 건수를 따로 센다.
    """
    visible_rows = rows[:_DISPLAY_ROW_LIMIT]
    total_row_count = len(rows) if len(rows) < DEFAULT_FETCH_ROW_LIMIT else count_result_rows(sql)
    return {
        "query_columns": columns,
        "query_rows": visible_rows,
        "query_error": "",
        "final_sql": sql,
        "result_scope": build_result_scope(
            sql,
            fetched_row_count=len(rows),
            displayed_row_count=len(visible_rows),
            total_row_count=total_row_count,
        ),
    }


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
    missing = []
    if any(re.search(pattern, question) for pattern in _AMBIGUOUS_TARGET_PATTERNS):
        missing.append({"name": "대상명", "label": "조회 대상명(기업명/가맹점명/고객명)"})

    labels: dict[str, list[dict]] = {}
    all_value_terms = set()
    for attribute in SCHEMA.get("semantic_attributes", []):
        for raw_value in (attribute.get("value_semantics") or {}).values():
            value_info = raw_value if isinstance(raw_value, dict) else {"label": raw_value}
            label = str(value_info.get("label") or "").strip()
            compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", label.lower())
            if len(compact) >= 2:
                labels.setdefault(compact, []).append(attribute)
                all_value_terms.add(compact)

    question_compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", (question or "").lower())

    def occurrences(term: str) -> list[tuple[int, int]]:
        spans = []
        start = 0
        while term and (index := question_compact.find(term, start)) >= 0:
            spans.append((index, index + len(term)))
            start = index + 1
        return spans

    for compact, attributes in labels.items():
        unique_attributes = list({str(item.get("name")): item for item in attributes}.values())
        if len(unique_attributes) < 2:
            continue
        # Do not treat a duplicate substring inside a longer valid code label
        # (for example 기타 in 기타제조업) as a standalone ambiguity.
        target_spans = occurrences(compact)
        shadow_spans = [
            span
            for term in all_value_terms
            if len(term) > len(compact) and compact in term
            for span in occurrences(term)
        ]
        if target_spans and all(
            any(start <= target_start and target_end <= end for start, end in shadow_spans)
            for target_start, target_end in target_spans
        ):
            continue
        raw_label = next(
            str((value if not isinstance(value, dict) else value.get("label")) or "")
            for attribute in unique_attributes
            for value in (attribute.get("value_semantics") or {}).values()
            if re.sub(
                r"[^0-9A-Za-z가-힣_]",
                "",
                str((value if not isinstance(value, dict) else value.get("label")) or "").lower(),
            )
            == compact
        )
        if not re.search(
            rf"(?<![0-9A-Za-z가-힣]){re.escape(raw_label)}"
            rf"(?=(?:의|은|는|이|가|을|를|에|에서|로|으로|와|과|도|만|별|인)?(?:[^0-9A-Za-z가-힣]|$))",
            question or "",
        ):
            continue
        if any(
            resolve_semantic_attribute_value(SCHEMA, str(attribute.get("name")), question)
            for attribute in unique_attributes
        ):
            continue
        options = [
            str(attribute.get("korean_name") or attribute.get("name") or "")
            for attribute in unique_attributes
        ]
        missing.append(
            {
                "name": f"{raw_label}분류축",
                "label": f"'{raw_label}' 분류 기준({' / '.join(options)})",
            }
        )
    return missing


def classify_question(state: Text2SQLState) -> dict:
    if state.get("safety_action") != "ALLOW":
        decision = check_content_safety(state.get("question", ""), direction="INPUT")
        if decision["action"] == "BLOCK":
            return {
                "question_type": "safety_blocked",
                "safety_action": decision["action"],
                "safety_category": decision["category"],
                "safety_reason_code": decision["reason_code"],
                "safety_direction": decision["direction"],
            }
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


def _normalize_retrieval_query(value: object, fallback: str) -> str:
    """Normalize a one-line retrieval rewrite without changing the source question."""
    parsed = _parse_llm_json(value)
    candidate = (
        parsed.get("retrieval_query") or parsed.get("resolved_question")
        if parsed
        else value
    )
    text = " ".join(_strip_llm_code_fence(candidate).split()).strip(" \"'“”‘’")
    text = re.sub(
        r"^(?:검색(?:용)?|정제(?:된)?)?\s*질의\s*[:：]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    source_numbers = set(re.findall(r"\d[\d,./:~-]*", fallback))
    refined_numbers = set(re.findall(r"\d[\d,./:~-]*", text))
    if (
        not text
        or text.upper() in {"NONE", "N/A"}
        or not re.search(r"[0-9A-Za-z가-힣]", text)
        or _looks_like_direct_sql(text)
        or not refined_numbers.issubset(source_numbers)
    ):
        return fallback
    return text[:1200]


def refine_search_query(state: Text2SQLState) -> dict:
    """Create an internal retrieval query while preserving the user question."""
    question = str(state.get("question") or "").strip()
    if state.get("retrieval_query"):
        return {"retrieval_query": state["retrieval_query"]}
    if not question or any(
        state.get(key)
        for key in (
            "previous_question",
            "followup_question",
            "selected_domain",
            "selected_tool",
            "matched_query_name",
            "selected_tables",
        )
    ):
        return {"retrieval_query": question}

    prompt = f"""당신은 KB카드 기업영업 데이터 검색을 위한 질의 정제기입니다.
원문의 의미와 사실을 바꾸지 말고, 검색 후보를 찾기 좋은 완전한 한국어 질의 한 문장으로 정리하세요.

규칙:
1. 기업명·가맹점명·브랜드명·코드·숫자·금액·날짜·기간을 원문 그대로 유지하세요.
2. 비교 조건, 부정 표현, 집계 단위, 정렬 조건을 빠뜨리지 마세요.
3. 최근·전월·올해 같은 상대 기간은 구체적인 날짜로 바꾸지 말고 그대로 유지하세요.
4. 인사말·군더더기·말투만 제거하고, 원문에 없는 대상·조건·사실은 추가하지 마세요.
5. 답변이나 SQL, 설명은 반환하지 마세요.

원문 질문:
{question[:4000]}

반환 형식:
{{"retrieval_query":"검색용 질의 한 문장"}}"""
    try:
        refined = _normalize_retrieval_query(_call_llm(prompt, max_tokens=256), question)
    except Exception:
        refined = question
    return {"retrieval_query": refined}


def _retrieval_question(state: Text2SQLState | dict) -> str:
    """Use the rewrite for recall and retain the source text for exact constraints."""
    question = str(state.get("question") or "").strip()
    refined = str(state.get("retrieval_query") or "").strip()
    if not refined or refined == question:
        return question
    return f"{refined}\n{question}"


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
    has_metric = bool(re.search(r"매출액|매출금액|매출|이용금액|금액|건수|(?:가맹점|업체|기업|법인|회사|회원|고객)\s*(?:수|개수)|가맹점수|(?:유효)?카드\s*(?:수|개수)|몇\s*좌|좌\s*수|발급|승인율|연체|잔액|한도|비율|률|순위|정렬", q))
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
    if semantic_routes and _contract_entity_bindings_available(question, semantic_routes[0]):
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
    retrieval_question = _retrieval_question(state)
    domains = SCHEMA.get("canonical_domains", [])
    if not domains:
        return {
            "selected_domain": "",
            "domain_candidates": [],
            "domain_routing_trace": "canonical_domains 없음",
            "domain_context": "(선택된 도메인 없음)",
        }

    keyword_scores = _keyword_rule_domain_scores(SCHEMA, retrieval_question)
    metric_entity_scores = _metric_entity_domain_scores(SCHEMA, retrieval_question)
    embedding_scores: dict[str, float] = {}
    embedding_note = "embedding=OFF"
    if DOMAIN_EMBEDDINGS_AVAILABLE and DOMAIN_EMBEDDINGS:
        try:
            q_emb = _get_embedding(retrieval_question)
            embedding_scores = {
                domain_name: _cosine_similarity(q_emb, emb)
                for domain_name, emb in DOMAIN_EMBEDDINGS.items()
            }
            embedding_note = "embedding=ON"
        except Exception as e:
            embedding_note = f"embedding=FAILED({e})"

    candidates = _weighted_domain_scores(keyword_scores, metric_entity_scores, embedding_scores)
    reference_domain = _reference_domain_by_rule(question)
    if (
        not reference_domain
        and state.get("retrieval_query")
        and state["retrieval_query"] != question
    ):
        reference_domain = _reference_domain_by_rule(state["retrieval_query"])
    selected = reference_domain or (candidates[0]["domain"] if candidates else "")
    adjudicated = False
    if not reference_domain and _needs_domain_adjudication(candidates):
        selected = _adjudicate_domain_with_llm(retrieval_question, candidates, SCHEMA)
        adjudicated = True

    domain_context = build_domain_context(SCHEMA, selected)
    trace_lines = [
        f"selected_domain={selected}",
        f"{embedding_note}",
        f"query_refinement={'ON' if state.get('retrieval_query') and state.get('retrieval_query') != question else 'OFF'}",
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
    question = _expand_two_digit_years(question)
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


@lru_cache(maxsize=8)
def _period_prefixed_column_patterns(prefix: str) -> tuple[re.Pattern[str], ...]:
    """시간 표현으로 시작하는 컬럼명을 질문에서 지울 패턴.

    "2025년 12월 가맹점 전월 일시불 매입건수" 의 전월은 조회 기간이 아니라
    tmdaaus01."전월일시불매입건수" 라는 사전 집계 컬럼 이름의 앞부분이고,
    "최근 한도변경 사유별로" 의 최근은 tbdaa1d12."최근한도변경사유코드" 다.
    이걸 상대 시점으로 읽으면 실행일 기준의 달 조건을 요구해 정답을 반려한다.
    """
    forms = {
        compacted
        for table in SCHEMA.get("tables", [])
        for section in ("dimensions", "measures", "time_dimensions")
        for column in table.get(section) or []
        for text in (
            str(column.get("name") or ""),
            *(str(value) for value in column.get("synonyms") or []),
        )
        if (compacted := _v2_compact(text)).startswith(prefix)
        and len(compacted) > len(prefix)
    }
    return tuple(
        re.compile(r"\W*".join(re.escape(char) for char in form))
        for form in sorted(forms, key=len, reverse=True)
    )


def _without_period_prefixed_columns(text: str, prefix: str) -> str:
    """질문에 적힌 "<접두사>…" 컬럼명을 지운 텍스트."""
    for pattern in _period_prefixed_column_patterns(prefix):
        text = pattern.sub(" ", text)
    return text


def _has_previous_month_lookup(question: str) -> bool:
    """Return whether ``전월`` is a requested period, not a comparison basis.

    Phrases such as ``전월 대비 증감률`` describe an analytic operation over
    the user's requested period.  Treating that ``전월`` as a relative date
    silently replaces an explicit year with the system's previous month.
    지표 컬럼 이름의 앞부분인 전월도 기간이 아니다.
    """
    lookup_text = _PREVIOUS_MONTH_COMPARISON_RE.sub(" ", question or "")
    lookup_text = _without_period_prefixed_columns(lookup_text, "전월")
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
        raw = _name_like_pattern(raw)
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
    r"오늘|어제|전일|내일|현재|최근|이번\s*(?:달|월|년)|저번\s*달|지난\s*(?:달|월|해|년)|전월|전년|작년|올해|"
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


def _latest_available_day(table_name: str) -> str:
    """전일 적재 테이블이 실제로 들고 있는 최신 일자.

    KST 전일은 "적재됐어야 하는 날"이지 "적재된 날"이 아니다. 적재가 하루라도
    밀리면 그 차이만큼 멀쩡한 SQL이 막히고, 정작 데이터가 있는 날은 "과거"로
    분류돼 월별 아카이브로 새어 나간다. 데이터를 못 읽을 때만 시계로 돌아간다.
    """
    bounds = loaded_period_range(table_name)
    latest = bounds[1] if bounds else ""
    return latest if len(latest) == 8 and latest.isdigit() else previous_day_ymd()


def _accumulation_policy_instruction(table_names: list[str] | None = None) -> str:
    """Return table-specific load-cycle rules for SQL generation and validation."""
    policies: list[tuple[str, dict]] = []
    for raw_name in table_names or []:
        name = str(raw_name or "").rsplit(".", 1)[-1].lower()
        policy = accumulation_policy_for(name)
        if policy and all(existing_name != name for existing_name, _ in policies):
            policies.append((name, policy))
    if not policies:
        return ""

    recent_start, recent_end = recent_window_ymd()
    lines = ["    - 선택 테이블별 적재 주기 계약을 최우선으로 적용합니다."]
    for name, policy in policies:
        cadence = str(policy.get("cadence") or "")
        column = str(policy.get("query_time_dimension") or "")
        lines.append(f"    - {name}: {format_accumulation_policy(policy)}")
        if cadence == "daily":
            if policy.get("available_days"):
                historical_source = policy.get("historical_source")
                historical_note = ""
                if isinstance(historical_source, dict):
                    historical_note = (
                        f" 범위 밖 일자나 명시 월/기간은 "
                        f"`{historical_source.get('table')}.{historical_source.get('query_time_dimension')}`"
                        f"({historical_source.get('format')})로 조회합니다."
                    )
                lines.append(
                    f'      `{column}`은 KST 기준 {recent_start}~{recent_end}만 조회할 수 있습니다. '
                    + historical_note
                )
            else:
                lines.append(f"      사용자의 일자/기간은 `{column}`의 YYYYMMDD 조건으로 변환합니다.")
        elif cadence == "monthly":
            lines.append(
                f"      일자 요청도 적재 기준 조건은 `{column}`의 YYYYMM으로 축약합니다. "
                "업무 발생일은 분석·집계 축으로 쓸 수 있지만 적재 월 조건을 생략하지 않습니다."
            )
            live_source = live_source_for(name)
            if live_source:
                lines.append(
                    f"      이번 달({_current_ym()})은 아직 적재되지 않았습니다. "
                    f"이번 달만 물으면 일별 짝 `{live_source['table']}."
                    f"{live_source['query_time_dimension']}`를 최신 가용일 "
                    f"`{_latest_available_day(str(live_source['table']))}` 1건으로 조회하고, "
                    f"지난 달 이전은 `{name}.{column}`을 그대로 씁니다."
                )
        elif cadence == "yearly":
            lines.append(f"      월/일 요청도 적재 기준 조건은 `{column}`의 YYYY로 축약합니다.")
        elif cadence == "previous_day":
            historical_source = policy.get("historical_source")
            latest_available = _latest_available_day(name)
            lines.append(
                f'      현재/오늘/전일/기본 조회의 최신 가용일은 `{latest_available}`이며 `{column}`로 조회합니다.'
            )
            if isinstance(historical_source, dict):
                lines.append(
                    f"      명시 과거 월/기간은 `{historical_source.get('table')}."
                    f"{historical_source.get('query_time_dimension')}`"
                    f"({historical_source.get('format')})로 조회합니다. 최신 스냅샷에만 D-1을 적용합니다."
                )
            else:
                lines.append(f"      명시 기간의 종료일도 `{latest_available}`를 넘길 수 없습니다.")
            if policy.get("has_reference_month") is False:
                lines.append(
                    f'      현재 원천에는 물리 `기준년월` 컬럼은 없습니다.'
                    + (
                        " 과거 월 비교는 위 월별 원천의 `기준년월`을 사용합니다."
                        if isinstance(historical_source, dict)
                        else f' 월 비교가 필요하면 SUBSTR("{column}", 1, 6)으로 파생합니다.'
                    )
                )
    return "\n".join(lines)


def _time_resolution_instruction(question: str, table_names: list[str] | None = None) -> str:
    current_ym = _current_ym()
    previous_ym = _previous_ym()
    policy_rules = _accumulation_policy_instruction(table_names)
    if _has_time_expression(question):
        return f"""17. 날짜 해석 ({_current_date_context()}):
    - 사용자가 명시한 월/기간/상대시점만 날짜 조건으로 반영합니다.
    - "2605 기준" 같은 YYMM 축약은 2000년대 YYYYMM인 "202605"로 해석합니다.
    - "N분기"는 3개월 구간입니다. 1분기 01~03, 2분기 04~06, 3분기 07~09, 4분기 10~12이며 연도가 없으면 올해, "작년 N분기"는 전년입니다.
    - 연도 없는 "N월"은 아직 오지 않은 달로 넘기지 않고 가장 최근에 지나간 그 달로, "작년 N월"은 전년 그 달로 해석합니다.
    - 기간 길이가 없는 단독 "최근", "최근 기준", "이번달", "이번 월"은 현재월 1개월 기준으로 해석합니다.
    - 이때 기준년월 컬럼은 "{current_ym}" 조건을 사용합니다.
    - 이때 기준년월일/실적기준년월일 컬럼은 SUBSTR(일자컬럼, 1, 6) = "{current_ym}" 범위 안의 최신 기준일을 사용합니다.
    - "최근 N개월/N달", "최근 반년/N년"은 1개월이 아닌 기간 표현입니다. 명시 기준월이 있으면 그 월을 종료월로, 없으면 현재일/현재월을 종료로 사용합니다.
    - "저번달", "지난달", 조회시점을 뜻하는 단독 "전월"은 지난달 1개월 기준으로 해석하고, 기준년월 컬럼은 "{previous_ym}" 조건을 사용합니다.
    - "전월 대비", "전월비", "전월과 비교"의 전월은 조회기간이 아니라 비교 기준입니다. 명시된 연/월 기간을 유지하고 LAG 등으로 전월 값을 계산합니다.
    - "가맹점별/각 가맹점별"은 가맹점번호와 가맹점명을 SELECT/GROUP BY에 포함합니다.
    - "매출액 높은 순"은 매출액 집계 alias 기준 DESC 정렬합니다.
{policy_rules}"""

    if _is_snapshot_status_question(question):
        return f"""17. 날짜 해석 ({_current_date_context()}):
    - 사용자 질문에 특정 월/기간/상대시점이 없어도 조회 실패로 처리하지 말고, 시스템이 기준시점을 정해 조회합니다.
    - "소지/보유/현황/유효" 같은 스냅샷성 질문의 기본 기준시점은 데이터 최신 스냅샷입니다.
    - 기준년월 컬럼은 해당 테이블의 MAX(기준년월)을 서브쿼리/CTE로 사용합니다.
    - 기준년월일/실적기준년월일 컬럼은 해당 테이블의 MAX(기준년월일/실적기준년월일)을 서브쿼리/CTE로 사용합니다.
    - 가능하면 SELECT 결과에 조회 기준시점 컬럼을 포함하세요(예: latest."실적기준년월일" AS "조회기준일").
    - 카드 유효성 판단은 질문에 기준일이 없으면 현재 실행일({datetime.now().strftime("%Y%m%d")}) 기준으로 실제카드만료년월일 > 현재일 조건을 사용합니다.
    - 예시 SQL의 sample_values/default 값은 형식 참고용입니다. 불가피하게 고정 기준시점을 쓰면 답변에서 그 기준시점을 반드시 명시할 수 있게 SQL에 드러내세요.
{policy_rules}"""

    return f"""17. 날짜 해석 ({_current_date_context()}):
    - 사용자 질문에 특정 월/기간/상대시점이 없어도 조회 실패로 처리하지 말고, 기준시점이 필요한 경우 시스템이 기준시점을 정해 조회합니다.
    - 기간 집계 질문은 시간 조건 없이 전체 기간 기준으로 집계하거나, 스키마 규칙상 스냅샷이 필수인 지표만 MAX(기준시점) 서브쿼리를 사용합니다.
    - 시스템이 기준시점을 정해 조회하면 SELECT 결과나 답변에서 그 기준시점을 명시할 수 있게 SQL에 드러내세요.
    - "가맹점별/각 가맹점별"은 가맹점번호와 가맹점명을 SELECT/GROUP BY에 포함합니다.
    - "매출액 높은 순"은 매출액 집계 alias 기준 DESC 정렬합니다.
{policy_rules}"""


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


def _merchant_master_period_note(start_ym: str, end_ym: str) -> str:
    """요청한 달을 마스터로 답할 때 붙일 기준시점 안내.

    "없는 달을 최신 시점으로 대신 답했다" 는 사실을 사용자가 답변에서 알아야
    한다. 없다고 단정하는 것은 적재 범위를 실제로 읽어냈을 때뿐이다.
    """
    latest = _latest_available_day(_TBDAADT01_TABLE)
    requested = start_ym if start_ym == end_ym else f"{start_ym}~{end_ym}"
    basis = (
        f"{_TBDAADT01_TABLE}의 최신 {_TBDAADT01_TIME_COLUMN} {latest} 기준으로 조회했습니다."
    )
    bounds = loaded_period_range(_TBDAADT01_TABLE)
    if not bounds:
        return f"요청하신 {requested} 시점 데이터는 {_TBDAADT01_TABLE}에 없을 수 있습니다. {basis}"
    if start_ym <= bounds[1][:6] and end_ym >= bounds[0][:6]:
        return basis
    return (
        f"{_TBDAADT01_TABLE}의 조회 가능 기간은 "
        f'"{_TBDAADT01_TIME_COLUMN}" {bounds[0]} ~ {bounds[1]}이라 '
        f"요청하신 {requested} 데이터는 없습니다. {basis}"
    )


def _implicit_time_basis_note(question: str, sql: str) -> str:
    sql_text = sql or ""
    used_tables = _extract_schema_tables(sql_text)
    cadence, requested_start, requested_end = _tbdaadt01_time_route(question)
    _, _, explicit_day = _extract_period_by_rule(question)
    if cadence == "master" and _TBDAADT01_TABLE in used_tables:
        return _merchant_master_period_note(requested_start, requested_end)
    # "이번 달"·"최근" 을 마스터로 답했으면 그 기준을 답변에 드러낸다. 이 안내가 없으면
    # 검증 LLM 이 "질문은 이번 달인데 SQL 에 그 달 조건이 없다" 로 읽어 되돌려보낸다.
    relative_ym, _ = _relative_month_target(question)
    if (
        relative_ym == _current_ym()
        and _TBDAADT01_TABLE in used_tables
        and _reads_only_master_tables(sql_text)
    ):
        return _merchant_master_period_note(relative_ym, relative_ym)
    if cadence == "monthly" and explicit_day and "tmdaa5d01" in used_tables:
        return (
            f"요청일 {explicit_day}은 tbdaadt01의 최근 10일 보관 범위 밖이므로 "
            f"tmdaa5d01의 월 스냅샷({explicit_day[:6]}) 기준으로 조회했습니다. "
            "일 단위 해상도는 제공되지 않습니다."
        )
    historical_start, historical_end = _previous_day_archive_route(question)
    historical_archives = sorted(
        table
        for table in used_tables
        if table in set(_PREVIOUS_DAY_ARCHIVE_TABLES.values())
    )
    if historical_start and historical_archives:
        period = (
            historical_start
            if historical_start == historical_end
            else f"{historical_start}~{historical_end}"
        )
        return (
            f"{', '.join(historical_archives)}의 월별 스냅샷({period}) 기준으로 "
            "과거 기간을 조회했습니다."
        )
    previous_day_tables = sorted(
        table
        for table in used_tables
        if (accumulation_policy_for(table) or {}).get("cadence") == "previous_day"
    )
    if previous_day_tables:
        return (
            f"{', '.join(previous_day_tables)}는 최신 가용일"
            f"({_latest_available_day(previous_day_tables[0])}) 기준으로 조회했습니다."
        )
    recent_window_tables = sorted(
        table
        for table in used_tables
        if (accumulation_policy_for(table) or {}).get("available_days")
    )
    if recent_window_tables:
        start, end = recent_window_ymd()
        return (
            f"{', '.join(recent_window_tables)}의 조회 가능 범위인 KST 최근 10일"
            f"({start}~{end}) 기준으로 조회했습니다."
        )
    if _has_time_expression(question):
        return ""

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
    today = kst_today()
    return f"{today.year}{today.month:02d}"


def _previous_ym() -> str:
    today = kst_today()
    year = today.year if today.month > 1 else today.year - 1
    month = today.month - 1 if today.month > 1 else 12
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


def _extract_half_year_ranges_by_rule(question: str) -> list[tuple[str, str]]:
    ranges = []
    for match in re.finditer(r"(20\d{2})\s*년\s*(상반기|하반기)", question or ""):
        year, half = match.groups()
        ranges.append((f"{year}01", f"{year}06") if half == "상반기" else (f"{year}07", f"{year}12"))
    return ranges


def _quarter_month_range(year: int, quarter: int) -> tuple[str, str]:
    start = (quarter - 1) * 3 + 1
    return f"{year}{start:02d}", f"{year}{start + 2:02d}"


def _extract_quarter_period_by_rule(question: str, today: date) -> tuple[str, str]:
    """분기 표현을 그 분기의 3개월로 읽는다.

    분기는 시간표현 탐지 키워드에만 있어서 "2026년 3분기" 가 연도 전체
    (202601~202612)로 떨어졌다. 연도가 없으면 실행연도, "작년 N분기" 는 전년이다.
    """
    text = question or ""
    ranges = []
    for match in re.finditer(
        r"(?:(20\d{2})\s*년|(작년|지난해|전년도?))?\s*(?<!\d)([1-4])\s*분기", text
    ):
        year_text, last_year, quarter = match.groups()
        year = int(year_text) if year_text else today.year - (1 if last_year else 0)
        ranges.append(_quarter_month_range(year, int(quarter)))
    if ranges:
        return min(start for start, _ in ranges), max(end for _, end in ranges)

    relative = re.search(r"(이번|금|당|지난|저번|직전|전)\s*분기", text)
    if not relative:
        return "", ""
    quarter, year = (today.month - 1) // 3 + 1, today.year
    if relative.group(1) not in {"이번", "금", "당"}:
        quarter, year = (4, year - 1) if quarter == 1 else (quarter - 1, year)
    return _quarter_month_range(year, quarter)


def _recent_ym_for_month(month: int, today: date) -> str:
    """연도 없는 "N월"은 미래로 넘기지 않고 가장 최근에 지나간 그 달로 읽는다."""
    return f"{today.year - (0 if month <= today.month else 1)}{month:02d}"


# 두 자리로 적은 연도. 기간 정규식은 모두 네 자리(20\d{2})를 전제하므로, 두 자리
# 연도는 통째로 버려지고 뒤의 월만 남는다. 그래서 "26년 7월 9일" 의 일자가 사라져
# 하루 질문이 월 집계로 답해지고, "25년 7월" 은 bare_month 규칙이 "가장 최근 7월"
# 을 집어 조용히 202607 이 됐다.
#
# 뒤에 월·분기·반기가 붙은 연도만 편다. "최근 10년", "설립 30년" 은 기간 연도가
# 아니라 길이여서 펴면 안 된다.
_TWO_DIGIT_YEAR_PERIOD_RE = re.compile(
    r"(?<!\d)(\d{2})\s*년(?=\s*(?:\d{1,2}\s*월|[1-4]\s*분기|상반기|하반기))"
)


def _expand_two_digit_years(text: str) -> str:
    """'26년 5월' 처럼 두 자리로 적은 기간 연도를 네 자리로 펴 준다."""
    return _TWO_DIGIT_YEAR_PERIOD_RE.sub(
        lambda match: f"{int(match.group(1)) + 2000}년", text or ""
    )


def _extract_period_by_rule(question: str) -> tuple[str, str, str]:
    """Return ``(start_ym, end_ym, explicit_yyyymmdd)`` from Korean date text."""
    text = _expand_two_digit_years(question or "")
    business_today = kst_today()
    day = re.search(
        r"(?<!\d)(20\d{2})[-./](\d{1,2})[-./](\d{1,2})(?!\d)|"
        r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-3]\d)(?!\d)|"
        r"(20\d{2})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일",
        text,
    )
    explicit_day = ""
    if day:
        parts = next(
            day.groups()[index : index + 3]
            for index in range(0, 9, 3)
            if day.group(index + 1)
        )
        try:
            explicit_day = f"{parts[0]}{int(parts[1]):02d}{int(parts[2]):02d}"
            datetime.strptime(explicit_day, "%Y%m%d")
        except ValueError:
            explicit_day = ""
        if explicit_day:
            return explicit_day[:6], explicit_day[:6], explicit_day

    inherited_year_range = re.search(
        r"(20\d{2})\s*년\s*(\d{1,2})\s*월?\s*(?:부터|에서|~|～|-)\s*"
        r"(\d{1,2})\s*월(?:\s*까지)?",
        text,
    )
    if inherited_year_range:
        year = inherited_year_range.group(1)
        start_month = int(inherited_year_range.group(2))
        end_month = int(inherited_year_range.group(3))
        if 1 <= start_month <= 12 and 1 <= end_month <= 12:
            return f"{year}{start_month:02d}", f"{year}{end_month:02d}", explicit_day

    half_year_ranges = _extract_half_year_ranges_by_rule(text)
    if half_year_ranges:
        return half_year_ranges[0][0], half_year_ranges[-1][1], explicit_day

    quarter_start, quarter_end = _extract_quarter_period_by_rule(text, business_today)
    if quarter_start:
        return quarter_start, quarter_end, explicit_day

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

    relative_year_months = [
        f"{business_today.year - (0 if word in ('올해', '금년') else 1)}{int(month):02d}"
        for word, month in re.findall(r"(작년|지난해|전년도?|올해|금년)\s*(\d{1,2})\s*월", text)
        if 1 <= int(month) <= 12
    ]
    if relative_year_months:
        return min(relative_year_months), max(relative_year_months), explicit_day

    bare_range = re.search(
        r"(?<![\d,A-Za-z])(\d{1,2})\s*월\s*(?:부터|에서|~|～|-)\s*(\d{1,2})\s*월(?:\s*까지)?",
        text,
    )
    if bare_range:
        start_month, end_month = int(bare_range.group(1)), int(bare_range.group(2))
        if 1 <= start_month <= 12 and 1 <= end_month <= 12:
            end = _recent_ym_for_month(end_month, business_today)
            start_year = int(end[:4]) - (0 if start_month <= end_month else 1)
            return f"{start_year}{start_month:02d}", end, explicit_day
    bare_month = re.search(r"(?<![\d,A-Za-z])(\d{1,2})\s*월", text)
    if bare_month and 1 <= int(bare_month.group(1)) <= 12:
        ym = _recent_ym_for_month(int(bare_month.group(1)), business_today)
        return ym, ym, explicit_day

    if re.search(r"어제|전일", text):
        explicit_day = previous_day_ymd(business_today)
        return explicit_day[:6], explicit_day[:6], explicit_day
    if re.search(r"오늘", text):
        explicit_day = business_today.strftime("%Y%m%d")
        return explicit_day[:6], explicit_day[:6], explicit_day

    now = business_today
    current = f"{now.year}{now.month:02d}"
    half_year = re.search(r"(?:(20\d{2})\s*년\s*)?(상반기|하반기)", text)
    if half_year:
        year = int(half_year.group(1) or now.year)
        return (
            (f"{year}01", f"{year}06", explicit_day)
            if half_year.group(2) == "상반기"
            else (f"{year}07", f"{year}12", explicit_day)
        )
    recent_months = _extract_recent_period_months_by_rule(text)
    if recent_months is not None:
        return _months_back_ym(current, recent_months - 1), current, explicit_day
    year_match = re.search(r"(20\d{2})\s*년", text)
    if year_match:
        year = year_match.group(1)
        return f"{year}01", f"{year}12", explicit_day
    if (
        re.search(r"작년|지난해|전년(?:도)?", text)
        and re.search(r"올해|금년", text)
    ):
        return f"{now.year - 1}01", current, explicit_day
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


def _extract_card_issue_cancel_periods_by_rule(question: str) -> tuple[str, str, str, str]:
    """Extract separate issue and cancellation month ranges from one sentence."""
    text = re.sub(r"\s+", " ", question or "").strip()
    month_spec = r"[0-9월,·~\-\s]+"
    patterns = (
        (
            rf"발급(?:을|한|하고|일)?\s*(?P<year>20\d{{2}}|\d{{2}})\s*년\s*"
            rf"(?P<issued>{month_spec})(?:에|중에)?\s*(?:하고|후|뒤|이후)\s*"
            rf"(?P<cancelled>{month_spec})(?:안에|내에|중에|에)?\s*해지"
        ),
        (
            rf"(?P<year>20\d{{2}}|\d{{2}})\s*년\s*(?P<issued>{month_spec})"
            rf"(?:에|중에)?\s*발급.{0,20}?(?:하고|후|뒤|이후)\s*"
            rf"(?P<cancelled>{month_spec})(?:안에|내에|중에|에)?\s*해지"
        ),
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        year = int(match.group("year"))
        year = year + 2000 if year < 100 else year
        issued = [int(value) for value in re.findall(r"\d{1,2}", match.group("issued"))]
        cancelled = [int(value) for value in re.findall(r"\d{1,2}", match.group("cancelled"))]
        if issued and cancelled and all(1 <= month <= 12 for month in issued + cancelled):
            return (
                f"{year}{min(issued):02d}",
                f"{year}{max(issued):02d}",
                f"{year}{min(cancelled):02d}",
                f"{year}{max(cancelled):02d}",
            )
    return "", "", "", ""


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
    """Return the explicit relative period as months.

    "최근 반년" 은 "최근 6개월" 과 같은 기간 표현이다. 이 함수가 유일한 해석
    지점이어야 한다 — _extract_period_by_rule 이 자기 정규식을 따로 들고 있던
    동안 "반" 을 숫자로 못 읽어 반년이 현재월 1개월로 좁혀졌다.
    """
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


def _extract_merchant_number_by_rule(question: str) -> str:
    match = re.search(r"가맹점\s*(?:번호|ID)\s*(?:[:=]\s*)?[\"']?(\d+)", question or "", re.IGNORECASE)
    return match.group(1) if match else ""


_PRESENTATION_SENTENCE_RE = re.compile(
    r"^(?:결과\s*확인\s*요청\s*:|(?:(?:업무|데이터|운영|간단|정확|분석|DB|리포트|담당자|수치|아래|현업|집계|"
    r"결과|정기|조건|통계|질문|조회|보고|자료|요청|현황)[^.!?\n:]{0,50}"
    r"(?:해줘|필요해|질문이야|요청이야|건이야|부탁해)|DB\s*조회\s*요청)\s*[.!?:])\s*",
    re.IGNORECASE,
)


def _strip_presentation_prefix(question: str) -> str:
    """Remove a leading reporting-style sentence before entity extraction."""
    text = str(question or "").strip()
    while True:
        stripped = _PRESENTATION_SENTENCE_RE.sub("", text, count=1).strip()
        if stripped == text:
            return text
        text = stripped


# 이름 자리 앞에는 시점·기준 표현이 겹쳐 붙는다("현재 기준 가맹점 신용판매 매출금액").
# 한 번만 벗기면 "현재" 뒤의 "기준" 이 이름에 남아 LIKE 필터로 들어갔다.
_NAME_PREFIX_NOISE_RE = re.compile(
    r"^(?:내가|우리|KB국민|기준|현재|오늘|지금|최신|요즘|당월|금월)(?:으로|로|에)?(?:\s+|$)"
)


def _strip_name_prefix_noise(candidate: str) -> str:
    """Peel stacked as-of words off the front of a name candidate."""
    text = str(candidate or "").strip()
    while True:
        stripped = _NAME_PREFIX_NOISE_RE.sub("", text).strip()
        if stripped == text:
            return text
        text = stripped


@lru_cache(maxsize=1)
def _declared_metric_surface_forms() -> tuple[str, ...]:
    """Metric·attribute surface forms the semantic layer declares."""
    forms: set[str] = set()
    for metric in SCHEMA.get("canonical_metrics", []):
        forms.update(
            str(term)
            for term in [metric.get("name"), *(metric.get("synonyms") or [])]
            if term
        )
    for attribute in SCHEMA.get("semantic_attributes", []):
        forms.update(
            str(term)
            for term in [attribute.get("korean_name"), *(attribute.get("aliases") or [])]
            if term
        )
    return tuple(
        form
        for form in sorted(forms)
        if len(re.sub(r"[^0-9A-Za-z가-힣_]", "", form)) >= 4
    )


def _is_declared_metric_fragment(candidate: str) -> bool:
    """이름 자리에 남은 말이 지표 이름의 한 토막인지.

    "오늘 기준 가맹점 신용판매 매출금액" 에서 기간·지표를 걷어내면 '가맹점 신용판매'
    가 남는다. 그것은 가맹점 이름이 아니라 semantic layer 가 선언한 지표 이름의 앞
    토막이다. 그 조각이 이름으로 잡히면 가맹점명이 required 인 계약이 authoritative
    로 잠겨, 이번 달을 들고 있는 tbdaadt01 대신 월 실적(tmdaa5e11)으로 갔다. 실행까지
    가면 가맹점명 LIKE '%가맹점 신용판매%' 가 붙어 0행이 나온다.
    """
    compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", str(candidate or "").lower())
    if len(compact) < 2:
        return False
    return any(
        compact in re.sub(r"[^0-9A-Za-z가-힣_]", "", form.lower())
        for form in _declared_metric_surface_forms()
    )


def _extract_merchant_name_by_rule(question: str) -> str:
    """Extract a merchant/brand name from high-confidence question shapes.

    Besides an explicit ``이름 + 브랜드 가맹점`` shape, this supports a name
    next to a period and sales metric, such as ``도미노 피자 최근 6개월
    매출액``. Generic subjects remain for the LLM/clarification path.
    """
    text = _strip_presentation_prefix(question)
    if _extract_merchant_number_by_rule(text):
        return ""
    recent_closed_name = _extract_recent_closed_merchant_name_by_rule(text)
    if recent_closed_name:
        return recent_closed_name
    text = re.sub(r"20\d{2}\s*년\s*\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?", " ", text)
    text = re.sub(r"(?<!\d)20\d{4}(?:\d{2})?(?!\d)", " ", text)
    text = re.sub(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])\s*(?=기준|월)(?!\d)", " ", text)
    token = r"[0-9A-Za-z가-힣&()._-]+"
    patterns = [
        rf"({token}(?:\s+{token}){{0,4}}?)\s+(?:가맹점\s*주|가맹점주|점주|대표자|점포별(?:의)?|매장별(?:의)?)",
        rf"({token}(?:\s+{token}){{0,4}})\s+브랜드\s+가맹점",
        rf"({token}(?:\s+{token}){{0,4}}?)\s+가맹점",
    ]
    generic = {
        "당사", "해당", "특정", "전체", "모든", "사용중인", "사용 중인",
        "신규", "활성", "기업", "법인", "개인사업자", "관리하고 있는",
        # "2026년 7월 기준 가맹점 대표자로 등록된 관계" 에서 날짜를 지우면
        # "기준 가맹점 대표자" 가 남아 '기준 가맹점' 이 이름으로 잡혔다.
        # 그 이름으로 LIKE '%기준%가맹점%' 필터가 붙어 SQL 전체가 어긋났다.
        "기준", "가맹점", "당월", "금월", "전월",
    }
    invalid_candidate_context = re.compile(
        r"최근|지난|이번|저번|전월|매출|(?:일|주|월|분기|반기|연도|년도|날짜|기간|업종|가맹점|기업|회사|지역|등급)별|법인카드|기업카드|"
        r"폐업|휴폐업|해지|일년|\d+\s*년|\d+\s*(?:개월|달)|반년"
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = re.sub(r"\s+", " ", match.group(1)).strip()
        candidate = re.sub(r"\s+기준$", "", candidate).strip()
        candidate = _strip_name_prefix_noise(candidate)
        compact = re.sub(r"\s+", "", candidate)
        if (
            candidate
            and candidate not in generic
            and len(candidate) <= 80
            and not invalid_candidate_context.search(compact)
            and not _is_grouping_axis_phrase(candidate)
            and not _is_declared_metric_fragment(candidate)
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
        "일별", "주별", "월별", "분기별", "반기별", "연도별", "년도별", "날짜별",
        "기간별", "기업별", "회사별", "지역별", "등급별",
        # "2026년 상반기 매출 상위 10개 가맹점" 에서 연도를 지우면 '상반기' 가
        # 남아 LIKE '%상반기%' 가 붙고 0행이 나왔다. 기간 어구는 이름이 아니다.
        "상반기", "하반기", "분기", "반기", "올해", "작년", "전년", "금년",
        "당월", "금월", "전월", "기준",
    }
    presentation_prefix = re.compile(
        r"(?:보고|보고용|업무|확인|질문|요청|조회|분석|정리|알려|보여|뽑아|추출|"
        r"해주세요|해줘|부탁)(?:\s|[.!?]|$)",
        re.IGNORECASE,
    )
    for pattern in fallback_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = re.sub(r"\s+", " ", match.group("name")).strip()
        candidate = _strip_name_prefix_noise(candidate)
        candidate = re.sub(r"(?:의|에서)$", "", candidate).strip()
        compact = re.sub(r"\s+", "", candidate)
        if (
            candidate
            and len(candidate) <= 80
            and compact not in generic_compact
            and not invalid_candidate_context.search(compact)
            and not _is_grouping_axis_phrase(candidate)
            and not presentation_prefix.search(candidate)
            and not _is_declared_metric_fragment(candidate)
            and not re.search(r"[.!?]$", candidate)
        ):
            return candidate
    return ""


# v2: "회원자격별로 현재 기준 통합한도금액을 보여줘" 에서 기업명 파라미터가
# '회원자격별로' 로 채워져, corporate_limit_status_at_month 가 이름 필터
# LIKE '%회원자격별로%' 로 실행되고 0행이 나왔다. "X별/X별로/X 기준으로" 는
# 그룹핑 축을 지목하는 말이지 어떤 기업·가맹점의 이름이 아니다.
_GROUPING_AXIS_PHRASE_RE = re.compile(r"(?:별로|별|기준(?:으로)?|단위(?:로)?)$")


def _is_grouping_axis_phrase(candidate: str) -> bool:
    """Return whether a name candidate is really a group-by axis, not an entity."""
    return bool(_GROUPING_AXIS_PHRASE_RE.search(re.sub(r"\s+", "", candidate or "")))


def _extract_company_name_by_rule(question: str) -> str:
    """Extract a named corporate customer from a timed usage or limit question."""
    text = _strip_presentation_prefix(question)
    if (
        not re.search(
            r"이용\s*(?:금액|액|실적)|사용\s*(?:금액|액)|카드\s*이용|"
            r"총\s*한도|잔여\s*한도|가용\s*한도|한도(?:\s*(?:금액|현황|소진율|사용률))?",
            text,
        )
        or not _has_time_expression(text)
    ):
        return ""

    token = r"[0-9A-Za-z가-힣&()._-]+"
    time_phrase = (
        r"현재(?:\s*기준)?|작년|지난해|전년(?:도)?|올해|금년|"
        r"최근|지난\s*\d+\s*(?:개월|달|년)|이번\s*(?:달|월|년)|"
        r"20\d{2}\s*년"
    )
    match = re.search(
        rf"^\s*(?P<name>{token}(?:\s+{token}){{0,4}}?)\s*(?:의|에서)?\s*(?={time_phrase})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""

    candidate = re.sub(r"\s+", " ", match.group("name")).strip()
    candidate = re.sub(r"(?:의|에서)$", "", candidate).strip()
    generic = {
        "당사", "해당", "특정", "전체", "모든", "기업", "법인", "회사",
        "기업회원", "법인회원", "기업카드", "법인카드", "카드",
    }
    if _is_grouping_axis_phrase(candidate):
        return ""
    return candidate if candidate and candidate not in generic and len(candidate) <= 80 else ""


def _extract_card_product_name_by_rule(question: str) -> str:
    """Extract a card product name from product-specific portfolio questions."""
    text = re.sub(r"\s+", " ", _strip_presentation_prefix(question)).strip()
    token = r"[0-9A-Za-z가-힣&()._-]+"
    patterns = (
        (
            rf"^(?:현재\s*기준|현재|최신\s*기준)?\s*"
            rf"(?P<name>{token}(?:\s+{token}){{0,5}}?)\s+"
            rf"(?:기업|법인)카드\s+유효\s*카드\s*(?:수|개수)"
        ),
        (
            rf"(?:(?:20\d{{2}}\s*년\s*)?(?:상반기|하반기))(?:에|동안|중)?\s+"
            rf"(?P<name>{token}(?:\s+{token}){{0,5}}?)\s+체크카드"
        ),
    )
    generic = {
        "기업", "법인", "기업카드", "법인카드", "체크", "체크카드",
        "상품", "카드상품", "해당", "특정", "전체", "모든",
    }
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        candidate = match.group("name").strip()
        if candidate and candidate not in generic and len(candidate) <= 80:
            return candidate
    return ""


def _is_named_merchant_sales_question(question: str) -> bool:
    """Return whether a question unambiguously asks named merchant sales."""
    return bool(
        _has_time_expression(question)
        and re.search(r"매출(?:액|금액)?|매출\s*추이", question or "")
        and (_extract_merchant_number_by_rule(question) or _extract_merchant_name_by_rule(question))
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


def _extract_params_by_rule(
    question: str,
    param_specs: list[dict],
    table_names: list[str] | set[str] | None = None,
) -> dict:
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
    half_year_ranges = _extract_half_year_ranges_by_rule(question)
    comparison_period_params = {}
    if len(half_year_ranges) >= 2:
        comparison_period_params = {
            "기준기간_시작": half_year_ranges[0][0],
            "기준기간_종료": half_year_ranges[0][1],
            "대상기간_시작": half_year_ranges[1][0],
            "대상기간_종료": half_year_ranges[1][1],
        }
        params.update({name: value for name, value in comparison_period_params.items() if name in names})
    start_ym, end_ym, explicit_day = _extract_period_by_rule(question)
    previous_day_source = next(
        (
            table_name
            for table_name in (table_names or [])
            if (accumulation_policy_for(table_name) or {}).get("cadence") == "previous_day"
        ),
        "",
    )
    for name in names:
        if not start_ym and not explicit_day:
            break
        if name in comparison_period_params:
            continue
        if name in {"기준년월일", "카드만료기준일"}:
            use_today = name == "기준년월일" and re.search(
                r"오늘|현재(?:\s*(?:시점|기준))?", question or ""
            )
            if name == "기준년월일" and previous_day_source and use_today:
                params[name] = _latest_available_day(previous_day_source)
            else:
                params[name] = explicit_day or (kst_today().strftime("%Y%m%d") if use_today else _month_end(end_ym))
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

    row_request = parse_row_request(question)
    if "limit" in names and row_request.limit is not None:
        params["limit"] = min(row_request.limit, MAX_QUERY_ROW_LIMIT)

    amount_specs = {
        "월평균금액": ("월평균", "월 평균", "평균 이용금액", "평균금액"),
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
    if "가맹점번호" in names:
        merchant_number = _extract_merchant_number_by_rule(question)
        if merchant_number:
            params["가맹점번호"] = merchant_number
    if "가맹점명" in names:
        merchant_name = _extract_merchant_name_by_rule(question)
        if merchant_name:
            params["가맹점명"] = merchant_name
    if "기업명" in names:
        company_name = _extract_company_name_by_rule(question)
        if company_name:
            params["기업명"] = company_name
    if "상품명" in names:
        product_name = _extract_card_product_name_by_rule(question)
        if product_name:
            params["상품명"] = product_name
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
    issue_start, issue_end, cancel_start, cancel_end = _extract_card_issue_cancel_periods_by_rule(question)
    for name, value in {
        "발급기간_시작": issue_start,
        "발급기간_종료": issue_end,
        "해지기간_시작": cancel_start,
        "해지기간_종료": cancel_end,
    }.items():
        if name in names and value:
            params[name] = value
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
                normalized[name] = min(max(number, 1), MAX_QUERY_ROW_LIMIT) if name == "limit" else number
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
    if _has_previous_month_lookup(question):
        return _previous_ym(), "저번달/지난달/전월"
    lookup_text = re.sub(_RECENT_CLOSURE_PERIOD_PATTERN, " ", question or "")
    lookup_text = _without_period_prefixed_columns(lookup_text, "최근")
    if re.search(r"최근|이번\s*달|이번\s*월|현재\s*월", lookup_text):
        return _current_ym(), "최근/이번달/최근 기준"
    return "", ""


def _validate_recent_month_semantics(question: str, sql: str) -> list[str]:
    target_ym, label = _relative_month_target(question)
    if not target_ym:
        return []
    normalized_sql = (sql or "").replace('"', "")
    if target_ym in normalized_sql:
        return []
    # 마스터는 grain 에 시간 축이 없다(tbdaadt01 은 "가맹점번호 1건"). 이번 달의 상태는
    # 마스터의 최신 적재분 그 자체여서, 기준년월 조건으로 바꿔 오라고 요구하면 마스터가
    # 답할 수 있는 유일한 형태를 반려하고 재시도만 돌린다. 과거 달은 적재 정책이 월
    # 스냅샷으로 돌려 놓으므로 여기까지 마스터로 오지 않는다.
    if target_ym == _current_ym() and _reads_only_master_tables(sql):
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
    # 마스터에 월 조건을 얹으면 달이 바뀐 직후(최신 적재분이 지난달인 시점) 0행이 된다.
    if target_ym == _current_ym() and _reads_only_master_tables(sql):
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


_TBDAADT01_TABLE = "tbdaadt01"
_TBDAADT01_TIME_COLUMN = "실적기준년월일"
_SQL_SOURCE_KEYWORDS = {
    "from", "where", "join", "left", "right", "inner", "full", "cross", "on",
    "using", "group", "order", "limit", "union", "having", "qualify", "window",
}
# 별칭 자리에서 다음 절의 키워드를 삼키면 그 뒤 관계를 아예 못 본다.
# ``FROM ms JOIN tmdaa5e11 p ON ...`` 은 별칭 없는 ms 다음의 JOIN 을 ms 의 별칭으로
# 먹어 치우고 스캔이 그 뒤에서 재개돼, tmdaa5e11 이 바인딩 목록에서 사라졌다.
# 그러면 적재 축 검사가 붙일 별칭을 못 찾아 "기간 조건 없음" 으로 정답을 반려하고,
# 테이블 치환도 그 관계를 건너뛴다.
_SQL_ALIAS_STOPWORDS = "|".join(sorted(_SQL_SOURCE_KEYWORDS))
_SQL_RELATION_RE = re.compile(
    r'\b(?P<kind>FROM|JOIN)\s+'
    r'(?:(?:"(?P<quoted_schema>[A-Za-z_][A-Za-z0-9_]*)"|'
    r'(?P<plain_schema>[A-Za-z_][A-Za-z0-9_]*))\s*\.\s*)?'
    r'(?:"(?P<quoted_table>[A-Za-z_][A-Za-z0-9_]*)"|'
    r'(?P<plain_table>[A-Za-z_][A-Za-z0-9_]*))'
    rf'(?:\s+(?:AS\s+)?(?!(?:{_SQL_ALIAS_STOPWORDS})\b)'
    r'(?:"(?P<quoted_alias>[A-Za-z_][A-Za-z0-9_]*)"|'
    r'(?P<plain_alias>[A-Za-z_][A-Za-z0-9_]*)))?',
    re.IGNORECASE,
)


def _sql_table_bindings(sql: str) -> list[dict[str, object]]:
    """Return physical table/alias bindings without treating CTEs as sources."""
    used_tables = _extract_schema_tables(sql)
    bindings: list[dict[str, object]] = []
    for match in _SQL_RELATION_RE.finditer(sql or ""):
        table = str(match.group("quoted_table") or match.group("plain_table") or "").lower()
        if table not in used_tables:
            continue
        raw_alias = str(match.group("quoted_alias") or match.group("plain_alias") or "")
        explicit_alias = bool(raw_alias) and raw_alias.lower() not in _SQL_SOURCE_KEYWORDS
        alias = raw_alias if explicit_alias else table
        table_group = "quoted_table" if match.group("quoted_table") is not None else "plain_table"
        bindings.append(
            {
                "table": table,
                "alias": alias.lower(),
                "explicit_alias": explicit_alias,
                "table_span": match.span(table_group),
            }
        )
    return bindings


def _table_aliases(sql: str, table_name: str) -> list[tuple[str, bool]]:
    target = str(table_name or "").lower()
    return list(
        dict.fromkeys(
            (str(binding["alias"]), bool(binding["explicit_alias"]))
            for binding in _sql_table_bindings(sql)
            if binding["table"] == target
        )
    )


def _qualified_column(alias: str, column: str) -> str:
    return (
        rf'"?{re.escape(alias)}"?\s*\.\s*"?{re.escape(column)}"?'
        r'(?![0-9A-Za-z_가-힣])'
    )


def _has_alias_column_equality(sql: str, left_alias: str, right_alias: str, column: str) -> bool:
    left = _qualified_column(left_alias, column)
    right = _qualified_column(right_alias, column)
    return bool(
        re.search(
            rf'(?:{left}\s*=\s*{right}|{right}\s*=\s*{left})',
            sql or "",
            re.IGNORECASE,
        )
    )


def _replace_physical_table(sql: str, source_table: str, target_table: str) -> str:
    """Replace only physical relation names and their unaliased qualifiers."""
    source = source_table.lower()
    spans = [
        binding["table_span"]
        for binding in _sql_table_bindings(sql)
        if binding["table"] == source
    ]
    rewritten = sql
    for start, end in sorted(spans, reverse=True):
        rewritten = rewritten[:start] + target_table + rewritten[end:]
    # An unaliased source may qualify columns with its physical table name.
    rewritten = re.sub(
        rf'(?<![A-Za-z0-9_])"?{re.escape(source_table)}"?(?=\s*\.)',
        target_table,
        rewritten,
        flags=re.IGNORECASE,
    )
    return rewritten


# 마스터 속성 조회가 아니라 집계임을 드러내는 표현. 관측된 실패 질문에서만 뽑았다.
_AGGREGATION_REQUEST_RE = re.compile(
    r"몇\s*[곳개건명군데]|개수|건수|좌수|비중|비율|평균|합계|총합|순위|"
    r"상위\s*\d|하위\s*\d|분포|추이|비교|많은\s*순|적은\s*순|수를\s*(?:알려|보여|집계)"
)


def _merchant_master_attribute_question(question: str) -> bool:
    """월 스냅샷으로 바꾸면 사용자가 지목한 테이블만 사라지는 질문인지.

    tmdaa5d01 은 "그 달에 어떤 가맹점이 어떤 상태였나" 를 세기 위한 월 스냅샷이다.
    주소·기본정보처럼 마스터 한 곳만 가리키는 semantic attribute 를 물었을 때는
    집계할 지표가 없어 스냅샷으로 돌려도 답이 달라지지 않는다. 대신 tbdaadt01 이
    후보에서 통째로 빠져서 "요청한 달이 마스터에 있느냐" 는 것조차 답할 수 없다.
    """
    candidates = semantic_attribute_candidates(SCHEMA, question, max_count=6)
    if any(_master_only_attribute(attribute) for attribute in candidates):
        return True
    # 현재·과거 원천을 나눠 선언한 속성이 이 질문에서 마스터를 골랐다면, 월 원천에는
    # 아직 그 달 행이 없다는 뜻이다. 집계 질문이어도 스냅샷으로 돌릴 곳이 없다.
    if any(
        isinstance(attribute.get("source_selection"), dict)
        and {
            str(mapping.get("table") or "").rsplit(".", 1)[-1].lower()
            for mapping in _attribute_source_mappings(question, attribute)
            if mapping.get("table")
        }
        == {_TBDAADT01_TABLE}
        for attribute in candidates
    ):
        return True
    # 속성은 별칭으로만 매칭된다. "우편번호" 처럼 사용자가 컬럼명을 그대로 부르면
    # 별칭에 없어서 안 걸리고, 남은 단서 하나("가맹점주")가 스냅샷을 끌어온다.
    # 별칭을 하나씩 채우는 대신 속성이 이미 선언한 컬럼으로도 판정한다.
    # 집계 질문은 그 달을 세는 질문이므로 스냅샷이 맞다. 컬럼이 집계 기준축으로만
    # 쓰인 경우("연체 발생한 기업을 가맹점 업종코드별로 몇 곳인지")를 지표명만으로는
    # 못 거른다. 세는 질문임을 드러내는 표현도 함께 본다.
    if any(
        _phrase_in_question(question, term)
        for metric in SCHEMA.get("canonical_metrics", [])
        for term in [metric.get("name", ""), *metric.get("synonyms", [])]
    ):
        return False
    if _AGGREGATION_REQUEST_RE.search(question or ""):
        return False
    return bool(_master_only_attribute_columns() & set(_v2_named_columns(question, _schema_column_names())))


def _master_only_attribute(attribute: dict) -> bool:
    """이 속성이 가맹점 마스터 한 곳만 가리키는지."""
    return {
        str(mapping.get("table") or "").rsplit(".", 1)[-1].lower()
        for mapping in attribute.get("source_mappings", [])
        if mapping.get("table")
    } == {_TBDAADT01_TABLE}


@lru_cache(maxsize=1)
def _master_only_attribute_columns() -> frozenset[str]:
    """마스터 한 곳만 가리키는 속성이 선언한 컬럼.

    가맹점번호처럼 어느 질문에나 섞이는 식별자는 뺀다. 그것만으로 마스터 질문이라
    단정하면 월 지표 질문까지 끌어온다.
    """
    columns: set[str] = set()
    for attribute in SCHEMA.get("semantic_attributes", []):
        if not _master_only_attribute(attribute):
            continue
        for mapping in attribute.get("source_mappings", []):
            columns.update(str(name) for name in mapping.get("columns", []) if name)
            if mapping.get("column"):
                columns.add(str(mapping["column"]))
    return frozenset(columns - {"가맹점번호", "기업고객식별자", "대표고객식별자"})


# "이번주 실적 기준" · "이번달 실적 기준" 은 적재 축(실적기준년월일)의 일자를 좁혀
# 달라는 말이다. 주는 월 스냅샷에 없는 단위이고 이번 달은 아직 월 스냅샷에 적재되지
# 않았으므로, 둘 다 마스터에서 일자로 자른다(사용자 제공).
_THIS_WEEK_RE = re.compile(r"이번\s*주|금주")
# 금월이용합계금액 같은 컬럼 이름에 걸리지 않도록 "금월" 은 넣지 않는다.
_THIS_MONTH_RE = re.compile(r"이번\s*(?:달|월)|당월")
_LOAD_AXIS_RE = re.compile(r"실적\s*기준|실적기준년월일")


def _merchant_load_axis_scope(question: str) -> str:
    """Whether the question asks to bound the merchant master by its load axis."""
    text = question or ""
    if _THIS_WEEK_RE.search(text):
        return "week"
    # 이번 달은 "그 달의 상태" 를 묻는 말로도 쓰인다(가맹점 신용판매 매출금액).
    # 적재 축을 함께 말했을 때만 일자로 자른다.
    if _LOAD_AXIS_RE.search(text) and _THIS_MONTH_RE.search(text):
        return "month"
    return ""


def _recent_merchant_time_route(question: str) -> tuple[str, str, str]:
    """Return ``(daily|monthly|master, start, end)`` for a recent merchant source."""
    start_ym, end_ym, explicit_day = _extract_period_by_rule(question)
    if explicit_day:
        recent_start, recent_end = recent_window_ymd(today=kst_today())
        if recent_start <= explicit_day <= recent_end:
            return "daily", explicit_day, explicit_day
        bounded = "master" if _merchant_master_attribute_question(question) else "monthly"
        return bounded, explicit_day[:6], explicit_day[:6]

    load_axis_scope = _merchant_load_axis_scope(question)
    if load_axis_scope:
        return ("daily", *load_axis_window_ymd(load_axis_scope, today=kst_today()))

    explicit_month = bool(
        _has_historical_period_expression(question)
        or re.search(r"이번\s*(?:달|월)", question or "")
    )
    if explicit_month and start_ym and end_ym:
        bounded = "master" if _merchant_master_attribute_question(question) else "monthly"
        return bounded, start_ym, end_ym
    return "", "", ""


# Kept for compatibility with focused helpers/tests that use the old private name.
_tbdaadt01_time_route = _recent_merchant_time_route


def _routed_time_predicate(column: str, cadence: str, start: str, end: str) -> str:
    routed_column = column
    if cadence == "monthly":
        routed_column = re.sub(
            rf'"?{re.escape(_TBDAADT01_TIME_COLUMN)}"?',
            '"기준년월"',
            routed_column,
            flags=re.IGNORECASE,
        )
    if start == end:
        return f"{routed_column} = '{start}'"
    return f"{routed_column} BETWEEN '{start}' AND '{end}'"


def _rewrite_tbdaadt01_time_predicates(
    sql: str,
    cadence: str,
    start: str,
    end: str,
    aliases: list[tuple[str, bool]] | None = None,
) -> tuple[str, int]:
    """Replace time predicates owned by the merchant source aliases only."""
    dynamic_start = (
        r"DATE_FORMAT\s*\(\s*DATE_ADD\s*\(\s*'day'\s*,\s*-9\s*,\s*"
        r"CURRENT_TIMESTAMP\s+AT\s+TIME\s+ZONE\s+'Asia/Seoul'\s*\)\s*,\s*'%Y%m%d'\s*\)"
    )
    dynamic_end = (
        r"DATE_FORMAT\s*\(\s*CURRENT_TIMESTAMP\s+AT\s+TIME\s+ZONE\s+'Asia/Seoul'"
        r"\s*,\s*'%Y%m%d'\s*\)"
    )
    rewritten = sql or ""
    replacements = 0
    source_aliases = aliases or _table_aliases(sql, _TBDAADT01_TABLE)
    for alias, explicit_alias in source_aliases:
        qualifier = _qualified_column(alias, _TBDAADT01_TIME_COLUMN)
        if not explicit_alias:
            qualifier = (
                rf'(?:(?:"?{re.escape(alias)}"?\s*\.\s*)?'
                rf'"?{re.escape(_TBDAADT01_TIME_COLUMN)}"?)'
            )
        column = rf'(?P<column>{qualifier})'
        patterns = (
            rf"{column}\s+BETWEEN\s+{dynamic_start}\s+AND\s+{dynamic_end}",
            rf"(?:SUBSTR|SUBSTRING)\s*\(\s*{column}\s*,\s*1\s*,\s*6\s*\)"
            r"\s+BETWEEN\s+'20\d{4}'\s+AND\s+'20\d{4}'",
            rf"(?:SUBSTR|SUBSTRING)\s*\(\s*{column}\s*,\s*1\s*,\s*6\s*\)"
            r"\s*=\s*'20\d{4}'",
            rf"{column}\s+BETWEEN\s+'20\d{{4,6}}'\s+AND\s+'20\d{{4,6}}'",
            rf"{column}\s*=\s*'20\d{{4,6}}'",
            rf"{column}\s*>=\s*'20\d{{4,6}}'\s+AND\s+{qualifier}"
            r"\s*<=\s*'20\d{4,6}'",
        )
        for pattern in patterns:
            rewritten, count = re.subn(
                pattern,
                lambda match: _routed_time_predicate(
                    match.group("column"), cadence, start, end
                ),
                rewritten,
                flags=re.IGNORECASE | re.DOTALL,
            )
            replacements += count
    return rewritten, replacements


def _shallowest_table_aliases(sql: str, table_name: str) -> list[tuple[str, bool]]:
    """Aliases the table binds in the outermost scope that reads it.

    같은 테이블이 서브쿼리에도 묶여 있으면(``= (SELECT MAX("실적기준년월일") FROM t x)``)
    그 별칭까지 바깥 WHERE 에 주입돼, 그 스코프에 없는 ``x."실적기준년월일" = '...'`` 를
    참조하는 SQL 이 나왔다. 주입 지점은 어차피 바깥 스코프 하나다.
    """
    bindings = [
        binding
        for binding in _sql_table_bindings(sql)
        if binding["table"] == str(table_name or "").rsplit(".", 1)[-1].lower()
    ]
    depths = [
        (lambda prefix: prefix.count("(") - prefix.count(")"))(
            re.sub(r"'[^']*'", "", (sql or "")[: binding["table_span"][0]])
        )
        for binding in bindings
    ]
    if not bindings:
        return []
    shallowest = min(depths)
    return [
        (str(binding["alias"]), bool(binding["explicit_alias"]))
        for binding, depth in zip(bindings, depths)
        if depth == shallowest
    ]


def _inject_routed_time_predicate(
    sql: str,
    predicate: str,
    table_name: str,
    alias: str = "",
) -> str:
    """Add a source filter to the SELECT scope that owns the routed source."""
    source = re.search(
        rf'\b(?:FROM|JOIN)\s+(?:(?:"?[A-Za-z_]\w*"?)\s*\.\s*)?"?{re.escape(table_name)}"?'
        r'(?:\s+(?:AS\s+)?(?!(?:WHERE|JOIN|LEFT|RIGHT|INNER|FULL|CROSS|ON|GROUP|ORDER|'
        r'HAVING|LIMIT|UNION)\b)"?[A-Za-z_]\w*"?)?',
        sql or "",
        re.IGNORECASE,
    )
    if not source:
        return sql
    tail = sql[source.end():]
    where = re.search(r"\bWHERE\b", tail, re.IGNORECASE)
    boundary = re.search(r"\b(?:GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|UNION)\b", tail, re.IGNORECASE)
    if where and (not boundary or where.start() < boundary.start()):
        position = source.end() + where.end()
        return f"{sql[:position]} {predicate} AND{sql[position:]}"
    if boundary:
        position = source.end() + boundary.start()
        return f"{sql[:position]} WHERE {predicate} {sql[position:]}"
    suffix = ";" if (sql or "").rstrip().endswith(";") else ""
    base = (sql or "").rstrip().removesuffix(";").rstrip()
    return f"{base} WHERE {predicate}{suffix}"


def _ensure_archive_month_joins(sql: str, archive_aliases: list[str]) -> str:
    """Pair monthly merchant snapshots with monthly facts at the same month."""
    monthly_fact_tables = {"tmdaa5e11", "tbdaabt30"}
    fact_aliases = [
        str(binding["alias"])
        for binding in _sql_table_bindings(sql)
        if binding["table"] in monthly_fact_tables
    ]
    rewritten = sql
    for archive_alias in archive_aliases:
        for fact_alias in fact_aliases:
            merchant_pair = (
                rf'(?:{_qualified_column(archive_alias, "가맹점번호")}\s*=\s*'
                rf'{_qualified_column(fact_alias, "가맹점번호")}|'
                rf'{_qualified_column(fact_alias, "가맹점번호")}\s*=\s*'
                rf'{_qualified_column(archive_alias, "가맹점번호")})'
            )
            if not re.search(merchant_pair, rewritten, re.IGNORECASE):
                continue
            if _has_alias_column_equality(rewritten, archive_alias, fact_alias, "기준년월"):
                continue
            match = re.search(merchant_pair, rewritten, re.IGNORECASE)
            if match:
                rewritten = (
                    rewritten[: match.end()]
                    + f' AND {archive_alias}."기준년월" = {fact_alias}."기준년월"'
                    + rewritten[match.end() :]
                )
    return rewritten


def _restore_master_from_archive(sql: str, archive_table: str) -> str:
    """마스터 속성 질문에 월 스냅샷으로 생성된 SQL 을 마스터로 되돌린다.

    테이블 선택은 tbdaadt01 로 고정했는데 SQL 생성은 프롬프트의 다른 맥락(검증된
    질의·조인 그래프)에서 tmdaa5d01 을 그대로 베껴 왔다. 둘이 어긋나면 스냅샷에
    없는 "기준년월일" 이 붙어 스키마 검증에서 끊긴다.

    아카이브가 월 팩트와 기준년월로 묶여 있으면 되돌릴 때 그 조인이 깨지므로
    그때는 손대지 않는다. 마스터에는 기준년월이 없다.
    """
    aliases = _table_aliases(sql, archive_table)
    if not aliases:
        return sql
    other_aliases = [
        str(binding["alias"])
        for binding in _sql_table_bindings(sql)
        if binding["table"] != archive_table
    ]
    for alias, _ in aliases:
        if any(
            _has_alias_column_equality(sql, alias, other, "기준년월")
            for other in other_aliases
        ):
            return sql

    restored = _replace_physical_table(sql, archive_table, _TBDAADT01_TABLE)
    for alias, explicit_alias in aliases:
        qualifier = (
            rf'"?{re.escape(alias)}"?\s*\.\s*'
            if explicit_alias
            else rf'(?:"?{re.escape(alias)}"?\s*\.\s*)?'
        )
        restored = re.sub(
            rf'(?P<qualifier>{qualifier})"?기준년월일?"?(?![0-9A-Za-z_가-힣])',
            lambda match: f'{match.group("qualifier")}"{_TBDAADT01_TIME_COLUMN}"',
            restored,
        )
    return restored


def _master_attribute_alternate_sources(question: str) -> list[str]:
    """이 질문에 걸린 속성이 마스터 대신 가리킬 수 있는 같은 엔티티의 스냅샷.

    "한신포차 가맹점주 가맹점 도로명 주소" 는 주소가 마스터에만 있어 마스터 질문으로
    판정되는데, 함께 걸린 '가맹점 주대표자' 속성이 tbdaaus01·tmdaaus01·tmdaa5d01 을
    같은 대표고객식별자의 원천으로 선언해 둔다. 그 목록이 프롬프트에 그대로 들어가서
    SQL 생성이 마스터 대신 일별요약을 베껴 왔고, 주소 컬럼이 없는 테이블로 답이 나갔다.
    """
    candidates = semantic_attribute_candidates(SCHEMA, question, max_count=6)
    if not any(_master_only_attribute(attribute) for attribute in candidates):
        return []
    alternates: set[str] = set()
    for attribute in candidates:
        tables = {
            str(mapping.get("table") or "").rsplit(".", 1)[-1].lower()
            for mapping in attribute.get("source_mappings", [])
            if mapping.get("table")
        }
        if _TBDAADT01_TABLE in tables:
            alternates.update(tables - {_TBDAADT01_TABLE})
    return sorted(alternates)


def _master_restore_missing_columns(sql: str, snapshot_table: str) -> list[str]:
    """마스터에 없는데 이 SQL 이 읽는 스냅샷 전용 컬럼."""
    _, schema_columns = _schema_table_index(SCHEMA)
    snapshot_only = schema_columns.get(snapshot_table, set()) - schema_columns.get(
        _TBDAADT01_TABLE, set()
    )
    # 시간 축은 _restore_master_from_archive 가 마스터의 축으로 바꿔 준다.
    snapshot_only -= {"기준년월", "기준년월일"}
    return sorted(
        column
        for column in snapshot_only
        if re.search(rf'"{re.escape(column)}"', sql or "", re.IGNORECASE)
    )


def _restore_master_attribute_sources(question: str, sql: str) -> str:
    """마스터에만 있는 속성을 물었는데 스냅샷으로 생성된 SQL 을 마스터로 되돌린다."""
    if _TBDAADT01_TABLE in _extract_schema_tables(sql):
        return sql
    if not _merchant_master_attribute_question(question):
        return sql
    # 마스터로 되돌리면 스냅샷의 시간 축은 실적기준년월일이 된다. 그 값을 마스터의
    # 적재일로 다시 쓰는 것은 시간 라우팅이 하는 일이라, 라우팅이 없는데 축에 값이
    # 박혀 있으면 되돌린 뒤 "실적기준년월일 = '202601'" 같은 조건이 남는다.
    cadence, _, _ = _recent_merchant_time_route(question)
    restored = sql
    for snapshot in _master_attribute_alternate_sources(question):
        if snapshot not in _extract_schema_tables(restored):
            continue
        axis = str((accumulation_policy_for(snapshot) or {}).get("query_time_dimension") or "")
        if not cadence and axis and _table_axis_literal_values(restored, snapshot, axis):
            continue
        # 스냅샷에만 있는 컬럼을 읽고 있으면 되돌릴 곳이 없다. 그대로 두고 검증이
        # 잡게 한다 — 없는 컬럼으로 바꾸면 빈 결과가 컬럼 오류로 바뀐다.
        if _master_restore_missing_columns(restored, snapshot):
            continue
        restored = _restore_master_from_archive(restored, snapshot)
    return restored


def _apply_tbdaadt01_historical_source(question: str, sql: str) -> str:
    """Route bounded merchant-master periods to their declared physical source."""
    cadence, start, end = _tbdaadt01_time_route(question)
    if not cadence:
        return sql
    policy = accumulation_policy_for(_TBDAADT01_TABLE) or {}
    historical_source = policy.get("historical_source")
    if cadence == "master" and isinstance(historical_source, dict):
        sql = _restore_master_from_archive(
            sql, str(historical_source.get("table") or "").lower()
        )
    if _TBDAADT01_TABLE not in _extract_schema_tables(sql):
        return sql
    if cadence == "master":
        # 마스터가 요청한 달을 들고 있을 리 없다. 최신 가용일 1건으로 좁혀 가맹점당
        # 보관일수만큼 행이 불어나는 것을 막고, 요청한 달이 없다는 사실은
        # _implicit_time_basis_note() 가 답변에서 밝힌다.
        start = end = _latest_available_day(_TBDAADT01_TABLE)

    if cadence == "monthly" and not isinstance(historical_source, dict):
        return sql

    source_aliases = _table_aliases(sql, _TBDAADT01_TABLE)
    routed, replacements = _rewrite_tbdaadt01_time_predicates(
        sql, cadence, start, end, source_aliases
    )
    if cadence == "monthly":
        table_name = str(historical_source.get("table") or "")
        time_column = str(historical_source.get("query_time_dimension") or "")
        if not table_name or not time_column:
            return sql
        routed = _replace_physical_table(routed, _TBDAADT01_TABLE, table_name)
        archive_aliases = [alias for alias, _ in source_aliases]
        routed = _ensure_archive_month_joins(routed, archive_aliases)
    else:
        table_name = _TBDAADT01_TABLE
        time_column = _TBDAADT01_TIME_COLUMN

    if replacements:
        return routed
    # A generated composition may put the merchant source after JOIN and omit
    # its own period. Inject the archive predicate in the same SELECT scope.
    for alias, explicit_alias in _shallowest_table_aliases(routed, table_name):
        qualifier = f'{alias}.' if explicit_alias else ""
        predicate = _routed_time_predicate(
            f'{qualifier}"{time_column}"', cadence, start, end
        )
        routed = _inject_routed_time_predicate(routed, predicate, table_name, alias)
    return routed


# 짝(tbd↔tmd)은 적재 정책의 historical_source 한 곳에만 적는다. 여기에 표를 또
# 두면 새 짝을 등록할 때 한쪽만 고쳐 두 방향이 어긋난다.
_PREVIOUS_DAY_ARCHIVE_TABLES = {
    table: str(policy["historical_source"]["table"])
    for table, policy in TABLE_ACCUMULATION_POLICIES.items()
    if policy.get("cadence") == "previous_day"
    and isinstance(policy.get("historical_source"), dict)
}


def _previous_day_archive_route(question: str) -> tuple[str, str]:
    """Return the monthly bounds for an explicitly historical request."""
    if not _has_historical_period_expression(question):
        return "", ""
    if re.search(r"오늘|전일|어제|최신|최근\s*기준", question or ""):
        return "", ""
    start_ym, end_ym, explicit_day = _extract_period_by_rule(question)
    if explicit_day == previous_day_ymd():
        return "", ""
    return (start_ym, end_ym) if start_ym and end_ym else ("", "")


def _rewrite_previous_day_archive_select(
    select_sql: str,
    source_table: str,
    target_table: str,
    start_ym: str,
    end_ym: str,
    preserve_pinned_current: bool = True,
) -> str:
    """Route one SELECT scope from a D-1 source to its monthly archive."""
    if source_table not in _extract_schema_tables(select_sql):
        return select_sql
    aliases = _table_aliases(select_sql, source_table)
    if not aliases:
        return select_sql
    # 조회기준일을 다른 스코프에서 받아 오는 조건은 그 참조를 지우는 순간
    # 깨진다. 스코프가 몇 개든 그대로 둔다.
    if re.search(
        r'"?기준년월일"?\s*=\s*"?[A-Za-z_]\w*"?\s*\.\s*"?현재기준일"?',
        select_sql,
        re.IGNORECASE,
    ):
        return select_sql
    # A mixed query may use the live table once for its current population and
    # again for historical measures. Preserve the exact D-1 population scope.
    if preserve_pinned_current and _has_exact_table_axis_value(
        select_sql,
        source_table,
        "기준년월일",
        previous_day_ymd(),
    ):
        return select_sql

    rewritten = select_sql
    touched = False
    # 함수 인자 안에 함수가 한 번 더 들어가는 D-1 표현
    # (DATE_FORMAT(DATE_ADD('day', -1, ...), '%Y%m%d'))까지 통째로 잡는다.
    # [^)]* 로 끊으면 안쪽 DATE_ADD 의 닫는 괄호에서 멈춰 ", '%Y%m%d')" 가
    # 남은 깨진 SQL 이 된다. 문자열 안의 괄호는 '...' 갈래가 먼저 삼킨다.
    call_args = r"(?:[^()']|'[^']*'|\((?:[^()']|'[^']*')*\))*"
    operand = (
        rf"(?:(?:CONCAT|DATE_FORMAT)\s*\({call_args}\)|"
        r"'[^']*'|\{[^}]+\}|"
        r"(?:(?:\"?[A-Za-z_][A-Za-z0-9_]*\"?)\s*\.\s*)?"
        r'"?[A-Za-z가-힣_][A-Za-z0-9가-힣_]*"?)'
    )
    for alias, explicit_alias in aliases:
        day_axis = _qualified_column(alias, "기준년월일")
        if not explicit_alias:
            day_axis = (
                rf'(?:(?:"?{re.escape(source_table)}"?\s*\.\s*)?)'
                r'"?기준년월일"?'
            )
        month_axis = f'{alias}."기준년월"' if explicit_alias else '"기준년월"'
        patterns = (
            rf'(?:SUBSTR|SUBSTRING)\s*\(\s*{day_axis}\s*,\s*1\s*,\s*6\s*\)'
            rf"\s+BETWEEN\s+{operand}\s+AND\s+{operand}",
            rf'(?:SUBSTR|SUBSTRING)\s*\(\s*{day_axis}\s*,\s*1\s*,\s*6\s*\)'
            rf"\s*=\s*{operand}",
            rf'{day_axis}\s+BETWEEN\s+{operand}\s+AND\s+{operand}',
            rf'{day_axis}\s*>=\s*{operand}\s+AND\s+{day_axis}\s*<=\s*{operand}',
            rf'{day_axis}\s*=\s*{operand}',
        )
        predicate = _routed_time_predicate(month_axis, "monthly", start_ym, end_ym)
        for pattern in patterns:
            rewritten, count = re.subn(
                pattern,
                predicate,
                rewritten,
                flags=re.IGNORECASE | re.DOTALL,
            )
            touched = touched or bool(count)

        # A D-1 cap is meaningful only on the live source. The archive already
        # has an explicit load-month boundary.
        if touched:
            rewritten = re.sub(
                rf'\s+AND\s+{day_axis}\s*<=\s*DATE_FORMAT\s*\(\s*DATE_ADD\s*\('
                r"\s*'day'\s*,\s*-1\s*,\s*CURRENT_TIMESTAMP\s+AT\s+TIME\s+ZONE\s+"
                r"'Asia/Seoul'\s*\)\s*,\s*'%Y%m%d'\s*\)",
                "",
                rewritten,
                flags=re.IGNORECASE | re.DOTALL,
            )

    if not touched:
        return select_sql
    # 조회기준일 도장까지 D-1 로 두면 2025년 3월 값을 세면서 "조회기준일 = 어제"를
    # 함께 내보낸다. 돌린 창의 기준월로 맞춘다. 별칭이 붙은 도장만 건드린다 —
    # 같은 스코프의 다른 테이블이 D-1 조건을 쓰고 있을 수 있다.
    rewritten = re.sub(
        rf"{_DYNAMIC_PREVIOUS_DAY_SQL}(?=\s+AS\s)",
        f"'{end_ym}'",
        rewritten,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _replace_physical_table(rewritten, source_table, target_table)


def _apply_previous_day_historical_sources(question: str, sql: str) -> str:
    """Route historical SELECT scopes while leaving current D-1 scopes intact."""
    start_ym, end_ym = _previous_day_archive_route(question)
    if not start_ym:
        return sql

    rewritten = sql

    def cte_body_spans(value: str) -> list[tuple[int, int]]:
        """Find balanced CTE bodies while ignoring parentheses in quotes."""
        starts = list(
            re.finditer(
                r'(?:\bWITH|,)\s+"?[A-Za-z_][A-Za-z0-9_]*"?'
                r'(?:\s*\([^)]*\))?\s+AS\s*\(',
                value or "",
                re.IGNORECASE,
            )
        )
        spans: list[tuple[int, int]] = []
        for match in starts:
            open_index = match.end() - 1
            depth = 1
            quote = ""
            index = open_index + 1
            while index < len(value) and depth:
                char = value[index]
                if quote:
                    if char == quote:
                        if index + 1 < len(value) and value[index + 1] == quote:
                            index += 2
                            continue
                        quote = ""
                elif char in {"'", '"'}:
                    quote = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        spans.append((open_index + 1, index))
                        break
                index += 1
        return list(dict.fromkeys(spans))

    for source_table, target_table in _PREVIOUS_DAY_ARCHIVE_TABLES.items():
        spans = cte_body_spans(rewritten)
        for start, end in sorted(spans, reverse=True):
            body = rewritten[start:end]
            routed_body = _rewrite_previous_day_archive_select(
                body,
                source_table,
                target_table,
                start_ym,
                end_ym,
            )
            rewritten = rewritten[:start] + routed_body + rewritten[end:]
        if not spans:
            # 스코프가 하나뿐이면 "현재 모집단 + 과거 실적" 분업이 없다. 그 하나를
            # 어제로 남겨 두면 어제 값이 그 달의 답으로 나가므로, D-1 로 못 박힌
            # 템플릿도 적재 정책이 선언한 아카이브로 돌린다.
            rewritten = _rewrite_previous_day_archive_select(
                rewritten,
                source_table,
                target_table,
                start_ym,
                end_ym,
                preserve_pinned_current=False,
            )
    return rewritten


def _live_month_source_relation(
    relation: str,
    live_table: str,
    day_column: str,
    day_value: str,
) -> str:
    """Expose a D-1 sibling under the monthly snapshot's `기준년월` axis.

    Wrapping instead of swapping column references keeps every position the
    caller already wrote working — SELECT list, PARTITION BY, join keys — and
    keeps `기준년월` in the output of a `SELECT *`.
    """
    return (
        f"(SELECT {live_table}.*, "
        f'SUBSTR({live_table}."{day_column}", 1, 6) AS "기준년월" '
        f"FROM {relation} {live_table} "
        f"WHERE {live_table}.\"{day_column}\" = '{day_value}')"
    )


def _live_source_missing_columns(sql: str, source_table: str, live_table: str) -> list[str]:
    """Columns this SQL reads that the daily source does not have.

    The twins are near-identical by design, but not identical — ``tmdaa1d12``
    owns `소호로지스틱BSS등급구분코드` and ``tbdaa1d12`` does not. 이름 접미사가
    다른 짝(``tmdaa5e11``·``tbdaadt01``)은 겹치는 컬럼이 신용판매 매출금액처럼
    일부뿐이다. 그런 컬럼을 읽는 쿼리를 돌리면 빈 결과가 컬럼 오류로 바뀌므로,
    월 원천에 그대로 두고 왜 값이 없는지를 답변에서 밝힌다.
    """
    _, schema_columns = _schema_table_index(SCHEMA)
    monthly_only = schema_columns.get(source_table, set()) - schema_columns.get(live_table, set())
    # 기준년월은 감싸는 SELECT 가 기준년월일에서 파생해 채운다.
    monthly_only.discard("기준년월")
    return sorted(
        column
        for column in monthly_only
        if re.search(rf'"{re.escape(column)}"', sql or "", re.IGNORECASE)
    )


def _live_source_covers_columns(sql: str, source_table: str, live_table: str) -> bool:
    """Whether the daily source carries every column this SQL reads."""
    return not _live_source_missing_columns(sql, source_table, live_table)


def _route_table_to_live_source(
    sql: str,
    source_table: str,
    live_table: str,
    day_column: str,
    day_value: str,
) -> str:
    """Replace each monthly relation with its D-1 sibling's latest day."""
    cte_names = _extract_cte_names(sql)
    rewritten = sql
    for match in reversed(list(_SQL_RELATION_RE.finditer(sql))):
        quoted_table = match.group("quoted_table") is not None
        table_group = "quoted_table" if quoted_table else "plain_table"
        if str(match.group(table_group) or "").lower() != source_table:
            continue
        schema_name = match.group("quoted_schema") or match.group("plain_schema") or ""
        if not schema_name and source_table in cte_names:
            continue

        table_start, table_end = match.span(table_group)
        relation_start = match.end("kind")
        relation_end = table_end + 1 if quoted_table else table_end
        relation = (
            sql[relation_start:table_start].strip()
            + live_table
            + sql[table_end:relation_end]
        )

        quoted_alias = match.group("quoted_alias") is not None
        alias_group = "quoted_alias" if quoted_alias else "plain_alias"
        raw_alias = str(match.group(alias_group) or "")
        explicit_alias = bool(raw_alias) and raw_alias.lower() not in _SQL_SOURCE_KEYWORDS
        if not explicit_alias:
            alias = source_table
        else:
            alias = f'"{raw_alias}"' if quoted_alias else raw_alias
        end = (
            match.end(alias_group) + (1 if quoted_alias else 0)
            if explicit_alias
            else relation_end
        )

        wrapped = _live_month_source_relation(relation, live_table, day_column, day_value)
        rewritten = rewritten[:relation_start] + f" {wrapped} {alias}" + rewritten[end:]
    return rewritten


def _apply_current_month_live_sources(
    sql: str,
    source_tables: set[str] | None = None,
) -> str:
    """Read the open month from the daily sibling of a monthly snapshot.

    `tmdaaus01` 같은 월말요약은 그 달이 닫힌 뒤에야 적재되므로 이번 달
    `기준년월`로 물으면 빈 결과가 나온다. 같은 달을 이미 들고 있는 일별 짝
    (`tbdaaus01`)의 최신 가용일 1건으로 돌린다 — 월별 테이블이 들고 있었을
    "월 최신 스냅샷 1건"과 같은 의미다. 지난 달 이전은 그대로 월별을 쓴다.
    """
    current_ym = kst_today().strftime("%Y%m")
    candidates = _extract_schema_tables(sql)
    if source_tables is not None:
        candidates &= source_tables
    rewritten = sql
    for source_table in sorted(candidates):
        live = live_source_for(source_table)
        day_column = str((live or {}).get("query_time_dimension") or "")
        if not live or not day_column:
            continue
        # 이번 달만 물었을 때만 돌린다. 지난 달이 섞인 기간은 월별이 정답이다.
        if set(_table_axis_literal_values(rewritten, source_table, "기준년월")) != {current_ym}:
            continue
        live_table = str(live["table"])
        # 월초나 적재 지연으로 짝이 아직 이번 달을 못 들고 있으면 돌릴 곳이 없다.
        # 여기서만 적재 범위를 읽으므로, 이번 달을 물은 SQL 외에는 조회가 없다.
        latest_day = _latest_available_day(live_table)
        if latest_day[:6] != current_ym:
            continue
        if not _live_source_covers_columns(rewritten, source_table, live_table):
            continue
        rewritten = _route_table_to_live_source(
            rewritten,
            source_table,
            live_table,
            day_column,
            latest_day,
        )
    return rewritten


def _apply_accumulation_historical_sources(question: str, sql: str) -> str:
    # 이번 달 라우팅은 처음부터 월별이던 테이블만 본다. 명시 과거 일자를 월별
    # 원천으로 돌린 결과("2026-08-10" → tmdaa1d12.기준년월 = '202608')를 이번
    # 달이라는 이유로 다시 일별 D-1 로 되돌리면 질문한 날짜를 잃는다.
    monthly_sources = {
        table for table in _extract_schema_tables(sql) if live_source_for(table)
    }
    routed = _restore_master_attribute_sources(question, sql)
    routed = _apply_tbdaadt01_historical_source(question, routed)
    routed = _apply_previous_day_historical_sources(question, routed)
    return _apply_current_month_live_sources(routed, monthly_sources)


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
    """Disable non-runtime or schema-incompatible deterministic SQL tools."""
    name = str(tool.get("name") or "")
    if name in _TOOL_SCHEMA_COMPATIBILITY:
        return _TOOL_SCHEMA_COMPATIBILITY[name]
    linked_query_name = str(tool.get("sql_query_name") or "")
    linked_query = next(
        (query for query in VERIFIED_QUERIES if query.get("name") == linked_query_name),
        None,
    )
    if linked_query and not _verified_query_is_executable(linked_query):
        _TOOL_SCHEMA_COMPATIBILITY[name] = False
        return False
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
        if tool["name"] == "corporate_card_active_no_usage_members" and not re.search(
            r"6\s*무|무실적|미이용|이용.{0,5}없|사용.{0,5}없|쓰지\s*않|한\s*번도\s*쓰지",
            question or "",
        ):
            continue
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


def _current_corporate_limit_sql(monthly_sql: str) -> str:
    """Use the live D-1 customer snapshot for a current limit request."""
    rewritten = str(monthly_sql or "").replace(
        "card_system.tmdaa1d12",
        "card_system.tbdaa1d12",
    )
    rewritten = rewritten.replace(
        'a."기준년월" AS "기준년월"',
        'SUBSTR(a."기준년월일", 1, 6) AS "기준년월"',
    )
    rewritten = rewritten.replace(
        'PARTITION BY a."고객식별자", a."기준년월"',
        'PARTITION BY a."고객식별자"',
    )
    rewritten = rewritten.replace(
        'a."기준년월" = \'{기준년월}\'',
        (
            'a."기준년월일" = DATE_FORMAT(\n'
            "        DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'),\n"
            "        '%Y%m%d'\n"
            '      )'
        ),
    )
    if (
        "card_system.tmdaa1d12" in rewritten
        or 'a."기준년월"' in rewritten
        or 'a."기준년월일" = DATE_FORMAT(' not in rewritten
    ):
        raise ValueError("현재 기업 한도 VQ를 KST 전일 스냅샷으로 변환하지 못했습니다.")
    return rewritten


def _current_corporate_no_usage_sql(monthly_sql: str) -> str:
    """Use D-1 for the current population and monthly rows for prior usage."""
    if (
        monthly_sql.count("card_system.tmdaa1d12") != 2
        or 'a."기준년월" = \'{기준년월}\'' not in monthly_sql
        or 'FROM card_system.tmdaa1d12 b' not in monthly_sql
    ):
        raise ValueError("현재 무실적 VQ의 월 원천 구조를 인식하지 못했습니다.")

    sql = monthly_sql.replace(
        "card_system.tmdaa1d12 a",
        "card_system.tbdaa1d12 a",
        1,
    ).replace(
        'a."기준년월" = \'{기준년월}\'',
        (
            'a."기준년월일" = DATE_FORMAT(\n'
            "    DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'),\n"
            "    '%Y%m%d'\n"
            '  )'
        ),
        1,
    )
    # The monthly archive may not yet contain the current partial month. Its
    # D-1 row carries month-to-date usage, so exclude those customers too.
    sql = sql.replace(
        '  AND NOT EXISTS (',
        (
            '  AND (\n'
            '    COALESCE(a."금월신용카드이용금액", 0)\n'
            '    + COALESCE(a."금월체크카드이용금액", 0)\n'
            '  ) = 0\n'
            '  AND NOT EXISTS ('
        ),
        1,
    )
    if (
        sql.count("card_system.tbdaa1d12") != 1
        or sql.count("card_system.tmdaa1d12") != 1
        or 'a."기준년월일" = DATE_FORMAT(' not in sql
        or 'b."기준년월" BETWEEN' not in sql
    ):
        raise ValueError("현재 무실적 VQ를 전일+월 이력 구조로 변환하지 못했습니다.")
    return sql


def _current_check_card_only_sql(monthly_sql: str) -> str:
    """Combine prior monthly rows with the D-1 current-month snapshot."""
    old = '''daily AS (
  SELECT
    a."기준년월일",
    a."기준년월" AS "기준년월",
    a."고객식별자",
    a."사업자등록번호",
    a."기업명",
    a."유효기업신용카드수",
    a."유효기업체크카드수",
    a."금월신용카드이용금액",
    a."금월체크카드이용금액"
  FROM card_system.tmdaa1d12 a
  CROSS JOIN params p
  WHERE a."기준년월" BETWEEN p."연초년월" AND p."기준년월"
),'''
    new = '''daily AS (
  SELECT
    a."기준년월일",
    a."기준년월" AS "기준년월",
    a."고객식별자",
    a."사업자등록번호",
    a."기업명",
    a."유효기업신용카드수",
    a."유효기업체크카드수",
    a."금월신용카드이용금액",
    a."금월체크카드이용금액"
  FROM card_system.tmdaa1d12 a
  CROSS JOIN params p
  WHERE a."기준년월" BETWEEN p."연초년월" AND DATE_FORMAT(
    DATE_ADD('month', -1, DATE_PARSE(CONCAT(p."기준년월", '01'), '%Y%m%d')),
    '%Y%m'
  )

  UNION ALL

  SELECT
    c."기준년월일",
    SUBSTR(c."기준년월일", 1, 6) AS "기준년월",
    c."고객식별자",
    c."사업자등록번호",
    c."기업명",
    c."유효기업신용카드수",
    c."유효기업체크카드수",
    c."금월신용카드이용금액",
    c."금월체크카드이용금액"
  FROM card_system.tbdaa1d12 c
  CROSS JOIN params p
  WHERE SUBSTR(c."기준년월일", 1, 6) = p."기준년월"
    AND c."기준년월일" = DATE_FORMAT(
    DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'),
    '%Y%m%d'
  )
),'''
    sql = monthly_sql.replace(old, new, 1)
    if (
        sql == monthly_sql
        or sql.count("card_system.tbdaa1d12") != 1
        or sql.count("card_system.tmdaa1d12") != 1
    ):
        raise ValueError("현재 체크카드 교차영업 VQ를 전일+월 이력 구조로 변환하지 못했습니다.")
    return sql


def _current_target_industry_usage_sql(monthly_sql: str) -> str:
    """Blend D-1 corporate state/MTD usage with prior monthly history."""
    old = '''customer_daily AS (
  SELECT
    a."기준년월일",
    a."기준년월" AS "기준년월",
    a."고객식별자",
    a."유효기업신용카드수",
    a."유효기업체크카드수",
    a."금월신용카드이용금액",
    a."금월체크카드이용금액",
    a."기업총한도금액",
    a."기업총잔여한도금액"
  FROM card_system.tmdaa1d12 a
  CROSS JOIN params p
  WHERE a."기준년월" BETWEEN LEAST(p."시작6개월", p."연초년월") AND p."기준년월"
),'''
    new = '''customer_daily AS (
  SELECT
    a."기준년월일",
    a."기준년월" AS "기준년월",
    a."고객식별자",
    a."유효기업신용카드수",
    a."유효기업체크카드수",
    a."금월신용카드이용금액",
    a."금월체크카드이용금액",
    a."기업총한도금액",
    a."기업총잔여한도금액"
  FROM card_system.tmdaa1d12 a
  CROSS JOIN params p
  WHERE a."기준년월" BETWEEN LEAST(p."시작6개월", p."연초년월") AND DATE_FORMAT(
    DATE_ADD('month', -1, DATE_PARSE(CONCAT(p."기준년월", '01'), '%Y%m%d')),
    '%Y%m'
  )

  UNION ALL

  SELECT
    c."기준년월일",
    SUBSTR(c."기준년월일", 1, 6) AS "기준년월",
    c."고객식별자",
    c."유효기업신용카드수",
    c."유효기업체크카드수",
    c."금월신용카드이용금액",
    c."금월체크카드이용금액",
    c."기업총한도금액",
    c."기업총잔여한도금액"
  FROM card_system.tbdaa1d12 c
  CROSS JOIN params p
  WHERE SUBSTR(c."기준년월일", 1, 6) = p."기준년월"
    AND c."기준년월일" = DATE_FORMAT(
    DATE_ADD('day', -1, CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Seoul'),
    '%Y%m%d'
  )
),'''
    sql = monthly_sql.replace(old, new, 1)
    old_check_target = '''  SELECT
    m."고객식별자",
    '3_체크카드교차영업' AS "대상구분"
  FROM customer_monthly m
  CROSS JOIN params p
  WHERE m."기준년월" BETWEEN p."연초년월" AND p."기준년월"
  GROUP BY m."고객식별자"
  HAVING MAX(COALESCE(m."유효기업신용카드수", 0)) = 0
    AND MAX(COALESCE(m."유효기업체크카드수", 0)) > 0
    AND SUM(COALESCE(m."금월신용카드이용금액", 0)) = 0
    AND SUM(COALESCE(m."금월체크카드이용금액", 0))
      / NULLIF(CAST(MAX(p."평균산정월수") AS DOUBLE), 0.0) >= {월평균금액}'''
    new_check_target = '''  SELECT
    c."고객식별자",
    '3_체크카드교차영업' AS "대상구분"
  FROM current_customers c
  JOIN customer_monthly m
    ON c."고객식별자" = m."고객식별자"
  CROSS JOIN params p
  WHERE m."기준년월" BETWEEN p."연초년월" AND p."기준년월"
    AND COALESCE(c."유효기업신용카드수", 0) = 0
    AND COALESCE(c."유효기업체크카드수", 0) > 0
  GROUP BY c."고객식별자"
  HAVING MAX(COALESCE(m."유효기업신용카드수", 0)) = 0
    AND SUM(COALESCE(m."금월신용카드이용금액", 0)) = 0
    AND SUM(COALESCE(m."금월체크카드이용금액", 0))
      / NULLIF(CAST(MAX(p."평균산정월수") AS DOUBLE), 0.0) >= {월평균금액}'''
    sql = sql.replace(old_check_target, new_check_target, 1)
    if (
        sql == monthly_sql
        or new_check_target not in sql
        or sql.count("card_system.tbdaa1d12") != 1
        or "card_system.tmdaa1d12" not in sql
        or "card_system.tbdaabt30" not in sql
    ):
        raise ValueError("현재 영업대상 업종 VQ의 기업 모집단을 전일 기준으로 변환하지 못했습니다.")
    return sql


def _verified_query_corporate_sources(question: str, query_name: str) -> set[str]:
    """Resolve the declared daily/monthly corporate sources for one VQ."""
    for collection in ("semantic_query_contracts", "query_references"):
        for entry in SCHEMA.get(collection, []):
            if str(entry.get("verified_query") or "") == query_name:
                return {
                    str(table).rsplit(".", 1)[-1].lower()
                    for table in source_tables_for_question(entry, question)
                }
    return set()


def _route_verified_query_accumulation(
    question: str,
    matched_query_name: str,
    sql: str,
) -> str:
    """Apply request-sensitive cadence routing to reusable VQ templates."""
    explicit_current_request = bool(
        re.search(r"현재|현시점|오늘|전일|어제|최신|지금", question or "")
    )
    corporate_sources = _verified_query_corporate_sources(
        question, matched_query_name
    )
    if (
        matched_query_name == "corporate_check_card_only_high_monthly_avg"
        and "tbdaa1d12" in corporate_sources
        and explicit_current_request
    ):
        return _current_check_card_only_sql(sql)
    if (
        matched_query_name == "managed_company_delinquency"
        and "tbdaa1d12" in corporate_sources
    ):
        return _current_corporate_limit_sql(sql)
    if matched_query_name == "corporate_target_industry_usage":
        current_term = bool(
            re.search(r"현재|현시점|오늘|전일|어제|최신|지금", question or "")
        )
        historical_as_of = bool(
            re.search(
                r"(?:20\d{2}\s*년\s*\d{1,2}\s*월|(?<!\d)20\d{4}(?!\d))"
                r"\s*(?:현재|기준|당시)",
                question or "",
            )
        )
        if current_term and not historical_as_of:
            return _current_target_industry_usage_sql(sql)
    if (
        matched_query_name == "corporate_limit_status_at_month"
        and "tbdaa1d12" in corporate_sources
    ):
        return _current_corporate_limit_sql(sql)
    if matched_query_name == "corporate_card_active_no_usage_members":
        if explicit_current_request:
            return _current_corporate_no_usage_sql(sql)
    return _apply_accumulation_historical_sources(question, sql)


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
3. 기간 길이가 없는 단독 "최근", "최근 기준", "이번달", "이번 월"만 현재월 1개월로 해석 → 기간_시작/기간_종료 모두 "{datetime.now().year}{datetime.now().month:02d}"
   "최근 N개월/N달/반년/N년"은 기간 조건으로 유지하고, 명시 기준월이 있으면 그 월을 종료월로 사용하세요.
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


def _asks_scalar_count(question: str) -> bool:
    """Whether the question wants a single count rather than a per-entity list.

    단독 '기업'도 개수를 묻는다("기업 수와 총한도를 알려줘"). 이게 빠져 있어서
    기업별 한도 목록 VQ가 그대로 나갔다. ``수(?!수료)`` 는 조사가 붙은 "수와"·"수를"은
    받고 "수수료"만 걸러낸다.
    """
    return bool(
        re.search(
            r"(?:기업회원|법인회원|회원|고객|가맹점|업체|기업)\s*(?:수(?!수료)|개수|몇\s*(?:명|개|곳))",
            question or "",
        )
        and not re.search(r"(?:상위|하위|목록|명단|리스트|상세|별)", question or "")
    )


_ENTITY_ID_COLUMNS = (
    "고객식별자", "사업자등록번호", "기업명", "가맹점번호", "가맹점명",
    "회원일련번호", "카드구분키번호", "특수채권관리번호",
)
_AGGREGATE_CALL_RE = re.compile(
    r"(?<![0-9A-Za-z_])(?:SUM|COUNT|AVG|MIN|MAX|ARBITRARY|ANY_VALUE)\s*\(", re.IGNORECASE
)


def _vq_lists_entities(sql: str) -> bool:
    """Whether the query's final SELECT emits one row per business entity.

    개수 질문에 붙으면 안 되는 것은 "집계가 아닌 쿼리"가 아니라 **업체별 목록**이다.
    corporate_cards_issued_then_cancelled_count 는 GROUP BY 가 조회기준일 하나뿐인
    카운트 쿼리라 목록이 아니고, 질문이 요구한 카드좌수·업체수를 그대로 낸다.
    반면 corporate_limit_status_at_month 는 고객식별자·기업명을 그대로 뽑는 목록이다.
    """
    text = str(sql or "")
    last = None
    for match in re.finditer(r"(?<![0-9A-Za-z_])SELECT(?![0-9A-Za-z_])", text, re.IGNORECASE):
        last = match
    if last is None:
        return False
    tail = text[last.end():]
    from_at = re.search(r"(?<![0-9A-Za-z_])FROM(?![0-9A-Za-z_])", tail, re.IGNORECASE)
    select_list = tail[: from_at.start()] if from_at else tail

    # 집계 호출을 지운 뒤에도 엔티티 식별자가 남아 있으면 행이 엔티티 단위다.
    stripped = _strip_aggregate_calls(select_list)
    return any(column in stripped for column in _ENTITY_ID_COLUMNS)


def _strip_aggregate_calls(select_list: str) -> str:
    """Drop aggregate calls with their arguments from a SELECT list.

    함수 이름만 지우면 COUNT(DISTINCT m."고객식별자") 의 고객식별자가 남아
    집계 결과가 엔티티 목록으로 잡힌다.
    """
    stripped: list[str] = []
    index = 0
    while index < len(select_list):
        call = _AGGREGATE_CALL_RE.search(select_list, index)
        if not call:
            stripped.append(select_list[index:])
            break
        stripped.append(select_list[index : call.start()])
        depth, cursor = 0, call.end() - 1
        while cursor < len(select_list):
            if select_list[cursor] == "(":
                depth += 1
            elif select_list[cursor] == ")":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        index = cursor + 1
    return "".join(stripped)


def _tool_request_is_supported(question: str, tool: dict) -> bool:
    """Return whether a deterministic Tool can preserve the question's shape."""
    name = str(tool.get("name") or "")
    row_request = parse_row_request(question)
    if name == "대손비용률_분석":
        return bool(
            not row_request.limit
            and not row_request.mode
            and re.search(r"대손(?:비용)?(?:률|율)|대손비율|보정계수", question or "")
            and not re.search(r"(?:회원|기업|가맹점|업종|지역|등급)별", question or "")
        )
    if name == "merchant_corporate_sales_target_no_corporate_card" and not (
        re.search(r"매출", question or "")
        and re.search(r"법인사업자|법인\s*가맹점", question or "")
        and re.search(r"(?:기업|법인)카드.{0,8}(?:미보유|없|보유하지\s*않)", question or "")
    ):
        return False

    linked_name = str(tool.get("sql_query_name") or "")
    linked_query = next(
        (query for query in VERIFIED_QUERIES if str(query.get("name") or "") == linked_name),
        None,
    )
    if linked_query and not _vq_row_request_is_supported(row_request, linked_query, question):
        return False
    if not linked_query:
        if row_request.mode in {"top", "bottom", "latest", "tail"}:
            return False
        if row_request.limit is not None and not any(
            str(spec.get("name") or "") == "limit" for spec in tool.get("parameters", [])
        ):
            return False

    asks_scalar_count = _asks_scalar_count(question or "")
    if asks_scalar_count and (not linked_query or not is_scalar_aggregate_query(str(linked_query.get("sql") or ""))):
        return False
    return True


def _select_tool_capability(
    question: str,
    candidate_tools: list[dict],
    forced_tool: str = "",
    domain_context: str = "",
) -> dict:
    if forced_tool:
        forced = TOOL_MAP.get(forced_tool)
        if not forced or not _tool_request_is_supported(question, forced):
            return _empty_capability_selection()
        candidate_tools = [forced]
    else:
        candidate_tools = [
            tool for tool in candidate_tools if _tool_request_is_supported(question, tool)
        ]
        if not candidate_tools:
            return _empty_capability_selection()

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
    if not _tool_request_is_supported(question, TOOL_MAP[tool_name]):
        return _empty_capability_selection()

    param_specs = TOOL_MAP[tool_name]["parameters"]
    linked_query_name = str(TOOL_MAP[tool_name].get("sql_query_name") or "")
    linked_query = next(
        (query for query in VERIFIED_QUERIES if str(query.get("name") or "") == linked_query_name),
        None,
    )
    tool_tables = _sql_table_names(str(linked_query.get("sql") or "")) if linked_query else set()
    rule_params = _extract_params_by_rule(question, param_specs, tool_tables)
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
        if not selected or not _tool_request_is_supported(state.get("question", ""), selected):
            return _empty_capability_selection()
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
    retrieval_question = _retrieval_question(state)
    domain_context = state.get("domain_context", "")
    semantic_contracts = semantic_query_contract_candidates(SCHEMA, retrieval_question, max_count=1)
    if semantic_contracts:
        contract = semantic_contracts[0]
        support_status = str(contract.get("support_status") or "supported").lower()
        if support_status.startswith("blocked"):
            reasons = contract.get("ambiguity_policy", [])
            if isinstance(reasons, str):
                reasons = [reasons]
            details = "\n".join(f"- {reason}" for reason in reasons if reason)
            result = _empty_capability_selection()
            result["question_type"] = "reject"
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

    forced_tool = _rule_match_tool(retrieval_question)
    if forced_tool:
        return _select_tool_capability(question, [], forced_tool, domain_context)

    candidate_tools = _tool_candidates(retrieval_question)
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
                    # Tool은 자기 결과를 전부 넘겨주므로 여기서는 전체 건수를 이미 안다.
                    total_row_count=len(rows),
                ),
                "answer": result.get("answer", ""),
                "bad_debt_excel_path": result.get("excel_path", ""),
                "tool_completed": True,
            }
        linked_query_name = str(tool.get("sql_query_name") or "")
        linked_query = next(
            (
                query
                for query in VERIFIED_QUERIES
                if str(query.get("name") or "") == linked_query_name
            ),
            None,
        )
        if isinstance(result, str) and linked_query:
            routed_template = _route_verified_query_accumulation(
                _retrieval_question(state),
                linked_query_name,
                str(linked_query.get("sql") or ""),
            )
            result = _apply_params_to_vq(
                routed_template,
                params,
                linked_query_name,
                linked_query.get("parameters", {}),
            )
        sql = _apply_name_filter_mode(state.get("question", ""), result)
        row_issues = _validate_requested_row_constraints(state.get("question", ""), sql)
        if row_issues:
            return {
                "final_sql": "",
                "query_error": "Tool이 요청한 결과 제약을 충족하지 못했습니다: " + " ".join(row_issues),
                "selected_tool": "",
                "tool_completed": False,
            }
        formatted_sql = sqlparse.format(sql, reindent=True, keyword_case="upper")
        return {"final_sql": prepare_sql_for_backend(formatted_sql), "tool_completed": False}
    except Exception as e:
        return {"final_sql": "", "query_error": str(e), "tool_completed": False}


def run_tool_query(state: Text2SQLState) -> dict:
    sql = state.get("final_sql", "")
    if not sql:
        return {"query_columns": [], "query_rows": [], "query_error": "SQL이 생성되지 않았습니다.", "selected_tool": "", "final_sql": ""}
    sql = _apply_accumulation_historical_sources(_retrieval_question(state), sql)
    prepared_sql = prepare_sql_for_backend(sql)
    availability_error = _availability_execution_error(state, prepared_sql)
    if availability_error:
        return {
            "query_columns": [],
            "query_rows": [],
            "query_error": f"테이블 적재 주기 위반: {availability_error}",
            "selected_tool": "",
            "final_sql": prepared_sql,
        }
    columns, rows, error = execute_sql(
        prepared_sql,
        max_rows=DEFAULT_FETCH_ROW_LIMIT,
        allow_cross_cycle_fallback=_allow_cross_cycle_fallback(
            state.get("question", ""), prepared_sql
        ),
    )
    if error:
        return {
            "query_columns": [],
            "query_rows": [],
            "query_error": error,
            "selected_tool": "",
            "final_sql": prepared_sql,
        }
    return _executed_query_state(prepared_sql, columns, rows)


def _match_vq_by_embedding(
    question: str,
    *,
    intent_question: str = "",
) -> dict | None:
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
            if not _verified_query_matches_intent(
                intent_question or question,
                matched,
                contract_question=question,
            ):
                return None
            return {
                "matched_query_name": matched["name"],
                "matched_query_sql": matched["sql"].strip(),
                "matched_query_params": matched.get("parameters", {}),
            }
    except Exception:
        pass
    return None


def _verified_query_matches_intent(
    question: str,
    matched: dict,
    *,
    contract_question: str = "",
) -> bool:
    """Authorize a retrieved VQ only when its fixed capability fits the request."""
    if not _verified_query_is_executable(matched):
        return False
    if not _verified_query_time_source_compatible(question, matched):
        return False
    if not _verified_query_serves_requested_period(question, matched):
        return False
    if not _verified_query_serves_named_industry(question, matched):
        return False
    question_text = question or ""

    # v2: 참고 SQL은 컬럼이 고정된 완제품이라 매칭되면 그대로 실행된다. 질문이
    # 이름을 댄 기간·축·지표를 못 내놓는 VQ는 어휘가 아무리 겹쳐도 답이 아니다.
    # 여기서 거부하면 LLM 생성 경로로 내려간다.
    #
    # 실행되는 것은 적재 정책이 다시 쓴 SQL 이다. 가맹점 마스터 템플릿은 기간 필터를
    # 들고 있지 않지만("날짜를 말할 때만" 규칙), 과거 월 질문이면 라우팅이 월
    # 스냅샷과 기준년월 조건을 붙여 준다. 템플릿만 보면 그 답을 거부하게 된다.
    output_gap = _v2_vq_output_gap(
        contract_question or question_text,
        {
            **matched,
            "sql": _route_verified_query_accumulation(
                contract_question or question_text,
                str(matched.get("name") or ""),
                str(matched.get("sql") or ""),
            ),
        },
        column_names=_schema_column_names(),
    )
    if output_gap:
        return False
    matched_metadata = " ".join(
        str(matched.get(key, ""))
        for key in ("name", "question")
    )
    matched_metadata += " " + " ".join(str(tag) for tag in matched.get("tags", []))
    matched_text = matched_metadata + " " + str(matched.get("sql") or "")

    matched_name = str(matched.get("name") or "")
    bound_contracts = [
        contract
        for contract in SCHEMA.get("semantic_query_contracts", [])
        if str(contract.get("verified_query") or "") == matched_name
    ]
    if bound_contracts:
        top_contracts = semantic_query_contract_candidates(
            SCHEMA,
            contract_question or question_text,
            max_count=1,
        )
        if not top_contracts:
            return False
        top_contract_name = str(top_contracts[0].get("name") or "")
        compatible_contracts = [
            contract
            for contract in bound_contracts
            if str(contract.get("name") or "") == top_contract_name
            and _semantic_contract_bindings_satisfied(question_text, contract, matched)
        ]
        if not compatible_contracts:
            return False
    else:
        # Lexical/embedding retrieval is allowed to propose an unbound VQ, but
        # it cannot waive that template's required entity inputs.
        param_defs = matched.get("parameters") if isinstance(matched.get("parameters"), dict) else {}
        required_bindings = [
            {
                "name": name,
                "type": info.get("type", "string"),
                "description": info.get("description", ""),
            }
            for name, info in param_defs.items()
            if isinstance(info, dict)
            and info.get("required")
            and name in _ENTITY_NAME_PARAM_NAMES.union({"가맹점번호"})
            and not re.search(r"(?:년월|기간|시작|종료|기준일|기준년)", str(name))
        ]
        extracted_bindings = _extract_params_by_rule(question_text, required_bindings)
        if any(extracted_bindings.get(spec["name"]) in (None, "") for spec in required_bindings):
            return False

    # Some concepts change the metric or cardinality rather than merely adding
    # a filter.  They must be present on both sides of an unstructured match.
    asks_delinquency = bool(re.search(r"연체|채권", question_text))
    if asks_delinquency and not re.search(r"연체|채권", matched_metadata):
        return False
    candidate_is_new_customer = bool(
        re.search(r"신규\s*(?:고객|회원)|가입\s*(?:고객|회원)|등록\s*(?:고객|회원)", matched_metadata)
    )
    if candidate_is_new_customer and not re.search(
        r"신규|새로\s*(?:가입|등록)|가입\s*(?:고객|회원)|등록\s*(?:고객|회원)",
        question_text,
    ):
        return False
    candidate_is_peak = bool(re.search(r"최다|피크|가장\s*(?:많|높|큰)|최대\s*(?:이용|사용)", matched_metadata))
    if candidate_is_peak and not re.search(
        r"최다|피크|가장\s*(?:많|높|큰)|최대\s*(?:이용|사용)",
        question_text,
    ):
        return False
    asks_no_usage = bool(
        re.search(r"무실적|미이용|이용.{0,5}없|사용.{0,5}없|쓰지\s*않|한\s*번도\s*쓰지", question_text)
    )
    if asks_no_usage and not re.search(
        r"무실적|미이용|이용.{0,5}없|사용.{0,5}없|쓰지\s*않",
        matched_metadata,
    ):
        return False

    row_request = parse_row_request(question_text)
    if not _vq_row_request_is_supported(row_request, matched, question_text):
        return False

    # v2: 개수를 묻는 질문에 업체별 목록을 내는 VQ가 붙으면 결과 단위가 다르다.
    # "기업 수와 총한도를 알려줘" 에 corporate_limit_status_at_month(기업별 한도 목록)가
    # 매칭돼 스칼라 두 개 대신 기업 목록이 나갔다. 같은 검사가 Tool 선택 쪽에는
    # 있었지만 VQ 경로에는 없었다.
    if _asks_scalar_count(question_text) and _vq_lists_entities(str(matched.get("sql") or "")):
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


def _fixed_vq_limits(sql: str) -> list[int]:
    value = outer_limit(sql)
    return [value] if value is not None else []


_RANK_METRIC_ALIASES = (
    ("limit_utilization", ("한도소진율", "한도 소진율", "한도사용률", "한도 사용률")),
    ("remaining_limit", ("기업총잔여한도금액", "총잔여한도금액", "잔여한도금액", "잔여 한도 금액", "잔여한도", "잔여 한도", "가용한도", "가용 한도")),
    ("used_limit", ("기업한도사용금액", "한도사용금액", "한도 사용 금액", "사용한도", "사용 한도")),
    ("total_limit", ("기업총한도금액", "총한도금액", "총 한도 금액", "총한도", "총 한도", "여신한도", "한도")),
    ("sales_count", ("총매출건수", "매출건수", "매출 건수", "거래건수", "거래 건수")),
    ("sales_amount", ("총매출금액", "매출금액", "매출 금액", "매출액", "매출")),
    ("fee_rate", ("평균수수료율", "수수료율", "수수료 비율")),
    ("fee_amount", ("수수료금액", "수수료 금액", "수수료수입", "수수료 수입", "수수료")),
    ("delinquency_rate", ("연체율", "채권연체율")),
    ("delinquency_amount", ("총연체원금", "연체원금", "연체 원금", "연체금액", "연체 금액", "채권금액", "채권 금액")),
    ("decline_amount", ("하락금액", "하락 금액", "감소금액", "감소 금액", "감액금액", "감액 금액")),
    ("usage_amount", ("총이용금액", "이용금액", "이용 금액", "이용액", "사용금액", "사용 금액", "사용액")),
    # 고객식별자(기업 1곳)와 회원일련번호(카드 계약 1건)는 1:N이라 개수가 다르다.
    # 한 버킷에 두면 "기업고객 수 상위" 요청에 회원수로 정렬한 SQL이 통과한다.
    # customer_count 가 먼저 와야 "기업고객수" 가 member_count 로 새지 않는다.
    ("customer_count", ("기업고객수", "기업고객 수", "법인고객수", "법인고객 수", "고객수", "고객 수", "업체수", "업체 수", "기업수", "기업 수")),
    ("member_count", ("회원수", "회원 수", "명수")),
    ("merchant_count", ("가맹점수", "가맹점 수")),
)


def _explicit_rank_metric(question: str) -> str:
    """Return a metric explicitly attached to ``상위/하위`` wording."""
    for metric, aliases in _RANK_METRIC_ALIASES:
        for alias in aliases:
            if re.search(
                rf"{re.escape(alias)}\s*(?:기준(?:으로)?|순(?:으로)?|이|가|의|을|를)?\s*"
                rf"(?:상위|하위|TOP|BOTTOM|톱|오름차순|내림차순|낮은\s*순|높은\s*순)",
                question or "",
                re.IGNORECASE,
            ):
                return metric
    return ""


def _rank_metric(text: str) -> str:
    compact = re.sub(r"[\s_\"'.]", "", str(text or "")).lower()
    for metric, aliases in _RANK_METRIC_ALIASES:
        if any(re.sub(r"\s+", "", alias).lower() in compact for alias in aliases):
            return metric
    return ""


def _vq_row_request_is_supported(
    row_request: RowRequest,
    matched: dict,
    question: str = "",
) -> bool:
    """Return whether a VQ can preserve the requested row direction/count."""
    if row_request.mode in {"bottom", "latest", "tail"}:
        # VQs currently have fixed ORDER BY clauses and no direction parameter.
        # Reversing a LIMIT or changing only ASC/DESC would not preserve ties,
        # metric choice, or the meaning of an outer query.
        return False

    if (
        row_request.limit is not None
        and row_request.limit > 1
        and is_scalar_aggregate_query(str(matched.get("sql") or ""))
    ):
        return False

    rank_metric = _explicit_rank_metric(question)
    if row_request.mode == "top" and rank_metric:
        first_key = first_order_key(str(matched.get("sql") or ""))
        ordered_metric = _rank_metric(first_key)
        if ordered_metric != rank_metric or not re.search(r"\bDESC\b", first_key, re.IGNORECASE):
            return False
    elif row_request.mode == "top" and not rank_metric:
        if not re.search(
            r"\bDESC\b",
            first_order_key(str(matched.get("sql") or "")),
            re.IGNORECASE,
        ):
            return False
    if row_request.limit is None:
        return True

    fixed_limits = _fixed_vq_limits(str(matched.get("sql") or ""))
    parameter_defs = matched.get("parameters") if isinstance(matched.get("parameters"), dict) else {}
    limit_is_parameterized = "limit" in parameter_defs or any(
        str(spec.get("name") or "") == "limit"
        for spec in VQ_PARAM_SPECS.get(str(matched.get("name") or ""), []) or []
    )
    if fixed_limits and not limit_is_parameterized:
        return all(value == row_request.limit for value in fixed_limits)
    return True


def _verified_query_is_executable(query: dict) -> bool:
    """Keep migration/reference SQL available as documentation, not runtime templates."""
    return str(query.get("runtime_mode") or "executable").lower() != "reference_only"


_VQ_RELATIVE_NOW_RE = re.compile(r"CURRENT_TIMESTAMP|CURRENT_DATE|\bNOW\s*\(", re.IGNORECASE)


def _verified_query_serves_requested_period(question: str, query: dict) -> bool:
    """Reject a now-pinned VQ when the question names a period it cannot reach.

    시간 조건이 "지금/어제" 상대 표현뿐이고 기간 파라미터도 없는 VQ는 현재 시점
    하나만 낸다. 질문이 다른 기간을 짚었을 때 답이 되는 건 적재 정책이 그 템플릿을
    과거 월 소스로 다시 써 줄 때뿐이고, 다시 못 쓰면 어제 값이 그 달의 답으로
    나간다.

    기간을 맞춰 준다고 답이 되는 건 아니다. 축까지 맞는지는
    _verified_query_serves_named_industry 가 따로 본다.
    """
    sql = str(query.get("sql") or "")
    if not _VQ_RELATIVE_NOW_RE.search(sql):
        return True
    parameters = query.get("parameters") if isinstance(query.get("parameters"), dict) else {}
    if any(re.search(r"(?:년월|기간|시작|종료|기준일|기준년)", str(name)) for name in parameters):
        return True
    # 연·월·일을 적어 못 박은 기간만 본다. "전월 매입금액" 처럼 컬럼명에 들어 있는
    # 상대 기간어를 기간 요청으로 읽으면 VQ 자신의 원래 질문까지 거부된다.
    if not _v2_absolute_period_requested(question):
        return True
    start_ym, _ = _previous_day_archive_route(question)
    if not start_ym:
        return True
    routed = _route_verified_query_accumulation(question, str(query.get("name") or ""), sql)
    return routed.strip() != sql.strip()


def _named_industry_value(question: str) -> str:
    """질문이 값으로 짚은 업종 이름. 축 이름을 부른 것이면 빈 문자열.

    "마트업종이 보유한" 처럼 이름을 업종에 붙여 쓴 형태만 값으로 읽는다.
    "마케팅 업종", "높은 업종", "10개 업종" 처럼 띄어 쓴 앞말은 축 이름이거나
    수식어라, 값으로 읽으면 멀쩡한 매칭까지 거부하게 된다. 띄어 쓴 업종명
    ("주유 업종")은 이 게이트가 그냥 안 걸리는 쪽으로 흘린다.
    """
    if "업종" not in (question or ""):
        return ""
    axis_prefixes = {
        name.split("업종")[0]
        for name in _schema_column_names()
        if "업종" in name and name.split("업종")[0]
    }
    for match in re.finditer(r"(?P<name>[0-9A-Za-z가-힣]{2,})업종", question or ""):
        name = match.group("name")
        if any(name.endswith(prefix) for prefix in axis_prefixes):
            continue
        return name
    return ""


def _verified_query_serves_named_industry(question: str, query: dict) -> bool:
    """Reject an all-industry template when the question names one industry.

    marketing_industry_card_portfolio 는 마케팅업종대분류 전체를 GROUP BY 만 하고
    업종 필터도 파라미터도 없다. "26년 7월 마트업종이 보유한 유효기업신용카드수"
    에 이게 붙으면 마트 한 줄이 아니라 전 업종 표가 나간다. 적재 정책이 이 VQ를
    과거 월 소스로 다시 써 주기 전에는 기간 게이트가 이 사례를 대신 막고 있었다.
    """
    if not _named_industry_value(question):
        return True
    sql = str(query.get("sql") or "")
    if "업종" not in sql:
        return True
    parameters = query.get("parameters") if isinstance(query.get("parameters"), dict) else {}
    if any("업종" in str(name) for name in parameters):
        return True
    # WHERE 에 업종이 있으면 그 업종만 낼 수 있다. GROUP BY 축에만 있으면 못 한다.
    return any("업종" in region for region in _where_regions(sql))


def _verified_query_time_source_compatible(question: str, query: dict) -> bool:
    """Require a declared archive source before ranking a bounded daily-only VQ."""
    cadence, _, _ = _tbdaadt01_time_route(question)
    if cadence != "monthly" or _TBDAADT01_TABLE not in _sql_table_names(str(query.get("sql") or "")):
        return True
    historical_source = (accumulation_policy_for(_TBDAADT01_TABLE) or {}).get("historical_source")
    return bool(
        isinstance(historical_source, dict)
        and historical_source.get("table")
        and historical_source.get("query_time_dimension")
    )


# 파생 인덱스는 스키마 딕셔너리 안에 담는다.
#
# 전역 하나로 캐시하면 workflow.SCHEMA 를 바꿔 끼우는 테스트에서 앞 테스트가 채운
# 값이 그대로 남는다. id(SCHEMA) 를 키로 쓰는 것도 안 된다 — 임시 스키마가 GC 되면
# CPython 이 그 id 를 재사용해서 엉뚱한 스키마의 인덱스를 돌려준다. 실제로 파일 하나만
# 돌리면 통과하고 전체 스위트에서는 깨지는 순서 의존이 났다.
_COLUMN_NAMES_KEY = "_v2_column_names_cache"
_TABLE_COLUMN_INDEX_KEY = "_v2_table_column_index_cache"


def _schema_column_names() -> frozenset[str]:
    """Every dimension/measure column name, for VQ output-coverage checks."""
    cached = SCHEMA.get(_COLUMN_NAMES_KEY)
    if cached is None:
        cached = frozenset(
            str(column.get("name") or "")
            for table in SCHEMA.get("tables", [])
            for section in ("dimensions", "measures")
            for column in (table.get(section) or [])
            if column.get("name")
        )
        SCHEMA[_COLUMN_NAMES_KEY] = cached
    return cached


def _table_column_index() -> tuple[dict[str, set[str]], dict[str, list[str]]]:
    """(테이블 → 컬럼 집합, 컬럼 → 그 컬럼을 가진 테이블 목록)."""
    cached = SCHEMA.get(_TABLE_COLUMN_INDEX_KEY)
    if cached is None:
        by_table: dict[str, set[str]] = {}
        by_column: dict[str, list[str]] = {}
        for table in SCHEMA.get("tables", []):
            name = str(table.get("name") or "")
            if not name:
                continue
            columns: set[str] = set()
            for section in ("dimensions", "measures", "time_dimensions"):
                for column in table.get(section) or []:
                    column_name = str(column.get("name") or "")
                    if not column_name:
                        continue
                    columns.add(column_name)
                    by_column.setdefault(column_name, []).append(name)
            by_table[name] = columns
        cached = (by_table, by_column)
        SCHEMA[_TABLE_COLUMN_INDEX_KEY] = cached
    return cached


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


def _match_vq_by_semantic_contract(
    question: str,
    *,
    intent_question: str = "",
) -> dict | None:
    """Select a VQ through reusable semantic-layer intent contracts."""
    contracts = semantic_query_contract_candidates(SCHEMA, question, max_count=1)
    if not contracts:
        return None
    contract = contracts[0]
    if str(contract.get("support_status") or "supported").lower().startswith("blocked"):
        return None
    queries_by_name = {str(vq.get("name") or ""): vq for vq in VERIFIED_QUERIES}
    matched = queries_by_name.get(str(contract.get("verified_query") or ""))
    if not matched or not _verified_query_matches_intent(
        intent_question or question,
        matched,
        contract_question=question,
    ):
        return None
    return {
        "matched_query_name": matched["name"],
        "matched_query_sql": matched["sql"].strip(),
        "matched_query_params": matched.get("parameters", {}),
    }


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
        if _verified_query_time_source_compatible(question, vq)
        if (score := _score_vq_candidate(question, vq)) >= VERIFIED_QUERY_MIN_LEXICAL_SCORE
    ]
    scored.sort(key=lambda item: (item[0], item[1].get("name", "")), reverse=True)
    return scored


def _rank_vq_candidates(question: str) -> list[dict]:
    scored = _rank_vq_candidate_scores(question)
    return [vq for _, vq in scored[:VERIFIED_QUERY_LLM_CANDIDATE_LIMIT]]


def _match_vq_by_rules(question: str, *, intent_question: str = "") -> dict | None:
    scored = _rank_vq_candidate_scores(question)
    if not scored:
        return None
    top_score, matched = scored[0]
    if top_score < VERIFIED_QUERY_RULE_MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and top_score - scored[1][0] < VERIFIED_QUERY_RULE_MATCH_MARGIN:
        return None
    if not _verified_query_matches_intent(
        intent_question or question,
        matched,
        contract_question=question,
    ):
        return None
    return {
        "matched_query_name": matched["name"],
        "matched_query_sql": matched["sql"].strip(),
        "matched_query_params": matched.get("parameters", {}),
    }


def _match_vq_by_llm(question: str, *, intent_question: str = "") -> dict:
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
2. 요청한 지표, 결과 단위, 필터, 정렬 방향, 행 개수를 해당 쿼리와 파라미터가 모두 표현할 수 있어야 합니다.
3. 추가 조건 하나라도 표현할 수 없거나 결과 단위가 다르면 NONE입니다.
4. 의도가 다르면 NONE입니다.

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
    if not _verified_query_matches_intent(
        intent_question or question,
        matched,
        contract_question=question,
    ):
        return {"matched_query_name": ""}
    return {
        "matched_query_name": matched["name"],
        "matched_query_sql": matched["sql"].strip(),
        "matched_query_params": matched.get("parameters", {}),
    }


def _select_verified_query_capability(question: str, state: Text2SQLState | dict) -> dict | None:
    if not ENABLE_VERIFIED_QUERY_MATCHING or state.get("skip_verified_query_matching"):
        return None
    retrieval_question = _retrieval_question({**state, "question": question})
    contracts = semantic_query_contract_candidates(SCHEMA, retrieval_question, max_count=1)
    if contracts and str(contracts[0].get("execution_mode") or "").lower() == "semantic_generation":
        return None

    def capability(result: dict | None) -> dict | None:
        if not result:
            return None
        matched = next(
            (vq for vq in VERIFIED_QUERIES if vq.get("name") == result.get("matched_query_name")),
            None,
        )
        if matched and not _vq_matches_selected_domain(matched, str(state.get("selected_domain") or "")):
            return None
        result = dict(result)
        result["matched_query_sql"] = _route_verified_query_accumulation(
            retrieval_question,
            str(result.get("matched_query_name") or ""),
            str(result.get("matched_query_sql") or ""),
        )
        return _verified_query_capability_result(result)

    result = _match_vq_by_semantic_contract(retrieval_question, intent_question=question)
    if result:
        return capability(result)

    result = _match_vq_by_embedding(retrieval_question, intent_question=question)
    if result:
        return capability(result)

    result = _match_vq_by_rules(retrieval_question, intent_question=question)
    if result:
        return capability(result)

    if ENABLE_VERIFIED_QUERY_LLM_FALLBACK:
        result = _match_vq_by_llm(retrieval_question, intent_question=question)
        if result.get("matched_query_name"):
            return capability(result)
    return None


def _vq_matches_selected_domain(matched: dict, selected_domain: str) -> bool:
    """Use the routed domain as a second guard for unbound lexical VQs."""
    if not selected_domain:
        return True
    matched_name = str(matched.get("name") or "")
    if any(
        str(contract.get("verified_query") or "") == matched_name
        for contract in SCHEMA.get("semantic_query_contracts", [])
    ):
        return True
    explicit_domains = matched.get("domains", matched.get("domain", []))
    if not explicit_domains:
        # Inferred table-domain membership is incomplete for legacy VQs and
        # can reject exact intents such as overseas card usage.  Keep domain
        # enforcement authoritative only where the VQ declares its domain.
        return True
    return _entry_matches_domain(
        SCHEMA,
        matched,
        selected_domain,
        _sql_table_names(str(matched.get("sql") or "")),
    )


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
    routing_question = _retrieval_question(state)
    row_request = parse_row_request(question)
    vq_name = state.get("matched_query_name", "")
    vq_params_def = state.get("matched_query_params", {})
    pristine_vq = next(
        (query for query in VERIFIED_QUERIES if str(query.get("name") or "") == vq_name),
        None,
    )
    base_sql = str((pristine_vq or {}).get("sql") or state["matched_query_sql"])
    base_sql = _route_verified_query_accumulation(routing_question, vq_name, base_sql)
    base_specs = VQ_PARAM_SPECS.get(vq_name)
    if base_specs is None:
        base_specs = [] if vq_params_def else [
            {"name": "기간_시작", "type": "string", "description": "조회 시작 기준년월 (YYYYMM)"},
            {"name": "기간_종료", "type": "string", "description": "조회 종료 기준년월 (YYYYMM)"},
            {"name": "기업명", "type": "string", "description": "기업/상호명 (부분일치)"},
            {"name": "가맹점명", "type": "string", "description": "가맹점명 (부분일치)"},
            {"name": "업종", "type": "string", "description": "업종명/업종 대분류 (부분일치)"},
            {"name": "limit", "type": "integer", "description": "결과 행수 제한"},
        ]
    param_specs = [dict(spec) for spec in base_specs]
    for pname, pinfo in vq_params_def.items():
        info = pinfo if isinstance(pinfo, dict) else {}
        existing = next((spec for spec in param_specs if spec["name"] == pname), None)
        if existing is None:
            param_specs.append({"name": pname, **info})
        else:
            for key, value in info.items():
                existing.setdefault(key, value)
    if row_request.limit is not None and not any(spec.get("name") == "limit" for spec in param_specs):
        # An explicit result count is a request constraint, not optional VQ
        # metadata.  The capability gate has already rejected incompatible
        # fixed-cardinality/direction templates.
        param_specs.append(
            {"name": "limit", "type": "integer", "description": "사용자가 요청한 결과 행수 제한"}
        )
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
3. 기간 길이가 없는 단독 "최근", "최근 기준", "이번달", "이번 월"만 현재월 1개월로 해석 → 기준년월/기간_시작/기간_종료 모두 "{_current_ym()}"
4. "저번달", "지난달", 조회시점을 뜻하는 단독 "전월"은 지난달 1개월로 해석 → 기준년월/기간_시작/기간_종료 모두 "{_previous_ym()}"
   단, "전월 대비", "전월비", "전월과 비교"의 전월은 비교 기준이므로 조회기간 파라미터로 추출하지 마세요.
5. "최근 N개월/N달"은 기준년월을 종료월로 보고 N개월 구간으로 해석합니다.
6. "상위/앞/최근 N개", "N개만" → limit: N. "최근 N개월"은 기간이며 limit이 아닙니다.
7. "개인카드 미보유" → 보유구분:"개인카드미보유", "기업카드 미보유/법인카드 미보유" → 보유구분:"기업카드미보유"
8. 이름 검색은 기본 부분일치입니다. "이름 고정", "이름만으로", "정확 일치"를 명시한 경우에만 이름정확일치:true로 추출하세요.
9. JSON만 반환하세요. 없으면 빈 오브젝트 {{}}.

## 사용자 질문
{question}

JSON:"""
    rule_params = _extract_params_by_rule(question, param_specs, _sql_table_names(base_sql))
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
    if row_request.limit is not None and any(spec.get("name") == "limit" for spec in param_specs):
        # Deterministic wording wins over a model omission or an unrelated
        # number selected by the parameter extractor.
        extracted["limit"] = min(row_request.limit, MAX_QUERY_ROW_LIMIT)
    if any(str(spec.get("name") or "") in _ENTITY_NAME_PARAM_NAMES for spec in param_specs):
        if _is_exact_name_match_requested(question):
            extracted["이름정확일치"] = True
        else:
            extracted.pop("이름정확일치", None)
    current_d1_basis = any(
        (accumulation_policy_for(table_name) or {}).get("cadence") == "previous_day"
        and _has_exact_table_axis_value(
            base_sql,
            table_name,
            str((accumulation_policy_for(table_name) or {}).get("query_time_dimension") or ""),
            previous_day_ymd(),
        )
        for table_name in _sql_table_names(base_sql)
    )
    explicit_current_request = bool(
        re.search(r"현재|현시점|오늘|전일|어제|최신|지금", routing_question or "")
    ) or (state.get("user_provided_params", {}) or {}).get("_cadence_hint") == "current"
    default_current_vqs = {"corporate_limit_status_at_month"}
    if (
        "기준년월" in vq_params_def
        and current_d1_basis
        and (explicit_current_request or vq_name in default_current_vqs)
    ):
        # The routed SQL already fixes the live population to KST D-1; its
        # month, not a separate history/fact period, anchors the current state.
        extracted["기준년월"] = _current_ym()
    elif "기준년월" in vq_params_def and not extracted.get("기준년월"):
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
    if extracted.get("기준년월"):
        basis_question = f'{extracted["기준년월"]} 기준'
        if (
            current_d1_basis
            and str(extracted["기준년월"]) == _current_ym()
            and (explicit_current_request or vq_name in default_current_vqs)
        ):
            basis_question = "현재"
        base_sql = _route_verified_query_accumulation(
            basis_question,
            vq_name,
            str((pristine_vq or {}).get("sql") or base_sql),
        )
    missing = _missing_vq_required_params(vq_params_def, extracted)
    if missing:
        provided_params = dict(extracted)
        if (state.get("user_provided_params", {}) or {}).get("_cadence_hint"):
            provided_params["_cadence_hint"] = (
                state.get("user_provided_params", {}) or {}
            )["_cadence_hint"]
        return {
            "extracted_params": extracted,
            "user_provided_params": provided_params,
            "param_stage": "need_params",
            "missing_params": missing,
        }
    final_sql = _apply_params_to_vq(base_sql, extracted, vq_name, vq_params_def)
    final_sql = _apply_name_filter_mode(question, final_sql)
    formatted_sql = sqlparse.format(final_sql, reindent=True, keyword_case="upper")
    return {"extracted_params": extracted, "param_stage": "done", "missing_params": [], "final_sql": prepare_sql_for_backend(formatted_sql)}


def run_matched_query(state: Text2SQLState) -> dict:
    sql = state.get("final_sql", "")
    sql = _apply_accumulation_historical_sources(_retrieval_question(state), sql)
    prepared_sql = prepare_sql_for_backend(sql)
    availability_error = _availability_execution_error(state, prepared_sql)
    if availability_error:
        return {
            "query_columns": [],
            "query_rows": [],
            "query_error": f"테이블 적재 주기 위반: {availability_error}",
            "validation_result": f"Verified Query 적재 주기 위반: {availability_error}",
            "is_valid": False,
            "retry_count": state.get("retry_count", 0) + 1,
            "final_sql": prepared_sql,
        }
    columns, rows, error = execute_sql(
        prepared_sql,
        max_rows=DEFAULT_FETCH_ROW_LIMIT,
        allow_cross_cycle_fallback=_allow_cross_cycle_fallback(
            state.get("question", ""), prepared_sql
        ),
    )
    if error:
        retry = state.get("retry_count", 0) + 1
        return {
            "query_columns": [],
            "query_rows": [],
            "query_error": error,
            "validation_result": f"Verified Query DB 실행 오류: {error}",
            "is_valid": False,
            "retry_count": retry,
            "final_sql": prepared_sql,
        }
    return _executed_query_state(prepared_sql, columns, rows)


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
    metrics = _route_merchant_time_context(
        question, build_metrics_summary(SCHEMA, question, selected_domain)
    )
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    references = _route_merchant_time_context(
        question,
        find_relevant_references(SCHEMA, question, domain_name=selected_domain),
    )
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


def policy_refusal(_: Text2SQLState) -> dict:
    return {"answer": SAFETY_REFUSAL}


_GENERIC_TABLE_TERMS = {"기준년월", "기준년월일", "고객식별자", "회원일련번호", "금액", "건수"}

# 이 개수 이하의 테이블만 가진 컬럼은 그 테이블을 특정하는 단서로 본다.
_DISTINCTIVE_OWNER_LIMIT = 3
_DISTINCTIVE_WEIGHT = 12
_DISTINCTIVE_CAP = 1

# "지표 X를 축 Y별로" 질문은 X와 Y를 함께 가진 테이블 하나로 답한다. 컬럼 겹침
# 점수는 min(hits, 4) * 3 이라서 둘 다 가진 테이블(6점)과 하나만 가진 테이블(3점)의
# 차이가 semantic attribute 한 건(36점)에 묻혔다. "법인카드 유이자 할부 이용금액을
# 카드거래매체별로" 는 두 컬럼을 함께 가진 유일한 테이블 tmdaa3e16 이 3순위였고,
# "법인카드" 라는 낱말로 걸린 가맹점 월 enrichment 가 1순위였다.
_COVERAGE_MIN_COLUMNS = 2
_COVERAGE_WEIGHT = 40

# 위 규칙은 축이 함께 잡힐 때만 듣는다. 축 없이 지표만 묻는 질문("기업카드 할부
# 이용금액은?")은 컬럼이 하나만 걸려서 겹침 3점 + 희소 컬럼 12점이 전부이고,
# 그 지표를 가진 유일한 테이블 tmdaa3e16 이 컬럼을 하나도 못 맞춘
# corporate_card_holding 원천(36점)에 밀려 후보에서 잘려 나갔다. semantic attribute
# 는 모집단을 좁히는 축이므로, 질문의 지표를 갖고 있지 않으면 1순위가 아니라
# 조인 상대다.
_SOLE_MEASURE_OWNER_WEIGHT = 40

# 원천을 한 곳만 선언한 semantic attribute 가 1순위로 걸렸을 때 그 원천에 얹는 가점.
# 컬럼명을 그대로 부른 증거(+40)와 같은 급으로 둔다.
_SINGLE_SOURCE_ATTRIBUTE_WEIGHT = 40

# 표면형이 정확히 맞지 않고 낱말 겹침만으로 걸린 query_reference 의 1순위 테이블 가점.
# 컬럼 두 개 겹침(min(hits,4)*3)과 같은 급이다. goldenset v3 500건에서 12 → 6 으로
# 내리면 1순위 적중이 328 → 334 로 오르고 후보 적중은 그대로다.
_FUZZY_REFERENCE_WEIGHT = 6


def _is_semantic_table_visible(table: dict) -> bool:
    return str(table.get("semantic_visibility") or "default").lower() != "restricted"


def _visible_table_columns(table: dict, section: str) -> list[dict]:
    """프롬프트에 실을 수 있는 컬럼.

    민감 컬럼 판정은 schema._is_restricted_column 한 곳에서만 한다. 여기에 같은
    패턴을 복사해 두었더니 사업장 주소 예외(가맹점상세주소 등)가 schema 쪽에만
    들어가, "가맹점 도로명 주소" 질문의 프롬프트에서 주소 본문 컬럼이 통째로
    빠졌다. 답할 컬럼이 없으니 모델은 도로명 번호 조각만 보고 답을 못 냈다.
    """
    return [
        column
        for column in table.get(section, [])
        if not _is_restricted_column(table, column.get("name"))
    ]


def _phrase_in_question(question: str, phrase: object) -> bool:
    return _phrase_in_text(question, phrase)


def _contract_source_tables(question: str, contract: dict) -> list[str]:
    return source_tables_for_question(contract, question)


def _requested_period_is_open_month(question: str) -> bool:
    """Whether the question asks only about the month the run date sits in."""
    start_ym, end_ym, explicit_day = _extract_period_by_rule(question)
    current_ym = _current_ym()
    if explicit_day:
        return explicit_day[:6] == current_ym
    return bool(start_ym) and start_ym == end_ym == current_ym


def _mask_attribute_column_names(question: str, attribute: dict) -> str:
    """Blank out the attribute's own column names before reading a period out of them.

    ``전년가맹점신용판매매출금액`` 은 가맹점기본이 들고 있는 컬럼 이름이다. 질문이
    그 이름을 그대로 부르면 '전년' 은 기간 조건이 아니라 이름의 한 토막인데,
    기간으로 읽혀서 "전년 가맹점 신용판매 매출금액" 질문이 이번 달을 들고 있는
    tbdaadt01 대신 월 실적(tmdaa5e11)의 지난 달 행으로 갔다.
    """
    text = question or ""
    for mapping in attribute.get("source_mappings", []):
        names = list(mapping.get("columns") or [])
        if mapping.get("column"):
            names.append(mapping["column"])
        for name in names:
            characters = [char for char in str(name) if not char.isspace()]
            if len(characters) < 4:
                continue
            text = re.sub(
                r"\s*".join(re.escape(char) for char in characters), " ", text
            )
    return text


@lru_cache(maxsize=512)
def _question_named_columns_of(
    question: str, table_name: str, section: str
) -> frozenset[str]:
    """질문이 이름이나 동의어로 부른 그 테이블의 컬럼."""
    table = next(
        (
            item
            for item in SCHEMA.get("tables", [])
            if str(item.get("name") or "").rsplit(".", 1)[-1] == table_name
        ),
        None,
    )
    if table is None:
        return frozenset()
    return frozenset(
        str(column.get("name") or "")
        for column in _visible_table_columns(table, section)
        if any(
            str(term or "") not in _GENERIC_TABLE_TERMS
            and _phrase_in_question(question, term)
            for term in [column.get("name"), *(column.get("synonyms") or [])]
        )
    )


def _question_names_measure_outside(question: str, tables: frozenset[str]) -> bool:
    """질문이 부른 지표 컬럼을 이 테이블들이 못 갖고 있는지.

    행을 세는 질의(좌수)는 지표 컬럼이 필요 없다. 반대로 질문이 어딘가의 지표 컬럼을
    이름으로 불렀다면 그 컬럼을 든 테이블이 답할 곳이다 — "기업카드 체크카드 이용금액을
    발급유형별로" 는 두 컬럼을 함께 든 카드 월실적(tmdaa3e16)의 질문이고, 발급유형구분코드가
    회원카드기본에도 있다는 사실은 근거가 못 된다.
    """
    owned = {name for table in tables for name in _question_named_columns_of(question, table, "measures")}
    for table in SCHEMA.get("tables", []):
        if not _is_semantic_table_visible(table):
            continue
        name = str(table.get("name") or "").rsplit(".", 1)[-1]
        if name in tables:
            continue
        if _question_named_columns_of(question, name, "measures") - owned:
            return True
    return False


def _axis_lives_only_in(question: str, axis: list[dict], others: list[dict]) -> bool:
    """질문의 분류축을 집계 원천은 못 갖고 있고 낟알 원천만 갖고 있는지.

    기업고객 스냅샷의 유효기업신용카드수는 기업고객당 이미 더해 놓은 수다. 카드
    등급그룹·브랜드·발급유형처럼 카드 한 장의 속성으로 쪼개는 질문은 그 축이 스냅샷에
    아예 없어서, 조인해도 좌수가 부풀려지고 답이 나오지 않는다. 그런 축을 카드 단위
    원천만 갖고 있으면 그 원천 하나로 답할 질문이다.
    """

    def tables(mappings: list[dict]) -> frozenset[str]:
        return frozenset(
            str(mapping.get("table") or "").rsplit(".", 1)[-1]
            for mapping in mappings
            if mapping.get("table")
        )

    def named(names: frozenset[str]) -> set[str]:
        return {
            column
            for table in names
            for column in _question_named_columns_of(question, table, "dimensions")
        }

    axis_tables = tables(axis)
    if not (named(axis_tables) - named(tables(others))):
        return False
    return not _question_names_measure_outside(question, axis_tables)


def _attribute_source_mappings(question: str, attribute: dict) -> list[dict]:
    """Choose current or historical mappings from a declarative attribute policy."""
    mappings = list(attribute.get("source_mappings", []))
    policy = attribute.get("source_selection")
    if not isinstance(policy, dict):
        return mappings

    # 시간축(현재/과거 월)과 나란한 세 번째 갈림길. 질문이 부른 축을 집계 원천이
    # 갖고 있지 않으면 그 원천은 조인 상대도 못 된다.
    axis_prefix = str(policy.get("axis_role_prefix") or "")
    if axis_prefix:
        axis = [
            mapping
            for mapping in mappings
            if str(mapping.get("role") or "").startswith(axis_prefix)
        ]
        others = [mapping for mapping in mappings if mapping not in axis]
        if axis and _axis_lives_only_in(question, axis, others):
            return axis

    q_compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", (question or "").lower())
    dated = _mask_attribute_column_names(question, attribute)
    start_ym, end_ym, explicit_day = _extract_period_by_rule(dated)
    historical_shape = _has_historical_period_expression(dated)
    has_period = bool(start_ym or end_ym or explicit_day or historical_shape)
    current_terms = [
        re.sub(r"[^0-9A-Za-z가-힣_]", "", str(term or "").lower())
        for term in policy.get("current_terms", [])
    ]
    attribute_cues = [
        re.sub(r"[^0-9A-Za-z가-힣_]", "", str(term or "").lower())
        for term in [
            attribute.get("korean_name", ""),
            attribute.get("parameter_name", ""),
            *attribute.get("aliases", []),
        ]
    ]
    date_scope_tail = re.compile(
        r"(?:20\d{2}년(?:\d{1,2}월)?|20\d{4}(?:\d{2})?|\d{1,2}월)(?:기준|의)?$"
    )
    explicitly_current_attribute = False
    for current in current_terms:
        for cue in attribute_cues:
            if not current or not cue:
                continue
            for match in re.finditer(
                rf"{re.escape(current)}(?:기준|의)?{re.escape(cue)}",
                q_compact,
            ):
                if not date_scope_tail.search(q_compact[: match.start()]):
                    explicitly_current_attribute = True
                    break
            if explicitly_current_attribute or re.search(
                rf"{re.escape(cue)}(?:은|는|이|가|을|를)?{re.escape(current)}",
                q_compact,
            ):
                explicitly_current_attribute = True
                break
        if explicitly_current_attribute:
            break
    generic_current = any(current and current in q_compact for current in current_terms)
    compares_current_and_period = bool(
        has_period
        and any(current and current in q_compact for current in current_terms)
        and re.search(r"비교|대비|변화|변동|추이", question or "")
    )
    if compares_current_and_period:
        return mappings
    use_period = has_period and not (
        explicitly_current_attribute or (generic_current and not historical_shape)
    )
    # 월 실적·월 스냅샷은 그 달이 닫힌 뒤에 적재된다. 요청 월이 실행일이 속한 달이면
    # 월 원천에 아직 그 달 행이 없으므로, 열린 달을 이미 들고 있는 일 적재 원천을 쓴다.
    # "2026년 8월"처럼 이번 달을 날짜로 쓴 질문이 여기 걸린다.
    if use_period and policy.get("open_month_uses_current") and _requested_period_is_open_month(question):
        use_period = False
    prefix_key = "period_role_prefix" if use_period else "default_role_prefix"
    prefix = str(policy.get(prefix_key) or "")
    preferred = [
        mapping
        for mapping in mappings
        if prefix and str(mapping.get("role") or "").startswith(prefix)
    ]
    return preferred or mappings


def _attribute_snapshot_exclusions(question: str) -> set[str]:
    """Return alternate snapshot tables forbidden by a matched attribute policy."""
    # 이름이 required 인 계약은 이름 없는 질문의 근거가 못 된다. 규칙 랭킹과 VQ 선택은
    # 이미 그 가드를 갖고 있는데 여기만 빠져 있어서, 이름 없는 "오늘 기준 가맹점 신용판매
    # 매출금액" 이 계약 하나로 이 배제를 통째로 껐다. 그러면 이번 달을 아직 담지 못한
    # 월 실적(tmdaa5e11)이 후보에 그대로 남는다.
    if any(
        str(contract.get("table_selection_mode") or "").lower() == "authoritative"
        and _contract_entity_bindings_available(question, contract)
        for contract in semantic_query_contract_candidates(SCHEMA, question, max_count=2)
    ):
        return set()

    preferred_tables: set[str] = set()
    alternate_tables: set[str] = set()
    for attribute in semantic_attribute_candidates(SCHEMA, question, max_count=6):
        policy = attribute.get("source_selection")
        if not isinstance(policy, dict):
            continue
        # 축 원천은 스냅샷의 대안이 아니라 다른 낟알이다. 일/월 스냅샷처럼 서로를
        # 배제하는 짝이 아니므로, 축 규칙이 걸리지 않았다고 해서 후보에서 빼지 않는다.
        # 빼면 "기업카드 좌수" 처럼 축 없는 질문에서 카드 마스터가 사라진다.
        axis_prefix = str(policy.get("axis_role_prefix") or "")
        all_tables = {
            str(mapping.get("table") or "").rsplit(".", 1)[-1]
            for mapping in attribute.get("source_mappings", [])
            if mapping.get("table")
            and not (
                axis_prefix and str(mapping.get("role") or "").startswith(axis_prefix)
            )
        }
        selected_tables = {
            str(mapping.get("table") or "").rsplit(".", 1)[-1]
            for mapping in _attribute_source_mappings(question, attribute)
            if mapping.get("table")
        }
        preferred_tables.update(selected_tables)
        alternate_tables.update(all_tables - selected_tables)
    return alternate_tables - preferred_tables


def _contract_entity_bindings_available(question: str, contract: dict) -> bool:
    """Whether an authoritative contract's required entity inputs are in the question.

    ``named_corporate_limit_status_at_month`` 은 기업명이 required 인데
    match.required 가 ["한도"] 하나뿐이라, 이름 없는 "현재 기준 CA한도별 CA한도금액"
    같은 질문까지 잡아 tbdaa1d12 를 authoritative 로 못박았다. 그러면 아래 점수
    계산이 통째로 생략돼 CA한도금액을 실제로 가진 tbdaaat03 은 후보에도 못 든다.
    goldenset v2 easy 실패 6건이 전부 이 조기 반환이었다.
    """
    bound_vq = next(
        (
            vq
            for vq in VERIFIED_QUERIES
            if str(vq.get("name") or "") == str(contract.get("verified_query") or "")
        ),
        None,
    )
    return _semantic_contract_bindings_satisfied(question, contract, bound_vq or {})


# 적재 정책이 이미 짝을 들고 있다. 여기에 목록을 또 두면 새 짝을 등록할 때
# 한쪽만 고쳐 두 곳이 어긋난다.
_ARCHIVE_SNAPSHOT_TABLES = frozenset(
    str(policy["historical_source"]["table"]).lower()
    for policy in TABLE_ACCUMULATION_POLICIES.values()
    if isinstance(policy.get("historical_source"), dict)
)


def _route_accumulation_table_names(
    question: str,
    table_names: list[str],
    contract: dict | None = None,
) -> list[str]:
    """Resolve bounded historical sources before SQL generation."""
    cadence, _, _ = _tbdaadt01_time_route(question)
    historical_source = (accumulation_policy_for(_TBDAADT01_TABLE) or {}).get(
        "historical_source"
    )
    archive_table = (
        str(historical_source.get("table") or "")
        if cadence == "monthly" and isinstance(historical_source, dict)
        else ""
    )
    # 반대 방향도 같은 곳에서 처리한다. 규칙 랭킹이 마스터를 1순위로 올려도
    # analyze_question 은 LLM 이 고른 목록을 그대로 쓰고(rule_tables 는 파싱
    # 실패용 폴백일 뿐), "2026년 5월" 이 붙은 질문에서 LLM 은 월 스냅샷을 고른다.
    # 치환을 한 방향으로만 두면 마스터 속성 질문이 여기서 도로 스냅샷이 된다.
    master_archive = (
        str(historical_source.get("table") or "").lower()
        if cadence == "master" and isinstance(historical_source, dict)
        else ""
    )
    routed: list[str] = []
    historical_start, _ = _previous_day_archive_route(question)
    verified_tables: set[str] = set()
    policy_tables = {
        str(table).rsplit(".", 1)[-1].lower()
        for table in (_contract_source_tables(question, contract) if contract else [])
    }
    if contract:
        verified_query_name = str(contract.get("verified_query") or "")
        verified_query = next(
            (
                query
                for query in VERIFIED_QUERIES
                if str(query.get("name") or "") == verified_query_name
            ),
            None,
        )
        if verified_query:
            routed_verified_sql = _route_verified_query_accumulation(
                question,
                verified_query_name,
                str(verified_query.get("sql") or ""),
            )
            verified_tables = {
                str(table).rsplit(".", 1)[-1].lower()
                for table in _sql_table_names(routed_verified_sql)
            }

    def add(name: str) -> None:
        if name and name not in routed:
            routed.append(name)

    for raw_name in table_names:
        name = str(raw_name or "").rsplit(".", 1)[-1]
        if archive_table and name.lower() == _TBDAADT01_TABLE:
            name = archive_table
        elif master_archive and name.lower() == master_archive:
            name = _TBDAADT01_TABLE
        elif historical_start:
            monthly_name = _PREVIOUS_DAY_ARCHIVE_TABLES.get(name.lower())
            if (
                monthly_name
                and name.lower() in policy_tables
                and monthly_name.lower() in policy_tables
            ):
                add(name)
                continue
            if monthly_name and verified_tables:
                # A verified composition is authoritative about whether the
                # live D-1 population, its monthly history, or both are needed.
                if name.lower() in verified_tables:
                    add(name)
                if monthly_name.lower() in verified_tables:
                    add(monthly_name)
                if name.lower() in verified_tables or monthly_name.lower() in verified_tables:
                    continue
            name = monthly_name or name
        add(name)
    if master_archive:
        # 같은 엔티티의 월 스냅샷은 마스터가 답할 질문에 보탤 것이 없다. 주소를
        # 물었는데 주소 컬럼이 아예 없는 tmdaaus01 이 후보로 남아 SQL 에 섞였다.
        # 월 "팩트"(tmdaa5e11 등)는 다른 엔티티이므로 걷어내지 않는다.
        routed = [name for name in routed if name.lower() not in _ARCHIVE_SNAPSHOT_TABLES]
        if _TBDAADT01_TABLE not in routed:
            # 마스터도 그 짝도 고르지 않았다면 답할 곳이 아예 없다.
            routed.insert(0, _TBDAADT01_TABLE)
    return routed


def _route_merchant_time_context(question: str, context: str) -> str:
    """Align metric/reference prompt text with preselected archive tables."""
    rendered = str(context or "")
    historical_start, _ = _previous_day_archive_route(question)
    if historical_start:
        matched_contracts = semantic_query_contract_candidates(
            SCHEMA, question, max_count=2
        )
        corporate_sources = {
            str(table).rsplit(".", 1)[-1].lower()
            for contract in matched_contracts
            for table in _contract_source_tables(question, contract)
        }
        preserve_live_corporate_source = {
            "tbdaa1d12",
            "tmdaa1d12",
        }.issubset(corporate_sources)
        for source_table, target_table in _PREVIOUS_DAY_ARCHIVE_TABLES.items():
            if source_table == "tbdaa1d12" and preserve_live_corporate_source:
                continue
            rendered = re.sub(
                rf'(?<![A-Za-z0-9_]){re.escape(source_table)}(?![A-Za-z0-9_])',
                target_table,
                rendered,
                flags=re.IGNORECASE,
            )
        rendered = rendered.replace("SUBSTR(기준년월일,1,6)", "기준년월")
    cadence, _, _ = _tbdaadt01_time_route(question)
    if cadence != "monthly":
        return rendered
    return re.sub(
        rf'(?<![A-Za-z0-9_]){re.escape(_TBDAADT01_TABLE)}(?![A-Za-z0-9_])',
        "tmdaa5d01",
        rendered,
        flags=re.IGNORECASE,
    ).replace(_TBDAADT01_TIME_COLUMN, "기준년월")


def _rule_rank_tables(question: str, max_tables: int = 4) -> list[str]:
    """Rank schema tables using explicit semantic-layer evidence only."""
    q_compact = re.sub(r"[^0-9A-Za-z가-힣_]", "", (question or "").lower())
    matched_contracts = semantic_query_contract_candidates(SCHEMA, question, max_count=2)
    for contract in matched_contracts:
        if str(contract.get("table_selection_mode") or "").lower() != "authoritative":
            continue
        if not _contract_entity_bindings_available(question, contract):
            continue
        selected = []
        for table_name in _contract_source_tables(question, contract):
            name = str(table_name or "").rsplit(".", 1)[-1]
            if name and name not in selected:
                selected.append(name)
        if selected:
            return _route_accumulation_table_names(
                question, selected, contract=contract
            )[:max_tables]

    scores: dict[str, int] = {}
    order: dict[str, int] = {}
    named_columns = set(_v2_named_columns(question, _schema_column_names()))
    _, column_owners = _table_column_index()

    def add(table_name: object, score: int) -> None:
        name = str(table_name or "").rsplit(".", 1)[-1]
        if not name:
            return
        scores[name] = scores.get(name, 0) + score
        order.setdefault(name, len(order))

    for metric in SCHEMA.get("canonical_metrics", []):
        terms = [metric.get("name", ""), *metric.get("synonyms", [])]
        if any(_phrase_in_question(question, term) for term in terms):
            add(metric.get("source_table"), 20)

    for position, attribute in enumerate(
        semantic_attribute_candidates(SCHEMA, question, max_count=6)
    ):
        attribute_score = 36 if position == 0 else max(3, 18 - position * 3)
        mappings = _attribute_source_mappings(question, attribute)
        # 원천을 한 곳만 선언한 속성은 "이 값은 거기서만 읽는다" 는 선언이다. 다른
        # 테이블이 같은 값을 제 이름으로 복사해 두고 있으면(전표의 가맹점우편번호),
        # 질문이 컬럼명을 그대로 불렀다는 가점(+40)이 사본 쪽에 붙어 선언을 이긴다.
        if position == 0 and len({
            str(mapping.get("table") or "").rsplit(".", 1)[-1]
            for mapping in mappings
            if mapping.get("table")
        }) == 1:
            attribute_score += _SINGLE_SOURCE_ATTRIBUTE_WEIGHT
        for mapping in mappings:
            add(mapping.get("table"), attribute_score)

    for contract in matched_contracts:
        # 이름이 required 인 계약은 이름 없는 질문의 근거가 못 된다. authoritative
        # 경로에는 이 가드가 있는데 점수 경로에는 없어서, "26년 7월 9일 하루 기준
        # 기업신용매출건수" 가 named_merchant_monthly_sales 의 +28 로 가맹점 월매출
        # (tmdaa5e11) 을 1순위로 받았다.
        if not _contract_entity_bindings_available(question, contract):
            continue
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
        exact_matches = sum(
            1
            for phrase in ref.get("when_user_says", [])
            if _phrase_in_question(question, phrase)
        )
        score = sum(1 for token in tokens if token in q_compact) + 4 * exact_matches
        references.append((exact_matches, score, ref))
    references.sort(key=lambda item: (item[0], item[1]), reverse=True)
    reference_snapshot_exclusions: set[str] = set()
    reference_routed_sources: set[str] = set()
    reference_routing_entry: dict | None = None
    if references and references[0][1] >= 2:
        exact_matches, best_score, best_ref = references[0]
        if isinstance(best_ref.get("source_table_policy"), dict):
            reference_routing_entry = best_ref
        declared_sources = {
            str(table).rsplit(".", 1)[-1]
            for table in source_tables_for_question(best_ref, question)
        }
        governed_sources = {
            str(table).rsplit(".", 1)[-1]
            for table in (best_ref.get("source_table_policy") or {}).get("default", [])
        }
        governed_sources.update(
            str(table).rsplit(".", 1)[-1]
            for table in (best_ref.get("source_table_policy") or {}).get("period_snapshot", [])
        )
        if str(best_ref.get("verified_query") or "") == "corporate_target_industry_usage":
            target_vq = next(
                (
                    query
                    for query in VERIFIED_QUERIES
                    if str(query.get("name") or "") == "corporate_target_industry_usage"
                ),
                None,
            )
            if target_vq:
                routed_tables = _sql_table_names(
                    _route_verified_query_accumulation(
                        question,
                        "corporate_target_industry_usage",
                        str(target_vq.get("sql") or ""),
                    )
                )
                reference_routed_sources = governed_sources.intersection(routed_tables)
                declared_sources.update(reference_routed_sources)
        reference_snapshot_exclusions = governed_sources - declared_sources
        best_ref = resolve_query_reference_for_question(best_ref, question)
        # The workbook reference questions are curated, high-confidence intent
        # anchors.  When one of their normalized phrases is present, keep its
        # required tables ahead of broad metric/table-name hits such as "회원".
        #
        # 표면형이 하나도 정확히 맞지 않은 참조는 "매출"·"조회"처럼 어디에나 있는 낱말
        # 두 개로도 문턱(2점)을 넘는다. 그 12점이 질문의 컬럼을 실제로 가진 테이블
        # (겹침 3점)을 눌러서, "해외가맹점매출건수" 가 그 컬럼을 가진 tmdaa5e11 옆에
        # 전표·가맹점 월스냅샷을 끌고 왔다. 정확히 맞은 표면형이 없는 참조는 컬럼 두 개
        # 겹침(6점)과 같은 급까지만 올린다 — 이기지 못하고 같이 남는다.
        primary_weight = 120 + best_score if exact_matches else _FUZZY_REFERENCE_WEIGHT + best_score
        join_weight = 100 + min(best_score, 12) if exact_matches else 4 + min(best_score, 4)
        weighted_tables = [
            (best_ref.get("primary_table"), primary_weight),
            *((name, join_weight) for name in best_ref.get("join_tables", [])),
            *((name, join_weight) for name in reference_routed_sources),
        ]
        for table_name, weight in weighted_tables:
            add(table_name, weight)

    matched_columns: dict[str, set[str]] = {}
    measure_specificity: dict[str, int] = {}
    for index, table in enumerate(SCHEMA.get("tables", [])):
        if not _is_semantic_table_visible(table):
            continue
        logical = str(table.get("name") or "")
        physical = str(table.get("physical_table") or logical).rsplit(".", 1)[-1]
        order.setdefault(logical, index)
        identity_terms = [logical, physical, table.get("korean_name", ""), *table.get("synonyms", [])]
        if any(_phrase_in_question(question, term) for term in identity_terms):
            add(logical, 14)
        column_hits = 0
        exact_hits = 0
        distinctive_hits = 0
        for section in ("dimensions", "measures", "time_dimensions"):
            for column in _visible_table_columns(table, section):
                name = str(column.get("name") or "")
                terms = [name, *column.get("synonyms", [])]
                matched_terms = [
                    term
                    for term in terms
                    if str(term or "") not in _GENERIC_TABLE_TERMS
                    and _phrase_in_question(question, term)
                ]
                if matched_terms:
                    column_hits += 1
                    matched_columns.setdefault(logical, set()).add(name)
                    if section == "measures":
                        measure_specificity[logical] = max(
                            measure_specificity.get(logical, 0),
                            max(len(_v2_compact(term)) for term in matched_terms),
                        )
                    if 0 < len(column_owners.get(name, ())) <= _DISTINCTIVE_OWNER_LIMIT:
                        distinctive_hits += 1
                if name in named_columns:
                    exact_hits += 1
        if column_hits:
            add(logical, min(column_hits, 4) * 3)
        # v2: 몇 안 되는 테이블만 가진 컬럼이 질문에 잡히면 그 자체가 강한 단서다.
        # "모바일 카드로 발급된 기업카드 좌수" 의 모바일카드여부는 tbdaaat05 계열에만
        # 있는데, 동의어 겹침 3점으로 묶여 있어서 카드 보유수 semantic attribute(36점)를
        # 가진 tbdaa1d12·tmdaa1d12 에 밀렸다. 정답 테이블이 후보에도 못 올랐다.
        if distinctive_hits:
            add(logical, min(distinctive_hits, _DISTINCTIVE_CAP) * _DISTINCTIVE_WEIGHT)
        # v2: 질문이 컬럼명을 그대로 부른 경우("CA한도금액", "부서사업장이용한도금액",
        # "가맹점월승인한도금액")는 동의어 겹침과 격이 다른 증거다. 그런데 동의어와
        # 같은 3점짜리로 묶여 있어서, 그 컬럼이 없는 tbdaa1d12 가 semantic attribute
        # 점수(36)로 이겼고 모델은 없는 컬럼을 "가장 가까운 컬럼"으로 대체했다.
        if exact_hits:
            add(logical, min(exact_hits, 2) * 40)

    # 질문의 컬럼을 가장 많이 가진 테이블이 하나뿐이면, 그 테이블만으로 질문의
    # 지표와 분류축이 모두 채워진다는 뜻이다. 같은 개수인 테이블이 여럿이면
    # (기업 일/월 스냅샷 짝처럼) 적재 정책이 고를 몫이므로 가점하지 않는다.
    coverage = sorted((len(names) for names in matched_columns.values()), reverse=True)
    if len(coverage) > 1 and coverage[0] >= _COVERAGE_MIN_COLUMNS and coverage[0] > coverage[1]:
        for name, names in matched_columns.items():
            if len(names) == coverage[0]:
                add(name, _COVERAGE_WEIGHT)

    # 질문이 가장 구체적으로 부른 지표를 가진 테이블이 하나뿐이면, 축이 없어도 그
    # 테이블이 답할 곳이다. 표면형 길이로 구체성을 잰다 — "할부이용금액"(tmdaa3e16)이
    # 전표의 매출금액이 '이용금액' 동의어로 걸린 것보다 구체적이다. 같은 구체성인
    # 테이블이 여럿이면(기업 일/월 스냅샷 짝처럼) 적재 정책이 고를 몫이므로 가점하지 않는다.
    if measure_specificity:
        best = max(measure_specificity.values())
        owners = [name for name, length in measure_specificity.items() if length == best]
        if len(owners) == 1:
            add(owners[0], _SOLE_MEASURE_OWNER_WEIGHT)

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
    for excluded in _attribute_snapshot_exclusions(question):
        normalized_scores.pop(known_by_physical.get(excluded, excluded), None)
    for excluded in reference_snapshot_exclusions:
        normalized_scores.pop(known_by_physical.get(excluded, excluded), None)
    ranked = sorted(normalized_scores, key=lambda name: (-normalized_scores[name], order.get(name, 10_000), name))
    selected = [name for name in ranked if normalized_scores[name] >= 3]
    return _route_accumulation_table_names(
        question, selected, contract=reference_routing_entry
    )[:max_tables]


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


def _compact_table_catalog(
    rule_tables: list[str],
    excluded_tables: set[str] | None = None,
) -> str:
    """Build a small selection catalog instead of serializing every column."""
    excluded = {
        str(name or "").rsplit(".", 1)[-1]
        for name in (excluded_tables or set())
        if name
    }
    candidate_names = set(rule_tables)
    if candidate_names:
        for path in semantic_join_paths_for_tables(SCHEMA, candidate_names, require_both=False):
            candidate_names.update(
                name for name in (path.get("from_table"), path.get("to_table")) if name
            )
    candidate_names.difference_update(excluded)
    tables = [
        table
        for table in SCHEMA.get("tables", [])
        if _is_semantic_table_visible(table)
        and str(table.get("name") or "") not in excluded
        and (not candidate_names or table.get("name") in candidate_names)
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
        if metric.get("source_table") in table_names and any(
            _phrase_in_question(question, term) for term in terms
        ):
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


def _matched_attribute_columns(
    question: str, table_names: list[str]
) -> dict[str, frozenset[str]]:
    """질문에 걸린 semantic attribute 가 원천으로 선언한 테이블별 컬럼."""
    wanted = {str(name or "").rsplit(".", 1)[-1] for name in table_names}
    columns: dict[str, set[str]] = {}
    for attribute in semantic_attribute_candidates(SCHEMA, question, max_count=6):
        for mapping in _attribute_source_mappings(question, attribute):
            table = str(mapping.get("table") or "").rsplit(".", 1)[-1]
            if table not in wanted:
                continue
            names = list(mapping.get("columns") or [])
            if mapping.get("column"):
                names.append(mapping["column"])
            columns.setdefault(table, set()).update(str(value) for value in names)
    return {table: frozenset(values) for table, values in columns.items()}


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
    attribute_columns = _matched_attribute_columns(question, selected_names)
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

    prompt_column_budget = max(
        (
            int(contract.get("prompt_column_budget") or 0)
            for contract in semantic_query_contract_candidates(SCHEMA, question, max_count=2)
        ),
        default=0,
    )
    per_table_limit = min(
        max(max_columns, prompt_column_budget),
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
        # 이 테이블을 기간으로 자를 때 쓰는 축. 적재 정책의 조회 컬럼이 grain 의
        # 시간축과 다른 테이블(tbdaadt01 의 실적기준년월일)이 있어 둘을 함께 본다.
        time_axis = {
            primary_time,
            str(
                (accumulation_policy_for(str(table.get("name") or "")) or {}).get(
                    "query_time_dimension"
                )
                or ""
            ),
        } - {""}

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
                if semantic_role == "시간" and name in time_axis:
                    # 시간 차원 전부에 가점을 주면 날짜 컬럼이 예산을 다 먹는다.
                    # 날짜 컬럼이 15개인 tmdaa5d01 은 16칸이 전부 날짜로 채워져
                    # 가맹점사업주체·상태·해지사유가 0점으로 탈락했다. 기간 조건에
                    # 필요한 건 조회 시간축 하나이고, 나머지 날짜는 질문이 이름을
                    # 대면(+70) 올라온다.
                    score += 35 if _has_time_expression(question) else 18
                terms = [name, *column.get("synonyms", [])]
                if any(_phrase_in_question(question, term) for term in terms):
                    score += 70
                # 속성이 "이 값은 이 컬럼에서 읽는다" 고 선언한 컬럼. 질문이 이름을 댄
                # 것과 같은 급의 증거다. 이름이 근거 문장에 스치는 것(+45)으로는
                # 예산을 못 넘겼다 — "가맹점 수수료가 가장 높은 가맹점" 에서
                # tmdaa5e11 의 가맹점수입수수료가 12칸 밖으로 밀려, 모델이 쓸 수 있는
                # 컬럼 중에 정답이 없었다.
                if name in attribute_columns.get(str(table.get("name") or ""), ()):
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
        accumulation_summary = format_accumulation_policy(table.get("accumulation_policy"))
        if accumulation_summary:
            lines.append(f"- accumulation_policy: {accumulation_summary}")

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
            if column.get("codebook_ref"):
                metadata.append("codebook_ref=" + str(column.get("codebook_ref")))
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
    retrieval_question = _retrieval_question(state)
    selected_domain = state.get("selected_domain", "")
    domain_context = state.get("domain_context") or build_domain_context(SCHEMA, selected_domain)
    domain_trace = state.get("domain_routing_trace", "")
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    join_context = build_semantic_join_context(SCHEMA, selected_domain, retrieval_question, max_paths=5)
    rule_tables = _rule_rank_tables(retrieval_question)
    authoritative_contract = any(
        str(contract.get("table_selection_mode") or "").lower() == "authoritative"
        for contract in semantic_query_contract_candidates(
            SCHEMA,
            retrieval_question,
            domain_name=selected_domain,
            max_count=2,
        )
    )
    if authoritative_contract and rule_tables:
        table_names = rule_tables[:4]
        return {
            "selected_tables": table_names,
            "table_details": _table_details(table_names, retrieval_question),
        }
    excluded_snapshot_tables = _attribute_snapshot_exclusions(retrieval_question)
    metrics_context = _route_merchant_time_context(
        retrieval_question,
        build_metrics_summary(SCHEMA, retrieval_question, selected_domain),
    )
    contracts_context = _route_merchant_time_context(
        retrieval_question,
        find_relevant_semantic_query_contracts(
            SCHEMA, retrieval_question, selected_domain
        ),
    )
    references_context = _route_merchant_time_context(
        retrieval_question,
        find_relevant_references(
            SCHEMA, retrieval_question, domain_name=selected_domain
        ),
    )
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
{_compact_table_catalog(rule_tables, excluded_snapshot_tables)}

## 비즈니스 용어집
{build_glossary_summary(SCHEMA, retrieval_question, selected_domain)}

## 재사용 가능한 Semantic Attribute
{build_semantic_attributes_summary(SCHEMA, retrieval_question, selected_domain)}

## 사전 정의된 메트릭
{metrics_context}

## 재사용 가능한 Semantic Query Contract
{contracts_context}

## 질문과 가까운 질의 작성 Reference
{references_context}

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
    llm_tables = [name for name in llm_tables if name not in excluded_snapshot_tables]
    if excluded_snapshot_tables:
        # A matched current/monthly attribute policy is deterministic.  Keep
        # its allowed rule tables ahead of any model-selected join additions.
        table_names = list(dict.fromkeys([*rule_tables, *llm_tables]))[:4]
    else:
        # A parseable explicit selection is the model's adjudication; rules are
        # a deterministic fallback for malformed/empty small-model output.
        table_names = llm_tables[:4] if llm_tables else rule_tables[:4]
    table_names = _route_accumulation_table_names(retrieval_question, table_names)
    return {
        "selected_tables": table_names,
        "table_details": _table_details(table_names, retrieval_question),
    }


def check_sql_gen_params(state: Text2SQLState) -> dict:
    if state.get("user_provided_params"):
        return {"param_stage": "done", "missing_params": []}

    question = state["question"]
    needed = _missing_ambiguous_target_params(question)
    query_frame = state.get("query_frame") or {}
    if needed and query_frame.get("entities"):
        needed = [item for item in needed if item.get("name") != "대상명"]

    if needed:
        return {
            "missing_params": needed,
            "param_stage": "need_params",
        }
    return {"missing_params": [], "param_stage": "done"}


def _corporate_scope_tables(selected_tables: list[str]) -> list[str]:
    """Selected tables that carry the individual/corporate discriminator."""
    wanted = {str(name).rsplit(".", 1)[-1].lower() for name in selected_tables or []}
    return [
        str(table.get("name") or "")
        for table in SCHEMA.get("tables", [])
        if str(table.get("name") or "").lower() in wanted
        and any(
            str(column.get("name") or "") == "개인기업구분코드"
            for column in (table.get("dimensions") or [])
        )
    ]


def _corporate_scope_rule(selected_tables: list[str]) -> str:
    """Spell out the corporate-only scope for the tables actually in the prompt.

    goldenset v2 정답 452건 중 276건이 ``개인기업구분코드 = '2'`` 를 쓰는데 모델은
    99건에만 붙였다. 질문에 "법인"·"기업" 이라는 말이 없으면 개인까지 함께 세서
    행 수와 금액이 전부 어긋난다. 이 에이전트의 조회 대상 자체가 기업영업이므로
    기본값으로 못 박고, 해당 컬럼을 가진 테이블 이름까지 같이 적는다.
    """
    tables = _corporate_scope_tables(selected_tables)
    if not tables:
        return ""
    return (
        "\n## 조회 대상 범위 (기업영업)\n"
        "이 시스템은 기업(법인·개인사업자) 고객만 다룹니다. 아래 테이블에는 "
        '"개인기업구분코드" = \'2\' 조건을 반드시 넣으세요. '
        "질문에 '법인'·'기업'이라는 말이 없어도 마찬가지입니다. "
        "질문이 개인 고객이나 전체를 명시한 경우에만 뺍니다.\n"
        f"- 대상 테이블: {', '.join(tables)}\n"
    )


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


def _prompt_source_tables(selected_tables: list[str]) -> list[str]:
    """고른 테이블과 그 적재 짝. SQL 생성 근거를 이 범위로 좁힐 때 쓴다.

    프롬프트의 지표·reference 는 질문의 낱말로 검색된다. "가맹점주" 하나에
    브랜드가맹점주수(tbdaaus01)와 폐업 가맹점 reference 가 딸려 오고, 과거 월이면
    시간 라우팅이 그것을 tmdaaus01 로 바꿔 완제품 SQL 로 만들어 준다. 정작 답은
    가맹점기본의 가맹점상세주소 한 컬럼이면 끝나는 질문인데도 그랬다.

    짝을 함께 넣는 이유는 프롬프트 문구가 이미 그 짝으로 치환되기 때문이다
    (_route_merchant_time_context). 마스터를 골랐으면 월 스냅샷 근거도 같은 답을 가리킨다.
    """
    allowed = {
        str(name).rsplit(".", 1)[-1].lower() for name in selected_tables or [] if name
    }
    for name in list(allowed):
        policy = accumulation_policy_for(name) or {}
        for key in ("historical_source", "live_source"):
            pair = policy.get(key)
            if isinstance(pair, dict) and pair.get("table"):
                allowed.add(str(pair["table"]).rsplit(".", 1)[-1].lower())
    return sorted(allowed)


def generate_sql(state: Text2SQLState) -> dict:
    question = state["question"]
    retrieval_question = _retrieval_question(state)
    table_details = state["table_details"]
    retry_count = state.get("retry_count", 0)
    validation_result = state.get("validation_result", "")
    selected_domain = state.get("selected_domain", "")
    selected_tables = state.get("selected_tables", [])
    prompt_sources = _prompt_source_tables(selected_tables)
    relevant_queries = _route_merchant_time_context(
        retrieval_question,
        find_relevant_queries(
            SCHEMA,
            retrieval_question,
            domain_name=selected_domain,
            table_names=prompt_sources,
        ),
    )
    relevant_references = _route_merchant_time_context(
        retrieval_question,
        find_relevant_references(
            SCHEMA,
            retrieval_question,
            domain_name=selected_domain,
            table_names=prompt_sources,
        ),
    )
    relevant_semantic_contracts = _route_merchant_time_context(
        retrieval_question,
        find_relevant_semantic_query_contracts(
            SCHEMA,
            retrieval_question,
            selected_domain,
        ),
    )
    relevant_metrics = _route_merchant_time_context(
        retrieval_question,
        build_metrics_summary(
            SCHEMA, retrieval_question, selected_domain, table_names=prompt_sources
        ),
    )
    domain_context = state.get("domain_context") or build_domain_context(SCHEMA, selected_domain)
    domain_trace = state.get("domain_routing_trace", "")
    semantic_contract = build_semantic_contract_summary(SCHEMA)
    join_context = _route_merchant_time_context(
        retrieval_question,
        build_semantic_join_context(
        SCHEMA,
        selected_domain,
        retrieval_question,
        max_paths=5,
        table_names=selected_tables,
        ),
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
{relevant_metrics}

## 재사용 가능한 Semantic Attribute
{build_semantic_attributes_summary(SCHEMA, retrieval_question, selected_domain, table_names=selected_tables)}

## 재사용 가능한 Semantic Query Contract
{relevant_semantic_contracts}

## 비즈니스 용어집
{build_glossary_summary(SCHEMA, retrieval_question, selected_domain)}

## 질문과 가까운 질의 작성 Reference
{relevant_references}

## 참고 SQL 예시
{relevant_queries}

{multiturn_context}
{retry_context}{user_params_context}
{_corporate_scope_rule(selected_tables)}
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
13. 상세 목록 조회는 요청 개수가 없으면 LIMIT {DEFAULT_QUERY_ROW_LIMIT}을 적용합니다. 집계 결과는 의미있는 순서로 정렬하고, CTE와 서브쿼리를 포함해 SELECT * 는 쓰지 않습니다.
13-1. 요청 지표 하나만 덜렁 내지 말고, 그 값을 읽는 데 필요한 컬럼을 함께 SELECT 합니다. SELECT * 를 쓰라는 뜻이 아니라 아래 네 가지를 빠뜨리지 말라는 뜻입니다.
    - 기업·가맹점·회원 목록이나 순위면 식별 컬럼을 같이 냅니다. 기업은 고객식별자·사업자등록번호·기업명, 가맹점은 가맹점번호·가맹점명입니다.
    - "X별"로 집계하면 그 축 컬럼을 SELECT에 남깁니다. 필터로 값이 하나로 고정된 축도 남깁니다.
    - 비율·평균·증감률은 분자와 분모(또는 비교 대상 두 시점의 값)를 함께 냅니다. 예: 월평균이용금액이면 기간총이용금액과 평균산정월수도 냅니다.
    - 정렬·필터 기준으로 쓴 지표는 결과에도 포함합니다. "연체원금이 가장 큰"이면 연체원금을 냅니다.
14. 가맹점명·기업명·상호명·브랜드명 등 이름 필터는 기본적으로 LIKE '%이름%' 부분일치를 사용합니다. 사용자가 "이름 고정", "이름만으로", "정확 일치"를 명시한 경우에만 앞뒤 % 없이 정확히 비교합니다.
15. 사용자가 N개/N건을 명시하면 최종 결과에 LIMIT N을 적용합니다. 상위/최근/최신/마지막/뒤에서 N은 요청 지표 또는 시간축을 DESC로, 하위 N은 ASC로 정렬한 뒤 LIMIT N을 적용합니다. "최근 N개월"은 행 개수가 아니라 조회 기간입니다.
16. 읽기 쉬운 alias. 17. 순수 SQL만 반환.
{_time_resolution_instruction(question, selected_tables)}
{_sql_dialect_rules()}

## 사용자 질문
{question}

SQL:"""
    # v2: 2048 토큰에서 복잡한 질의가 문장 중간에 끊겨 mismatched input '<EOF>' 가 났다.
    sql = _extract_sql_from_llm(_call_llm(prompt, max_tokens=SQL_GENERATION_MAX_TOKENS))

    # v2: 모델이 SQL 대신 "컬럼명을 알려주시면..." 으로 되묻는 응답을 내면 그대로
    # 실행 단계로 넘어가 읽기 전용 가드에서 죽는다. 재시도로 돌린다.
    if _v2_looks_like_prose(sql):
        return {
            "generated_sql": sql,
            "validation_result": _v2_prose_reason(sql),
            "is_valid": False,
            "retry_count": retry_count + 1,
        }

    sql = _v2_normalize_sql(sql)
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


def _validate_requested_row_constraints(question: str, sql: str) -> list[str]:
    """Check that generated SQL preserves an explicit result count/direction."""
    request = parse_row_request(question)
    if request.limit is None and not request.mode:
        return []

    issues: list[str] = []
    rendered_limit = outer_limit(sql)
    if request.limit is not None and rendered_limit != min(request.limit, MAX_QUERY_ROW_LIMIT):
        issues.append(f"요청한 결과 행수 LIMIT {request.limit}이 최종 SQL에 반영되지 않았습니다.")

    if request.mode in {"top", "bottom", "latest", "tail"}:
        outer_order = first_order_key(sql)
        wants_desc = request.mode in {"top", "latest", "tail"}
        expected_direction = "DESC" if wants_desc else "ASC"
        if not outer_order:
            issues.append(
                f"'{request.mode}' 행 선택에 필요한 최종 ORDER BY {expected_direction}가 없습니다."
            )
        elif not re.search(rf"\b{expected_direction}\b", outer_order, re.IGNORECASE):
            issues.append(
                f"'{request.mode}' 요청의 최종 정렬 방향은 {expected_direction}여야 합니다."
            )
        if request.mode in {"latest", "tail"} and outer_order and not re.search(
            r"년월|일자|날짜|일시|시각|(?:^|[_\.])(?:date|day|month|year|time|timestamp|ym)(?=[_\.]|\s+(?:ASC|DESC)\b|$)",
            outer_order,
            re.IGNORECASE,
        ):
            issues.append(f"'{request.mode}' 요청의 첫 ORDER BY 기준은 시간 컬럼이어야 합니다.")
        requested_metric = _explicit_rank_metric(question)
        if request.mode in {"top", "bottom"} and requested_metric:
            ordered_metric = _rank_metric(outer_order)
            if ordered_metric and ordered_metric != requested_metric:
                issues.append("요청한 순위 지표가 최종 ORDER BY의 첫 정렬 기준과 일치하지 않습니다.")
    if request.limit is not None and request.limit > 1 and is_scalar_aggregate_query(sql):
        issues.append("단일 집계값 SQL은 여러 결과 행 요청을 충족할 수 없습니다.")
    return issues


# "상위 10개 업체" 처럼 순위의 단위가 기업일 때만 잡는다. "상위 10개 가맹점"은
# 가맹점번호가, "상위 20개를 보여줘"는 축 값이 단위라 여기 걸리면 안 된다.
_CORPORATE_ENTITY_RANK_RE = re.compile(
    r"(?:상위|하위|TOP|톱|BOTTOM)\s*\d[\d,]*\s*"
    r"(?:개사|(?:개|곳|군데)?\s*(?:업체|회사|고객사|거래처|기업(?!카드)|법인(?!카드)))",
    re.IGNORECASE,
)
_CARD_GRAIN_COLUMNS = ("회원일련번호", "카드구분키번호")


def _validate_corporate_entity_grain(question: str, sql: str) -> list[str]:
    """Check that a corporate ranking emits one row per 고객식별자.

    같은 질문을 "상위 10개 업체"로 물으면 회원일련번호·카드구분키번호로 쪼갠 카드
    목록이, "상위 10개 회사"로 물으면 고객식별자 목록이 나왔다(cs-workbook-017/018).
    두 표현은 같은 단위를 가리키므로 결과 grain 도 같아야 한다.
    """
    if not _CORPORATE_ENTITY_RANK_RE.search(question or ""):
        return []
    # 질문이 카드·회원 단위를 따로 요구하면 그 grain 이 맞다.
    if re.search(r"카드별|회원별", question or ""):
        return []
    select_list = outer_select_list(sql)
    if not select_list:
        return []

    issues: list[str] = []
    if "고객식별자" not in select_list:
        issues.append(
            "업체·회사 단위 순위 결과에 고객식별자가 없습니다. 고객식별자를 SELECT에 포함하세요."
        )
    leaked = [
        column
        for column in _CARD_GRAIN_COLUMNS
        if column in _strip_aggregate_calls(select_list)
    ]
    if leaked:
        issues.append(
            f"업체·회사 단위 순위 결과가 {'·'.join(leaked)}로 쪼개져 있습니다. "
            "고객식별자로 GROUP BY 해 업체 1행씩 내세요."
        )
    return issues


_SLIP_TABLES = ("tbdaabt30", "tbdaabt08")
# 사용자가 순액 대상으로 지정한 전표 금액 컬럼과 통화만 다른 미화 쌍둥이.
_SLIP_NET_MEASURES = (
    "매출금액",
    "매출미화금액",
    "봉사료",
    "미화봉사료",
    "부가가치세",
    "가맹점수수료",
)
# SUM(매출금액), AVG(a."매출금액"), SUM(COALESCE(s.봉사료, 0)) 을 모두 잡되
# 가맹점일시불매출금액·가맹점수수료율처럼 이름이 다른 컬럼은 건드리지 않는다.
_RAW_SLIP_AMOUNT_RE = re.compile(
    r"(?:SUM|AVG)\((?:COALESCE\()?[A-Za-z]?\.?(?:" + "|".join(_SLIP_NET_MEASURES) + r")[,)]",
    re.IGNORECASE,
)
# COUNT(매출전표번호), COUNT(DISTINCT a."해외매출전표번호") 처럼 전표 행을 세는 집계.
_RAW_SLIP_COUNT_RE = re.compile(
    r"COUNT\((?:DISTINCT)?(?:\*|[A-Za-z]?\.?[가-힣]*매출전표번호)\)", re.IGNORECASE
)


def _validate_sales_slip_net_amount(sql: str) -> list[str]:
    """Check that 전표 금액·건수 are aggregated as 순액.

    전표 한 행은 정당·정정·취소 중 한 종류이고 취소전표도 금액을 양수로 들고 있다.
    그대로 SUM·COUNT하면 취소·환급까지 더해져 이용금액과 건수가 부풀려진다.
    매출전표종류구분코드를 이미 쓰고 있으면(부호 CASE·전표종류별 GROUP BY·코드 필터)
    질문이 요구한 집계이므로 건드리지 않는다.
    """
    text = str(sql or "")
    if not any(table in text.lower() for table in _SLIP_TABLES):
        return []
    compact = re.sub(r'["\s]', "", text)
    if "매출전표종류구분코드" in compact:
        return []

    issues: list[str] = []
    if _RAW_SLIP_AMOUNT_RE.search(compact):
        issues.append(
            "매출전표의 금액(매출금액·봉사료·부가가치세·가맹점수수료)은 매출전표종류구분코드로 "
            "부호를 정해 순액으로 집계해야 합니다. "
            "SUM(CASE WHEN \"매출전표종류구분코드\" IN ('1','4') THEN \"매출금액\" ELSE -\"매출금액\" END) 처럼 "
            "정당(1)·취소정정(4)은 더하고 정정(2)·취소(3)·청구보류(5)·체크환급(6)은 빼세요."
        )
    if _RAW_SLIP_COUNT_RE.search(compact):
        issues.append(
            "매출전표 건수도 순액입니다. COUNT로 세면 취소전표가 건수를 늘립니다. "
            "SUM(CASE WHEN \"매출전표종류구분코드\" IN ('1','4') THEN 1 ELSE -1 END) 으로 세세요."
        )
    return issues


# KST D-1 을 날짜 리터럴 없이 계산하는 표현. DATE_ADD 인자에 CAST(... AS DATE) 를
# 끼운 형태도 같은 하루를 가리킨다. 적재 가용일 검증과 스냅샷 판정이 서로 다른
# 패턴을 들고 있어서, CAST 형으로 D-1 한 날을 고른 SQL이 "최신 가용일로 제한하지
# 않았다" 로 반려됐다.
_DYNAMIC_PREVIOUS_DAY_SQL = (
    r"DATE_FORMAT\s*\(\s*DATE_ADD\s*\(\s*'day'\s*,\s*-1\s*,"
    r"[^)]*?CURRENT_TIMESTAMP\s+AT\s+TIME\s+ZONE\s+'Asia/Seoul'[^,]*?"
    r"\)\s*,\s*'%Y%m%d'\s*\)"
)


def _where_regions(sql: str) -> list[str]:
    """Return WHERE regions from every CTE and subquery."""
    regions: list[str] = []

    def visit(token: object) -> None:
        if isinstance(token, sqlparse.sql.Where):
            regions.append(str(token))
            return
        if isinstance(token, sqlparse.sql.TokenList):
            for child in token.tokens:
                visit(child)

    try:
        for statement in sqlparse.parse(sql or ""):
            visit(statement)
    except Exception:
        return []
    return regions


def _table_axis_patterns(sql: str, table_name: str, column: str) -> list[str]:
    """Build table-owned load-axis patterns, preserving explicit aliases."""
    patterns: list[str] = []
    for alias, explicit_alias in _table_aliases(sql, table_name):
        if explicit_alias:
            patterns.append(_qualified_column(alias, column))
        else:
            patterns.append(
                rf'(?<![0-9A-Za-z_가-힣])'
                rf'(?:(?:"?{re.escape(table_name)}"?\s*\.\s*)?)'
                rf'"?{re.escape(column)}"?'
                rf'(?![0-9A-Za-z_가-힣])'
            )
    return list(dict.fromkeys(patterns))


def _has_table_axis_filter(sql: str, table_name: str, column: str) -> bool:
    """Whether the declared load axis participates in a WHERE predicate."""
    tail = r"(?:(?!\b(?:AND|OR|WHERE|HAVING)\b).){0,120}"
    operators = r"(?:=|<>|!=|<=|>=|<|>|\bBETWEEN\b|\bIN\b|\bIS\b|\bLIKE\b)"
    for region in _where_regions(sql):
        for axis in _table_axis_patterns(sql, table_name, column):
            if re.search(
                rf"(?:{axis}{tail}{operators}|{operators}{tail}{axis})",
                region,
                re.IGNORECASE | re.DOTALL,
            ):
                return True
    return False


def _table_axis_literal_values(sql: str, table_name: str, column: str) -> list[str]:
    """Extract only date literals compared with a table's declared load axis."""
    values: list[str] = []
    tail = r"(?:(?!\b(?:AND|OR|WHERE|HAVING)\b).){0,100}"
    for region in _where_regions(sql):
        for axis in _table_axis_patterns(sql, table_name, column):
            patterns = (
                rf"{axis}{tail}\bBETWEEN\s*'(20\d{{2,6}})'\s+AND\s+'(20\d{{2,6}})'",
                rf"{axis}{tail}(?:=|<>|!=|<=|>=|<|>)\s*'(20\d{{2,6}})'",
                rf"'(20\d{{2,6}})'\s*(?:=|<>|!=|<=|>=|<|>){tail}{axis}",
                rf"{axis}{tail}\bIN\s*\(([^)]*)\)",
            )
            for pattern in patterns:
                for match in re.finditer(pattern, region, re.IGNORECASE | re.DOTALL):
                    for group in match.groups():
                        values.extend(re.findall(r"(?<!\d)20\d{2,6}(?!\d)", group or ""))
    return list(dict.fromkeys(values))


def _has_exact_table_axis_value(
    sql: str,
    table_name: str,
    column: str,
    value: str,
) -> bool:
    """Whether a load axis is fixed to the requested one-day snapshot."""
    exact_value = rf"(?:'{re.escape(value)}'|{_DYNAMIC_PREVIOUS_DAY_SQL})"
    for region in _where_regions(sql):
        for axis in _table_axis_patterns(sql, table_name, column):
            if re.search(
                rf"(?:{axis}\s*=\s*{exact_value}|{exact_value}\s*=\s*{axis}|"
                rf"{axis}\s+BETWEEN\s+{exact_value}\s+AND\s+{exact_value})",
                region,
                re.IGNORECASE | re.DOTALL,
            ):
                return True
    return False


def _has_recent_table_axis_window(
    sql: str,
    table_name: str,
    column: str,
    start: str,
    end: str,
) -> bool:
    values = _table_axis_literal_values(sql, table_name, column)
    if start in values and end in values:
        return True
    for region in _where_regions(sql):
        if not any(
            re.search(axis, region, re.IGNORECASE)
            for axis in _table_axis_patterns(sql, table_name, column)
        ):
            continue
        if re.search(
            r"DATE_ADD\s*\(\s*'day'\s*,\s*-9\b.*?"
            r"CURRENT_TIMESTAMP\s+AT\s+TIME\s+ZONE\s+'Asia/Seoul'",
            region,
            re.IGNORECASE | re.DOTALL,
        ):
            return True
    return False


def _monthly_archive_join_issues(sql: str) -> list[str]:
    """Reject merchant-only joins between monthly snapshots and facts."""
    archive_aliases = [
        str(binding["alias"])
        for binding in _sql_table_bindings(sql)
        if binding["table"] == "tmdaa5d01"
    ]
    fact_aliases = [
        str(binding["alias"])
        for binding in _sql_table_bindings(sql)
        if binding["table"] in {"tmdaa5e11", "tbdaabt30"}
    ]
    for archive_alias in archive_aliases:
        for fact_alias in fact_aliases:
            if not _has_alias_column_equality(sql, archive_alias, fact_alias, "가맹점번호"):
                continue
            if not _has_alias_column_equality(sql, archive_alias, fact_alias, "기준년월"):
                return [
                    "월별 가맹점 스냅샷(tmdaa5d01)과 월별 실적 테이블은 "
                    '"가맹점번호"와 "기준년월"을 모두 같게 조인해야 합니다.'
                ]
    return []


_MASTER_TABLE_KINDS = frozenset(
    {"master", "code_master", "effective_dated_master", "effective_dated_code_master"}
)


def _is_master_table(table_name: str) -> bool:
    """Whether the table's grain carries no time axis."""
    for table in SCHEMA.get("tables", []):
        if str(table.get("name") or "").lower() == str(table_name or "").lower():
            return str(table.get("table_kind") or "") in _MASTER_TABLE_KINDS
    return False


def _reads_only_master_tables(sql: str) -> bool:
    """Whether every schema table the SQL reads is a time-axis-free master."""
    used_tables = _extract_schema_tables(sql)
    return bool(used_tables) and all(_is_master_table(table) for table in used_tables)


def _availability_policy_issues(
    question: str,
    sql: str,
    selected_tables: list[str] | None = None,
) -> list[str]:
    """Validate each physical source against its declared accumulation axis."""
    del selected_tables  # Physical SQL sources, not retrieval candidates, are authoritative.
    used_tables = _extract_schema_tables(sql)
    governed_tables = sorted(
        table for table in used_tables if accumulation_policy_for(table)
    )
    if not governed_tables:
        return []

    issues = _monthly_archive_join_issues(sql)
    recent_start, recent_end = recent_window_ymd()
    has_requested_period = _has_time_expression(question)

    for table_name in governed_tables:
        policy = accumulation_policy_for(table_name) or {}
        cadence = str(policy.get("cadence") or "")
        column = str(policy.get("query_time_dimension") or "")

        if policy.get("has_reference_month") is False:
            reference_month_patterns = _table_axis_patterns(
                sql, table_name, "기준년월"
            )
            if any(
                re.search(pattern, sql or "", re.IGNORECASE)
                for pattern in reference_month_patterns
            ):
                issues.append(
                    f'{table_name}에는 물리 "기준년월" 컬럼이 없습니다. '
                    f'SUBSTR("{column}", 1, 6)으로 월을 파생하세요.'
                )

        if cadence == "previous_day":
            current_request = not _has_historical_period_expression(question) or bool(
                re.search(
                    r"오늘|현재|전일|어제|최신|최근\s*기준",
                    question or "",
                )
            )
            latest_available = _latest_available_day(table_name)
            axis_values = [
                value
                for value in _table_axis_literal_values(sql, table_name, column)
                if len(value) == 8
            ]
            if any(value > latest_available for value in axis_values):
                issues.append(
                    f"{table_name}의 최신 가용일은 {latest_available}입니다. "
                    f'"{column}"에 그보다 뒤의 일자를 사용하지 마세요.'
                )
            if current_request and not _has_exact_table_axis_value(
                sql, table_name, column, latest_available
            ):
                issues.append(
                    f"{table_name}의 현재 가용일은 {latest_available}입니다. "
                    f'"{column}"을 그 일자로 제한하고 MAX 전체시점이나 금일을 사용하지 마세요.'
                )
            elif not current_request and not _has_table_axis_filter(
                sql, table_name, column
            ):
                issues.append(
                    f'{table_name}의 명시 기간은 "{column}"(YYYYMMDD)로 제한해야 합니다.'
                )
            continue

        available_days = int(policy.get("available_days") or 0)
        if available_days:
            if not _has_table_axis_filter(sql, table_name, column):
                issues.append(
                    f'{table_name}은 최근 {available_days}일만 제공되므로 '
                    f'"{column}" 기간 조건이 필요합니다.'
                )
                continue

            day_literals = [
                value
                for value in _table_axis_literal_values(sql, table_name, column)
                if len(value) == 8
            ]
            if any(value < recent_start or value > recent_end for value in day_literals):
                issues.append(
                    f"{table_name}의 조회 가능 범위는 KST {recent_start}~{recent_end}입니다. "
                    "범위 밖 일자는 조회할 수 없습니다."
                )
            explicit_requested_day = _extract_period_by_rule(question)[2]
            if explicit_requested_day:
                if explicit_requested_day not in day_literals:
                    issues.append(
                        f'{table_name}의 명시 일자는 "{column}" = '
                        f"'{explicit_requested_day}' 조건으로 조회해야 합니다."
                    )
            elif not _has_recent_table_axis_window(
                sql, table_name, column, recent_start, recent_end
            ):
                issues.append(
                    f'{table_name}의 기본 조회는 "{column}"을 KST 최근 {available_days}일 '
                    f"({recent_start}~{recent_end})로 제한해야 합니다."
                )
            continue

        # 마스터는 grain 에 시간 축이 없다(tbdaadt01 은 "가맹점번호 1건"). 적재가
        # 일별로 돈다는 이유로 기간 조건을 요구하면 "도미노피자 가맹점 기본 정보"
        # 같은 조회까지 막힌다. 적재 주기는 신선도이지 조회 조건이 아니다.
        # 다만 질문이 특정 일자를 짚었다면 그 일자는 적재 축으로 걸러야 한다.
        if has_requested_period and cadence in {"daily", "monthly", "yearly"}:
            if _is_master_table(table_name) and not _extract_period_by_rule(question)[2]:
                continue
            if not _has_table_axis_filter(sql, table_name, column):
                cadence_label = {
                    "daily": "일별",
                    "monthly": "월별",
                    "yearly": "연별",
                }[cadence]
                issues.append(
                    f'{table_name}의 {cadence_label} 적재 기간은 "{column}"'
                    f'({policy.get("format") or ""}) 조건으로 제한해야 합니다.'
                )

    return list(dict.fromkeys(issues))


def _availability_execution_error(state: Text2SQLState, sql: str) -> str:
    question = _retrieval_question(state)
    # Low-level/backwards-compatible callers can provide only rendered SQL.
    # In that mode the DB layer's registered TBD→TMD fallback remains the
    # source of truth because there is no user period intent to validate.
    if not str(question or "").strip():
        return ""
    issues = _availability_policy_issues(
        question,
        sql,
        state.get("selected_tables", []),
    )
    return " ".join(issues)


def _uses_exact_previous_day_snapshot(sql: str) -> bool:
    """Whether SQL fixes a previous-day table to the one latest KST day."""
    previous_day_value = previous_day_ymd()
    day_column = r'(?:(?:"?[A-Za-z_]\w*"?)\s*\.\s*)?"?기준년월일"?'
    exact_value = rf"'{previous_day_value}'|{_DYNAMIC_PREVIOUS_DAY_SQL}"
    return bool(
        re.search(
            rf"{day_column}\s*=\s*(?:{exact_value})|(?:{exact_value})\s*=\s*{day_column}",
            sql or "",
            re.IGNORECASE | re.DOTALL,
        )
    )


def _allow_cross_cycle_fallback(question: str, sql: str) -> bool:
    """Allow daily→monthly archival fallback only for historical requests.

    The DB layer cannot infer user intent from a rendered query. Keeping this
    decision here preserves old VQ fallbacks for explicit periods while a
    current/latest previous-day snapshot never silently changes its cadence.
    """
    used_tables = _extract_schema_tables(sql)
    if not any(
        (accumulation_policy_for(table) or {}).get("cadence") == "previous_day"
        for table in used_tables
    ):
        return True
    # Low-level/backwards-compatible callers may not carry the natural-language
    # question. Preserve the historical DB fallback in that case.
    if not str(question or "").strip():
        return True
    # "전월 매입금액"처럼 전월이 스냅샷 안의 지표명인 VQ도 있다.
    # SQL이 최신 가용일(D-1) 한 날을 정확히 고르면 현재 스냅샷이므로,
    # 질문의 단어보다 SQL의 시간 조건을 우선한다.
    if _uses_exact_previous_day_snapshot(sql):
        return False
    if _has_historical_period_expression(question) and not re.search(
        r"어제|전일", question or ""
    ):
        return True
    return bool(
        re.search(
            r"최근\s*(?:\d{1,3}\s*(?:개월|달)|(?:\d{1,2}|일|반)\s*년)|"
            r"지난\s*(?:\d{1,3}\s*(?:개월|달)|(?:\d{1,2}|일|반)\s*년)|"
            r"상반기|하반기|전월|지난\s*달|저번\s*달",
            question or "",
        )
    )


def validate_sql(state: Text2SQLState) -> dict:
    question = state["question"]
    retrieval_question = _retrieval_question(state)
    sql = _apply_accumulation_historical_sources(retrieval_question, state["generated_sql"])
    sql = _apply_recent_month_sql_fix(question, sql)
    sql = _apply_name_filter_mode(question, sql)
    # v2: expr::type 은 Athena 문법 오류다. 실행 전에 CAST(...)로 교정한다.
    sql = _v2_normalize_sql(sql)
    # v2: 실행 실패 40건 중 27건이 COLUMN_NOT_FOUND 였다. 축약·오타는 여기서 고치고,
    # 못 고치는 건 "그 컬럼은 어느 테이블에 있다"까지 적어 재시도로 넘긴다.
    table_columns, column_owners = _table_column_index()
    sql, column_issues = _v2_repair_columns(sql, table_columns, column_owners=column_owners)
    sql = prepare_sql_for_backend(sql)
    selected_tables = state["selected_tables"]
    issues = _validate_sql_against_schema(sql, selected_tables)
    issues.extend(column_issues)
    # v2: QUALIFY·WHERE 절 윈도함수·잘린 SQL 은 Athena 가 거절하므로 미리 잡아 재시도시킨다.
    issues.extend(_v2_audit_sql(sql))
    issues.extend(_validate_required_semantic_tables(retrieval_question, sql, selected_tables))
    issues.extend(_validate_recent_month_semantics(question, sql))
    issues.extend(_validate_requested_row_constraints(question, sql))
    issues.extend(_validate_corporate_entity_grain(question, sql))
    issues.extend(_validate_sales_slip_net_amount(sql))
    issues.extend(_availability_policy_issues(retrieval_question, sql, selected_tables))
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

    # 재시도가 남지 않은 마지막 검증에서는 LLM 의미 검증을 하지 않는다. 여기서
    # 실패로 돌려도 고칠 기회가 없어 사용자에게는 SQL 오류만 남고, 정적으로 안전한
    # SQL 을 실행조차 못 한 채 버리게 된다. 정적 검증은 위에서 이미 끝났다.
    if state.get("retry_count", 0) >= SQL_RETRY_LIMIT - 1:
        return valid_result("VALID (정적 검증 통과, 마지막 시도라 LLM 의미 검증 생략)")

    validation_prompt = f"""SQL 검증 전문가로서, 아래 SQL이 사용자 질문에 정확히 답하는지 검증하세요.

사용자 질문: {question}
SQL:
{sql}
사용 테이블: {', '.join(selected_tables)}
검증 기준:
- 기간 길이가 없는 단독 "최근", "최근 기준", "이번달", "이번 월"은 현재월({current_ym}) 1개월 기준입니다.
- "최근 N개월/N달", "최근 반년/N년"은 기간 표현입니다. 명시 기준월이 있으면 그 월을 종료로, 없으면 CURRENT_DATE/현재월을 종료로 사용한 SQL이 올바릅니다.
- 기준월의 사전 집계 "최근N개월" 지표를 사용하는 경우에도 질문의 명시 기준월을 유지하면 올바릅니다.
- "저번달", "지난달", 조회시점을 뜻하는 단독 "전월"은 {_current_date_context()}의 지난달 기준으로 해석합니다.
- "전월 대비", "전월비", "전월과 비교"는 조회기간이 아닌 비교 연산입니다. 질문의 명시 기간을 유지한 SQL이어야 합니다.
- 가맹점명·기업명·상호명·브랜드명 등 이름 필터는 기본 LIKE '%이름%'여야 합니다. "이름 고정", "이름만으로", "정확 일치"가 명시된 경우에만 앞뒤 %가 없어야 합니다.
- 질문에 특정 시점/기간 표현이 없으면 시스템이 기준시점을 정해 조회할 수 있으며, 이것만으로 SQL을 실패 처리하지 않습니다.
- 질문에 특정 시점/기간 표현이 없고 "소지/보유/현황/유효" 같은 스냅샷성 질문이면 데이터 최신 MAX(기준년월/기준년월일) 또는 현재 실행일 기준 유효성 조건을 사용할 수 있습니다.
- 시스템이 정한 기준시점이 SQL에 있으면 결과 답변에서 그 기준시점을 명시하면 됩니다.
- 아래 "시스템이 정한 기준시점"이 비어 있지 않으면, 그 기준시점은 적재 범위를 읽어 시스템이 결정한 것입니다. SQL의 기간 조건이 질문의 기간과 달라도 기준시점 불일치로 보지 말고, 그 사유가 답변에 전달되는 것으로 충분합니다.
- N개/N건 요청은 최종 LIMIT N과 요청 방향의 ORDER BY가 모두 있어야 합니다. 최근/최신/마지막/뒤에서 N은 DESC, 하위 N은 ASC입니다. "최근 N개월"은 행 제한이 아닙니다.
메트릭 정의:
{_route_merchant_time_context(retrieval_question, build_metrics_summary(SCHEMA, retrieval_question, state.get('selected_domain', '')))}

Semantic Query Contract:
{_route_merchant_time_context(retrieval_question, find_relevant_semantic_query_contracts(SCHEMA, retrieval_question, state.get('selected_domain', '')))}

시스템이 정한 기준시점:
{implicit_time_basis or "(없음 - 질문의 기간을 그대로 지켜야 합니다)"}

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
    sql = _apply_accumulation_historical_sources(_retrieval_question(state), sql)
    prepared_sql = prepare_sql_for_backend(sql)
    availability_error = _availability_execution_error(state, prepared_sql)
    if availability_error:
        return {
            "query_columns": [],
            "query_rows": [],
            "query_error": f"테이블 적재 주기 위반: {availability_error}",
            "validation_result": f"SQL 적재 주기 위반: {availability_error}",
            "is_valid": False,
            "retry_count": state.get("retry_count", 0) + 1,
            "final_sql": prepared_sql,
        }
    columns, rows, error = execute_sql(
        prepared_sql,
        max_rows=DEFAULT_FETCH_ROW_LIMIT,
        allow_cross_cycle_fallback=_allow_cross_cycle_fallback(
            state.get("question", ""), prepared_sql
        ),
    )
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
    return _executed_query_state(prepared_sql, columns, rows)


def _markdown_cell(value: object, max_length: int = 80) -> str:
    text = str(value if value is not None else "-").replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


def _total_row_count(state: Text2SQLState) -> int | None:
    """원본 결과의 전체 건수. 조회 한도에 걸려 세지 못했으면 None."""
    scope = state.get("result_scope") or {}
    total = scope.get("total_row_count")
    if isinstance(total, int):
        return total
    if "total_row_count" in scope:
        return None
    # 건수를 기록하지 않은 예전 결과. 화면 한도 아래면 잘릴 수 없으므로 그대로 전체다.
    rows = state.get("query_rows") or []
    return len(rows) if len(rows) < _DISPLAY_ROW_LIMIT else None


def _known_row_floor(state: Text2SQLState) -> int:
    """전체 건수를 세지 못했을 때 "N건 이상"의 근거가 되는 최소 건수."""
    scope = state.get("result_scope") or {}
    return max(len(state.get("query_rows") or []), int(scope.get("fetched_row_count") or 0))


def _row_count_label(total_row_count: int | None, known_row_floor: int) -> str:
    return f"{total_row_count:,}건" if total_row_count is not None else f"{known_row_floor:,}건 이상"


def _deterministic_result_answer(
    columns: list,
    rows: list[tuple],
    implicit_time_basis: str = "",
    total_row_count: int | None = None,
    known_row_floor: int = 0,
) -> str:
    """Render a truthful result summary when the answer model is unavailable.

    total_row_count=None 은 조회 한도에 잘려 전체 건수를 세지 못한 경우다.
    """
    # 답변 모델이 없을 때의 대체 답변도 질문 커버리지를 보여줘야 한다. 컬럼 6개·
    # 3행만 내면 "무엇을 조회했는지" 는 알아도 "무엇이 나왔는지" 는 안 보인다.
    known_row_floor = max(known_row_floor, len(rows))
    visible_columns = [str(column) for column in columns[:10]]
    preview_rows = rows[:10]
    lines = ["### 핵심 요약", f"- 조회 결과: {_row_count_label(total_row_count, known_row_floor)}"]
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
        for row in preview_rows:
            cells = [row[index] if index < len(row) else None for index in range(len(visible_columns))]
            lines.append("| " + " | ".join(_markdown_cell(value) for value in cells) + " |")
        if len(rows) > len(preview_rows):
            total_label = _row_count_label(total_row_count, known_row_floor)
            where = (
                f"전체 {total_label}은 결과 표에서 확인할 수 있습니다."
                if total_row_count == len(rows)
                else f"전체 {total_label} 중 결과 표에는 상위 {len(rows):,}행이 담겨 있습니다."
            )
            lines.append(f"\n상위 {len(preview_rows)}건을 표시했습니다. {where}")
    return "\n".join(lines)


def _result_cell_for_llm(value: object) -> str:
    """0 은 "값 없음"이 아니다. falsy 값을 빈칸으로 바꾸면 모델이 그대로 옮겨 적는다."""

    return "" if value is None else str(value)


def _loaded_period_notes(sql: str) -> list[str]:
    """SQL이 쓴 적재 관리 테이블의 실제 조회 가능 기간을 데이터에서 읽어 온다.

    "왜 없는지"는 요청한 기간이 적재 범위 밖인지 아닌지로 갈린다. 정책표의 주기와
    서버 시계로는 그 판단을 사용자에게 넘겨줄 수 없다.
    """
    notes = []
    for table in sorted(_extract_schema_tables(sql)):
        policy = accumulation_policy_for(table)
        bounds = loaded_period_range(table) if policy else None
        if bounds:
            column = policy.get("query_time_dimension")
            notes.append(f'{table} 조회 가능 기간: "{column}" {bounds[0]} ~ {bounds[1]}')
    return notes


def _open_month_live_source_notes(sql: str) -> list[str]:
    """이번 달을 물었는데 일 적재 원천으로 돌리지 못한 이유.

    월 실적·월 스냅샷은 달이 닫힌 뒤 적재되므로 이번 달은 0건이 나온다. 열린 달을
    들고 있는 일 적재 짝으로 돌리는 것이 기본이고(_apply_current_month_live_sources),
    짝이 그 컬럼을 갖고 있지 않아 돌리지 못했으면 그 사실을 답변에서 밝힌다.
    """
    current_ym = kst_today().strftime("%Y%m")
    notes: list[str] = []
    for table in sorted(_extract_schema_tables(sql)):
        live = live_source_for(table)
        if not live:
            continue
        if set(_table_axis_literal_values(sql, table, "기준년월")) != {current_ym}:
            continue
        live_table = str(live.get("table") or "")
        missing = _live_source_missing_columns(sql, table, live_table)
        if missing:
            notes.append(
                f"{table}의 {current_ym} 행은 달이 닫힌 뒤 적재됩니다. "
                f"이번 달을 들고 있는 {live_table}에는 {', '.join(missing)}이(가) 없어 "
                "대신 조회할 수 없습니다."
            )
            continue
        latest_day = _latest_available_day(live_table)
        if latest_day[:6] != current_ym:
            notes.append(
                f"{table}의 {current_ym} 행은 달이 닫힌 뒤 적재되고, "
                f"{live_table}도 아직 이번 달을 적재하지 않았습니다(최신 {latest_day})."
            )
    return notes


def generate_answer(state: Text2SQLState) -> dict:
    if state.get("answer"):
        return {"answer": state["answer"]}

    question = state["question"]
    sql = state.get("final_sql", "")
    columns = state.get("query_columns", [])
    rows = state.get("query_rows", [])
    if not rows:
        lines = ["해당 데이터가 없습니다. (조회 결과 0건)"]
        basis = state.get("implicit_time_basis", "") or _implicit_time_basis_note(question, sql)
        if basis:
            lines.append(f"- 조회 기준: {basis}")
        lines.extend(f"- {note}" for note in _open_month_live_source_notes(sql))
        lines.extend(f"- {note}" for note in _loaded_period_notes(sql))
        lines.append("- 기간을 넓히거나 이름·업종 등의 검색어를 줄여서 다시 시도해 보세요.")
        return {"answer": "\n".join(lines)}
    implicit_time_basis = state.get("implicit_time_basis", "") or _implicit_time_basis_note(question, sql)
    total_row_count = _total_row_count(state)
    known_row_floor = _known_row_floor(state)
    fallback_answer = _deterministic_result_answer(
        columns, rows, implicit_time_basis, total_row_count, known_row_floor
    )
    if state.get("question_type") == "direct_sql":
        return {"answer": fallback_answer}
    result_text = " | ".join(columns) + "\n" + "-" * 40 + "\n"
    for row in rows[:20]:
        result_text += " | ".join(_result_cell_for_llm(v) for v in row) + "\n"
    if len(rows) > 20:
        result_text += f"\n... 외 {len(rows) - 20}행 더 있음(프롬프트에 담긴 행만 기준)"
    source_label = _get_source_label(state)
    summary_text = _result_summary(columns, rows, total_row_count=total_row_count)
    # 프롬프트에 담기는 행은 상위 20행, state에 남은 행도 최대 100행이다. 이 숫자를
    # 전체 건수로 착각하면 "100건"짜리 답변이 나가므로 원본 건수를 따로 알려 준다.
    prompt_row_count = min(len(rows), 20)
    total_row_label = _row_count_label(total_row_count, known_row_floor)
    if total_row_count is None:
        row_scope_note = (
            f"조회 한도에 걸려 전체 건수를 세지 못했습니다. "
            f'건수·개수는 "{total_row_label}"으로만 쓰세요.'
        )
    elif total_row_count > prompt_row_count:
        row_scope_note = (
            f"전체 {total_row_count:,}건이며, 아래 쿼리 결과에는 상위 {prompt_row_count:,}행만 담았습니다. "
            f"건수·개수는 {total_row_count:,}건으로 답하세요."
        )
    else:
        row_scope_note = f"전체 {total_row_count:,}건이 모두 아래 쿼리 결과에 있습니다."
    prompt = f"""당신은 KB카드 기업영업 데이터 분석가입니다.
사용자의 질문과 SQL 쿼리 결과를 바탕으로 짧고 직관적인 한국어 답변을 작성하세요.

## 사용자 질문
{question}

## 실행된 SQL ({source_label})
{sql[:4000]}

## 쿼리 결과 (전체 {total_row_label})
{result_text}

## 결과 범위
{row_scope_note}

## 계산 요약
{summary_text}

## 기준시점 안내
{implicit_time_basis or "(사용자 질문 또는 SQL에서 별도 기준시점 안내 없음)"}

## 답변 형식
아래 세 절을 모두 채우세요. 전체 답변은 25줄 이내로 씁니다.

### 핵심 요약
| 항목 | 값 |
|---|---:|
| 주요 지표 | ... |
| 비교/순위 | ... |
| 특이사항 | ... |

### 주요 데이터
쿼리 결과를 표로 최대 10행까지 보여주세요. 컬럼은 결과에 있는 것을 그대로 쓰고,
행이 더 있으면 표 아래에 "전체 {total_row_label} 중 상위 N행"으로 적습니다.
결과가 1행이면 이 절은 생략합니다.

### 해석
- 데이터에서 바로 확인되는 내용을 2~4개 bullet로 작성하세요.

## 답변 규칙
1. 표와 bullet 중심으로 쓰되, 질문이 물은 항목은 하나도 빠뜨리지 마세요.
   "A와 B를 비교" 면 A와 B를 각각 보여주고, "X별" 이면 X축을 보여줍니다.
2. 금액은 억원/만원 단위로 변환하세요.
3. 비율은 %로 표시하세요.
4. 상위/하위 결과는 5개까지 언급하고, 나머지는 "주요 데이터" 표에 맡기세요.
5. 데이터에 없는 원인 추정이나 영업 제안은 쓰지 마세요.
6. 계산 요약의 row_count, 합계, 평균, 최소, 최대를 우선 활용하세요.
7. 기준시점 안내가 있으면 핵심 요약 또는 해석에 "조회 기준"으로 반드시 명시하세요.
8. 결과 컬럼 중 질문과 직접 관련된 것은 요약이나 표에서 최소 한 번은 드러내세요.
9. 건수·개수는 "결과 범위"에 적힌 전체 건수({total_row_label})로만 답하세요.
   위에 담긴 행 수나 표에 쓴 행 수를 전체 건수처럼 말하면 안 됩니다.

답변:"""
    try:
        # 표 10행 + 요약 + 해석을 한글로 담으려면 1200 토큰은 중간에서 끊긴다.
        answer = _coerce_llm_text(_call_llm(prompt, max_tokens=2000))
    except Exception:
        answer = ""
    return {"answer": answer or fallback_answer}


# 적재 범위 밖 기간은 SQL을 고쳐도 결과가 나오지 않는다. "SQL을 잘못 만들었다"와
# 같은 화면으로 알리면 사용자는 질문을 고쳐야 하는지 기다려야 하는지 알 수 없다.
_NO_DATA_VALIDATION_RE = re.compile(r"최신 가용일은|범위 밖 일자는 조회할 수 없습니다")


def handle_error(state: Text2SQLState) -> dict:
    validation_result = str(state.get("validation_result") or "")
    if _NO_DATA_VALIDATION_RE.search(validation_result):
        sql = state.get("final_sql") or state.get("generated_sql") or ""
        details = _loaded_period_notes(sql) or [
            re.sub(r"^.*?적재 주기 위반:\s*", "", validation_result).strip()
        ]
        return {
            "query_error": "",
            "error_message": "",
            "answer": "\n".join(["해당 데이터가 없습니다.", *(f"- {line}" for line in details)]),
        }
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
) -> Literal["refine_search_query", "prepare_direct_sql", "direct_answer", "reject_answer", "policy_refusal"]:
    qtype = state.get("question_type", "need_sql")
    if qtype == "safety_blocked":
        return "policy_refusal"
    if qtype == "direct_sql":
        return "prepare_direct_sql"
    if qtype == "direct":
        return "direct_answer"
    if qtype == "reject":
        return "reject_answer"
    return "refine_search_query"


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


def after_matched_query(state: Text2SQLState) -> Literal["generate_answer", "handle_error"]:
    # A Verified Query is the authoritative SQL for its intent. Replacing it
    # with unconstrained generated SQL after a DB error can silently drop the
    # required business filters and return a false-success result.
    return "handle_error" if state.get("query_error") else "generate_answer"


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
    return "handle_error" if state.get("retry_count", 0) >= SQL_RETRY_LIMIT else "generate_sql"


def after_query(state: Text2SQLState) -> Literal["generate_answer", "generate_sql", "handle_error"]:
    if state.get("query_error"):
        if state.get("question_type") == "direct_sql":
            return "handle_error"
        return "handle_error" if state.get("retry_count", 0) >= SQL_RETRY_LIMIT else "generate_sql"
    return "generate_answer"


# ---------------------------------------------------------------------------
# 11. 그래프 구성
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    graph = StateGraph(Text2SQLState)

    graph.add_node("classify_question", classify_question)
    graph.add_node("refine_search_query", refine_search_query)
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
    graph.add_node("policy_refusal", policy_refusal)
    graph.add_node("analyze_question", analyze_question)
    graph.add_node("check_sql_gen_params", check_sql_gen_params)
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("validate_sql", validate_sql)
    graph.add_node("run_query", run_query)
    graph.add_node("generate_answer", generate_answer)
    graph.add_node("handle_error", handle_error)

    graph.set_entry_point("classify_question")
    graph.add_conditional_edges("classify_question", route_by_question_type)
    graph.add_edge("refine_search_query", "route_domain")
    graph.add_edge("prepare_direct_sql", "validate_sql")
    graph.add_edge("route_domain", "select_tool")

    graph.add_edge("direct_answer", END)
    graph.add_edge("reject_answer", END)
    graph.add_edge("policy_refusal", END)

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
        "question": question, "retrieval_query": "", "question_type": "",
        "safety_action": "", "safety_category": "", "safety_reason_code": "", "safety_direction": "",
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
